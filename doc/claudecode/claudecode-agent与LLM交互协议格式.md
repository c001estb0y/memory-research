# Claude Code Agent 与 LLM 交互协议格式

基于 Claude Code 开源快照源码的深度分析。

---

## 一、总体回答：是 JSON，也是 user/assistant/system 结构

Claude Code 与 Claude API 之间的通信格式是 **JSON**，且严格遵循 Anthropic Messages API 的 **system + user + assistant** 三层结构。但 Claude Code 内部维护了一套更丰富的消息类型系统，在发送给 API 之前会做一次"归一化"。

---

## 二、API 请求的完整 JSON 结构

每次 Agent Loop 调用 LLM，发送的是一个这样的 JSON 请求：

```json
{
  "model": "claude-sonnet-4-20250514",

  "system": [
    {
      "type": "text",
      "text": "You are Claude Code, Anthropic's official CLI for Claude...",
      "cache_control": { "type": "ephemeral" }
    },
    {
      "type": "text",
      "text": "CWD: /project\nDate: 2026-03-31\nOS: win32..."
    }
  ],

  "messages": [
    { "role": "user", "content": "帮我修复购物车的并发问题" },
    {
      "role": "assistant",
      "content": [
        { "type": "text", "text": "让我先搜索相关代码。" },
        {
          "type": "tool_use",
          "id": "toolu_abc123",
          "name": "Grep",
          "input": { "pattern": "cart.*concurrent", "path": "src/" }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_abc123",
          "content": [
            { "type": "text", "text": "src/cart/service.ts:42: async getCart..." }
          ]
        }
      ]
    }
  ],

  "tools": [
    {
      "name": "Grep",
      "description": "Search for patterns in files...",
      "input_schema": {
        "type": "object",
        "properties": {
          "pattern": { "type": "string", "description": "Regex pattern" },
          "path": { "type": "string", "description": "Directory to search" }
        },
        "required": ["pattern"]
      }
    }
  ],

  "max_tokens": 16384,
  "thinking": { "type": "enabled", "budget_tokens": 10000 },
  "betas": ["interleaved-thinking-2025-05-14", "prompt-caching-2024-07-31"]
}
```

### 核心字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `model` | string | 模型标识符 |
| `system` | TextBlockParam[] | system prompt，拆成多个 text block（带缓存标记） |
| `messages` | MessageParam[] | user/assistant 交替的对话历史 |
| `tools` | ToolUnion[] | 工具定义列表（名称 + 描述 + JSON Schema） |
| `max_tokens` | number | 最大输出 token 数 |
| `thinking` | object | 扩展思维配置（可选） |
| `betas` | string[] | 启用的 beta 特性 |
| `tool_choice` | object | 工具选择策略（可选） |
| `temperature` | number | 温度（可选） |
| `metadata` | object | 请求元数据 |

---

## 三、system 字段——不是单个字符串

system prompt **不是一整段文本**，而是一个 **TextBlockParam 数组**，每个 block 可以独立设置缓存策略：

```json
{
  "system": [
    {
      "type": "text",
      "text": "<静态部分：身份、编码规范、工具使用规则...>",
      "cache_control": { "type": "ephemeral", "scope": "global" }
    },
    {
      "type": "text",
      "text": "<动态部分：CWD、日期、记忆、MCP 指令、语言...>"
    }
  ]
}
```

源码中的构建过程：

```typescript
// claude.ts — 把 system prompt 数组拆成带缓存标记的 text block
export function buildSystemPromptBlocks(
  systemPrompt: SystemPrompt,
  enablePromptCaching: boolean,
): TextBlockParam[] {
  return splitSysPromptPrefix(systemPrompt).map(block => ({
    type: 'text',
    text: block.text,
    ...(enablePromptCaching && block.cacheScope !== null && {
      cache_control: getCacheControl({ scope: block.cacheScope })
    }),
  }))
}
```

之所以拆成多个 block，是为了 **Prompt Caching**——静态部分（身份描述、编码规范等）用 `cache_control` 标记，API 可以缓存这些前缀字节，后续请求不需要重新处理。动态部分（时间、CWD 等）放在后面的 block 里，不影响前面的缓存。

---

## 四、messages 字段——user 和 assistant 严格交替

### 4.1 API 层面只有两种角色

Anthropic Messages API **没有 system 角色的消息**（system 信息在单独的 `system` 字段中），messages 数组中只有：

- `role: "user"` — 用户消息
- `role: "assistant"` — 助手消息

且必须**严格交替**：user → assistant → user → assistant...（以 user 开头）。

### 4.2 Claude Code 内部的消息类型（远比 API 丰富）

Claude Code 内部定义了约 10 种消息类型，在发送给 API 前归一化为 user/assistant 两种：

| 内部类型 | 说明 | 归一化为 |
|----------|------|----------|
| `user` | 真正的用户输入 | user |
| `assistant` | 模型回复 | assistant |
| `system` (local_command) | 本地命令输出（如 `/compact`） | user |
| `attachment` | 附件消息（文件、hook 上下文、agent 列表等） | user |
| `progress` | 进度消息 | 丢弃（不发送） |
| `system` (其他子类型) | 内部系统消息（API 错误、压缩边界等） | 丢弃（不发送） |

```typescript
// messages.ts — 归一化过程
export function normalizeMessagesForAPI(
  messages: Message[],
  tools: Tools = [],
): (UserMessage | AssistantMessage)[] {
  // 过滤掉 progress 和大部分 system 消息
  // system(local_command) → 转为 user
  // attachment → 合并到相邻 user 消息
  // 连续 user 消息 → 合并为一条
  // ...
}
```

### 4.3 连续 user 消息的合并

API 要求 user/assistant 严格交替，但 Claude Code 内部可能产生连续的 user 消息（比如用户输入后紧跟一个附件消息）。归一化时会自动合并：

```
内部:
  user: "修复购物车"
  attachment: { type: "memory_context", content: "相关记忆..." }
  attachment: { type: "agent_listing", content: "可用 agent..." }

归一化后:
  user: [
    { type: "text", text: "修复购物车" },
    { type: "text", text: "<memory_context>相关记忆...</memory_context>" },
    { type: "text", text: "Available agents: ..." }
  ]
```

---

## 五、content 的内部结构——多种 block 类型

user 和 assistant 消息的 `content` 都不是纯文本，而是 **content block 数组**。

### 5.1 user 消息可包含的 block 类型

```json
{
  "role": "user",
  "content": [
    { "type": "text", "text": "用户的问题..." },
    { "type": "image", "source": { "type": "base64", "data": "..." } },
    { "type": "document", "source": { "type": "base64", "data": "..." } },
    {
      "type": "tool_result",
      "tool_use_id": "toolu_abc123",
      "content": [
        { "type": "text", "text": "工具执行结果..." }
      ]
    }
  ]
}
```

| Block 类型 | 说明 |
|-----------|------|
| `text` | 纯文本 |
| `image` | 图片（base64 或 URL） |
| `document` | 文档/PDF |
| `tool_result` | 工具执行结果（必须匹配前面 assistant 消息中的 `tool_use`） |

### 5.2 assistant 消息可包含的 block 类型

```json
{
  "role": "assistant",
  "content": [
    { "type": "thinking", "thinking": "让我分析一下这个问题..." },
    { "type": "text", "text": "让我搜索相关代码。" },
    {
      "type": "tool_use",
      "id": "toolu_abc123",
      "name": "Read",
      "input": { "file_path": "src/cart/service.ts" }
    },
    {
      "type": "tool_use",
      "id": "toolu_def456",
      "name": "Grep",
      "input": { "pattern": "getCart", "path": "src/" }
    }
  ]
}
```

| Block 类型 | 说明 |
|-----------|------|
| `text` | 模型生成的文本 |
| `thinking` | 扩展思维（Extended Thinking，用户不可见但保留在上下文中） |
| `tool_use` | 工具调用请求（包含 id、工具名、输入参数） |

**关键**：一条 assistant 消息中可以包含**多个 tool_use block**——这就是 Claude Code 并行工具调用的原理。

---

## 六、工具调用的完整生命周期

### 6.1 一次完整的工具调用对话

```
 ┌─ 发给API ─────────────────────────────────────┐
 │                                                │
 │  messages: [                                   │
 │    { role: "user",      text: "读取 app.ts" }  │
 │  ]                                             │
 │                                                │
 └────────────────────────────────────────────────┘
                      ↓
 ┌─ API 返回 ────────────────────────────────────┐
 │                                                │
 │  { role: "assistant", content: [               │
 │    { type: "text", text: "让我读取这个文件。" },│
 │    { type: "tool_use",                         │
 │      id: "toolu_abc",                          │
 │      name: "Read",                             │
 │      input: { file_path: "src/app.ts" }        │
 │    }                                           │
 │  ]}                                            │
 │                                                │
 └────────────────────────────────────────────────┘
                      ↓
          Claude Code 本地执行工具
                      ↓
 ┌─ 发给API（下一轮）────────────────────────────┐
 │                                                │
 │  messages: [                                   │
 │    { role: "user",      text: "读取 app.ts" }, │
 │    { role: "assistant", content: [tool_use] },  │
 │    { role: "user",      content: [             │
 │      { type: "tool_result",                    │
 │        tool_use_id: "toolu_abc",               │
 │        content: [{ type: "text",               │
 │          text: "export function main()..."     │
 │        }]                                      │
 │      }                                         │
 │    ]}                                          │
 │  ]                                             │
 │                                                │
 └────────────────────────────────────────────────┘
                      ↓
 ┌─ API 返回 ────────────────────────────────────┐
 │                                                │
 │  { role: "assistant", content: [               │
 │    { type: "text", text: "文件内容如下..." }   │
 │  ]}                                            │
 │  stop_reason: "end_turn"                       │
 │                                                │
 └────────────────────────────────────────────────┘
```

### 6.2 tool_use 和 tool_result 的配对规则

这是 API 的强制要求：

- 每个 `tool_use` block（在 assistant 消息中）**必须**有一个对应的 `tool_result` block（在随后的 user 消息中）
- 通过 `tool_use.id` 和 `tool_result.tool_use_id` 配对
- Claude Code 用 `ensureToolResultPairing()` 在发送前修复可能的配对不一致

### 6.3 并行工具调用

当 assistant 消息包含多个 tool_use block 时，Claude Code 可以并行执行：

```json
// assistant 消息
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "让我同时搜索两个文件。" },
    { "type": "tool_use", "id": "toolu_1", "name": "Read",
      "input": { "file_path": "src/auth.ts" } },
    { "type": "tool_use", "id": "toolu_2", "name": "Read",
      "input": { "file_path": "src/cart.ts" } }
  ]
}

// 随后的 user 消息（包含两个 tool_result）
{
  "role": "user",
  "content": [
    { "type": "tool_result", "tool_use_id": "toolu_1",
      "content": [{ "type": "text", "text": "auth.ts 内容..." }] },
    { "type": "tool_result", "tool_use_id": "toolu_2",
      "content": [{ "type": "text", "text": "cart.ts 内容..." }] }
  ]
}
```

---

## 七、tools 字段——工具定义的 JSON Schema

每个工具通过 `toolToAPISchema()` 转换为 API 格式：

```json
{
  "name": "Edit",
  "description": "Performs exact string replacements in files...",
  "input_schema": {
    "type": "object",
    "properties": {
      "file_path": {
        "type": "string",
        "description": "The absolute path to the file to modify"
      },
      "old_string": {
        "type": "string",
        "description": "The text to replace"
      },
      "new_string": {
        "type": "string",
        "description": "The text to replace it with"
      }
    },
    "required": ["file_path", "old_string", "new_string"]
  }
}
```

工具的 `description` 来自各工具的 `prompt()` 方法——这就是为什么 Agent 工具描述里包含了详细的使用指南和示例（参见 subagent 文档中的 `prompt.ts` 分析）。

工具的 `input_schema` 由 Zod Schema 通过 `zodToJsonSchema()` 自动转换。

---

## 八、Streaming——API 返回的流式格式

Claude Code 使用 Streaming 模式接收 API 响应，格式是 SSE（Server-Sent Events）：

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_xxx","type":"message","role":"assistant",...}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"让我分析..."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"让我"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"搜索代码。"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: content_block_start
data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_abc","name":"Grep","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\"pattern\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"\"cart\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":2}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":87}}

event: message_stop
data: {"type":"message_stop"}
```

Claude Code 通过 `StreamingToolExecutor` 实现**流式工具执行**——当 tool_use 的 `input_json_delta` 流到足够时就开始执行工具，不必等整个消息结束。

---

## 九、stop_reason——决定 Agent Loop 是否继续

API 响应中的 `stop_reason` 决定了 Agent Loop 的下一步：

| stop_reason | 含义 | Agent Loop 行为 |
|-------------|------|----------------|
| `"end_turn"` | 模型认为回答完成 | 停止循环，返回结果给用户 |
| `"tool_use"` | 模型想调用工具 | 执行工具，把结果作为 tool_result 追加到 messages，再次调用 API |
| `"max_tokens"` | 输出达到上限 | 尝试继续（拼接后再调用） |

```
Agent Loop:
  while (true) {
    response = callAPI(messages)
    
    if (response.stop_reason === "end_turn") {
      return response  // 完成
    }
    
    if (response.stop_reason === "tool_use") {
      results = executeTools(response.tool_uses)
      messages.push(assistantMessage)
      messages.push(userMessage_with_tool_results)
      continue  // 再次调用 API
    }
  }
```

---

## 十、完整的一轮交互示例

以"用户让 Claude Code 读取并修改一个文件"为例，展示完整的 JSON 交互流：

**第 1 次 API 调用：**

```json
{
  "model": "claude-sonnet-4-20250514",
  "system": [
    { "type": "text", "text": "You are Claude Code...<5000字系统提示>..." }
  ],
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "把 src/app.ts 里的 console.log 都换成 logger.info" }
      ]
    }
  ],
  "tools": [
    { "name": "Read", "description": "...", "input_schema": {...} },
    { "name": "Edit", "description": "...", "input_schema": {...} },
    { "name": "Grep", "description": "...", "input_schema": {...} }
  ],
  "max_tokens": 16384
}
```

**API 返回（stop_reason: tool_use）：**

```json
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "让我先读取文件内容。" },
    { "type": "tool_use", "id": "toolu_read1", "name": "Read",
      "input": { "file_path": "src/app.ts" } }
  ],
  "stop_reason": "tool_use"
}
```

**Agent 本地执行 Read 工具，然后第 2 次 API 调用：**

```json
{
  "messages": [
    { "role": "user", "content": "把 src/app.ts 里的 console.log..." },
    {
      "role": "assistant",
      "content": [
        { "type": "text", "text": "让我先读取文件内容。" },
        { "type": "tool_use", "id": "toolu_read1", "name": "Read",
          "input": { "file_path": "src/app.ts" } }
      ]
    },
    {
      "role": "user",
      "content": [
        { "type": "tool_result", "tool_use_id": "toolu_read1",
          "content": [{ "type": "text",
            "text": "1|import express from 'express'\n2|const app = express()\n3|console.log('Starting...')\n4|app.get('/', () => console.log('hit'))" }]
        }
      ]
    }
  ]
}
```

**API 返回（再次 tool_use）：**

```json
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "找到了两处 console.log，我来替换。" },
    { "type": "tool_use", "id": "toolu_edit1", "name": "Edit",
      "input": {
        "file_path": "src/app.ts",
        "old_string": "console.log('Starting...')",
        "new_string": "logger.info('Starting...')"
      }
    },
    { "type": "tool_use", "id": "toolu_edit2", "name": "Edit",
      "input": {
        "file_path": "src/app.ts",
        "old_string": "console.log('hit')",
        "new_string": "logger.info('hit')"
      }
    }
  ],
  "stop_reason": "tool_use"
}
```

**Agent 本地并行执行两个 Edit，然后第 3 次 API 调用：**

```json
{
  "messages": [
    "...(前面的历史)...",
    {
      "role": "assistant",
      "content": [
        { "type": "text", "text": "找到了两处 console.log，我来替换。" },
        { "type": "tool_use", "id": "toolu_edit1", "name": "Edit", "input": {...} },
        { "type": "tool_use", "id": "toolu_edit2", "name": "Edit", "input": {...} }
      ]
    },
    {
      "role": "user",
      "content": [
        { "type": "tool_result", "tool_use_id": "toolu_edit1",
          "content": [{ "type": "text", "text": "Successfully edited src/app.ts" }] },
        { "type": "tool_result", "tool_use_id": "toolu_edit2",
          "content": [{ "type": "text", "text": "Successfully edited src/app.ts" }] }
      ]
    }
  ]
}
```

**API 返回（end_turn）：**

```json
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "已完成替换，将 src/app.ts 中的 2 处 console.log 替换为 logger.info。" }
  ],
  "stop_reason": "end_turn"
}
```

Agent Loop 结束，把最终文本展示给用户。

---

## 十一、关键设计总结

| 维度 | 说明 |
|------|------|
| **传输格式** | JSON（通过 Anthropic SDK 序列化） |
| **传输方式** | HTTPS POST + SSE Streaming 返回 |
| **角色结构** | `system`（独立字段）+ `user`/`assistant`（messages 数组交替） |
| **system 多 block** | 拆成静态/动态部分，静态部分标记 `cache_control` 实现 Prompt Caching |
| **content 多 block** | text / image / document / tool_use / tool_result / thinking |
| **工具定义** | JSON Schema 格式，通过 `tools` 字段传递 |
| **工具调用** | assistant 用 `tool_use` 请求 → Claude Code 本地执行 → user 用 `tool_result` 返回 |
| **循环控制** | `stop_reason` 驱动：`tool_use` 继续循环，`end_turn` 停止 |
| **内部消息归一化** | 约 10 种内部类型 → 发送前过滤/合并为 user/assistant |
| **并行工具** | 单条 assistant 消息中的多个 `tool_use` 可并行执行 |
| **流式执行** | `StreamingToolExecutor` 在 tool_use 参数流式到达时就开始执行 |
