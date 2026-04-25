# SDK 与 LLM Proxy 关系解析

以 OpenAI Python SDK 为例，解析 SDK 封装的关键 API、与 LLM Proxy（LiteLLM）的分工关系，以及两者如何组合使用。

---

## 一、SDK 是什么

SDK（Software Development Kit）是 LLM 提供商发布的**客户端库**，封装了 HTTP 请求的构造、认证、流式解析、类型定义和错误处理。本质上是对 REST API 的**语言级包装**。

```
没有 SDK 时，你需要手写 HTTP 请求：

  url = "https://api.openai.com/v1/chat/completions"
  headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
  body = json.dumps({"model": "gpt-5", "messages": [...]})
  response = requests.post(url, headers=headers, data=body)
  result = response.json()["choices"][0]["message"]["content"]

有 SDK 时，一行搞定：

  response = client.chat.completions.create(model="gpt-5", messages=[...])
  result = response.choices[0].message.content
```

## 二、OpenAI SDK 关键 API

### 2.1 核心对话 API

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-...",
    base_url="https://api.openai.com/v1",  # 可改指向 Proxy
    timeout=60.0,
    max_retries=2,
)

# 非流式
response = client.chat.completions.create(
    model="gpt-5",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ],
    temperature=0.7,
    max_tokens=1024,
)

print(response.choices[0].message.content)  # 文本回复
print(response.usage.prompt_tokens)         # 输入 token
print(response.usage.completion_tokens)     # 输出 token
```

### 2.2 流式输出

```python
stream = client.chat.completions.create(
    model="gpt-5",
    messages=[{"role": "user", "content": "写一首诗"}],
    stream=True,
)

for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        print(delta.content, end="", flush=True)
```

### 2.3 工具调用（Function Calling）

```python
response = client.chat.completions.create(
    model="gpt-5",
    messages=[{"role": "user", "content": "北京今天天气如何？"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市的天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名"}
                },
                "required": ["city"]
            }
        }
    }],
    tool_choice="auto",
)

tool_call = response.choices[0].message.tool_calls[0]
print(tool_call.function.name)       # "get_weather"
print(tool_call.function.arguments)  # '{"city": "北京"}'
```

### 2.4 Embeddings

```python
response = client.embeddings.create(
    model="text-embedding-3-small",
    input="Hello world",
)

vector = response.data[0].embedding  # [0.0023, -0.009, ...]
```

### 2.5 SDK 关键 API 汇总

| API | 用途 | 对应 REST 端点 |
| --- | --- | --- |
| `client.chat.completions.create()` | 对话补全（核心） | `POST /chat/completions` |
| `client.embeddings.create()` | 文本向量化 | `POST /embeddings` |
| `client.images.generate()` | 图像生成 | `POST /images/generations` |
| `client.audio.transcriptions.create()` | 语音转文字 | `POST /audio/transcriptions` |
| `client.moderations.create()` | 内容审核 | `POST /moderations` |
| `client.responses.create()` | Responses API（新） | `POST /responses` |

---

## 三、Proxy 是什么

Proxy（如 LiteLLM）是部署在**服务端**的中间层，对外暴露与 OpenAI 兼容的 REST API，对内路由到不同的 LLM 提供商。

```
SDK 工作在客户端（你的代码里）
Proxy 工作在服务端（独立进程/服务）

SDK 负责：构造请求、解析响应、类型安全、流式处理
Proxy 负责：路由转发、格式转换、日志记录、限流、缓存
```

---

## 四、SDK 与 Proxy 的关系

### 4.1 四种组合方式

```
方式 1：SDK 直连 LLM（最简单）

  OpenAI SDK → OpenAI API
  你的代码直接调用一家提供商，SDK 内置认证和重试。

方式 2：SDK + Proxy → LLM（推荐生产环境）

  OpenAI SDK → LiteLLM Proxy → OpenAI / Anthropic / Google ...
  SDK 只负责客户端封装，Proxy 负责路由和观测。

方式 3：Proxy SDK 直接调用（无独立 Proxy 服务）

  import litellm
  litellm.completion(model="anthropic/claude-sonnet-4-6", ...)
  LiteLLM 作为 Python 库内嵌在你的代码中。

方式 4：纯 HTTP 请求 + Proxy（无 SDK）

  requests.post("http://proxy:4000/chat/completions", ...)
  不用任何 SDK，手写 HTTP 请求发给 Proxy。← 你的 venus_client.py 就是这种
```

### 4.2 分层关系图

```
┌──────────────────────────────────────────────────────────────┐
│  你的应用代码                                                  │
│                                                              │
│  ┌────────────────────┐    ┌─────────────────────────────┐   │
│  │ OpenAI SDK          │    │ Anthropic SDK                │   │
│  │ client.chat.        │    │ client.messages.             │   │
│  │   completions.      │    │   create()                   │   │
│  │   create()          │    │                              │   │
│  │                     │    │ 封装：认证、类型、流式解析      │   │
│  │ 封装：认证、类型、   │    └──────────┬──────────────────┘   │
│  │  流式解析、重试      │               │                     │
│  └──────────┬─────────┘               │                     │
│             │                         │                     │
│             │  base_url 可以改指向 ↓    │  base_url 可改指向 ↓ │
└─────────────│─────────────────────────│─────────────────────┘
              │                         │
              ▼                         ▼
┌─────────────────────────────────────────────────────────────┐
│  LLM Proxy 层（LiteLLM / Venus / TIMI API / OpenRouter）     │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ 格式转换  │  │ 日志记录  │  │ 限流/缓存 │  │ 负载均衡   │  │
│  └──────────┘  └──────────┘  └──────────┘  └────────────┘  │
│                                                             │
│  接收 OpenAI 格式请求 → 转换为目标提供商格式 → 转发                │
└──────────────────────────┬──────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ OpenAI   │ │Anthropic │ │ Google   │
        │ API      │ │ API      │ │ API      │
        └──────────┘ └──────────┘ └──────────┘
```

关键点：**SDK 的 `base_url` 参数是连接两层的桥梁**。只需改一个 URL，SDK 的请求就从直连 LLM 变成经过 Proxy。

---

## 五、具体对比示例

同一个需求：**调用 Claude Sonnet 模型回答问题，并记录 token 用量**。

### 5.1 方式一：直接用 Anthropic SDK

```python
from anthropic import Anthropic

client = Anthropic(api_key="sk-ant-...")

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "什么是 LLM Gateway？"}],
)

print(response.content[0].text)
print(f"Tokens: {response.usage.input_tokens} in, {response.usage.output_tokens} out")
```

特点：
- 直连 Anthropic API
- 用的是 **Anthropic 专有格式**（`messages.create`、`content[0].text`）
- 想换成 GPT？需要换 SDK、改代码结构

### 5.2 方式二：用 LiteLLM SDK（Python 库模式）

```python
import litellm

litellm.success_callback = ["langfuse"]  # 一行接入可观测性

response = litellm.completion(
    model="anthropic/claude-sonnet-4-6",  # 前缀指定提供商
    messages=[{"role": "user", "content": "什么是 LLM Gateway？"}],
    max_tokens=1024,
)

print(response.choices[0].message.content)  # OpenAI 格式的响应
print(f"Tokens: {response.usage.prompt_tokens} in, {response.usage.completion_tokens} out")
```

特点：
- LiteLLM 在你的进程内，把 Anthropic 格式**翻译成** OpenAI 格式
- 换模型只需改 `model` 参数：`"openai/gpt-5"` / `"vertex_ai/gemini-pro"`
- `success_callback` 自动上报到 Langfuse

### 5.3 方式三：OpenAI SDK + LiteLLM Proxy

```bash
# 先启动 LiteLLM Proxy（独立进程）
litellm --model anthropic/claude-sonnet-4-6 --port 4000
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000",  # 指向 LiteLLM Proxy
    api_key="sk-anything",             # Proxy 层统一鉴权
)

response = client.chat.completions.create(
    model="anthropic/claude-sonnet-4-6",
    messages=[{"role": "user", "content": "什么是 LLM Gateway？"}],
    max_tokens=1024,
)

print(response.choices[0].message.content)
print(f"Tokens: {response.usage.prompt_tokens} in, {response.usage.completion_tokens} out")
```

特点：
- 客户端代码和"直连 OpenAI"**完全一样**，只改了 `base_url`
- Proxy 负责把 OpenAI 格式翻译成 Anthropic 格式再转发
- Proxy 侧配置 Langfuse，客户端代码零侵入

### 5.4 方式四：纯 HTTP + Venus Proxy（你的 venus_client.py）

```python
import requests

response = requests.post(
    "http://v2.open.venus.oa.com/llmproxy/chat/completions",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "什么是 LLM Gateway？"}],
    },
)

result = response.json()
print(result["choices"][0]["message"]["content"])
print(f"Tokens: {result['usage']['prompt_tokens']} in, {result['usage']['completion_tokens']} out")
```

特点：
- 不依赖任何 SDK，纯 HTTP
- Venus 做格式转换（你发 OpenAI 格式，Venus 转成 Anthropic 格式转发给 Claude）
- 自己在代码层加日志/限流/重试（你的 `venus_client.py` 做的事）

### 5.5 四种方式对比

| 维度 | ① Anthropic SDK 直连 | ② LiteLLM SDK | ③ OpenAI SDK + LiteLLM Proxy | ④ HTTP + Venus Proxy |
| --- | --- | --- | --- | --- |
| 客户端依赖 | `anthropic` | `litellm` | `openai` | `requests` |
| API 格式 | Anthropic 专有 | OpenAI 兼容 | OpenAI 兼容 | OpenAI 兼容 |
| 换模型改动 | 换 SDK + 改代码 | 改 `model` 参数 | 改 `model` 参数 | 改 `model` 参数 |
| 可观测性 | 自己加 | `success_callback` | Proxy 侧配置 | 自己加 |
| 额外部署 | 无 | 无 | 需部署 Proxy | 需有 Proxy |
| 类型安全 | ✅ 强类型 | ✅ | ✅ 强类型 | ❌ 裸 dict |
| 流式解析 | SDK 封装好 | SDK 封装好 | SDK 封装好 | 需自己解析 SSE |

---

## 六、SDK 封装了什么（你自己写就要处理这些）

不用 SDK 时，以下这些都需要你自己实现（以 `venus_client.py` 为例对照）：

| SDK 自动处理的 | 不用 SDK 时你需要 | venus_client.py 的做法 |
| --- | --- | --- |
| 认证 header 构造 | 手拼 `Authorization` | `_get_venus_token()` |
| 请求体 JSON 序列化 | `json.dumps()` | `requests.post(json=payload)` |
| HTTP 错误处理 | 检查 `status_code` | `if response.status_code != 200` |
| 重试逻辑（429/500） | 自己实现退避 | `for attempt in range(1, max_retries+1)` + 指数退避 |
| 限流 | 自己实现 | `RateLimiter`（滑动窗口） |
| 响应解析 | 手动 `response.json()["choices"][0]...` | 同左 |
| 流式 SSE 解析 | 逐行读取 `text/event-stream` | 未实现（只用非流式） |
| 类型定义 | 无，全是 dict | 无 |
| 超时控制 | `timeout` 参数 | `timeout=payload.pop("_timeout", 120)` |

你的 `venus_client.py` 实际上是一个**手写的迷你 SDK**，覆盖了认证、重试、限流、日志这四个核心能力。没覆盖的是流式解析和类型定义——如果后续需要流式输出或更严格的类型校验，可以考虑换用 OpenAI SDK + `base_url` 指向 Venus。

---

## 七、实例分析：openclaw 的 Anthropic SDK 调用链

通过阅读 openclaw 源码，确认其原生配置 API Key 时，底层直接使用 `@anthropic-ai/sdk`（Anthropic 官方 Node.js SDK）。

### 7.1 源码证据

核心文件：`src/agents/anthropic-transport-stream.ts`

```typescript
import Anthropic from "@anthropic-ai/sdk";  // ← 直接导入 Anthropic SDK
```

客户端实例化（API Key 鉴权模式）：

```typescript
// src/agents/anthropic-transport-stream.ts — createAnthropicTransportClient()
client = new Anthropic({
    apiKey,
    baseURL: model.baseUrl,           // ← 可通过配置改指向代理
    dangerouslyAllowBrowser: true,
    defaultHeaders: {
        "accept": "application/json",
        "anthropic-beta": betaFeatures.join(","),
        ...model.headers,
    },
    fetch,                            // ← 可注入自定义 fetch
});
```

实际 API 调用：

```typescript
// 流式调用 Anthropic Messages API
const anthropicStream = client.messages.stream(
    { ...params, stream: true },
    transportOptions.signal ? { signal: transportOptions.signal } : undefined,
);
```

### 7.2 请求参数组装（buildAnthropicParams）

openclaw 在调用 SDK 前，会组装完整的 Anthropic Messages API 参数：

```typescript
// buildAnthropicParams() 生成的请求体
{
    model: "claude-sonnet-4-6",
    messages: convertAnthropicMessages(...),  // 内部格式 → Anthropic 格式
    system: [{ type: "text", text: "系统提示词..." }],
    tools: convertAnthropicTools(...),        // 工具定义转换
    thinking: { type: "adaptive" },           // 4.6 模型的自适应思维
    max_tokens: 32000,
    stream: true,
}
```

### 7.3 SSE 流事件解析

SDK 返回的 SSE 流被逐事件处理，提取完整的结构化数据：

```
SSE 事件                   openclaw 提取的数据
─────────────────────────────────────────────────────
message_start           → usage.input_tokens, cache_read_input_tokens
content_block_start     → 识别 text / thinking / tool_use 类型
content_block_delta     → 累积文本、thinking、工具参数 JSON
content_block_stop      → 完成一个内容块
message_delta           → stop_reason, 最终 usage（output_tokens）
```

每个内容块被解析为统一的 `TransportContentBlock` 类型：

```typescript
type TransportContentBlock =
    | { type: "text"; text: string }
    | { type: "thinking"; thinking: string; thinkingSignature: string; redacted?: boolean }
    | { type: "toolCall"; id: string; name: string; arguments: unknown };
```

最终组装为包含完整 usage 的输出对象：

```typescript
{
    role: "assistant",
    content: [/* text, thinking, toolCall 块 */],
    api: "anthropic-messages",
    provider: "anthropic",
    model: "claude-sonnet-4-6",
    usage: {
        input: 8234,
        output: 567,
        cacheRead: 7800,
        cacheWrite: 0,
        totalTokens: 16601,
        cost: { input: 0.024, output: 0.008, cacheRead: 0.007, cacheWrite: 0, total: 0.039 },
    },
    stopReason: "toolUse",
}
```

### 7.4 完整调用链路图

```
openclaw 用户配置
  │
  │  config: { model: "anthropic/claude-sonnet-4-6", apiKey: "sk-ant-..." }
  │
  ▼
registerAnthropicPlugin()               ← extensions/anthropic/register.runtime.ts
  provider: "anthropic"
  api: "anthropic-messages"
  envVars: ["ANTHROPIC_API_KEY"]
  │
  ▼
createAnthropicMessagesTransportStreamFn()  ← src/agents/anthropic-transport-stream.ts
  │
  ├── 1. 获取 API Key
  │       getEnvApiKey("anthropic") → "sk-ant-..."
  │
  ├── 2. 创建 Anthropic SDK 客户端
  │       new Anthropic({ apiKey, baseURL: model.baseUrl })
  │                                          │
  │                              默认: https://api.anthropic.com
  │                              改 Venus: http://v2.open.venus.oa.com/llmproxy
  │
  ├── 3. 组装请求参数
  │       buildAnthropicParams() → { model, messages, system, tools, thinking, ... }
  │
  ├── 4. 调用 SDK
  │       client.messages.stream(params)
  │          │
  │          ▼  HTTP POST /v1/messages (SSE)
  │       Anthropic API（或代理层）
  │
  └── 5. 逐事件解析 SSE 流 → TransportContentBlock[]
          → { text, thinking, toolCall, usage, stopReason }
```

### 7.5 改接 Venus API 时发生了什么

当 openclaw 从直连 Anthropic 改为走 Venus API 时，唯一的变化是 `baseURL`：

```
直连模式（默认）：

  Anthropic SDK                          Anthropic API
  new Anthropic({                        https://api.anthropic.com/v1/messages
      baseURL: "https://api.anthropic.com",  ← SDK 默认值
      apiKey: "sk-ant-..."
  })
  client.messages.stream(params)  ──────→  POST /v1/messages
                                           Anthropic Messages 格式


改接 Venus 后：

  Anthropic SDK                          Venus API（代理层）        Anthropic API
  new Anthropic({
      baseURL: "http://v2.open.venus.oa.com/llmproxy",  ← 改了这一行
      apiKey: venus_token
  })
  client.messages.stream(params)  ──→  Venus 接收  ──→  格式转换  ──→  POST 实际 API
                                       Anthropic 格式    （如需要）
```

**关键点**：openclaw 的代码逻辑、参数组装、SSE 解析全部不变。SDK 的 `baseURL` 参数就是连接应用与代理层的桥梁——这与第四章的分层关系图完全一致。

### 7.6 与 LiteLLM 内部机制的类比

上一轮对话中提到 LiteLLM 内部也是调用各家原生 SDK，openclaw 的实现证实了这一点：

```
LiteLLM 内部：                            openclaw 内部：

litellm.completion(                      agent 调度 →
  model="anthropic/claude-sonnet-4-6")     createAnthropicMessagesTransportStreamFn()
      │                                        │
      ├── 解析 provider 前缀                    ├── 根据 provider config 选择 transport
      ├── import anthropic                     ├── import Anthropic from "@anthropic-ai/sdk"
      ├── 格式转换 (OpenAI → Anthropic)         ├── 格式转换 (内部格式 → Anthropic Messages)
      ├── anthropic.messages.create()          ├── client.messages.stream()
      ├── 格式转换 (Anthropic → OpenAI)         ├── SSE 解析 → TransportContentBlock
      └── 返回 OpenAI 格式                      └── 返回内部统一格式
```

两者都是**在自己的代码层调用原生 SDK**，区别在于：
- LiteLLM 对外暴露 OpenAI 兼容格式（作为 Proxy / RHI 层）
- openclaw 对外暴露自己的内部 Agent 格式（作为终端产品）

但底层都是 `new Anthropic(...)` → `client.messages.stream()`——都要经过原厂 SDK 这一层。
