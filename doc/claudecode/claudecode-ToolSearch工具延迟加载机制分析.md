# Claude Code ToolSearch 与工具延迟加载机制分析

> 基于 Claude Code 源码分析，解释截图中提到的 `ToolSearch`、`defer_loading`、`tool_reference` 和工具延迟加载（Tool Deferred Loading）机制；并说明它和 MCP 工具数量膨胀、第三方 API 代理兼容性、Trae 这类 AI IDE 的关系。

---

## 1. 核心结论

`ToolSearch` 不是代码搜索工具，也不是 WebSearch。它搜索的对象是 **工具定义本身**。

更准确地说：

```text
ToolSearch =
  在工具太多时，
  先不把所有工具的完整 JSON Schema 放进 prompt，
  而是只告诉模型“有哪些工具名可用”，
  等模型真的需要某个工具时，
  再通过 ToolSearch 拉取这个工具的完整 schema。
```

这套机制解决的是一个很实际的问题：Claude Code 里除了内置工具，还有大量 MCP 工具。如果每次请求都把所有工具的完整 description、parameters、JSON Schema 全部塞进系统提示词，会明显挤占上下文窗口，也会破坏 prompt cache 的稳定性。

因此 Claude Code 引入了工具延迟加载：

```text
没有延迟加载：
  初始请求直接携带 40 个 MCP 工具的完整 schema
  模型一开始就知道所有工具怎么调用
  代价是 prompt 很大、缓存不稳定、上下文浪费

启用延迟加载：
  初始请求只暴露 ToolSearch + 非延迟工具
  延迟工具只以“名字列表”形式提醒模型
  模型需要某个工具时，先调用 ToolSearch
  ToolSearch 返回 tool_reference
  服务端再把对应工具 schema 展开给模型
```

---

## 2. 为什么需要 ToolSearch

### 2.1 工具数量会爆炸

普通内置工具数量有限，例如 `Read`、`Edit`、`Bash`、`Grep`、`Glob` 等。但 MCP 接入后，工具数量可能快速膨胀：

```text
mcp__github__create_issue
mcp__github__list_pull_requests
mcp__github__get_file_contents
mcp__slack__send_message
mcp__slack__list_channels
mcp__notion__create_page
mcp__figma__get_node
...
```

如果一个 IDE 或 Agent 内置 40 个 MCP tools，每个工具都有一段描述和参数 schema，那么每次模型请求都要携带大量重复内容。

这会带来三个问题：

1. **上下文浪费**：大量工具本轮根本不会用到，却占据 token。
2. **prompt cache 不稳定**：MCP 工具连接、断开、顺序变化、schema 变化都会导致工具前缀变化。
3. **模型选择负担变重**：工具列表太长，模型更容易误选或忽略真正需要的工具。

### 2.2 ToolSearch 的角色

`ToolSearch` 的角色类似“工具目录检索器”。

它不执行业务动作，只负责从 deferred tools 池子里找工具：

```text
用户需求：
  “帮我在 GitHub 创建一个 issue”

模型初始只知道：
  有一个 mcp__github__create_issue 工具名，
  但不知道它的参数 schema。

模型先调用：
  ToolSearch({ "query": "select:mcp__github__create_issue" })

ToolSearch 返回：
  tool_reference: mcp__github__create_issue

随后模型才能按 schema 调用：
  mcp__github__create_issue({ ... })
```

---

## 3. 源码入口

### 3.1 ToolSearchTool 本身

源码位置：

```text
ClaudeCode/src/tools/ToolSearchTool/ToolSearchTool.ts
ClaudeCode/src/tools/ToolSearchTool/prompt.ts
ClaudeCode/src/utils/toolSearch.ts
ClaudeCode/src/services/api/claude.ts
ClaudeCode/src/utils/api.ts
```

`ToolSearchTool` 的输入 schema 很小，只需要一个查询词和可选的结果数量：

```typescript
// ClaudeCode/src/tools/ToolSearchTool/ToolSearchTool.ts
export const inputSchema = lazySchema(() =>
  z.object({
    query: z
      .string()
      .describe(
        'Query to find deferred tools. Use "select:<tool_name>" for direct selection, or keywords to search.',
      ),
    max_results: z
      .number()
      .optional()
      .default(5)
      .describe('Maximum number of results to return (default: 5)'),
  }),
)
```

也就是说，`ToolSearch` 支持两类查询：

```text
select:Read,Edit,Grep
notebook jupyter
+slack send
```

其中：

| 查询形式 | 示例 | 含义 |
|---|---|---|
| `select:` | `select:mcp__github__create_issue` | 精确选择某个工具 |
| 普通关键词 | `notebook jupyter` | 按工具名、描述、search hint 做关键词匹配 |
| `+` 必选词 | `+slack send` | 要求工具名或描述里必须匹配 `slack`，再按 `send` 排序 |

### 3.2 哪些工具会被延迟加载

`isDeferredTool()` 决定工具是否需要通过 `ToolSearch` 延迟加载：

```typescript
// ClaudeCode/src/tools/ToolSearchTool/prompt.ts
export function isDeferredTool(tool: Tool): boolean {
  if (tool.alwaysLoad === true) return false

  if (tool.isMcp === true) return true

  if (tool.name === TOOL_SEARCH_TOOL_NAME) return false

  return tool.shouldDefer === true
}
```

核心规则是：

| 工具类型 | 是否延迟加载 | 原因 |
|---|---:|---|
| MCP 工具 | 是 | MCP 工具数量多、工作流相关、schema 体积大 |
| `shouldDefer: true` 的内置工具 | 是 | 内置工具也可能很重，例如某些搜索/浏览类工具 |
| `ToolSearch` 自身 | 否 | 模型必须先能调用它，才能发现其他延迟工具 |
| `alwaysLoad: true` 的工具 | 否 | 明确要求首轮就完整暴露 |

---

## 4. 请求链路：从工具池到 API

### 4.1 先判断是否启用 ToolSearch

Claude Code 会在每次 API 请求前判断本轮是否启用工具延迟加载：

```typescript
// ClaudeCode/src/services/api/claude.ts
let useToolSearch = await isToolSearchEnabled(
  options.model,
  tools,
  options.getToolPermissionContext,
  options.agents,
  'query',
)
```

`isToolSearchEnabled()` 会综合判断：

1. 当前模式是否允许 ToolSearch。
2. 当前模型是否支持 `tool_reference`。
3. `ToolSearchTool` 是否可用。
4. `auto` 模式下，deferred tools 的总 token / 字符量是否超过阈值。

源码里定义了三种模式：

```typescript
export type ToolSearchMode = 'tst' | 'tst-auto' | 'standard'
```

对应环境变量：

| `ENABLE_TOOL_SEARCH` | 模式 | 含义 |
|---|---|---|
| 未设置 | `tst` | 默认启用 |
| `true` | `tst` | 强制启用 |
| `auto` / `auto:N` | `tst-auto` | deferred 工具体积超过阈值才启用 |
| `false` | `standard` | 关闭 ToolSearch |
| `auto:100` | `standard` | 等价于关闭 |

此外，`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 会强制走 `standard`，避免把实验性的 API 字段发给不兼容的代理。

### 4.2 启用后，过滤工具列表

启用 ToolSearch 后，Claude Code 不会把所有 deferred tools 都直接塞进 prompt，而是过滤工具列表：

```typescript
// ClaudeCode/src/services/api/claude.ts
if (useToolSearch) {
  const discoveredToolNames = extractDiscoveredToolNames(messages)

  filteredTools = tools.filter(tool => {
    if (!deferredToolNames.has(tool.name)) return true
    if (toolMatchesName(tool, TOOL_SEARCH_TOOL_NAME)) return true
    return discoveredToolNames.has(tool.name)
  })
} else {
  filteredTools = tools.filter(
    t => !toolMatchesName(t, TOOL_SEARCH_TOOL_NAME),
  )
}
```

这段逻辑很关键：

```text
启用 ToolSearch：
  保留所有非 deferred 工具
  保留 ToolSearch 自身
  deferred 工具只有在被 tool_reference 发现后才进入本轮工具列表

关闭 ToolSearch：
  去掉 ToolSearch
  其他工具正常进入工具列表
```

这也是截图中提到的点：如果没有启用 ToolSearch，那么 Claude Code 会把 `ToolSearchTool` 自己过滤掉；如果启用了，则通过 `ToolSearch` 动态发现 deferred tools。

### 4.3 给延迟工具加 `defer_loading`

工具 schema 最终会经过 `toolToAPISchema()` 转成 Anthropic API 的工具格式。如果本轮需要延迟加载，就给 schema 加上 `defer_loading: true`：

```typescript
// ClaudeCode/src/utils/api.ts
const schema: BetaToolWithExtras = {
  name: base.name,
  description: base.description,
  input_schema: base.input_schema,
  ...(base.strict && { strict: true }),
  ...(base.eager_input_streaming && { eager_input_streaming: true }),
}

if (options.deferLoading) {
  schema.defer_loading = true
}
```

`defer_loading` 是 Anthropic beta 形态的一部分。它不是普通 OpenAI function calling 的标准字段。

---

## 5. `tool_reference` 是什么

`ToolSearch` 真正返回给模型的不是普通字符串，而是 `tool_reference` block。

源码里：

```typescript
// ClaudeCode/src/tools/ToolSearchTool/ToolSearchTool.ts
return {
  type: 'tool_result',
  tool_use_id: toolUseID,
  content: content.matches.map(name => ({
    type: 'tool_reference' as const,
    tool_name: name,
  })),
}
```

可以理解成：

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_123",
  "content": [
    {
      "type": "tool_reference",
      "tool_name": "mcp__github__create_issue"
    }
  ]
}
```

Claude Code 源码注释说明，`tool_reference` 会由服务端展开为完整工具定义：

```text
ToolSearchTool 返回 tool_reference
Anthropic 服务端读取 tool_reference
服务端把对应工具 schema 展开成 <functions>...</functions>
模型随后就能按完整 schema 调用该工具
```

所以 `tool_reference` 不是一个普通文本提示，而是 API 协议里的特殊内容块。

这也是为什么第三方 OpenAI 兼容代理经常不支持它：很多代理只理解普通 `messages`、`tools`、`tool_calls`，但不理解 Anthropic 的 beta content block。

---

## 6. 一个完整例子

### 6.1 场景

假设 Claude Code 当前连接了一个 GitHub MCP server，里面有很多工具：

```text
mcp__github__create_issue
mcp__github__list_issues
mcp__github__create_pull_request
mcp__github__get_file_contents
mcp__github__search_repositories
...
```

用户输入：

```text
帮我给这个仓库创建一个 issue，标题是“修复登录失败”，正文写上复现步骤。
```

### 6.2 没有 ToolSearch 时

没有延迟加载时，请求一开始就要带上所有 GitHub MCP 工具的完整 schema：

```json
{
  "tools": [
    {
      "name": "mcp__github__create_issue",
      "description": "Create a new issue in a GitHub repository...",
      "input_schema": {
        "type": "object",
        "properties": {
          "owner": { "type": "string" },
          "repo": { "type": "string" },
          "title": { "type": "string" },
          "body": { "type": "string" }
        },
        "required": ["owner", "repo", "title"]
      }
    },
    {
      "name": "mcp__github__list_issues",
      "description": "List issues in a repository...",
      "input_schema": {}
    }
  ]
}
```

如果 MCP 工具很多，这个 `tools` 数组会很大。

### 6.3 有 ToolSearch 时

启用 ToolSearch 后，模型初始不会看到所有 GitHub 工具的完整 schema，只会通过系统注入的元信息知道“有这些 deferred tools 名称”。

可能类似：

```xml
<available-deferred-tools>
mcp__github__create_issue
mcp__github__list_issues
mcp__github__create_pull_request
mcp__github__get_file_contents
</available-deferred-tools>
```

或者在新版机制中，通过 `deferred_tools_delta` attachment，以 `<system-reminder>` 形式告诉模型 deferred tools 发生了哪些变化。

模型要创建 issue 时，先调用：

```json
{
  "name": "ToolSearch",
  "input": {
    "query": "select:mcp__github__create_issue"
  }
}
```

`ToolSearch` 返回：

```json
{
  "matches": ["mcp__github__create_issue"],
  "query": "select:mcp__github__create_issue",
  "total_deferred_tools": 28
}
```

映射到 API 的 `tool_result` 时，内容变成：

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_search_001",
  "content": [
    {
      "type": "tool_reference",
      "tool_name": "mcp__github__create_issue"
    }
  ]
}
```

随后服务端展开该工具的完整 schema，模型再调用真正的 GitHub 工具：

```json
{
  "name": "mcp__github__create_issue",
  "input": {
    "owner": "example",
    "repo": "demo",
    "title": "修复登录失败",
    "body": "复现步骤：\n1. 打开登录页\n2. 输入正确账号密码\n3. 点击登录\n\n期望：登录成功\n实际：页面提示登录失败"
  }
}
```

### 6.4 关键词搜索例子

如果模型不知道精确工具名，也可以搜索：

```json
{
  "name": "ToolSearch",
  "input": {
    "query": "+github issue create",
    "max_results": 3
  }
}
```

可能返回：

```json
{
  "matches": [
    "mcp__github__create_issue",
    "mcp__github__list_issues",
    "mcp__github__update_issue"
  ],
  "query": "+github issue create",
  "total_deferred_tools": 28
}
```

这种方式类似“工具目录搜索”，不是互联网搜索，也不是代码搜索。

---

## 7. 它和 `<system-reminder>` 的关系

ToolSearch 需要让模型知道“当前有哪些 deferred tools 可以搜索”。Claude Code 有两种方式注入这类信息。

旧路径或未启用 delta attachment 时，会在请求前插入一个 meta user message：

```typescript
// ClaudeCode/src/services/api/claude.ts
messagesForAPI = [
  createUserMessage({
    content: `<available-deferred-tools>\n${deferredToolList}\n</available-deferred-tools>`,
    isMeta: true,
  }),
  ...messagesForAPI,
]
```

新版路径中，deferred tool 池子的变化可以通过 `deferred_tools_delta` attachment 持久化，并以 `<system-reminder>` 的形式提醒模型。

所以它和 `<system-reminder>` 的关系是：

```text
ToolSearch:
  负责按名称/关键词检索工具 schema

<available-deferred-tools> / <system-reminder>:
  负责告诉模型“有哪些工具名可被 ToolSearch 检索”

tool_reference:
  负责让服务端把某个工具的完整 schema 展开给模型
```

---

## 8. 第三方代理为什么容易失效

截图里提到“如果走第三方协议，Claude Code 的 Tool Deferred Loading 机制不透明，tool search 基本无用了”，核心原因在这里。

ToolSearch 不是纯客户端字符串技巧，它依赖 Anthropic Messages API 的几个特殊能力：

1. `defer_loading: true`
2. `tool_reference` content block
3. 对应的 beta header
4. 服务端把 `tool_reference` 展开成完整工具定义

这些并不是 OpenAI function calling 的通用标准。

Claude Code 源码里也明确做了兼容性判断：

```typescript
// ClaudeCode/src/utils/toolSearch.ts
if (
  !process.env.ENABLE_TOOL_SEARCH &&
  getAPIProvider() === 'firstParty' &&
  !isFirstPartyAnthropicBaseUrl()
) {
  return false
}
```

意思是：

```text
如果 provider 看起来是 firstParty，
但 ANTHROPIC_BASE_URL 指向的不是 Anthropic 官方地址，
且用户没有显式设置 ENABLE_TOOL_SEARCH，
则默认关闭 ToolSearch。
```

源码注释也说明了原因：第三方 API gateway 通常不支持 `tool_reference`，可能直接返回 400。

另外，关闭实验 beta 时，Claude Code 会在工具 schema 出口统一剥离非标准字段：

```typescript
// ClaudeCode/src/utils/api.ts
if (isEnvTruthy(process.env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS)) {
  const allowed = new Set([
    'name',
    'description',
    'input_schema',
    'cache_control',
  ])
  // defer_loading 等字段会被去掉
}
```

因此，对第三方代理来说，关键不是“模型会不会用工具”，而是代理链路是否完整支持 Anthropic 的 beta tool search 协议。

---

## 9. 和普通 function calling 的区别

普通 function calling 的模式是：

```text
客户端把完整 tools schema 发给模型
模型选择某个 tool_call
客户端执行工具
把 tool_result 返回给模型
```

ToolSearch / deferred loading 的模式是：

```text
客户端把部分工具标记为 defer_loading
模型只知道这些工具的名字
模型先调用 ToolSearch
ToolSearch 返回 tool_reference
服务端展开工具 schema
模型再调用真正工具
客户端执行工具
```

区别可以概括为：

| 维度 | 普通 function calling | ToolSearch / deferred loading |
|---|---|---|
| 初始 prompt | 带完整工具 schema | 只带非延迟工具 + ToolSearch |
| deferred 工具 | 不存在这个概念 | MCP / `shouldDefer` 工具延迟暴露 |
| 工具发现 | 模型直接看完整 tools | 模型先搜索工具目录 |
| 协议依赖 | 大多数 OpenAI 兼容代理支持 | 依赖 Anthropic beta 字段 |
| 主要收益 | 简单直接 | 减少工具 schema 占用，提升缓存稳定性 |
| 主要风险 | 工具体积大 | 第三方代理可能不兼容 |

---

## 10. 和 prompt cache 的关系

ToolSearch 和 prompt cache 是两套机制，但目标有重叠：都想减少重复上下文成本。

没有 ToolSearch 时：

```text
system prompt
+ 40 个 MCP tools 的完整 schema
+ 用户消息
+ 历史消息
```

如果 MCP 工具列表变化，或者 schema 顺序变化，工具前缀就会变化，prompt cache 更容易 miss。

有 ToolSearch 时：

```text
system prompt
+ ToolSearch schema
+ 常用非 deferred 工具 schema
+ deferred tool names / delta
+ 用户消息
+ 历史消息
```

完整 schema 只在需要时通过 `tool_reference` 展开。这让“每轮都稳定出现”的工具 schema 更少，有利于减少上下文膨胀，也能降低工具池变化对 cache key 的影响。

Claude Code 在 prompt cache break detection 里还专门排除了 `defer_loading` 工具：

```typescript
// ClaudeCode/src/services/api/claude.ts
const toolsForCacheDetection = allTools.filter(
  t => !('defer_loading' in t && t.defer_loading),
)
```

源码注释说明：API 会把 `defer_loading` 工具从 prompt 中剥离，因此它们不应该影响实际的服务端 cache key。如果检测时把它们算进去，会造成误报。

---

## 11. Trae 是什么

Trae 是字节跳动推出的 AI 编程 IDE / AI coding 产品，不是一个协议，也不是一个模型名称。

可以把它理解为和 Cursor、Claude Code、OpenClaw 类似的 AI 编程环境或 Agent 产品形态：

```text
Trae =
  AI IDE
  + 编程智能体
  + 代码生成/修改/调试能力
  + 可能接入多模型
  + 支持或集成 MCP 等外部工具生态
```

在截图里的语境中，提到 Trae 主要是为了说明一个问题：

```text
当一个 AI IDE 内置很多 MCP tools 时，
工具 schema 会非常多，
如果全部塞进每轮 prompt，
上下文会快速膨胀。
```

所以 Trae 这类产品也会遇到类似的工具加载问题：

1. 是否一开始暴露所有工具？
2. 是否只暴露工具名，按需加载 schema？
3. 是否依赖模型服务端支持特殊的 tool reference？
4. 如果走第三方模型代理，协议字段是否能透传？

Claude Code 的 `ToolSearch` 是 Anthropic 生态里的一个具体解法。Trae 如果要实现类似能力，可以有两种路线：

| 路线 | 说明 | 优缺点 |
|---|---|---|
| Anthropic 原生路线 | 复用 `defer_loading` / `tool_reference` 等服务端协议能力 | 上下文收益好，但强依赖 Anthropic 兼容性 |
| 客户端自研路线 | IDE/Agent 自己维护工具索引，模型选中后客户端再补充 schema | 更适合多模型/第三方代理，但实现更复杂 |

因此，“Trae 内置很多 MCP tools”本身不是问题；真正的问题是这些工具 schema 如何被暴露给模型，以及所走的模型 API 是否支持延迟工具加载协议。

---

## 12. 一句话总结

`ToolSearch` 是 Claude Code 为了解决“工具太多导致 prompt 膨胀”的动态工具发现机制。

它通过：

```text
defer_loading
+ deferred tool name list
+ ToolSearch 查询
+ tool_reference
+ 服务端 schema 展开
```

把“所有工具一开始都完整塞给模型”改成“模型需要时再加载工具 schema”。

但这套机制不是普通 function calling，而是依赖 Anthropic Messages API 的 beta 能力。因此在第三方代理、OpenAI 兼容网关、多模型 IDE 场景下，不能默认认为它一定可用。Trae 这类 AI IDE 面对的是同一个工具膨胀问题，但是否能复用 Claude Code 的 ToolSearch，取决于它底层模型 API 和代理链路是否支持这些特殊协议字段。

