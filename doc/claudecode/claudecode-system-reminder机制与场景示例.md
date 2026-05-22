# Claude Code `<system-reminder>` 机制与场景示例

> 基于 Claude Code 源码分析，说明 `<system-reminder>` 是什么、它在 API 消息里长什么样、为什么需要它，以及一个交互模式下的完整触发场景。

## 1. 核心结论

`<system-reminder>` 是 Claude Code 在 **user message 通道里模拟动态 system message** 的机制。

Anthropic Messages API 的对话历史只有 `user / assistant` 两种角色，真正的 `system` 是请求顶层字段，不能在对话中途随意插入新的 system role。因此 Claude Code 把运行时上下文、IDE 状态、文件变更提醒、Plan/Auto/Todo 提醒、memory、skill discovery 等信息包装成：

```text
<system-reminder>
这里是 Claude Code 自动注入给模型看的系统上下文
</system-reminder>
```

这些内容在 API 层仍然是 `role: "user"`，但通常带有 `isMeta: true`，表示它不是用户真实输入，而是系统注入的 meta attachment。

一句话概括：

```text
system prompt:
  长期、稳定、基础行为规则

<system-reminder>:
  短期、动态、运行时上下文提醒
```

---

## 2. 源码入口

### 2.1 包装函数

`src/utils/messages.ts` 中的 `wrapInSystemReminder()` 负责把任意文本包成 `<system-reminder>`：

```typescript
export function wrapInSystemReminder(content: string): string {
  return `<system-reminder>\n${content}\n</system-reminder>`
}
```

如果 attachment 转成的 user message 还没有 wrapper，`ensureSystemReminderWrap()` 会补上：

```typescript
function ensureSystemReminderWrap(msg: UserMessage): UserMessage {
  const content = msg.message.content
  if (typeof content === 'string') {
    if (content.startsWith('<system-reminder>')) return msg
    return {
      ...msg,
      message: { ...msg.message, content: wrapInSystemReminder(content) },
    }
  }
  // array content 中的 text block 也会被逐个 wrap
}
```

这说明 `<system-reminder>` 不是模型自己生成的格式，而是 Claude Code 在消息归一化阶段主动加的标签。

### 2.2 attachment 到 API 消息

运行时 attachment 会经过 `normalizeAttachmentForAPI()` 转成 user message。很多分支都会调用 `wrapMessagesInSystemReminder()`：

```typescript
case 'opened_file_in_ide': {
  return wrapMessagesInSystemReminder([
    createUserMessage({
      content: `The user opened the file ${attachment.filename} in the IDE. This may or may not be related to the current task.`,
      isMeta: true,
    }),
  ])
}
```

最终送到 API 的形态大致是：

```json
{
  "role": "user",
  "content": [
    {
      "type": "text",
      "text": "<system-reminder>\nThe user opened the file src/app.ts in the IDE. This may or may not be related to the current task.\n</system-reminder>"
    }
  ]
}
```

注意：API 只看到 `role: "user"` 和文本内容；`isMeta: true` 是 Claude Code 内部消息属性，用于 UI、transcript、归一化逻辑区分“真实用户输入”和“系统注入上下文”。

---

## 3. 它为什么存在

### 3.1 API 角色限制

Anthropic Messages API 的结构大致是：

```json
{
  "system": [
    { "type": "text", "text": "长期系统提示词..." }
  ],
  "messages": [
    { "role": "user", "content": "用户请求" },
    { "role": "assistant", "content": "助手回复" }
  ]
}
```

中途动态上下文不能以 `role: "system"` 插入到 `messages` 数组里。Claude Code 如果想告诉模型：

- 用户刚打开了某个文件
- 用户刚选中了某几行代码
- 某个文件被用户或 linter 改过
- 当前仍处于 Plan Mode / Auto Mode
- TodoWrite 很久没用了
- 有新的 LSP diagnostics
- 有相关 memory 被检索出来

就需要通过 user message 通道传递。

`<system-reminder>` 就是这个通道的“语义标记”：告诉模型“这不是用户真实请求，而是系统提醒”。

### 3.2 动态上下文不适合放 system prompt

这些信息通常是短期、动态、会随回合变化的：

```text
用户打开文件: src/app.ts
用户选中行: 10-30
TodoWrite 10 轮没用
Plan Mode 刚退出
新的诊断错误出现
```

如果全部放进顶层 system prompt，会带来两个问题：

1. system prompt 变得频繁变化，破坏 prompt cache 稳定前缀。
2. 一些上下文只对当前或下一轮有用，不应该长期污染基础行为规则。

因此 Claude Code 采用：

```text
稳定规则 → system prompt
动态提醒 → <system-reminder> attachment
```

---

## 4. 一个具体例子

假设用户在 IDE 中打开了 `src/cart/service.ts`，并选中了第 42 到 58 行，然后对 Claude Code 说：

```text
帮我看看这里的折扣计算有没有问题
```

Claude Code 可能会在用户消息旁边自动注入两个 meta attachment。

### 4.1 用户真实输入

```json
{
  "role": "user",
  "content": [
    {
      "type": "text",
      "text": "帮我看看这里的折扣计算有没有问题"
    }
  ]
}
```

### 4.2 打开文件提醒

```json
{
  "role": "user",
  "content": [
    {
      "type": "text",
      "text": "<system-reminder>\nThe user opened the file src/cart/service.ts in the IDE. This may or may not be related to the current task.\n</system-reminder>"
    }
  ]
}
```

### 4.3 选中代码提醒

```json
{
  "role": "user",
  "content": [
    {
      "type": "text",
      "text": "<system-reminder>\nThe user selected the lines 42 to 58 from src/cart/service.ts:\n\nfunction calculateDiscount(cart) {\n  const total = cart.items.reduce((sum, item) => sum + item.price, 0)\n  if (total > 100) return total * 0.1\n  return 0\n}\n\nThis may or may not be related to the current task.\n</system-reminder>"
    }
  ]
}
```

模型最终看到的上下文大致是：

```text
Human:
帮我看看这里的折扣计算有没有问题

Human:
<system-reminder>
The user opened the file src/cart/service.ts in the IDE. This may or may not be related to the current task.
</system-reminder>

Human:
<system-reminder>
The user selected the lines 42 to 58 from src/cart/service.ts:

function calculateDiscount(cart) {
  ...
}

This may or may not be related to the current task.
</system-reminder>
```

这会让模型更容易理解用户说的“这里”指的是 IDE 当前选中的代码，而不是让模型盲猜上下文。

---

## 5. 完整交互场景

下面用一个更接近真实 Claude Code 的场景说明 `<system-reminder>` 的作用。

### 5.1 场景：用户让 Claude 修复一个函数

用户操作：

1. 在 IDE 打开 `src/cart/service.ts`
2. 选中 `calculateDiscount()` 函数
3. 输入：“这里满减逻辑好像不对，帮我修一下”

Claude Code 进入第 1 轮模型调用前，会构造：

```text
真实用户输入:
  这里满减逻辑好像不对，帮我修一下

system reminder 1:
  用户打开了 src/cart/service.ts

system reminder 2:
  用户选中了第 42-58 行代码
```

模型据此判断：

```text
用户说的“这里”
  ≈ IDE 当前选中的 calculateDiscount()
```

于是它可能调用 `Read` 工具读取完整文件，再调用 `Edit` 修改函数。

### 5.2 中途文件被外部修改

假设模型修改前，用户或 linter 已经改动了 `src/cart/service.ts`。Claude Code 可能注入一个 `edited_text_file` attachment：

```text
<system-reminder>
Note: src/cart/service.ts was modified, either by the user or by a linter. This change was intentional, so make sure to take it into account as you proceed (ie. don't revert it unless the user asks you to). Don't tell the user this, since they are already aware. Here are the relevant changes (shown with line numbers):
...
</system-reminder>
```

这个提醒的目标不是告诉用户，而是告诉模型：

```text
不要把外部修改当成异常
不要无意中回滚用户或 linter 的改动
基于最新文件状态继续工作
```

### 5.3 Todo reminder 触发

如果任务变复杂，模型连续很多轮没有使用 TodoWrite，而 TodoWrite 工具可用，Claude Code 可能注入：

```text
<system-reminder>
The TodoWrite tool hasn't been used recently. If you're working on tasks that would benefit from tracking progress, consider using the TodoWrite tool to track progress. Also consider cleaning up the todo list if has become stale and no longer matches what you are working on. Only use it if it's relevant to the current work. This is just a gentle reminder - ignore if not applicable. Make sure that you NEVER mention this reminder to the user
</system-reminder>
```

它的作用是轻推模型：

```text
如果任务已经变成多步骤工作
  可以考虑用 TodoWrite 管理进度
否则忽略
```

注意这里写了 `NEVER mention this reminder to the user`，说明 system reminder 是“给模型看的内部提示”，不是用户对话内容。

---

## 6. 与普通用户输入的区别

| 维度 | 普通用户输入 | `<system-reminder>` |
|------|-------------|---------------------|
| 来源 | 用户手动输入 | Claude Code 自动注入 |
| API 角色 | `user` | `user` |
| 内部标记 | 通常不是 `isMeta` | 通常是 `isMeta: true` |
| 内容包装 | 原始文本 | `<system-reminder>...</system-reminder>` |
| 用途 | 表达用户意图 | 提供运行时上下文和提醒 |
| 是否应回复给用户 | 通常需要回应 | 通常不应直接提及 |
| 强制力 | 用户指令，模型应遵守 | 提示词软约束，不能代替代码权限控制 |

一个容易误解的点是：用户也可以手打 `<system-reminder>...</system-reminder>`，模型当然能看到这段文本，但这只是普通用户输入伪装成标签。它不会变成 Claude Code 内部的 meta attachment，也不会获得内部隐藏、重排、归一化、attachment 生命周期等语义。

---

## 7. 与 prompt cache 的关系

`<system-reminder>` 是动态上下文，通常位于对话尾部或工具结果附近。它有两个缓存相关影响：

1. **避免污染稳定 system prompt**  
   动态信息不塞进顶层 system prompt，减少破坏稳定前缀的风险。

2. **自身仍会进入 messages 历史**  
   一旦作为 user message 进入历史，它也会成为后续 prompt 的一部分。如果内容每轮变化，就会影响后续消息前缀稳定性。

Claude Code 对一些动态内容做了稳定性处理。例如 relevant memory 的 header 在 attachment 创建时就预计算，避免每轮重新计算 “saved 3 days ago” 导致字节变化：

```typescript
// rendered bytes are stable across turns (prompt-cache hit)
const header = m.header ?? memoryHeader(m.path, m.mtimeMs)
```

这说明 `<system-reminder>` 虽然是动态注入机制，但仍需要考虑 prompt cache 命中。

---

## 8. 设计边界

`<system-reminder>` 是软控制，不是安全边界。

例如 `critical_system_reminder` 可以每轮提醒 Verification Agent：

```text
不要修改项目文件
必须以 VERDICT 结尾
```

但真正的安全保障仍应来自：

```text
工具池过滤
权限系统
Bash 权限检查
只读模式
文件写入工具禁用
```

所以它适合做：

- 上下文补充
- 状态提醒
- 行为 nudging
- 防止模型忘记当前模式
- 降低误操作概率

不适合单独承担：

- 安全隔离
- 权限控制
- 数据外发控制
- 文件写入禁止

---

## 9. 总结

`<system-reminder>` 是 Claude Code 在 Agent Loop 中非常重要的动态上下文机制。

它的本质不是一个新的 API role，而是：

```text
meta attachment
  → normalizeAttachmentForAPI()
  → role:user + isMeta:true
  → wrapMessagesInSystemReminder()
  → <system-reminder>...</system-reminder>
  → 发送给模型
```

它让 Claude Code 可以在不改动长期 system prompt 的情况下，把 IDE 状态、文件变化、计划模式、todo 提醒、memory、skill、diagnostics 等短期上下文注入给模型。

这类设计体现了 Claude Code 的一个核心模式：

```text
长期规则放 system prompt
动态状态放 system reminder
硬安全靠代码和权限系统
```

---

## 10. 关键源码索引

| 文件 | 作用 |
|------|------|
| `src/utils/messages.ts` | `wrapInSystemReminder()`、`wrapMessagesInSystemReminder()`、attachment 归一化 |
| `src/utils/attachments.ts` | attachment 类型定义、各类提醒生成逻辑 |
| `src/query.ts` | Agent Loop 中收集 attachment 并追加到下一轮上下文 |
| `src/utils/forkedAgent.ts` | `criticalSystemReminder_EXPERIMENTAL` 在子 agent 上下文中的传递 |
