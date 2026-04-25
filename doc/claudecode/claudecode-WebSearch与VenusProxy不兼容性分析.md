# Claude Code WebSearch 与 Venus Proxy 不兼容性分析

> 本文档基于 `@anthropic-ai/claude-code@2.1.87` 的压缩源码（`cli.js`）逆向分析，结合项目已有的搜索工具体系文档，系统说明切换到 Venus API 代理后 WebSearch 功能失效的根因、涉及的源码机制与可行的替代方案。

---

## 目录

1. [问题现象](#1-问题现象)
2. [根因总结](#2-根因总结)
3. [WebSearch 不是普通 tool_use](#3-websearch-不是普通-tooluse)
4. [源码证据：isEnabled 白名单](#4-源码证据isenabled-白名单)
5. [源码证据：嵌套模型调用链路](#5-源码证据嵌套模型调用链路)
6. [Venus Proxy 为什么无法提供 server tool 能力](#6-venus-proxy-为什么无法提供-server-tool-能力)
7. [普通 tool_use 与 WebSearch 的对比](#7-普通-tooluse-与-websearch-的对比)
8. [工具注册与延迟加载的影响](#8-工具注册与延迟加载的影响)
9. [替代方案](#9-替代方案)
10. [附录：相关源码片段](#10-附录相关源码片段)

---

## 1. 问题现象

将 Claude Code 的 API 端点从 Anthropic 直连切换到 Venus Proxy（`v2.open.venus.oa.com/llmproxy/chat/completions`）后：

- **Grep、Glob、FileRead、Bash** 等本地工具完全正常
- **WebSearch 工具消失**，模型不再具备联网搜索能力
- `/tools` 命令列出的可用工具中不再包含 WebSearch

直觉上 WebSearch 应该和其他工具一样属于 tool_use 的一种，理应不受 API 端点影响。但实际情况是 WebSearch 的架构与其他工具有本质区别。

---

## 2. 根因总结

WebSearch 失效由两个层面的原因共同决定：

### 2.1 注册层：Provider 白名单硬编码

Claude Code 在**工具注册阶段**就通过 `isEnabled()` 检查 API Provider 类型。只有 `firstParty`（Anthropic 直连）、`vertex`（Google Vertex AI）、`foundry`（Amazon Bedrock Foundry）三种 provider 返回 `true`。Venus Proxy 被识别为第三方 provider，WebSearch 从未进入可用工具列表。

### 2.2 执行层：依赖 Anthropic 服务端 server tool

即使绕过 `isEnabled()` 检查，WebSearch 的实际执行也需要向 Anthropic API 发起一个携带 `web_search_20250305` server tool 的嵌套模型请求。Venus Proxy 作为 OpenAI 兼容格式的代理层，不支持 Anthropic Messages API 中的 server tool 机制，请求会在运行时失败。

两层保护确保了：不支持的场景在注册阶段就被拦截，而非等到运行时才报错。

---

## 3. WebSearch 不是普通 tool_use

Claude Code 的工具分为两大类：

### 3.1 客户端工具（Client-side Tool）

- **执行位置**：Claude Code 本地进程
- **执行方式**：模型输出 `tool_use` → Claude Code 本地执行 → 将结果作为 `tool_result` 返回给模型
- **典型代表**：Grep（调用 ripgrep）、Bash（调用 shell）、FileRead（读本地文件）、Glob（调用 ripgrep --files）
- **API 依赖**：无。只要模型能输出合法的 tool_use JSON，工具就能在本地执行
- **Provider 限制**：无

### 3.2 服务端工具（Server-side Tool）

- **执行位置**：Anthropic 服务端基础设施
- **执行方式**：Claude Code 发起**一个全新的 API 请求**，请求中附带 server tool schema → Anthropic 服务端让模型自动调用搜索服务 → 搜索结果作为 `web_search_tool_result` content block 返回
- **典型代表**：WebSearch（`web_search_20250305`）
- **API 依赖**：强依赖 Anthropic Messages API 原生的 server tool 扩展点
- **Provider 限制**：必须是支持 server tool 的端点

WebSearch 看起来像一个普通的 tool_use，但本质是对 Anthropic 服务端能力的一次封装调用。

---

## 4. 源码证据：isEnabled 白名单

> 源码位置：`cli.js` 第 3456 行附近，WebSearchTool 定义块内

从压缩源码中提取的 `isEnabled` 函数（反混淆还原）：

```javascript
// 原始压缩形式：
// isEnabled(){let q=V7(),K=Z5();if(q==="firstParty")return!0;if(q==="vertex")return K.includes("claude-opus-4")||K.includes("claude-sonnet-4")||K.includes("claude-haiku-4");if(q==="foundry")return!0;return!1}

// 还原后：
isEnabled() {
  const apiProvider = getApiProvider();   // V7()
  const modelName = getModelName();       // Z5()

  // Anthropic 直连 → 启用
  if (apiProvider === "firstParty") return true;

  // Google Vertex AI → 仅 Claude 4.x 系列启用
  if (apiProvider === "vertex") {
    return modelName.includes("claude-opus-4")
        || modelName.includes("claude-sonnet-4")
        || modelName.includes("claude-haiku-4");
  }

  // Amazon Bedrock Foundry → 启用
  if (apiProvider === "foundry") return true;

  // 其他所有 provider（包括 Venus Proxy）→ 禁用
  return false;
}
```

**Provider 判定逻辑**：当用户设置 `ANTHROPIC_BASE_URL` 指向 Venus Proxy 时，Claude Code 将其识别为非官方端点，provider 类型不属于 `firstParty` / `vertex` / `foundry` 中的任何一种，`isEnabled()` 返回 `false`，WebSearch 被排除在可用工具之外。

---

## 5. 源码证据：嵌套模型调用链路

> 源码位置：`cli.js` 第 3456 行附近，WebSearchTool 的 `call` 方法

### 5.1 server tool schema 构建

```javascript
// 原始压缩形式：
// _6z=(q)=>{return{type:"web_search_20250305",name:"web_search",allowed_domains:q.allowed_domains,blocked_domains:q.blocked_domains,max_uses:8}}

// 还原后：
function buildWebSearchServerTool(input) {
  return {
    type: "web_search_20250305",  // Anthropic 服务端工具类型标识
    name: "web_search",
    allowed_domains: input.allowed_domains,
    blocked_domains: input.blocked_domains,
    max_uses: 8                    // 单次最多执行 8 次搜索
  };
}
```

`web_search_20250305` 是 Anthropic Messages API 中的**服务端工具类型**，不是客户端定义的 function tool。它告知 Anthropic 服务端在模型推理过程中自动调用搜索服务。

### 5.2 嵌套 API 调用

```javascript
// 还原后的调用链路（简化）：
async call(input, context, ...) {
  const startTime = performance.now();
  const { query } = input;

  // 构建一条独立的用户消息
  const userMessage = createUserMessage({
    content: "Perform a web search for the query: " + query
  });

  // 构建 server tool schema
  const webSearchTool = buildWebSearchServerTool(input);

  // 发起一次全新的模型 API 请求
  const stream = callModel({
    messages: [userMessage],
    systemPrompt: formatPrompt([
      "You are an assistant for performing a web search tool use"
    ]),
    tools: [],                          // 无客户端工具
    extraToolSchemas: [webSearchTool],  // ← 关键：附带 server-side tool
    querySource: "web_search_tool",
    // ...其他参数
  });

  // 消费流式响应
  const contentBlocks = [];
  for await (const event of stream) {
    if (event.type === "assistant") {
      contentBlocks.push(...event.message.content);
    }
    // 处理 web_search_tool_result 类型的 content block
    if (event.type === "stream_event") {
      // 提取搜索结果、进度更新等
    }
  }

  // 将搜索结果组装为 WebSearch 的输出格式
  const duration = (performance.now() - startTime) / 1000;
  return { data: parseSearchResults(contentBlocks, query, duration) };
}
```

### 5.3 完整调用时序

```
用户向 Claude Code 提问 → 主模型决定使用 WebSearch
    │
    ▼
WebSearchTool.call() 启动
    │
    ├─ 构建 server tool: { type: "web_search_20250305", max_uses: 8 }
    │
    ├─ 发起一次独立的 API 请求（不是复用主对话的请求）
    │   → POST /v1/messages
    │   → body.tools = []
    │   → body.extraToolSchemas = [web_search_20250305]
    │   → body.messages = [{ content: "Perform a web search for: ..." }]
    │
    ├─ Anthropic 服务端处理：
    │   ├─ 模型生成 server_tool_use（搜索请求）
    │   ├─ 服务端执行搜索，返回 web_search_tool_result
    │   ├─ 模型基于搜索结果生成文本总结
    │   └─ 可重复执行，最多 8 次搜索
    │
    ├─ Claude Code 接收流式响应
    │   ├─ 收集 web_search_tool_result 块
    │   ├─ 收集 text 块（模型总结）
    │   └─ 发送进度事件（query_update、search_results_received）
    │
    └─ 返回结构化结果给主模型
        { query, results: [...], durationSeconds }
```

---

## 6. Venus Proxy 为什么无法提供 server tool 能力

### 6.1 Venus Proxy 的定位

Venus Proxy（`v2.open.venus.oa.com/llmproxy/chat/completions`）是腾讯内部提供的 LLM 代理服务，API 格式遵循 **OpenAI Chat Completions** 兼容规范：

```python
# Venus Proxy 的标准调用方式
payload = {
    'model': 'claude-sonnet-4-6',
    'messages': [
        { 'role': 'system', 'content': '...' },
        { 'role': 'user', 'content': '...' }
    ]
}
# POST http://v2.open.venus.oa.com/llmproxy/chat/completions
```

### 6.2 不兼容的三个层面

| 层面 | Anthropic 原生 API | Venus Proxy |
|------|-------------------|-------------|
| **请求格式** | Anthropic Messages API（`/v1/messages`） | OpenAI Chat Completions（`/chat/completions`） |
| **工具定义** | 支持 `tools`（客户端工具）+ `extraToolSchemas`（服务端工具） | 仅支持 `tools`（函数调用），无 server tool 概念 |
| **响应格式** | 包含 `server_tool_use`、`web_search_tool_result` 等专属 content block 类型 | 仅标准的 `function_call` / `tool_calls` |

关键矛盾：

1. **`web_search_20250305` 是 Anthropic 独有的 server tool 类型**。Venus Proxy 的 OpenAI 兼容层无法识别和转发这种 tool schema
2. **搜索执行发生在 Anthropic 服务端**。Venus Proxy 即使能转发请求到上游 Claude 模型，中间层的格式转换也会丢失 server tool 相关字段
3. **响应中的 `web_search_tool_result` content block** 不属于 OpenAI 格式规范，Venus Proxy 可能无法正确透传

### 6.3 不是"接口不通"而是"能力不存在"

这不是简单的 URL 映射问题。`web_search_20250305` 是一种**服务端基础设施能力**，类似于 ChatGPT 的 Browse with Bing——它需要 Anthropic 的搜索索引、反爬虫系统、结果过滤管道等后端组件协同工作。Venus Proxy 只是一个 API 转发层，不具备也不需要复制这套基础设施。

---

## 7. 普通 tool_use 与 WebSearch 的对比

| 维度 | 普通 tool_use (Grep/Bash/FileRead) | WebSearch |
|------|-------------------------------------|-----------|
| 执行位置 | Claude Code 本地进程 | Anthropic 服务端 |
| 触发方式 | 模型输出 → 本地执行 → 回传结果 | 模型输出 → 新建 API 调用 → 服务端搜索 → 回传结果 |
| API 依赖 | 无。只需模型能输出合法 JSON | 依赖 `web_search_20250305` 服务端能力 |
| 数据流向 | 模型 → 本地文件系统/命令 | 模型 → Anthropic 搜索服务 → 互联网 |
| Provider 限制 | 无限制 | 仅 firstParty / vertex / foundry |
| 通过 Venus Proxy | 正常工作 | **不可用** |
| 通过 Bedrock | 正常工作 | 正常工作（foundry provider） |
| 通过 Vertex AI | 正常工作 | Claude 4.x 模型可用 |
| 延迟加载 | 多数不延迟 | `shouldDefer: true`（需要 ToolSearch 拉取） |

---

## 8. 工具注册与延迟加载的影响

WebSearch 被标记为 `shouldDefer: true`，意味着即使在 `isEnabled()` 返回 `true` 的场景下，它的完整 schema 也不会出现在初始系统提示词中，而是通过 ToolSearch 延迟加载：

```
初始提示词 → 仅包含 "WebSearch" 名称（无参数 schema）
模型需要搜索时 → 调用 ToolSearch("select:WebSearch") 获取完整 schema
获得 schema 后 → 正常调用 WebSearch
```

在 Venus Proxy 场景下，由于 `isEnabled()` 返回 `false`，WebSearch 连名称都不会出现在延迟加载列表中，模型完全不知道这个工具的存在。

---

## 9. 替代方案

### 9.1 方案对比

| 方案 | 可行性 | 复杂度 | 说明 |
|------|--------|--------|------|
| 切回 Anthropic 直连 | ★★★★★ | 低 | provider = firstParty，WebSearch 自然恢复 |
| 使用 Vertex AI | ★★★★ | 中 | 需要 GCP 配置，Claude 4.x 模型支持 |
| 使用 Bedrock Foundry | ★★★★ | 中 | 需要 AWS 配置 |
| 配置 MCP WebSearch 服务 | ★★★★ | 中 | 客户端侧执行搜索，完全绕过 server tool 限制 |
| 让 Venus 支持 Anthropic 原生格式 | ★★ | 高 | 需要 Venus 团队适配 Messages API server tool |
| 修改 `isEnabled()` 返回值 | ★ | 低 | 技术上可行但无实际意义——运行时仍会失败 |

### 9.2 推荐方案：MCP WebSearch 服务

最实用的方案是通过 MCP（Model Context Protocol）接入第三方搜索服务，作为 WebSearch 的替代品：

**原理**：MCP 工具是纯客户端工具，Claude Code 在本地调用 MCP 服务器完成搜索，不依赖 Anthropic 服务端能力。

**可选的 MCP 搜索服务**：

- **Brave Search MCP**：基于 Brave 搜索引擎，支持 Web 搜索和本地搜索
- **Tavily MCP**：面向 AI Agent 优化的搜索 API
- **SerpAPI MCP**：聚合多种搜索引擎结果
- **Exa MCP**：语义搜索引擎

**配置示例**（`.mcp.json`）：

```json
{
  "mcpServers": {
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@anthropic/brave-search-mcp"],
      "env": {
        "BRAVE_API_KEY": "your-brave-api-key"
      }
    }
  }
}
```

配置后，模型可通过 MCP 工具进行联网搜索，功能等价于内置 WebSearch，且与 Venus Proxy 完全兼容。

---

## 10. 附录：相关源码片段

### 10.1 WebSearchTool 完整定义结构

以下为从 `cli.js` 第 3456 行附近提取的 WebSearchTool 注册结构（反混淆还原）：

```javascript
const WebSearchTool = {
  name: "WebSearch",
  searchHint: "search the web for current information",
  maxResultSizeChars: 100000,
  shouldDefer: true,                    // 延迟加载

  async description(input) {
    return `Claude wants to search the web for: ${input.query}`;
  },

  userFacingName() { return "Web Search"; },

  // ── Provider 白名单 ──
  isEnabled() {
    const provider = getApiProvider();
    const model = getModelName();
    if (provider === "firstParty") return true;
    if (provider === "vertex") {
      return model.includes("claude-opus-4")
          || model.includes("claude-sonnet-4")
          || model.includes("claude-haiku-4");
    }
    if (provider === "foundry") return true;
    return false;
  },

  // ── 输入 Schema ──
  inputSchema: z.strictObject({
    query: z.string().min(2),
    allowed_domains: z.array(z.string()).optional(),
    blocked_domains: z.array(z.string()).optional(),
  }),

  isConcurrencySafe() { return true; },
  isReadOnly() { return true; },

  // ── 执行逻辑：嵌套模型调用 ──
  async call(input, context, ...) {
    const { query } = input;
    const serverTool = {
      type: "web_search_20250305",
      name: "web_search",
      allowed_domains: input.allowed_domains,
      blocked_domains: input.blocked_domains,
      max_uses: 8,
    };

    const stream = callModel({
      messages: [{ content: "Perform a web search for the query: " + query }],
      systemPrompt: ["You are an assistant for performing a web search tool use"],
      tools: [],
      extraToolSchemas: [serverTool],
      querySource: "web_search_tool",
      // ...
    });

    // 消费流式搜索结果...
    return { data: parseSearchResults(...) };
  },

  // ── 输出格式 ──
  // 末尾强制附加引用提醒：
  // "REMINDER: You MUST include the sources above in your response
  //  to the user using markdown hyperlinks."
};
```

### 10.2 Provider 判定的其他引用

同一 `cli.js` 中，`V7()` (getApiProvider) 还被以下功能引用：

- **Team Memory 同步**：`if(V7()!=="firstParty"||!eM()) return` — 非 firstParty 禁用团队记忆
- **远程设置同步**：`eligible: false` — 3P provider 不符合远程配置同步条件
- **WebFetch 工具**：可能有类似的 provider 检查（待确认）

这表明 Provider 白名单是 Claude Code 中的**通用治理机制**，不仅影响 WebSearch，也影响其他依赖 Anthropic 平台服务的功能。

### 10.3 web_search_20250305 的版本演进

从源码中还发现了更新版本的提示：

```
Without dynamic filtering, the previous `web_search_20250305` version is also available.
```

这暗示 Anthropic 已在开发更新的搜索 tool 版本（可能带有动态过滤能力），但 `web_search_20250305` 仍作为 fallback 保留。

---

## 参考文档

- [Claude Code 搜索工具体系与实现机制](./claudecode-搜索工具体系与实现机制.md) — 本项目已有文档，覆盖 Grep/Glob/WebSearch/LSP/ToolSearch 的完整分析
- [Claude Code AgentLoop、工具调用与提示词设计](./claudecode-agentloop-工具调用与提示词设计.md) — 工具注册、执行生命周期、权限检查的详细分析
- [Anthropic Messages API 文档](https://docs.anthropic.com/en/docs/build-with-claude/tool-use) — server tool 的官方说明
