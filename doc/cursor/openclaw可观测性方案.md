# OpenClaw LLM 调用可观测性方案

如何在不改动（或最小改动）openclaw 源码的前提下，记录其与 Venus API / Anthropic API 之间的完整 request/response JSON 数据。

---

## 一、背景：openclaw 的原生调用链路

通过源码分析（`src/agents/anthropic-transport-stream.ts`），openclaw 原生使用 `@anthropic-ai/sdk` 直接调用 Anthropic Messages API：

```
openclaw Agent 调度
  │
  ├── buildAnthropicParams()           组装请求参数
  │     { model, messages, system, tools, thinking, max_tokens, stream }
  │
  ├── new Anthropic({ apiKey, baseURL })   创建 SDK 客户端
  │
  ├── client.messages.stream(params)   发起流式 API 调用
  │     │
  │     ▼  HTTP POST /v1/messages (SSE)
  │   baseURL 指向的目标（Anthropic / Venus / 自建代理）
  │
  └── 逐事件解析 SSE → TransportContentBlock[]
        { text, thinking, toolCall, usage, stopReason, cost }
```

当改接 Venus API 时，仅 `baseURL` 变化，其余逻辑不变：

```
直连：  Anthropic SDK → https://api.anthropic.com/v1/messages → Anthropic
Venus： Anthropic SDK → http://v2.open.venus.oa.com/llmproxy/v1/messages → Venus → Anthropic
```

**需求**：在这条链路上，记录 openclaw 发出的完整请求体和收到的完整响应体。

---

## 二、方案对比总览

| 方案 | 改 openclaw 源码？ | 能记录什么 | 复杂度 | 推荐场景 |
| --- | --- | --- | --- | --- |
| **A. 本地 LiteLLM Proxy** | 只改 `baseURL` 配置 | request + response 全量 JSON | 低 | 持续观测、Langfuse 可视化 |
| **B. mitmproxy 抓包** | 完全不改 | 原始 HTTP 流量 | 低 | 快速调试、临时查看 |
| **C. 源码层加日志** | 需要改 | 最灵活，能记录业务上下文 | 中 | 深度分析 prompt 组装逻辑 |
| **D. SDK httpx 中间件** | 少量改 | request/response + 自定义逻辑 | 中 | openclaw 用 OpenAI SDK 时 |

---

## 三、方案 A：本地 LiteLLM Proxy（推荐）

### 3.1 原理

在 openclaw 和 Venus/Anthropic 之间插入一层本地 LiteLLM Proxy，透传请求的同时记录全量 JSON 到 Langfuse 或本地文件。

```
改之前：
  openclaw → Venus API → Claude

改之后：
  openclaw → localhost:4000 (LiteLLM) → Venus API → Claude
                    │
                    ├── 记录完整 request JSON（messages, system, tools, thinking...）
                    ├── 记录完整 response JSON（content blocks, usage, stop_reason...）
                    └── 上报到 Langfuse / 写入本地日志文件
```

### 3.2 配置步骤

**1. 安装 LiteLLM**

```bash
pip install litellm
```

**2. 编写配置文件 `litellm_config.yaml`**

```yaml
model_list:
  - model_name: "claude-sonnet-4-6"
    litellm_params:
      model: "openai/claude-sonnet-4-6"
      api_base: "http://v2.open.venus.oa.com/llmproxy"  # 或 Anthropic 直连地址
      api_key: "your_venus_token@5172"

litellm_settings:
  success_callback: ["langfuse"]   # 上报到 Langfuse
  failure_callback: ["langfuse"]

environment_variables:
  LANGFUSE_PUBLIC_KEY: "pk-..."
  LANGFUSE_SECRET_KEY: "sk-..."
  LANGFUSE_HOST: "https://cloud.langfuse.com"  # 或自部署地址

general_settings:
  master_key: "sk-local-dev"
```

> 如果不需要 Langfuse，可将 `success_callback` 改为 `["log_to_file"]` 记录到本地文件。

**3. 启动 Proxy**

```bash
litellm --config litellm_config.yaml --port 4000 --detailed_debug
```

**4. 修改 openclaw 配置**

只需将 `baseURL` 从 Venus/Anthropic 改为本地 LiteLLM：

```
# openclaw 的 provider 配置中
baseURL: http://localhost:4000
apiKey: sk-local-dev
```

### 3.3 能记录到的数据

每次 LLM 调用，LiteLLM 的 Langfuse callback 自动记录：

| 字段 | 内容 | 示例 |
| --- | --- | --- |
| **Input (request)** | 完整 messages 数组 + system prompt + tools 定义 | `{ system: [...], messages: [...], tools: [...] }` |
| **Output (response)** | 结构化内容块数组 | `[{ type: "thinking", ... }, { type: "text", ... }, { type: "tool_use", ... }]` |
| **Token 用量** | input / output / cache_read / cache_write | `{ input_tokens: 8234, output_tokens: 567, cache_read: 7800 }` |
| **Cost** | 基于 token 的费用估算 | `$0.039` |
| **Latency** | API 调用耗时 | `2.3s` |
| **Model** | 实际调用的模型名 | `claude-sonnet-4-6` |
| **stop_reason** | 停止原因 | `end_turn` / `tool_use` / `max_tokens` |

Langfuse 中的 Trace 结构：

```
OpenClaw Session
  ├── litellm.completion.LLM (12.3s, 8.2k tokens)    ← 第 1 轮
  │     Input:  system + user message + tools
  │     Output: [thinking + text + tool_use(Read)]
  │
  ├── litellm.completion.LLM (8.7s, 6.1k tokens)     ← 第 2 轮
  │     Input:  messages + tool_result
  │     Output: [thinking + text + tool_use(Grep)]
  │
  └── litellm.completion.LLM (5.1s, 4.2k tokens)     ← 最终轮
        Output: [thinking + text]
        stop_reason: end_turn
```

### 3.4 优缺点

| 优点 | 缺点 |
| --- | --- |
| openclaw 源码零修改 | 增加一跳网络延迟（本地部署可忽略） |
| 全量 request/response JSON | 需要额外启动 LiteLLM 进程 |
| Langfuse 可视化 Trace | LiteLLM 可能不兼容某些实验性 API 字段 |
| 自动 token 计数和成本估算 | |

> **兼容性提示**：如果 openclaw 发送了 LiteLLM 不认识的实验性字段（如 `defer_loading`），可能返回 `400 Extra inputs are not permitted`。参考 Claude Code 的做法，可设置 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 或在 LiteLLM 配置中忽略未知字段。

---

## 四、方案 B：mitmproxy 抓包（零改动）

### 4.1 原理

在网络层截获 openclaw 与 Venus/Anthropic 之间的 HTTP 流量，完全不碰 openclaw 代码。

```
openclaw → HTTP Proxy (mitmproxy:8080) → Venus API → Claude
                │
                └── Web UI 实时查看每个请求的 request body / response body
```

### 4.2 配置步骤

```bash
# 1. 安装
pip install mitmproxy

# 2. 启动（监听 8080，Web UI 在 8081）
mitmweb --mode regular

# 3. 设置环境变量，让 openclaw 的 HTTP 请求走代理
# Windows PowerShell:
$env:HTTP_PROXY = "http://localhost:8080"
$env:HTTPS_PROXY = "http://localhost:8080"

# 4. 启动 openclaw，然后在浏览器打开 http://localhost:8081 查看流量
```

> Venus API 使用 HTTP（非 HTTPS），不需要处理证书信任问题。如果目标是 HTTPS 的 Anthropic API，需要额外安装 mitmproxy 的 CA 证书。

### 4.3 能记录到的数据

在 mitmproxy Web UI 中，每个请求可以看到：

- **Request**：完整的 HTTP headers + JSON body（messages, system, tools...）
- **Response**：SSE 流的原始文本（`event: message_start`、`event: content_block_delta` 等）
- **Timing**：请求发起时间、首字节时间、完成时间

### 4.4 优缺点

| 优点 | 缺点 |
| --- | --- |
| 完全不改 openclaw | SSE 流式响应是原始文本，需手动解析 |
| 即开即用，适合临时调试 | 无 Langfuse 集成，无法持久化分析 |
| 能看到完整 HTTP 层细节 | 不适合长期运行 |
| 支持过滤、搜索、导出 | HTTPS 场景需额外处理证书 |

---

## 五、方案 C：源码层加日志

### 5.1 原理

在 openclaw 发起 API 调用的位置插入日志逻辑，记录组装好的请求参数和 SDK 返回的完整响应。

### 5.2 切入点

根据源码分析，最佳切入点是 `anthropic-transport-stream.ts` 中的 `createAnthropicMessagesTransportStreamFn()`：

```typescript
// src/agents/anthropic-transport-stream.ts 第 596-866 行
export function createAnthropicMessagesTransportStreamFn(): StreamFn {
  return (rawModel, context, rawOptions) => {
    // ...
    const params = buildAnthropicParams(model, context, isOAuthToken, transportOptions);

    // ✅ 切入点 1：记录完整请求参数
    // console.log(JSON.stringify(params, null, 2));

    const anthropicStream = client.messages.stream(params);

    // SSE 解析循环中...
    // event.type === "message_start" → usage
    // event.type === "content_block_delta" → text/thinking/toolCall

    // ✅ 切入点 2：流结束后记录完整输出
    // console.log(JSON.stringify(output, null, 2));
  };
}
```

### 5.3 实现方式：利用现有 onPayload 钩子

源码中已有一个 `onPayload` 回调机制（第 625-628 行），可以在不大改代码的情况下拦截请求参数：

```typescript
let params = buildAnthropicParams(model, context, isOAuthToken, transportOptions);
const nextParams = await transportOptions.onPayload?.(params, model);
if (nextParams !== undefined) {
  params = nextParams as Record<string, unknown>;
}
```

如果 openclaw 的配置或插件系统支持注入 `onPayload` 回调，可以在这里记录请求体而不改核心代码。

### 5.4 独有优势

只有方案 C 能记录到 openclaw **内部的业务逻辑**：

| 独有数据 | 说明 |
| --- | --- |
| system prompt 组装过程 | `buildAgentSystemPrompt()` 如何拼接静态指令 + skills + 工具描述 |
| messages 上下文管理 | 哪些历史消息被保留、哪些被压缩（compaction） |
| 工具定义转换细节 | `convertAnthropicTools()` 如何将内部工具映射为 Anthropic 格式 |
| thinking 配置决策 | 模型是否启用 adaptive thinking、budget 如何计算 |
| OAuth vs API Key 路径差异 | 不同鉴权模式下 headers 和参数的区别 |

### 5.5 优缺点

| 优点 | 缺点 |
| --- | --- |
| 最灵活，能记录任何内部数据 | 需要改 openclaw 源码 |
| 能记录请求组装前的业务逻辑 | 源码更新后需要重新适配 |
| 可以精确记录每一轮 Agent Loop | 日志格式需自行设计 |

---

## 六、方案 D：SDK 层 httpx 中间件

### 6.1 原理

Anthropic Node.js SDK 底层使用 `fetch` 发起 HTTP 请求。openclaw 的 `createAnthropicTransportClient()` 中已有自定义 `fetch` 注入点：

```typescript
const fetch = buildGuardedModelFetch(model);  // ← 可以包装这个 fetch

client = new Anthropic({
    apiKey,
    baseURL: model.baseUrl,
    fetch,  // ← 自定义 fetch 函数
});
```

### 6.2 实现思路

包装 `buildGuardedModelFetch()` 返回的 fetch 函数，在请求前后记录数据：

```typescript
function createLoggingFetch(originalFetch: typeof fetch): typeof fetch {
    return async (url, init) => {
        // 记录请求
        const requestBody = init?.body ? JSON.parse(init.body as string) : {};
        logger.info(`[LLM Request] ${url}`, JSON.stringify(requestBody, null, 2));

        // 实际调用
        const response = await originalFetch(url, init);

        // 注意：SSE 流式响应需要 clone 后读取，否则会消耗掉原始流
        const cloned = response.clone();
        cloned.text().then(text => {
            logger.info(`[LLM Response]`, text.slice(0, 5000));
        });

        return response;
    };
}
```

### 6.3 优缺点

| 优点 | 缺点 |
| --- | --- |
| 只需改 fetch 包装层 | 需要改 openclaw 源码 |
| 能记录原始 HTTP request/response | SSE 流式响应处理复杂（需 clone + 重组） |
| 不依赖外部服务 | 大型响应可能影响性能 |

---

## 七、方案选择决策树

```
你的目标是什么？
│
├── 快速看一眼请求/响应长什么样，临时调试
│     → 方案 B（mitmproxy）
│     零改动，设个环境变量就行
│
├── 持续记录所有 LLM 调用，可视化分析
│     → 方案 A（本地 LiteLLM + Langfuse）
│     只改一个 baseURL 配置，Langfuse 提供完整 Trace UI
│
├── 需要分析 openclaw 内部的 prompt 组装 / 上下文压缩逻辑
│     → 方案 C（源码层加日志）
│     唯一能看到 buildAnthropicParams 之前发生了什么的方式
│
└── 想在 SDK 层做最小侵入的拦截
      → 方案 D（自定义 fetch 中间件）
      改动集中在 fetch 包装，不影响业务逻辑
```

---

## 八、与三层可观测性架构的关系

参照 `LLM-API代理层与可观测性差距分析.md` 的三层架构：

```
┌─────────────────────────────────┐
│ Layer 3: IDE/Agent 事件层        │  ← 方案 C / D 工作在这里
│   openclaw 内部的业务逻辑         │     能看到 prompt 组装、工具选择、compaction
└────────────────┬────────────────┘
                 │
┌────────────────▼────────────────┐
│ Layer 2: LLM API 代理层          │  ← 方案 A 工作在这里
│   LiteLLM / Venus / TIMI API    │     能看到完整 request/response JSON
└────────────────┬────────────────┘
                 │
┌────────────────▼────────────────┐
│ Layer 1: LLM 推理层（黑盒）       │  ← 不可观测
│   模型内部的 attention/推理过程    │
└─────────────────────────────────┘

方案 B（mitmproxy）横切 Layer 2 和 Layer 3 之间的网络层
```

**最佳实践**：方案 A（Layer 2）和方案 C（Layer 3）可以同时部署，互不冲突：
- Layer 2（LiteLLM）：记录完整的 LLM API 调用——messages、thinking、token、cost
- Layer 3（源码日志）：记录 openclaw 内部的业务决策——prompt 如何拼的、上下文如何压缩的
- 通过 `session_id` 在 Langfuse 中关联两层数据
