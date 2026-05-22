# Claude Code XML 标签在提示词中的实际用法

> 基于 Anthropic 官方文档（Prompting Best Practices）和 Claude Code 源码分析，说明 XML 标签在提示词工程中的定位、使用方式和实际效果。

## 1. 官方定位

Anthropic 官方 Prompting Best Practices 文档明确推荐使用 XML 标签来组织提示词：

> XML tags help Claude parse complex prompts unambiguously, especially when your prompt mixes instructions, context, examples, and variable inputs.

文档建议：

- 用一致的、描述性的标签名
- 用标签包裹不同类型的内容（指令、上下文、示例、用户输入）
- 当内容有层级关系时使用嵌套标签

参考链接：https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices#structure-prompts-with-xml-tags

---

## 2. 技术本质

XML 标签在 Claude 中**不是 API 层面的特殊语义**，而是**提示词级别的结构化约定**。

关键事实：

- API 只看到纯文本，不会对 XML 标签做特殊解析
- 标签名由开发者自定义，没有"保留标签"的概念
- 模型通过训练学会了理解 XML 标签的语义边界
- Claude Code 自身的 system prompt 大量使用这类标签，模型在训练过程中对此有强关联

这意味着：

```text
标签是软约束，不是硬约束
效果来自模型训练，不来自 API 解析
标签名的语义越清晰，模型遵循度越高
```

---

## 3. 两种使用场景

### 3.1 API 调用层面

直接调用 Anthropic API 时，XML 标签可以放在 `system` 字段或 `messages` 中：

```python
import anthropic

client = anthropic.Anthropic()

system_prompt = """
<role>
你是一个代码审查助手，只指出 bug 和安全问题。
</role>

<output_format>
用 JSON 返回：{"issues": [{"line": 数字, "severity": "high/medium/low", "description": "..."}]}
</output_format>
"""

user_context = """
<user_profile>
用户等级：VIP
最近订单：#20260509-1234，状态：已签收
</user_profile>

<user_message>
我前天买的东西收到了但是有破损，怎么办？
</user_message>
"""

message = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=1024,
    system=system_prompt,
    messages=[
        {"role": "user", "content": user_context}
    ],
)
```

### 3.2 CLAUDE.md / Cursor Rules 层面

使用 Claude Code 或 Cursor 等闭源客户端时，不直接调 API，但可以通过规则文件注入 XML 标签。

CLAUDE.md 的内容会被注入到 system prompt 中：

```text
CLAUDE.md 文件内容
  → claudemd.ts 发现和加载
  → 注入到 system prompt 的 userContext 部分
  → 作为 system 字段的 text block 发送给 API
  → 模型完整看到
```

在 system prompt 中的位置：

```text
[稳定规则: 身份/工具/行为规范]
__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__
[动态部分: cwd/日期/平台/shell/MCP]
[CLAUDE.md / rules / memory]     ← 你写的内容在这里
[追加 prompt]
```

所以在 CLAUDE.md 或 `.cursor/rules/` 中写 XML 标签，模型同样能看到并遵循。

---

## 4. 常用标签及效果

### 4.1 `<system-reminder>`

Claude Code 自身使用的标签，模型对它有很强的训练关联。

```text
<system-reminder>
回复一律使用中文。
修改代码前先读取文件。
不要主动提交 commit。
</system-reminder>
```

效果：模型会把内容当作"系统级提醒"，遵循度比普通纯文本指令更高。

源码中的原生用法（`src/utils/messages.ts`）：

```typescript
export function wrapInSystemReminder(content: string): string {
  return `<system-reminder>\n${content}\n</system-reminder>`
}
```

Claude Code 用它来传递：IDE 状态、文件变更提醒、Plan/Auto/Todo 提醒、memory、skill discovery 等动态上下文。

### 4.2 `<knowledge-cutoff>`

用于提醒模型注意知识截止时间。

```text
<knowledge-cutoff>
你的知识截止到 2025 年 4 月。对于之后的信息，请使用工具搜索确认，不要猜测。
</knowledge-cutoff>
```

效果：模型会更谨慎地对待时效性信息，减少"自信地编造过期知识"的情况。

### 4.3 官方文档中的标签示例

Anthropic 官方文档中使用了大量自定义标签：

| 标签 | 用途 |
|------|------|
| `<frontend_aesthetics>` | 前端设计规范 |
| `<default_to_action>` | 鼓励主动执行而非仅建议 |
| `<do_not_act_before_instructions>` | 抑制过早行动 |
| `<use_parallel_tool_calls>` | 鼓励并行工具调用 |
| `<avoid_excessive_markdown_and_bullet_points>` | 控制输出格式 |
| `<investigate_before_answering>` | 减少幻觉 |
| `<example>` / `<examples>` | 包裹 few-shot 示例 |
| `<documents>` / `<document>` | 包裹长文档输入 |

---

## 5. 为什么遵循度更高

XML 标签比纯文本指令遵循度更高，原因有三：

### 5.1 训练数据中大量出现

Claude Code 自身的 system prompt 就使用这些标签（`<system-reminder>`、`<bash-input>`、`<task-notification>` 等），模型在 RLHF/训练过程中学会了"看到这类标签就当作结构化指令来理解"。

### 5.2 语义边界清晰

标签把不同类型的内容隔开，模型不容易"跳过"或"混淆"：

```text
不用标签（容易混淆）：
  回复使用中文。代码注释用英文。commit message 用 conventional commits 格式。
  优先编辑现有文件。测试文件放在 __tests__ 目录下。

用标签（边界清晰）：
  <output_format>
  回复使用中文。代码注释用英文。commit message 用 conventional commits 格式。
  </output_format>

  <coding_rules>
  优先编辑现有文件。测试文件放在 __tests__ 目录下。
  </coding_rules>
```

### 5.3 标签名的语义锚定

`system-reminder` 暗示"这是系统提醒"，`knowledge-cutoff` 暗示"这是知识边界"。模型会按标签名理解内容的重要性和类型。

---

## 6. 在 CLAUDE.md 中的推荐写法

```markdown
<system-reminder>
核心行为规则，希望模型每轮都遵守的。
比如：回复使用中文、不要主动 commit、修改前先读文件。
</system-reminder>

<knowledge-cutoff>
知识截止提醒，让模型对不确定的信息主动搜索而非猜测。
</knowledge-cutoff>

<coding_rules>
编码规范、项目约定。
比如：文件组织、命名规范、测试要求。
</coding_rules>

<output_format>
输出格式、语言、风格约束。
比如：commit message 格式、代码注释语言。
</output_format>

<review_policy>
审查策略。
比如：每次修改后跑 lint 和 test、报告所有问题不要过滤。
</review_policy>
```

适合场景：CLAUDE.md 内容较长（超过几十行）、规则类别多、需要明确区分不同约束域。

如果 CLAUDE.md 只有几行简单规则，用 Markdown 标题即可，不需要引入 XML 标签。

---

## 7. 使用建议

| 方面 | 建议 |
|------|------|
| **标签名** | 用 Claude Code 已有的标签名（如 `system-reminder`）效果可能略好；自定义名字也完全可以，语义清晰即可 |
| **嵌套** | 内容有层级关系时使用嵌套，如 `<documents>` 内含多个 `<document>` |
| **数量** | 不要过度标签化，3-8 个顶层标签通常够用 |
| **内容** | 标签内用简洁的规则列表或短段落，避免大段散文 |
| **Markdown 混用** | 标签内部可以正常使用 Markdown 列表和格式 |
| **限制** | XML 标签是软约束，不能替代代码层面的权限控制和安全措施 |

---

## 8. 与 `<system-reminder>` 机制的关系

Claude Code 内部的 `<system-reminder>` 机制是一个更完整的工程系统：

```text
meta attachment
  → normalizeAttachmentForAPI()
  → role:user + isMeta:true
  → wrapMessagesInSystemReminder()
  → <system-reminder>...</system-reminder>
  → 发送给模型
```

用户在 CLAUDE.md 中写的 `<system-reminder>` 和 Claude Code 内部注入的 `<system-reminder>` 效果类似——模型看到的都是同样的文本标签。区别在于：

- Claude Code 内部的：通过代码自动注入，带有 `isMeta: true` 标记，有归一化和生命周期管理
- 用户在 CLAUDE.md 中写的：作为静态规则文本注入 system prompt，没有内部元数据

对模型而言，两者效果一致——它只看到文本和标签，不感知内部元数据。

---

## 9. 关键源码索引

| 文件 | 作用 |
|------|------|
| `src/utils/messages.ts` | `wrapInSystemReminder()` 定义 |
| `src/constants/xml.ts` | XML tag 常量定义 |
| `src/utils/systemPrompt.ts` | system prompt 构建与优先级 |
| `src/utils/claudemd.ts` | CLAUDE.md 发现与加载 |
| `src/constants/prompts.ts` | 动态边界标记定义 |

---

## 10. 参考资料

- [Anthropic Prompting Best Practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
- [Claude Code system-reminder 机制与场景示例](./claudecode-system-reminder机制与场景示例.md)
- [Claude Code system prompt 编排语言与格式分析](./claudecode-system-prompt编排语言与格式分析.md)
