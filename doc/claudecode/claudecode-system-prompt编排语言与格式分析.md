# Claude Code 系统提示词编排语言与格式分析

> 基于 Claude Code 源码分析 system prompt 的组织方式：它不是 Jinja2 模板，也不是 XML 模板文件，而是由 TypeScript 字符串数组、条件分支、Markdown 结构和 XML-like tags 共同编排。

## 1. 核心结论

Claude Code 的系统提示词可以从三层理解：

```text
内部表示：
  SystemPrompt = string[]

API 表示：
  system: TextBlockParam[]

正文风格：
  Markdown 章节
  + TypeScript 字符串模板
  + 条件分支 / feature gate
  + XML-like tags 作为语义边界
  + 少量自定义 {{var}} 正则替换
```

它不是：

- Jinja2
- Nunjucks
- Handlebars
- Mustache
- XML 模板引擎

更准确的说法是：

> Claude Code 用 TypeScript 代码直接编排 prompt，用 Markdown 组织长文本，用 XML-like tags 标记特殊内容边界。

---

## 2. 内部格式：`SystemPrompt = string[]`

Claude Code 内部不是把 system prompt 当成单个大字符串，而是把它当成多个字符串片段组成的数组。

核心构建入口是 `src/utils/systemPrompt.ts` 的 `buildEffectiveSystemPrompt()`。

源码注释给出了优先级：

```typescript
/**
 * Builds the effective system prompt array based on priority:
 * 0. Override system prompt (if set, e.g., via loop mode - REPLACES all other prompts)
 * 1. Coordinator system prompt (if coordinator mode is active)
 * 2. Agent system prompt (if mainThreadAgentDefinition is set)
 * 3. Custom system prompt (if specified via --system-prompt)
 * 4. Default system prompt (the standard Claude Code prompt)
 *
 * Plus appendSystemPrompt is always added at the end if specified.
 */
```

最终返回逻辑类似：

```typescript
return asSystemPrompt([
  ...(agentSystemPrompt
    ? [agentSystemPrompt]
    : customSystemPrompt
      ? [customSystemPrompt]
      : defaultSystemPrompt),
  ...(appendSystemPrompt ? [appendSystemPrompt] : []),
])
```

也就是说，最终 system prompt 大致是：

```typescript
[
  "默认 Claude Code 身份和行为规则",
  "工具使用规则",
  "任务执行规范",
  "输出风格约束",
  "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__",
  "当前工作目录 / 平台 / shell",
  "日期 / 模型 / 会话信息",
  "MCP 指令",
  "项目记忆 / CLAUDE.md / AGENTS.md",
  "附加 system prompt"
]
```

这种数组化设计有两个好处：

1. 便于按优先级替换、追加、组合不同 system prompt。
2. 便于在 API 层拆成多个 text block，并对稳定前缀加 `cache_control`。

---

## 3. 优先级：谁能覆盖谁

`buildEffectiveSystemPrompt()` 的优先级可以理解为：

```text
overrideSystemPrompt
  最高优先级，直接替换所有其他提示词

coordinator system prompt
  Coordinator Mode 专用提示词

main-thread agent system prompt
  当前主线程 agent 的专用提示词

customSystemPrompt
  用户或 CLI 指定的自定义 system prompt

defaultSystemPrompt
  标准 Claude Code 默认提示词

appendSystemPrompt
  追加在最终结果末尾，除 override 情况外都会加入
```

特殊情况是 proactive mode：

```typescript
return asSystemPrompt([
  ...defaultSystemPrompt,
  `\n# Custom Agent Instructions\n${agentSystemPrompt}`,
  ...(appendSystemPrompt ? [appendSystemPrompt] : []),
])
```

普通 agent prompt 通常会替换默认 prompt；但 proactive mode 下，agent prompt 会追加到默认 prompt 后面，形成“默认自主代理规则 + 领域专用规则”的组合。

---

## 4. API 格式：`system: TextBlockParam[]`

发送到 Anthropic API 时，Claude Code 会把内部 `string[]` 转成 `system` 字段中的 text block 数组。

形态类似：

```json
{
  "system": [
    {
      "type": "text",
      "text": "You are Claude Code..."
    },
    {
      "type": "text",
      "text": "You are an interactive CLI tool..."
    },
    {
      "type": "text",
      "text": "Current working directory: /repo/project"
    }
  ],
  "messages": [...]
}
```

如果启用了 prompt caching，稳定 block 会带上 `cache_control`：

```json
{
  "type": "text",
  "text": "You are Claude Code...",
  "cache_control": {
    "type": "ephemeral",
    "scope": "global"
  }
}
```

这说明 system prompt 的数组化不只是代码组织风格，也服务于 API 侧的 prompt cache。

---

## 5. 静态/动态边界

Claude Code 在 `src/constants/prompts.ts` 中定义了一个动态边界：

```typescript
export const SYSTEM_PROMPT_DYNAMIC_BOUNDARY =
  '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'
```

源码注释说明：

```text
Everything BEFORE this marker in the system prompt array can use scope: 'global'.
Everything AFTER contains user/session-specific content and should not be cached.
```

也就是说：

```text
边界之前：
  身份、长期规则、工具行为规范
  内容稳定
  可使用 global prompt cache

边界之后：
  cwd、日期、平台、shell、模型、MCP、记忆、输出风格
  用户/会话相关
  不适合跨用户缓存
```

这和之前分析的 prompt cache 密切相关：Claude Code 会尽量让前半段长期稳定，避免动态信息污染可缓存前缀。

---

## 6. 正文语言：Markdown 章节

Claude Code 的 system prompt 正文主要是 Markdown 风格，而不是结构化 JSON 或 XML。

典型片段类似：

```markdown
# Tone and style

You should be concise, direct, and to the point.

# Doing tasks

When making code changes, prefer editing existing files.

# Using tools

Use specialized tools when available.
```

Markdown 的作用是给模型提供人类可读的层级结构：

- 标题表示规则类别
- 列表表示约束清单
- 代码块表示示例或格式
- 强调文本表示高优先级提醒

这种方式比纯自然语言大段文本更容易让模型区分规则范围。

---

## 7. XML-like tags：语义边界，不是模板引擎

Claude Code 使用了很多 XML 风格标签，但它们不是 XML 模板，也不是为了被 XML parser 严格解析。

`src/constants/xml.ts` 中定义了一批 tag 常量：

```typescript
export const COMMAND_NAME_TAG = 'command-name'
export const COMMAND_MESSAGE_TAG = 'command-message'
export const COMMAND_ARGS_TAG = 'command-args'

export const BASH_INPUT_TAG = 'bash-input'
export const BASH_STDOUT_TAG = 'bash-stdout'
export const BASH_STDERR_TAG = 'bash-stderr'
export const LOCAL_COMMAND_STDOUT_TAG = 'local-command-stdout'
export const LOCAL_COMMAND_STDERR_TAG = 'local-command-stderr'
```

常见用途：

| Tag | 作用 |
|-----|------|
| `<system-reminder>` | 系统动态提醒 |
| `<bash-input>` | bash 输入 |
| `<bash-stdout>` | bash 标准输出 |
| `<bash-stderr>` | bash 错误输出 |
| `<local-command-stdout>` | 本地命令输出 |
| `<task-notification>` | 后台任务通知 |
| `<command-name>` | slash command 名称 |
| `<command-message>` | slash command 内容 |
| `<teammate-message>` | swarm / teammate 消息 |

这些标签的目的主要是：

```text
告诉模型：这段文本是什么来源、是什么类型、应该如何理解。
```

例如：

```xml
<system-reminder>
The user opened the file src/app.ts in the IDE.
</system-reminder>
```

这不是让 XML 引擎渲染模板，而是告诉模型：这是系统自动注入的上下文，不是用户真实输入。

---

## 8. `<system-reminder>` 在 system prompt 中也被解释

Claude Code 的默认 system prompt 中还有专门一段说明 `<system-reminder>`：

```typescript
function getSystemRemindersSection(): string {
  return `- Tool results and user messages may include <system-reminder> tags. <system-reminder> tags contain useful information and reminders. They are automatically added by the system, and bear no direct relation to the specific tool results or user messages in which they appear.
- The conversation has unlimited context through automatic summarization.`
}
```

这很重要：Claude Code 不只是把 `<system-reminder>` 塞进消息里，还在 system prompt 里提前告诉模型：

```text
你可能会在 tool result 或 user message 中看到 <system-reminder>
这些是系统自动添加的有用提醒
它们不一定和所在的 user/tool message 有直接关系
```

也就是说，`<system-reminder>` 是一个被 system prompt 正式定义过的内部标记。

---

## 9. 少量 `{{variable}}` 替换，但不是 Jinja2

Claude Code 某些子系统中有简单的 `{{variable}}` 替换逻辑，例如 Session Memory / Magic Docs。

源码形态类似：

```typescript
function substituteVariables(
  template: string,
  variables: Record<string, string>,
): string {
  return template.replace(/\{\{(\w+)\}\}/g, (match, key: string) =>
    Object.prototype.hasOwnProperty.call(variables, key)
      ? variables[key]!
      : match,
  )
}
```

这只是单轮正则替换：

```text
{{summary}} → 实际摘要
{{date}}    → 实际日期
```

它不具备 Jinja2 的能力：

- 没有 `{% if %}` / `{% for %}` 控制流
- 没有 filter
- 没有 macro
- 没有 include
- 没有模板继承
- 没有沙箱化模板运行时

所以不能说 Claude Code 用 Jinja2 渲染 prompt。更准确地说，它在少数场景里使用了自定义的 `{{var}}` 字符串替换。

---

## 10. 一个简化的完整编排例子

假设 Claude Code 当前处于普通交互模式，最终 system prompt 可以抽象成：

```typescript
const defaultSystemPrompt = [
  `You are Claude Code, Anthropic's official CLI for Claude.`,
  `# Tone and style\nBe concise, direct, and helpful.`,
  `# Doing tasks\nPrefer editing existing files over creating new ones.`,
  `# Using tools\nUse specialized tools when available.`,
  getSystemRemindersSection(),
  SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
  `Current working directory: D:\\GitHub\\memory-research`,
  `Today's date: 2026-04-25`,
  `Shell: powershell`,
  `MCP servers: ...`,
  `Project memory: ...`,
]
```

然后 `buildEffectiveSystemPrompt()` 根据当前模式决定是否替换或追加：

```typescript
const finalSystemPrompt = asSystemPrompt([
  ...defaultSystemPrompt,
  appendSystemPrompt,
])
```

发送 API 前变成：

```json
{
  "system": [
    {
      "type": "text",
      "text": "You are Claude Code, Anthropic's official CLI for Claude.",
      "cache_control": { "type": "ephemeral", "scope": "global" }
    },
    {
      "type": "text",
      "text": "# Tone and style\nBe concise, direct, and helpful.",
      "cache_control": { "type": "ephemeral", "scope": "global" }
    },
    {
      "type": "text",
      "text": "Current working directory: D:\\GitHub\\memory-research\nToday's date: 2026-04-25"
    }
  ],
  "messages": [...]
}
```

这里可以看到三个层次：

```text
TypeScript 负责拼接
Markdown 负责组织文本层级
XML-like tags 负责局部语义标记
cache_control 负责提示服务端缓存稳定前缀
```

---

## 11. 和 `<system-reminder>` 的关系

system prompt 和 `<system-reminder>` 是互补关系：

| 维度 | system prompt | `<system-reminder>` |
|------|---------------|---------------------|
| API 位置 | 顶层 `system` 字段 | `messages` 中的 `user` 消息 |
| 生命周期 | 长期，贯穿会话 | 短期，按轮次/事件注入 |
| 内容类型 | 身份、行为规范、工具规则 | IDE 状态、文件变更、todo、memory、diagnostics |
| 缓存策略 | 静态前缀可 cache | 动态内容通常在尾部，需控制漂移 |
| 强制力 | 高于普通消息，但仍是提示词 | 运行时提醒，软约束 |

Claude Code 的设计取舍是：

```text
稳定规则进入 system prompt
动态状态进入 <system-reminder>
真正安全边界靠工具池和权限系统
```

这样既能保持 system prompt 稳定，又能给模型持续补充运行时上下文。

---

## 12. 总结

Claude Code 的系统提示词不是“一个模板文件渲染出来的大 prompt”，而是一个多层编排系统：

```text
1. TypeScript 代码决定 prompt 的组成和优先级
2. string[] 保存多个 system prompt 片段
3. Markdown 章节承载可读规则
4. XML-like tags 标记特殊内容边界
5. 动态边界区分可缓存静态前缀和会话动态内容
6. API 层转成 system: TextBlockParam[]
7. 少量子系统使用 {{var}} 正则替换，但不是 Jinja2
```

因此如果要描述 Claude Code 的提示词编排语言，最准确的说法是：

> TypeScript 驱动的 Markdown prompt assembly，并辅以 XML-like semantic tags。

---

## 13. 关键源码索引

| 文件 | 作用 |
|------|------|
| `src/utils/systemPrompt.ts` | system prompt 优先级合并与最终 `SystemPrompt` 组装 |
| `src/constants/prompts.ts` | 默认 system prompt 片段、动态边界、system reminder 说明 |
| `src/constants/systemPromptSections.ts` | system prompt section 的封装与解析 |
| `src/constants/xml.ts` | XML-like tag 常量定义 |
| `src/services/api/claude.ts` | 将 system prompt 转成 API text blocks，并添加 `cache_control` |
| `src/services/SessionMemory/prompts.ts` | 简单 `{{variable}}` 正则替换示例 |
| `src/services/MagicDocs/prompts.ts` | 简单 `{{variable}}` 正则替换示例 |
