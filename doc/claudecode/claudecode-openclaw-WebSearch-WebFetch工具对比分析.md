# Claude Code 与 OpenClaw 的 WebSearch / WebFetch 工具对比分析

> 基于 `ClaudeCode` 与 `openclaw` 源码，说明 `web-search`、`web-fetch` 两类工具的概念区别、调用方式、返回结果、实现边界，并给出具体使用例子。文末补充说明 Trae 是什么，以及它和这类 Web 工具 / MCP 工具体系的关系。

---

## 1. 核心结论

`web-search` 和 `web-fetch` 解决的是两个不同问题：

```text
web-search:
  我不知道目标网页在哪，需要先搜索互联网。

web-fetch:
  我已经知道 URL，需要读取这个网页的正文内容。
```

换句话说：

```text
web-search = 找网页
web-fetch  = 读网页
```

在 Claude Code 和 OpenClaw 中，这两个工具的实现路线明显不同：

| 维度 | Claude Code WebSearch | Claude Code WebFetch | OpenClaw web_search | OpenClaw web_fetch |
|---|---|---|---|---|
| 工具目的 | 搜索互联网 | 读取指定 URL 并按 prompt 处理 | 搜索互联网 | 读取指定 URL 并抽取正文 |
| 执行位置 | 依赖 Anthropic server tool | 本地 fetch + Haiku 二次处理 | 客户端 provider 体系 | 默认本地 fetch，可 fallback provider |
| 输入 | `query`、域名白/黑名单 | `url`、`prompt` | provider 定义的搜索参数 | `url`、`extractMode`、`maxChars` |
| 返回 | 搜索结果 + 模型文本 | 被 prompt 处理后的结果 | JSON 搜索结果 + provider | 抽取后的 markdown/text + 元数据 |
| 是否依赖模型服务商特殊能力 | 是，`web_search_20250305` | 部分依赖 Haiku 处理内容 | 否，主要依赖外部搜索 provider | 否，本地抓取为主 |
| 代理兼容性 | 对 OpenAI 兼容代理不友好 | 相对可控，但仍依赖 Anthropic 小模型 | 更适合多 provider | 更适合多 provider / 本地执行 |

---

## 2. 一个具体例子

假设用户问：

```text
帮我了解 OpenAI 最近发布的 Responses API，然后总结官方文档里的关键限制。
```

Agent 通常应该分两步：

### 2.1 先用 web-search 找入口

如果不知道官方页面 URL，先搜索：

```json
{
  "query": "OpenAI Responses API official documentation limitations"
}
```

可能得到：

```json
{
  "results": [
    {
      "title": "Responses API - OpenAI API",
      "url": "https://platform.openai.com/docs/api-reference/responses"
    },
    {
      "title": "Built-in tools - OpenAI API",
      "url": "https://platform.openai.com/docs/guides/tools"
    }
  ]
}
```

这一步的目标不是阅读全文，而是定位可信 URL。

### 2.2 再用 web-fetch 读取具体 URL

拿到 URL 后，再读取页面：

```json
{
  "url": "https://platform.openai.com/docs/api-reference/responses",
  "extractMode": "markdown",
  "maxChars": 20000
}
```

这一步才是把网页正文抽取出来，供模型总结、引用或分析。

所以二者的边界是：

```text
不知道地址 → web-search
已经有地址 → web-fetch
```

---

## 3. Claude Code 的 WebSearch 实现

源码位置：

```text
ClaudeCode/src/tools/WebSearchTool/WebSearchTool.ts
ClaudeCode/src/tools/WebSearchTool/prompt.ts
```

### 3.1 工具 schema

Claude Code 的 WebSearch 输入参数是：

```typescript
z.strictObject({
  query: z.string().min(2).describe('The search query to use'),
  allowed_domains: z
    .array(z.string())
    .optional()
    .describe('Only include search results from these domains'),
  blocked_domains: z
    .array(z.string())
    .optional()
    .describe('Never include search results from these domains'),
})
```

也就是：

```json
{
  "query": "Claude Code web_search_20250305 server tool",
  "allowed_domains": ["docs.anthropic.com"]
}
```

### 3.2 不是普通本地搜索，而是 Anthropic server tool

关键源码：

```typescript
function makeToolSchema(input: Input): BetaWebSearchTool20250305 {
  return {
    type: 'web_search_20250305',
    name: 'web_search',
    allowed_domains: input.allowed_domains,
    blocked_domains: input.blocked_domains,
    max_uses: 8,
  }
}
```

这里的 `type: 'web_search_20250305'` 是重点。它说明 Claude Code 的 WebSearch 不是在本地调用 DuckDuckGo / Google API，而是把 Anthropic 的 server-side web search tool 作为 `extraToolSchemas` 注入到一次嵌套模型调用里。

调用链简化如下：

```text
用户请求搜索
  ↓
Claude Code WebSearchTool.call()
  ↓
构造 server tool schema: web_search_20250305
  ↓
queryModelWithStreaming({
  messages: ["Perform a web search for the query: ..."],
  extraToolSchemas: [web_search_20250305]
})
  ↓
Anthropic 服务端执行 web search
  ↓
返回 server_tool_use / web_search_tool_result / text blocks
  ↓
Claude Code 整理成工具结果
```

### 3.3 Provider 白名单

Claude Code 还会判断当前 API provider 是否支持 WebSearch：

```typescript
isEnabled() {
  const provider = getAPIProvider()
  const model = getMainLoopModel()

  if (provider === 'firstParty') {
    return true
  }

  if (provider === 'vertex') {
    const supportsWebSearch =
      model.includes('claude-opus-4') ||
      model.includes('claude-sonnet-4') ||
      model.includes('claude-haiku-4')

    return supportsWebSearch
  }

  if (provider === 'foundry') {
    return true
  }

  return false
}
```

这解释了为什么 Claude Code 的 WebSearch 在第三方 OpenAI 兼容代理里经常不可用：它依赖的是 Anthropic Messages API 的 server tool 能力，不是普通 function calling。

---

## 4. Claude Code 的 WebFetch 实现

源码位置：

```text
ClaudeCode/src/tools/WebFetchTool/WebFetchTool.ts
ClaudeCode/src/tools/WebFetchTool/utils.ts
```

### 4.1 工具 schema

Claude Code 的 WebFetch 输入不是 `extractMode`，而是：

```typescript
z.strictObject({
  url: z.string().url().describe('The URL to fetch content from'),
  prompt: z.string().describe('The prompt to run on the fetched content'),
})
```

示例：

```json
{
  "url": "https://docs.anthropic.com/en/docs/claude-code",
  "prompt": "总结这个页面里和工具调用有关的设计。"
}
```

### 4.2 本地抓取，再用小模型处理

`WebFetchTool.call()` 的主流程是：

```typescript
const response = await getURLMarkdownContent(url, abortController)
```

拿到内容后，如果不是预批准的 markdown 短内容，会调用：

```typescript
result = await applyPromptToMarkdown(
  prompt,
  content,
  abortController.signal,
  isNonInteractiveSession,
  isPreapproved,
)
```

`applyPromptToMarkdown()` 内部会通过 Haiku / small fast model 对网页 markdown 执行用户给定的 prompt。

所以 Claude Code 的 WebFetch 更像：

```text
fetch URL
  ↓
HTML / markdown 转换
  ↓
把网页内容 + prompt 交给小模型
  ↓
返回“处理后的答案”
```

它不是简单返回整篇网页原文，而是返回“根据 prompt 处理后的结果”。

### 4.3 安全与资源控制

Claude Code 的 WebFetch 有多层限制：

1. URL 长度限制。
2. 不允许用户名密码形式的 URL。
3. 域名需要可公开解析。
4. 调用 Anthropic 的 domain info 接口做 domain blocklist 检查。
5. 限制 HTTP 内容大小。
6. 限制 redirect。
7. 对二进制内容落盘并提示路径。
8. 使用 LRU cache 缓存 URL 内容。

这说明 WebFetch 不是“任意读取内网 URL”的工具，它被设计成读取公开网页，并通过权限和网络策略降低 SSRF / 数据外泄风险。

---

## 5. OpenClaw 的 web_search 实现

源码位置：

```text
openclaw/src/agents/tools/web-search.ts
openclaw/src/web-search/runtime.ts
openclaw/src/plugins/web-search-providers.runtime.ts
openclaw/src/plugins/web-search-providers.shared.ts
openclaw/extensions/*/web-search*.ts
```

### 5.1 工具入口

OpenClaw 的 `web_search` 工具由 `createWebSearchTool()` 创建：

```typescript
export function createWebSearchTool(options?: {
  config?: OpenClawConfig;
  sandboxed?: boolean;
  runtimeWebSearch?: RuntimeWebSearchMetadata;
}): AnyAgentTool | null {
  const resolved = resolveWebSearchDefinition({
    ...options,
    preferRuntimeProviders,
  });
  if (!resolved) {
    return null;
  }

  return {
    label: "Web Search",
    name: "web_search",
    description: resolved.definition.description,
    parameters: resolved.definition.parameters,
    execute: async (_toolCallId, args) => {
      const result = await runWebSearch({ ... });
      return jsonResult({
        ...result.result,
        provider: result.provider,
      });
    },
  };
}
```

这里的重点是：OpenClaw 的 `web_search` 不绑定某一个搜索引擎，而是通过 provider definition 动态决定工具描述、参数 schema 和执行逻辑。

### 5.2 Provider 体系

OpenClaw 的搜索 provider 来自插件 / extension：

```text
openclaw/extensions/brave
openclaw/extensions/duckduckgo
openclaw/extensions/exa
openclaw/extensions/firecrawl
openclaw/extensions/google
openclaw/extensions/minimax
openclaw/extensions/moonshot
openclaw/extensions/perplexity
openclaw/extensions/searxng
openclaw/extensions/tavily
openclaw/extensions/xai
```

运行时会通过 `resolveWebSearchDefinition()` 选择 provider：

```typescript
export function resolveWebSearchDefinition(
  options?: ResolveWebSearchDefinitionParams,
) {
  const providers = sortWebSearchProvidersForAutoDetect(
    options?.preferRuntimeProviders
      ? resolveRuntimeWebSearchProviders(...)
      : resolvePluginWebSearchProviders(...)
  );

  return resolveWebProviderDefinition({
    providerId: options?.providerId,
    providers,
    resolveAutoProviderId: ...,
    createTool: ({ provider, config, toolConfig, runtimeMetadata }) =>
      provider.createTool({ config, searchConfig: toolConfig, runtimeMetadata }),
  });
}
```

再由 `runWebSearch()` 执行：

```typescript
for (const candidate of candidates) {
  const definition = candidate.createTool(...)
  if (!definition) {
    continue
  }
  return {
    provider: candidate.id,
    result: await definition.execute(params.args),
  }
}
```

这说明 OpenClaw 的 `web_search` 是客户端 provider 调度体系。它不要求模型服务端支持 `web_search_20250305` 这种 Anthropic server tool。

### 5.3 和 Claude Code 的核心差异

Claude Code：

```text
模型服务端提供 web_search 能力
Claude Code 只是把 server tool schema 注入进去
```

OpenClaw：

```text
OpenClaw 本地选择 web search provider
客户端调用 Brave / Tavily / Exa / Perplexity / DuckDuckGo 等 provider
把结果作为普通 tool result 返回给模型
```

所以 OpenClaw 更适合多模型、多 provider、第三方代理场景；Claude Code 更依赖 Anthropic 原生协议能力。

---

## 6. OpenClaw 的 web_fetch 实现

源码位置：

```text
openclaw/src/agents/tools/web-fetch.ts
openclaw/src/agents/tools/web-fetch-utils.ts
openclaw/src/web-fetch/runtime.ts
openclaw/src/plugins/web-fetch-providers.runtime.ts
openclaw/extensions/firecrawl/web-fetch-provider.ts
```

### 6.1 工具 schema

OpenClaw 的 `web_fetch` 输入是：

```typescript
const WebFetchSchema = Type.Object({
  url: Type.String({ description: "HTTP or HTTPS URL to fetch." }),
  extractMode: Type.Optional(
    stringEnum(["markdown", "text"], {
      description: 'Extraction mode ("markdown" or "text").',
      default: "markdown",
    }),
  ),
  maxChars: Type.Optional(
    Type.Number({
      description: "Maximum characters to return (truncates when exceeded).",
      minimum: 100,
    }),
  ),
});
```

示例：

```json
{
  "url": "https://docs.anthropic.com/en/docs/claude-code",
  "extractMode": "markdown",
  "maxChars": 30000
}
```

这和 Claude Code 的 WebFetch 很不一样：

```text
Claude Code WebFetch:
  输入 url + prompt
  输出 prompt 处理后的结果

OpenClaw web_fetch:
  输入 url + extractMode + maxChars
  输出网页正文 markdown/text 与元数据
```

### 6.2 默认是本地抓取

OpenClaw 的 `runWebFetch()` 会先做 URL 校验和网络抓取：

```typescript
const result = await fetchWithWebToolsNetworkGuard({
  url: params.url,
  maxRedirects: params.maxRedirects,
  timeoutSeconds: params.timeoutSeconds,
  lookupFn: params.lookupFn,
  init: {
    headers: {
      Accept: "text/markdown, text/html;q=0.9, */*;q=0.1",
      "User-Agent": params.userAgent,
      "Accept-Language": "en-US,en;q=0.9",
    },
  },
});
```

也就是说，OpenClaw 的默认路径是客户端直接 HTTP(S) 抓取网页，而不是让模型服务端去抓。

### 6.3 HTML 内容抽取

如果响应是 `text/html`，OpenClaw 会优先用 Readability 抽取正文：

```typescript
const readable = await extractReadableContent({
  html: body,
  url: finalUrl,
  extractMode: params.extractMode,
});
```

`extractReadableContent()` 内部懒加载：

```typescript
Promise.all([import("@mozilla/readability"), import("linkedom")])
```

提取逻辑是：

```text
HTML
  ↓
sanitizeHtml
  ↓
linkedom parseHTML
  ↓
Mozilla Readability
  ↓
markdown 或 text
```

如果 Readability 失败，会尝试：

1. provider fallback，例如 Firecrawl。
2. basic HTML cleanup。
3. 仍失败则返回错误。

### 6.4 Provider fallback

OpenClaw 的 `web_fetch` 不是只有本地抓取。源码里也支持 provider fallback：

```typescript
const providerFallback = params.resolveProviderFallback();
if (!providerFallback) {
  return null;
}
const rawPayload = await providerFallback.definition.execute({
  url: params.urlToFetch,
  extractMode: params.extractMode,
  maxChars: params.maxChars,
});
```

当前源码里可见的 fetch provider 主要是 Firecrawl：

```text
openclaw/extensions/firecrawl/web-fetch-provider.ts
openclaw/extensions/firecrawl/src/firecrawl-fetch-provider.ts
```

因此 OpenClaw 的 web_fetch 可以概括为：

```text
优先本地 fetch + Readability 抽取
失败时尝试 provider fallback
最后返回结构化 JSON
```

### 6.5 返回结果结构

OpenClaw 会返回丰富元数据：

```json
{
  "url": "https://example.com/page",
  "finalUrl": "https://example.com/page",
  "status": 200,
  "contentType": "text/html",
  "title": "Example Page",
  "extractMode": "markdown",
  "extractor": "readability",
  "externalContent": {
    "untrusted": true,
    "source": "web_fetch",
    "wrapped": true
  },
  "truncated": false,
  "length": 12000,
  "rawLength": 11500,
  "wrappedLength": 12000,
  "fetchedAt": "2026-04-27T00:00:00.000Z",
  "tookMs": 800,
  "text": "..."
}
```

这里的 `externalContent.untrusted` 很重要：OpenClaw 明确把网页内容标记为不可信外部内容，避免模型把网页里的 prompt injection 当成系统指令执行。

---

## 7. 两个 Agent 的架构差异

### 7.1 Claude Code 更依赖 Anthropic 原生能力

Claude Code 的 WebSearch 是 Anthropic server tool 封装：

```text
Claude Code
  ↓
Anthropic Messages API
  ↓
web_search_20250305 server tool
  ↓
搜索结果回到模型 response blocks
```

优点：

1. 与 Claude 模型深度集成。
2. 搜索、引用、结果组织由 Anthropic 服务端统一处理。
3. 对 first-party 用户体验更自然。

缺点：

1. 强依赖 Anthropic 原生 Messages API。
2. OpenAI 兼容代理通常不支持。
3. 不容易替换搜索 provider。

### 7.2 OpenClaw 更偏客户端工具编排

OpenClaw 的 WebSearch / WebFetch 都是普通客户端工具：

```text
OpenClaw Agent
  ↓
createWebSearchTool / createWebFetchTool
  ↓
provider runtime / local fetch runtime
  ↓
外部搜索 API 或 HTTP(S) fetch
  ↓
JSON tool result
  ↓
模型继续推理
```

优点：

1. 不强依赖某个模型服务商的 server tool。
2. 可以接多个 provider。
3. 对第三方代理、多模型环境更友好。
4. `web_fetch` 默认可本地执行，并有 SSRF guard。

缺点：

1. 搜索质量取决于 provider。
2. 需要用户配置 API key 或 provider。
3. 客户端要自己处理安全、缓存、错误、内容抽取。

---

## 8. WebSearch 与 WebFetch 的使用建议

### 8.1 什么时候用 WebSearch

适合：

```text
查最新新闻
查某个库的最新版本
查官方文档入口
查多个网页候选结果
查不知道 URL 的问题
```

例子：

```json
{
  "query": "Claude Code ToolSearch defer_loading tool_reference"
}
```

### 8.2 什么时候用 WebFetch

适合：

```text
读取一个已知 URL
总结某篇文档
提取网页正文
分析指定页面内容
把搜索结果中的某个页面读深
```

Claude Code 示例：

```json
{
  "url": "https://docs.anthropic.com/en/docs/claude-code",
  "prompt": "提取这个页面中关于工具调用的关键设计。"
}
```

OpenClaw 示例：

```json
{
  "url": "https://docs.anthropic.com/en/docs/claude-code",
  "extractMode": "markdown",
  "maxChars": 20000
}
```

---

## 9. Trae 是什么

Trae 是字节跳动推出的 AI 编程 IDE / AI coding 产品，不是模型，也不是协议。

可以把它理解为与 Cursor、Claude Code、OpenClaw 同一类问题域里的产品：

```text
Trae =
  AI IDE
  + 编程 Agent
  + 代码生成 / 修改 / 调试
  + 多模型接入
  + 可能接入 MCP / 外部工具生态
```

它和本文主题的关系在于：这类 AI IDE 都会遇到同一个工具系统设计问题。

```text
Agent 要不要联网搜索？
要不要读取网页？
工具是在客户端执行，还是依赖模型服务端执行？
MCP 工具很多时，schema 如何管理？
第三方模型代理能不能透传特殊工具协议？
```

Claude Code 的路线是深度绑定 Anthropic 能力，例如 `web_search_20250305`、`tool_reference`、`defer_loading` 等。OpenClaw 的路线更偏开放 provider / 客户端工具编排。Trae 如果支持类似能力，也必须在这两种路线之间取舍：

| 路线 | 特点 |
|---|---|
| 服务端原生工具 | 能和特定模型深度集成，但代理兼容性差 |
| 客户端 provider 工具 | 更开放、更适合多模型，但客户端复杂度更高 |

---

## 10. 总结

`web-search` 和 `web-fetch` 不是同一个工具的两种叫法，而是 Agent 联网能力的两个阶段：

```text
web-search:
  从互联网中找到候选页面。

web-fetch:
  从指定 URL 中读取可供模型分析的正文。
```

Claude Code 和 OpenClaw 的实现差异可以压缩成一句话：

```text
Claude Code 更依赖 Anthropic 原生 server tool；
OpenClaw 更依赖客户端 provider 和本地 fetch runtime。
```

因此，在 Anthropic first-party 环境里，Claude Code 的 WebSearch 集成更深；在第三方代理、多模型、多 provider 的场景里，OpenClaw 的实现更灵活。

