# Claude Code Prompt Cache 命中的内部设计

> 基于 Claude Code 源码与 Anthropic Messages API 行为的分析。本文重点解释：Claude Code 的 prompt cache 到底缓存了什么、它和 KV cache 是什么关系、为什么它能降低计算成本，以及 Claude Code 为了提高命中率做了哪些工程设计。

## 1. 先看现象：Cursor 里的 Cache Read 是什么

Cursor 用量界面里常见的几项 token 统计，可以直接对应到 prompt cache 的工作方式：

| 统计项 | 含义 |
|------|------|
| `Input` | 本次请求中需要重新处理的输入 token，包括新用户输入、新工具结果、新代码上下文，以及未命中缓存的历史片段 |
| `Cache Read` | 本次请求中命中服务端 prompt cache、被复用的历史上下文前缀 token |
| `Cache Write` | 本次请求中新写入服务端 prompt cache 的前缀 token |
| `Output` | 模型本次生成的输出 token |

所以 `Cache Read` 不是 Cursor 本地“读取聊天记录”，也不是免费 token。它表示：

> 这部分上下文 token 仍然在本次请求语义里存在，但服务端已经缓存过它们的 prefix/KV 状态，本次可以读取复用，不需要像普通 input 那样完整重新 prefill 计算。

这和 Claude Code 文档里的字段基本对应：

```text
Cursor Cache Read
  ≈ Anthropic usage.cache_read_input_tokens

Cursor Cache Write
  ≈ Anthropic usage.cache_creation_input_tokens

Cursor Input
  ≈ 本次需要正常处理的 input tokens
```

这也是为什么有些长会话里 `Cache Read` 会比 `Input` 大得多：Agent 每一轮都会重复携带大量 system prompt、工具定义、历史消息和代码上下文，如果这些前缀稳定，就可以被服务端复用。

---

## 2. Agent 层面的 Prompt Cache 原理

Claude Code 的 prompt cache 不是本地缓存，也不是“缓存模型回答”。它是 Claude Code 在 API 请求中通过 `cache_control` 标记稳定前缀，让 Anthropic 服务端缓存并复用这段前缀的模型计算状态。

Agent 场景里，模型不是只调用一次。一次任务可能经过多轮：

```text
用户请求
  → 第 1 轮 LLM：决定调用 Read
  → 工具返回文件内容
  → 第 2 轮 LLM：基于文件内容决定调用 Grep/Edit
  → 工具返回结果
  → 第 3 轮 LLM：总结或继续行动
```

每一轮 API 请求都会重新携带大量上下文：

```text
system prompt
tools schema
历史 user / assistant / tool_use / tool_result
本轮新增输入
```

如果没有 prompt cache，服务端每一轮都要重新计算前面那些重复上下文。Prompt cache 的作用就是把稳定前缀变成可复用的计算状态：

```text
Claude Code 负责：
  1. 构造稳定的 prompt 前缀
  2. 在 system / tools / messages 的合适位置插入 cache_control
  3. 保证后续请求的前缀字节完全一致
  4. 根据 API 返回的 cache_read / cache_creation 诊断命中情况

Anthropic 服务端负责：
  1. 对 cache_control 前的前缀做缓存
  2. 在后续请求中识别相同前缀
  3. 复用已经计算好的前缀状态
  4. 只对新增 suffix 继续做 prefill / decode
```

和 KV cache 的关系可以一句话概括：

> Prompt cache 是 API 产品层的抽象；它真正节省算力，大概率就是在服务端复用了 prefix prefill 阶段生成的 KV / attention 状态。Claude Code 不直接操作 KV cache，但它通过 `cache_control` 间接控制服务端能否复用这些 KV 状态。

所以你的理解基本是对的：**最后要节省算力，确实要在 KV cache 或等价的模型中间状态层做事**。区别在于，Claude Code 不是自己实现 KV cache，而是把稳定前缀标出来，交给 Anthropic 服务端实现。

### 2.1 判断前提：不稳定通常是谁造成的

Prompt cache 命中依赖“稳定前缀”，但前缀不稳定不一定是用户的问题。更准确地说：

```text
用户负责提出任务；
Agent 负责把任务、工具、历史上下文稳定地序列化成 API 请求。
```

正常情况下，用户主动做这些事会自然导致 cache miss：

- 切换模型、effort、输出风格或系统设置
- 切换项目目录、启用/关闭 MCP、插件或工具
- 触发 compact、清理历史，或长时间间隔导致服务端 TTL 过期
- 新增大量代码上下文、工具结果或附件

但在连续会话里，如果用户没有显式改变环境，前缀仍频繁变化，通常就是 Agent 的上下文构造设计不够稳定：

| 不稳定来源 | 更像谁的问题 | 典型原因 |
|-----------|-------------|---------|
| system prompt 前部日期、时间、相对时间变化 | Agent 设计问题 | 动态信息被放进了应当稳定的前缀 |
| tools schema 顺序变化 | Agent 设计问题 | 工具来自 Map、插件扫描、MCP 重连，缺少固定排序 |
| tools description 细微变化 | Agent 设计问题 | GrowthBook、feature flag、动态 prompt 渲染让 schema 字节变化 |
| `src/app.ts` 变成 `./src/app.ts` | Agent 设计或规范化问题 | 语义等价，但路径字符串字节不同 |
| tool_result 一轮完整、一轮摘要或引用 | Agent 设计问题 | 替换策略没有冻结，历史消息字节变化 |

所以本文后面说的“不稳定版本”，主要不是指用户“问错了”，而是指 Agent 没有把 prompt cache 命中当成一条工程约束来维护。

---

## 3. 为什么重要：一个三轮命中例子

假设第 1 轮请求：

```text
system_static: 8K
tools:         3K
user:          100
```

服务端处理：

```text
prefill 11.1K tokens
在 cache_control boundary 写入缓存
返回 usage:
  cache_creation_input_tokens: 11000
  cache_read_input_tokens: 0
```

第 2 轮请求：

```text
system_static: 8K     ← 完全相同，可复用
tools:         3K     ← 完全相同，可复用
user:          100    ← 完全相同，可复用
assistant:     50     ← 新增
tool_result:   800    ← 新增
```

服务端处理：

```text
读取前 11.1K tokens 的 cached prefix state
只 prefill 新增 assistant/tool_result suffix
返回 usage:
  cache_creation_input_tokens: 850
  cache_read_input_tokens: 11000
```

第 3 轮继续：

```text
继续复用更长的历史前缀
新增内容越靠后，越能利用前面已经缓存的计算状态
```

如果把它映射到 Cursor 的用量界面，就是：

```text
Cache Read 增加：
  说明大量历史上下文命中了服务端缓存

Input 相对较小：
  说明本轮主要只需要处理新增输入或未命中片段

Cache Write 增加：
  说明本轮又产生了新的可缓存前缀
```

这就是为什么 Claude Code / Cursor 这类 Agent 特别依赖 prompt cache。Agent 每次工具调用后都会再次调用模型；没有缓存，每一轮都要重复计算庞大的 system prompt、工具 schema 和历史上下文。

---

## 4. Prompt Cache 不是三类东西

为了避免混淆，先排除几个常见误解。

| 误解 | 实际情况 |
|------|---------|
| Prompt cache 是本地缓存 | 不是。Claude Code 本地只做前缀稳定性管理，真正的 prompt cache 在 Anthropic 服务端 |
| Prompt cache 缓存最终回答 | 不是。它缓存的是输入前缀的计算状态，不是 assistant 的最终文本 |
| Prompt cache 是语义缓存 | 不是。它要求前缀字节级一致，不是“意思差不多就命中” |
| Prompt cache 等于普通 KV cache | 不完全等于。KV cache 是底层推理机制，prompt cache 是跨请求复用前缀状态的 API 能力 |

可以把它理解成：

```text
普通 KV cache:
  单次请求内部使用
  用于 decode 阶段避免重复计算历史 token 的 K/V

Prompt cache:
  跨 API 请求使用
  用于复用上一轮请求中稳定 prompt 前缀的 prefill 结果
```

---

## 5. Detail：API 请求层如何开启 Prompt Cache

### 5.1 system prompt 被拆成多个 text block

Claude Code 不把 system prompt 当成一个大字符串发送，而是拆成 `TextBlockParam[]`。不同 block 可以独立挂 `cache_control`：

```typescript
// src/services/api/claude.ts
export function buildSystemPromptBlocks(
  systemPrompt: SystemPrompt,
  enablePromptCaching: boolean,
): TextBlockParam[] {
  return splitSysPromptPrefix(systemPrompt).map(block => ({
    type: 'text',
    text: block.text,
    ...(enablePromptCaching && block.cacheScope !== null && {
      cache_control: getCacheControl({ scope: block.cacheScope }),
    }),
  }))
}
```

请求大致长这样：

```json
{
  "system": [
    {
      "type": "text",
      "text": "<静态部分：身份、行为规范、工具使用规则...>",
      "cache_control": { "type": "ephemeral", "scope": "global" }
    },
    {
      "type": "text",
      "text": "<动态部分：CWD、日期、记忆、MCP 指令、语言偏好...>"
    }
  ],
  "messages": [...],
  "tools": [...]
}
```

这里的关键点是：**静态部分尽量放前面并标记缓存，动态部分放后面**。这样时间、目录、记忆等动态信息变化时，不会破坏前面大段稳定前缀的缓存命中。

### 5.2 `getCacheControl()` 生成缓存标记

源码中通过 `getCacheControl()` 生成 Anthropic API 需要的缓存标记：

```typescript
// src/services/api/claude.ts
export function getCacheControl({
  scope,
  querySource,
}: {
  scope?: CacheScope
  querySource?: QuerySource
} = {}): {
  type: 'ephemeral'
  ttl?: '1h'
  scope?: CacheScope
} {
  return {
    type: 'ephemeral',
    ...(should1hCacheTTL(querySource) && { ttl: '1h' }),
    ...(scope === 'global' && { scope }),
  }
}
```

几个要点：

| 字段 | 含义 |
|------|------|
| `type: "ephemeral"` | 临时缓存，默认 TTL 通常是 5 分钟 |
| `ttl: "1h"` | 符合条件时升级为 1 小时缓存 |
| `scope: "global"` | 允许稳定的全局前缀跨更大范围复用 |

### 5.3 cache breakpoint 的本质

`cache_control` 可以理解为一个 **cache breakpoint**：

```text
system + tools + messages prefix
              ↑
        cache_control 标记

服务端尝试缓存 / 复用这个标记之前的稳定前缀
```

这不是告诉服务端“缓存某个字段”，而是告诉服务端：

> 到这个位置为止的 prompt 前缀值得缓存。后续如果有请求带着完全相同的前缀，可以从这个位置恢复计算状态。

---

## 6. Detail：命中条件为什么强调“字节级稳定”

Prompt cache 的命中不是语义匹配，而是前缀匹配。只要前缀中任意一处字节变化，都可能导致 cache miss。

典型请求结构如下：

```text
Request N:
  system_static      ← 稳定，适合缓存
  system_dynamic     ← 可能变化
  tools              ← 工具 schema，必须稳定
  messages[0..k]     ← 历史消息前缀，必须稳定
  new user message   ← 新增 suffix

Request N+1:
  system_static      ← 必须和上轮完全一样
  system_dynamic     ← 如果在缓存边界前变化，会破坏缓存
  tools              ← 名称、顺序、schema、description 都不能变
  messages[0..k]     ← 历史消息前缀必须完全一样
  new tool_result    ← 新增 suffix
```

所以 Claude Code 的缓存优化重点不是“少发 prompt”，而是：

```text
让每一轮 API 请求的长前缀尽量保持完全一致
```

### 6.1 “稳定前缀”到底指什么

稳定前缀指的是：**从 API 请求开头开始，到某个 cache breakpoint 之前，连续的一段字节完全不变的输入内容**。

它不是一个语义概念，而是一个序列概念。只要中间插入、删除、换序或改动了任意字符，后面的内容即使完全相同，也不再是同一个前缀。

可以把一次 Claude Code 请求简化成：

```text
Request body:
  system[0] 静态系统提示
  system[1] 动态系统提示
  tools[0] Read schema
  tools[1] Edit schema
  tools[2] Grep schema
  messages[0] user 初始请求
  messages[1] assistant tool_use
  messages[2] user tool_result
  messages[3] user 本轮新增输入
```

所谓“稳定前缀”就是其中开头连续不变的部分：

```text
稳定前缀:
  system[0]
  tools[0..2]
  messages[0..2]

新增 suffix:
  messages[3]
```

如果下一轮请求的 `system[0]`、`tools[0..2]`、`messages[0..2]` 的序列和字节完全一样，就可以命中这段前缀的 prompt cache。

### 6.2 Prompt 前缀例子：稳定 vs 不稳定

假设 Claude Code 的 system prompt 被拆成静态和动态两段。

**稳定写法：动态信息放在缓存边界之后**

```json
{
  "system": [
    {
      "type": "text",
      "text": "You are Claude Code.\nFollow security rules.\nUse tools carefully.",
      "cache_control": { "type": "ephemeral", "scope": "global" }
    },
    {
      "type": "text",
      "text": "Current working directory: /repo/app\nToday's date: 2026-04-25"
    }
  ]
}
```

第 2 轮日期变化时：

```json
{
  "system": [
    {
      "type": "text",
      "text": "You are Claude Code.\nFollow security rules.\nUse tools carefully.",
      "cache_control": { "type": "ephemeral", "scope": "global" }
    },
    {
      "type": "text",
      "text": "Current working directory: /repo/app\nToday's date: 2026-04-26"
    }
  ]
}
```

这时第一个 system block 仍然完全相同，所以它前面的缓存仍可复用。变化只发生在后面的动态 block。

**不稳定写法：动态信息混入静态前缀**

```json
{
  "system": [
    {
      "type": "text",
      "text": "You are Claude Code.\nToday's date: 2026-04-25\nFollow security rules.\nUse tools carefully.",
      "cache_control": { "type": "ephemeral", "scope": "global" }
    }
  ]
}
```

第 2 轮变成：

```json
{
  "system": [
    {
      "type": "text",
      "text": "You are Claude Code.\nToday's date: 2026-04-26\nFollow security rules.\nUse tools carefully.",
      "cache_control": { "type": "ephemeral", "scope": "global" }
    }
  ]
}
```

虽然只有日期变了一个字符，但这段 block 的字节已经不同。服务端看到的不是“同一段 prompt 的新日期”，而是一个新的前缀，之前缓存的 prefix state 就不能直接复用。

差异可以简化为：

```diff
 You are Claude Code.
-Today's date: 2026-04-25
+Today's date: 2026-04-26
 Follow security rules.
 Use tools carefully.
```

这就是 Claude Code 要把静态提示和动态提示拆开的原因。

### 6.3 Tools schema 前缀例子：工具定义必须稳定

注意这里要区分两个概念：

```text
tools schema:
  API 请求里的工具定义列表，告诉模型有哪些工具、参数是什么。

tool_use:
  assistant 消息里模型实际发起的一次工具调用。
```

Prompt cache 对两者都敏感，但最容易造成大面积 miss 的通常是 `tools` schema，因为它位于请求前部且体积很大。

**稳定的 tools schema**

第 1 轮：

```json
{
  "tools": [
    {
      "name": "Read",
      "description": "Read a file from the local filesystem.",
      "input_schema": {
        "type": "object",
        "properties": {
          "file_path": { "type": "string" }
        },
        "required": ["file_path"]
      }
    },
    {
      "name": "Grep",
      "description": "Search file contents with ripgrep.",
      "input_schema": {
        "type": "object",
        "properties": {
          "pattern": { "type": "string" },
          "path": { "type": "string" }
        },
        "required": ["pattern"]
      },
      "cache_control": { "type": "ephemeral" }
    }
  ]
}
```

第 2 轮保持完全相同：

```json
{
  "tools": [
    { "name": "Read", "description": "Read a file from the local filesystem.", "input_schema": "..." },
    { "name": "Grep", "description": "Search file contents with ripgrep.", "input_schema": "...", "cache_control": { "type": "ephemeral" } }
  ]
}
```

这种情况下，`system + tools` 这段大前缀可以命中。

**不稳定的 tools schema：工具顺序变化**

第 2 轮如果变成：

```json
{
  "tools": [
    { "name": "Grep", "description": "Search file contents with ripgrep.", "input_schema": "..." },
    { "name": "Read", "description": "Read a file from the local filesystem.", "input_schema": "..." }
  ]
}
```

即使工具集合完全一样，只是 `Read` 和 `Grep` 顺序交换了，字节序列也变了：

```text
第 1 轮 tools 前缀:
  Read schema -> Grep schema

第 2 轮 tools 前缀:
  Grep schema -> Read schema
```

这会让 `tools` 以及其后的 messages 前缀都无法复用。

**不稳定的 tools schema：描述文本变化**

第 1 轮：

```json
{
  "name": "Read",
  "description": "Read a file from the local filesystem."
}
```

第 2 轮：

```json
{
  "name": "Read",
  "description": "Read a file from the local filesystem. Use this before editing."
}
```

这类变化看起来只是提示词增强，但对 prompt cache 来说就是工具 schema 字节变化。Claude Code 的 `toolSchemaCache` 就是为了防止 GrowthBook、MCP 重连或动态 prompt 渲染让工具 schema 在会话中漂移。

### 6.4 tool_use 前缀例子：历史消息也必须稳定

`tool_use` 不在 `tools` 字段里，而在 assistant 历史消息里。它同样会成为后续请求的 prompt 前缀。

第 1 轮模型请求读文件：

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_read_001",
      "name": "Read",
      "input": {
        "file_path": "src/app.ts"
      }
    }
  ]
}
```

Claude Code 执行工具后，第 2 轮会把这条 assistant 消息原样带回 API：

```json
{
  "messages": [
    { "role": "user", "content": "帮我检查 src/app.ts" },
    {
      "role": "assistant",
      "content": [
        {
          "type": "tool_use",
          "id": "toolu_read_001",
          "name": "Read",
          "input": {
            "file_path": "src/app.ts"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_read_001",
          "content": "1|import express from 'express'\n2|..."
        }
      ]
    }
  ]
}
```

如果第 3 轮继续携带完全相同的历史：

```text
user 初始请求
assistant tool_use(id=toolu_read_001, input=src/app.ts)
user tool_result(tool_use_id=toolu_read_001, content=同样的文件结果)
```

这段历史就可以作为稳定 messages 前缀继续命中。

不稳定情况包括：

```text
tool_use id 被重新生成:
  toolu_read_001 -> toolu_read_999

tool input 的路径格式变化:
  src/app.ts -> ./src/app.ts

tool_result 内容被事后替换:
  原始文件内容 -> [content replaced, see reference #abc]

tool_result 的行尾变化:
  \n -> \r\n

历史消息顺序变化:
  assistant tool_use / user tool_result 的顺序被重排
```

这些变化语义上可能等价，但字节上不等价，因此会打破 messages 前缀缓存。

### 6.5 稳定与不稳定的完整对照

下面用一个三轮 Agent Loop 例子说明区别。

**稳定版本**

```text
第 1 轮请求:
  [system_static v1]
  [tools: Read, Grep, Edit v1]
  [user: "检查 src/app.ts"]

API 返回:
  [assistant tool_use: Read {"file_path":"src/app.ts"}]

第 2 轮请求:
  [system_static v1]                         ← 相同，命中
  [tools: Read, Grep, Edit v1]               ← 相同，命中
  [user: "检查 src/app.ts"]                  ← 相同，命中
  [assistant tool_use: Read src/app.ts]      ← 相同，成为新前缀
  [user tool_result: 文件内容 v1]             ← 新增 suffix

API 返回:
  [assistant tool_use: Grep {"pattern":"TODO","path":"src"}]

第 3 轮请求:
  [system_static v1]                         ← 相同，命中
  [tools: Read, Grep, Edit v1]               ← 相同，命中
  [user: "检查 src/app.ts"]                  ← 相同，命中
  [assistant tool_use: Read src/app.ts]      ← 相同，命中
  [user tool_result: 文件内容 v1]             ← 相同，命中
  [assistant tool_use: Grep TODO]            ← 新增 suffix
```

这种情况下，`cache_read_input_tokens` 会随着历史增长而变大，因为越来越多旧前缀被服务端复用。

**不稳定版本**

```text
第 1 轮请求:
  [system_static v1]
  [tools: Read, Grep, Edit v1]
  [user: "检查 src/app.ts"]

第 2 轮请求:
  [system_static v1 + 日期文案变化]           ← 前部变化，cache miss
  [tools: Grep, Read, Edit v1]               ← 工具顺序变化，cache miss
  [user: "检查 src/app.ts"]
  [assistant tool_use: Read ./src/app.ts]    ← 路径格式变化
  [user tool_result: 文件内容被重新格式化]     ← 历史字节变化
```

这里任何一个早期变化都会导致后续大段内容失去复用价值。结果通常表现为：

```text
cache_read_input_tokens 下降
cache_creation_input_tokens 上升
请求延迟和服务端计算成本增加
```

所以稳定前缀的核心不是“内容不要变”，而是更精确地说：

> 会被反复发送的长前缀，必须在字节序列、字段顺序、工具顺序、消息顺序、ID、路径格式、替换策略上都保持稳定。

---

## 7. Detail：与 KV Cache 的关系

### 7.1 LLM 推理里的两个阶段

LLM 推理通常可以粗略分成两个阶段：

```text
Prefill 阶段：
  输入完整 prompt tokens
  一次性计算这些 token 的 hidden states / attention K/V

Decode 阶段：
  逐 token 生成输出
  利用已经生成的 K/V cache，避免每生成一个 token 都重算全部历史
```

普通的 KV cache 主要解决的是 decode 阶段：

```text
没有 KV cache:
  生成第 t 个输出 token 时，要反复重算前面所有 token

有 KV cache:
  前面 token 的 K/V 已经保存，只需要算新 token 的 Q/K/V
```

### 7.2 Prompt Cache 解决的是跨请求 prefill 重算

Claude Code 的场景里，每一轮 Agent Loop 都会把大量历史上下文重新发给模型：

```text
第 1 轮：
  [system 8K] + [tools 3K] + [user 100]

第 2 轮：
  [system 8K] + [tools 3K] + [user 100] + [assistant tool_use] + [tool_result]

第 3 轮：
  [system 8K] + [tools 3K] + [user 100] + [assistant tool_use] + [tool_result] + ...
```

如果没有 prompt cache，服务端每一轮都要重新 prefill 前面的 `system + tools + history`。这部分可能有上万 token，而且每轮都重复。

有 prompt cache 后：

```text
第 1 轮：
  计算 prefix 11K tokens
  写入 prompt cache

第 2 轮：
  读取 prefix 11K tokens 的缓存状态
  只计算新增 suffix

第 3 轮：
  继续读取已缓存 prefix
  只计算更后面的新增内容
```

这里节省的不是 decode 阶段，而是 **重复 prompt 前缀的 prefill 计算**。

### 7.3 为什么说底层仍然绕不开 KV cache

Transformer 要想“接着某段 prompt 往后算”，必须知道这段 prompt 在每一层 attention 中形成的历史状态。最典型的形式就是每层、每个 attention head 的 K/V 张量。

所以如果服务端要复用 prompt prefix，大概率需要保存类似这样的状态：

```text
cached prefix state:
  layer_0: K_prefix, V_prefix
  layer_1: K_prefix, V_prefix
  ...
  layer_N: K_prefix, V_prefix
```

后续请求命中时，新 token 的 attention 可以直接 attend 到这些 cached K/V，而不用重新为 prefix tokens 跑完整 transformer forward。

因此可以这样理解：

```text
Prompt cache:
  对外暴露的 API 能力
  以 prompt 前缀和 cache_control 为单位

KV cache:
  底层推理加速机制
  以模型层、attention head、token position 的 K/V 状态为单位
```

两者不是同一层概念，但 prompt cache 的算力节省最终依赖 KV cache 或等价的 prefix state 复用。

---

## 8. Detail：Prompt Cache 如何降低计算

### 8.1 没有缓存时

假设一轮请求包含：

```text
system prompt: 8K tokens
tools schema:  3K tokens
history:       20K tokens
new input:     500 tokens
```

没有 prompt cache 时，服务端每轮都要对 `31.5K` 输入 token 做 prefill：

```text
prefill cost = compute(system + tools + history + new input)
```

长会话里，`system + tools + old history` 每轮都重复计算，浪费非常明显。

### 8.2 有缓存时

如果前面的 `system + tools + history_prefix` 已经命中：

```text
cache_read: 31K tokens
new input:  500 tokens
```

服务端可以：

```text
restore cached prefix state
compute(new suffix only)
decode(output)
```

也就是：

```text
prefill cost ≈ compute(new input + 必要的 attention 连接)
```

注意这里不是完全“零成本”。新 suffix 仍然需要 attend 到 cached prefix，所以还有一部分注意力计算。但最大头的节省来自：**不用重新为 prefix 的每个 token 计算每一层的 hidden state / K/V**。

### 8.3 Token 统计中的体现

Claude Code 能从 API usage 中看到这些字段：

```json
{
  "input_tokens": 8234,
  "cache_creation_input_tokens": 836,
  "cache_read_input_tokens": 175500,
  "output_tokens": 567
}
```

含义大致是：

| 字段 | 含义 |
|------|------|
| `input_tokens` | 本轮正常处理的输入 token |
| `cache_creation_input_tokens` | 本轮写入 prompt cache 的 token |
| `cache_read_input_tokens` | 本轮从 prompt cache 读取复用的 token |
| `output_tokens` | 模型生成的输出 token |

`cache_read_input_tokens` 越高，说明越多历史前缀被服务端复用，而不是重新计算。

---

## 9. Detail：Claude Code 为了提高命中率做了什么

Claude Code 的真正工作，不是实现 cache，而是保护 cache hit。

### 9.1 System Prompt 静态/动态分区

系统提示词被分成两大区域：

```text
静态区域：
  Intro
  System
  Doing Tasks
  Actions
  Using Your Tools
  Tone and Style
  Output Efficiency

动态区域：
  Session Guidance
  Memory
  Environment
  Language
  Output Style
  MCP Instructions
  Scratchpad
  Function Result Clearing
```

源码中通过 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 区分两者。边界前尽量稳定，并可用 `scope: "global"` 缓存；边界后包含 CWD、日期、MCP、记忆等会话特定信息。

### 9.2 Tool Schema 锁定

工具 schema 位于 prompt 前部，体积大，变化频繁时极容易打破缓存。

```typescript
// src/utils/toolSchemaCache.ts
const TOOL_SCHEMA_CACHE = new Map<string, CachedSchema>()
```

Claude Code 会在会话首次渲染工具 schema 后锁定结果。后续即使 GrowthBook 特性开关刷新、MCP 工具重连，也尽量不让 schema 字节发生变化。

### 9.3 工具排序稳定

工具列表不能随意变动顺序，因为顺序变化也会改变 prompt 字节。

```typescript
// src/utils/toolPool.ts
// 内置工具保持连续前缀，MCP 工具追加在后
const [mcp, builtIn] = partition(
  uniqBy([...initialTools, ...assembled], 'name'),
  /* ... */
)
```

这保证了内置工具形成稳定前缀，动态 MCP 工具尽量追加在后面，降低对大块缓存前缀的破坏。

### 9.4 Tool Result 替换决定冻结

工具结果太大时可能被替换为引用或摘要。但如果某个 tool_result 这一轮没替换、下一轮又替换了，历史消息字节就变了，缓存会失效。

所以 Claude Code 的策略是：

```text
一旦某个 tool_result 决定替换：
  后续一直使用同一个替换字符串

一旦某个 tool_result 决定不替换：
  后续不再事后替换
```

这不是为了语义，而是为了字节稳定。

### 9.5 时间和日期信息冻结

类似 `saved 3 days ago` 这样的相对时间，如果每轮重新计算，会变成：

```text
saved 3 days ago
saved 4 days ago
```

这类微小变化也会打破缓存。Claude Code 会冻结附件创建时的 header，避免时间文案漂移。

日期变化也不直接修改最早的 system/user 前缀，而是追加一个 `date_change` 附件到后面：

```text
旧前缀保持不动
新日期作为尾部附件追加
```

### 9.6 Fork 子代理共享缓存前缀

Fork 子代理需要和父代理共享 prompt cache，因此 Claude Code 保存一组 `CacheSafeParams`：

```typescript
// src/utils/forkedAgent.ts
export type CacheSafeParams = {
  systemPrompt: SystemPrompt
  userContext: { [k: string]: string }
  systemContext: { [k: string]: string }
  toolUseContext: ToolUseContext
  forkContextMessages: Message[]
}
```

共享缓存前缀要求：

```text
system prompt 完全相同
tools 列表完全相同
model 完全相同
messages 前缀完全相同
thinking config 完全相同
```

这也是为什么 fork 子代理会继承父级 `thinkingConfig`：不是因为子代理一定需要同样深度思考，而是为了避免请求参数变化导致 prompt cache miss。

### 9.7 SkillTool 的 prompt cache

Skill body 按需加载后，会以 user message 形式注入会话。Claude Code 会记录已经注入过的 skill，避免 compact 后重复注入造成 prompt cache miss。

```text
模型调用 SkillTool
  → runtime 读取 SKILL.md body
  → 以 user message 注入
  → 记录到 Invoked skills
  → 后续轮次命中 SkillTool prompt cache
```

---

## 10. Detail：为什么每次只放一个 message 级 cache_control

Claude Code 遵循一个重要原则：

```text
每次 API 请求只放置一个 message 级别的 cache_control 标记
```

源码注释提到 Mycro 的 turn-to-turn eviction 机制：

> page_manager 会释放任何不在 `cache_store_int_token_boundaries` 中的 local-attention KV pages。两个标记会导致倒数第二个位置被保护，即使没有请求会从该位置恢复。

这段注释非常关键，因为它直接说明 prompt cache 的服务端实现确实和 KV pages / cache boundary 有关。

换句话说，cache_control 不是纯粹的账单标签。它会影响服务端哪些 token boundary 的 KV pages 被保留、哪些可以被淘汰。

两个 cache_control 反而可能造成无用 KV pages 被保护，浪费服务端资源。因此 Claude Code 会精确控制 cache breakpoint 的数量和位置。

---

## 11. Detail：缓存失效检测怎么知道 miss 了

Claude Code 有一个两阶段缓存失效检测系统：

```text
Phase 1: recordPromptState()
  API 调用前记录 system / tools / model / betas 等 hash
  和上一轮请求比对，记录 pendingChanges

Phase 2: checkResponseForCacheBreak()
  API 返回后检查 cache_read_tokens 是否下降 > 5%
  结合 pendingChanges 推断根因
  触发 analytics 事件并写入 diff 文件
```

监控维度包括：

| 维度 | 检查内容 |
|------|---------|
| System Prompt | 去除 `cache_control` 后的内容 hash |
| Cache Control | 包含 TTL / scope 的完整 hash |
| Tool Schemas | 聚合 hash + 每个工具单独 hash |
| Model | 模型是否切换 |
| Betas | beta header 是否增减 |
| Effort | effort 值是否变化 |
| Extra Body | 额外请求体是否变化 |

典型诊断输出：

```text
[PROMPT CACHE BREAK] system prompt changed (+150 chars), tools changed (+1/-0 tools)
  [source=repl_main_thread, call #5, cache read: 45000 -> 2000,
   creation: 43000, diff: /tmp/claude-xxx/cache-break-a1b2.diff]
```

这说明 Claude Code 对 prompt cache 的态度不是“能命中就命中”，而是把它当成核心性能路径来监控。

---

## 12. Detail：和普通 KV Cache 的完整对比

| 维度 | 普通 KV Cache | Prompt Cache |
|------|---------------|--------------|
| 所在层级 | 模型推理内部机制 | API 产品能力 |
| 生命周期 | 单次请求内，通常随请求结束释放 | 跨请求，受 TTL 控制 |
| 复用对象 | 已处理 token 的 K/V 张量 | prompt 前缀对应的服务端计算状态 |
| 主要优化阶段 | Decode 阶段 | Prefill 阶段 |
| 命中条件 | 同一次生成过程中的历史 token | 跨请求的前缀字节完全一致 |
| 用户/客户端可见性 | 通常不可见 | 通过 `cache_control` 和 usage 字段可见 |
| Claude Code 是否直接实现 | 否 | 否，只负责标记和稳定前缀 |

更准确的关系是：

```text
Prompt Cache = 跨请求可持久化的 Prefix KV Cache / Prefix State Cache 的 API 化封装
```

它不是传统意义上的 decode KV cache，但底层节省计算的原理和 KV 复用高度相关。

---

## 13. 结论

Claude Code 的 prompt cache 设计可以分成两层理解：

第一层是客户端工程：

```text
拆分 system prompt
标记 cache_control
锁定 tool schema
稳定工具排序
冻结 tool_result 替换
冻结时间文案
让 fork 子代理共享 cache-safe 参数
监控 cache_read 下降
```

第二层是服务端推理：

```text
缓存 prompt prefix 的 prefill 状态
后续请求命中相同前缀时恢复这些状态
避免重复计算长前缀
只对新增 suffix 和输出 decode 继续计算
```

所以，prompt cache 和 KV cache 的关系不是“二选一”，而是上下两层：

```text
Claude Code 看到的是 prompt cache
Anthropic API 暴露的是 cache_control / cache_read
推理服务内部真正省算力的地方，是 prefix KV / attention state 的复用
```

如果只从客户端看，prompt cache 像是“缓存 prompt”；但从推理系统看，它本质上是“把跨请求的长前缀 prefill 结果持久化并复用”。这正是它能够降低计算成本的根本原因。

---

## 14. 关键源码文件索引

| 文件 | 作用 |
|------|------|
| `src/services/api/claude.ts` | API 请求构建、`getCacheControl()`、system block 的 `cache_control` 标记 |
| `src/services/api/promptCacheBreakDetection.ts` | prompt cache miss 检测、hash 比对、根因诊断 |
| `src/utils/toolSchemaCache.ts` | 工具 schema 会话级锁定，避免工具块变动导致 miss |
| `src/utils/forkedAgent.ts` | `CacheSafeParams`，保证 fork 子代理共享父级 prompt cache 前缀 |
| `src/utils/queryContext.ts` | 构建 API cache-key 相关前缀上下文 |
| `src/utils/toolResultStorage.ts` | 冻结 tool_result 替换策略，保证历史消息字节稳定 |
| `src/utils/attachments.ts` | 冻结附件时间 header，避免相对时间变化打破缓存 |
| `src/tools/BashTool/prompt.ts` | 临时目录路径规范化，提升 global prompt cache 命中率 |
