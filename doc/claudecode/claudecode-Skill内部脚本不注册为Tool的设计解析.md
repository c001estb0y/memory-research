# Claude Code Skill 内部脚本不注册为 Tool 的设计解析

> 以 Claude Code 的 `daily-journal` Skill 为例，说明 agent 框架中"skill 内部脚本是否应注册为 function call tool"这个设计问题，以及 Claude Code 为什么选择"不注册，用 Bash 执行"。

## 1. 问题背景

在实现 agent 框架时，一个常见的设计决策是：

> **Skill 内部包含的脚本/工具，应该以什么方式暴露给 LLM？**

有三种方案：

| 方案 | 做法 | 代表 |
|------|------|------|
| **A：注册时全部暴露** | 注册 skill 时，把 skill 内的每个脚本都注册为独立的 function call tool | 无主流框架采用 |
| **B：激活后再暴露** | 先只注册 skill 本身，等模型激活 skill 后再动态注册内部脚本为 tool | 部分框架采用 |
| **C：不注册，用通用工具执行** | skill 内部脚本不注册为 tool，模型看到操作手册后用 Bash/Read 等通用工具执行 | **Claude Code 采用** |

---

## 2. 用一个具体例子来理解

假设我们有一个 `daily-journal` skill，功能是：读数据库 → 生成日报 Markdown → git push 到 GitHub。

它内部涉及的"脚本/操作"有：

```text
1. sqlite3 查询数据库
2. 组织 Markdown 内容
3. 写入文件
4. git add / commit / push
```

下面用这个例子对比三种方案。

### 2.1 方案 A：注册时全部暴露

```text
注册阶段：
  → register_tool("daily_journal_query_db", schema={...})
  → register_tool("daily_journal_write_md", schema={...})
  → register_tool("daily_journal_git_push", schema={...})

模型看到的工具列表：
  Bash, Read, Write, ...
  daily_journal_query_db    ← 新增
  daily_journal_write_md    ← 新增
  daily_journal_git_push    ← 新增
  westock_query_price       ← 另一个 skill 的
  westock_query_finance     ← 另一个 skill 的
  westock_query_news        ← 另一个 skill 的
  shadow_push_sync          ← 又一个 skill 的
  ...

模型调用：
  tool_use: daily_journal_query_db(date="2026-05-09")
  tool_use: daily_journal_write_md(content="...")
  tool_use: daily_journal_git_push(repo="/d/github/journal")
```

**问题**：

- 10 个 skill，每个 5 个脚本 = 50 个工具 schema，工具列表爆炸
- 每个工具的 schema（name + description + parameters JSON Schema）都要进 system prompt
- token 浪费严重，模型选择困难，prompt cache 命中率下降
- 大量工具在当前场景下根本用不到

### 2.2 方案 B：激活后再暴露

```text
注册阶段：
  → register_tool("daily_journal", schema={name: string})
  （只注册 skill 本身，不注册内部脚本）

模型看到的工具列表：
  Bash, Read, Write, ...
  daily_journal             ← 只有这一个

模型调用 daily_journal → runtime 动态注册内部工具：
  → register_tool("daily_journal_query_db", schema={...})
  → register_tool("daily_journal_write_md", schema={...})
  → register_tool("daily_journal_git_push", schema={...})

模型下一轮看到：
  Bash, Read, Write, ...
  daily_journal
  daily_journal_query_db    ← 动态新增
  daily_journal_write_md    ← 动态新增
  daily_journal_git_push    ← 动态新增
```

**问题**：

- 工具列表在会话中途发生变化，system prompt 需要重新发送
- prompt cache 被打破（稳定前缀变了）
- 需要维护"动态工具注册/注销"的复杂机制
- 每个 skill 内部的脚本仍然需要定义严格的 JSON Schema

### 2.3 方案 C：Claude Code 的做法——不注册，用 Bash 执行

```text
注册阶段（一次性，永不变化）：
  工具列表（恒定）：
    Bash, Read, Write, Grep, Glob, SkillTool, Task, ...

第 1 段 — system prompt 中的 skill 目录：
  <available_skills>
    <skill name="daily-journal">
      Read today's summaries from codebuddy-mem.db,
      generate a daily work journal in Chinese,
      and push it to the journal GitHub repo.
      Use when the user wants to write a daily journal.
    </skill>
    <skill name="westock-data">查询A股、港股、美股...</skill>
  </available_skills>

  （每个 skill 只占 name + description，约 30-50 token）

第 2 段 — 模型决定使用 daily-journal：
  模型 → tool_use: SkillTool(skill_name="daily-journal")
  runtime → 读取 SKILL.md body → 作为 user message 注入会话

  模型现在看到的"操作手册"：

  ┌──────────────────────────────────────────────────┐
  │  # Daily Journal Generator                       │
  │                                                  │
  │  ## Database Info                                │
  │  - Path: ~/.codebuddy-mem/codebuddy-mem.db       │
  │  - Table: session_summaries                      │
  │                                                  │
  │  ## Workflow                                     │
  │  ### Step 1: Query today's summaries             │
  │  ```bash                                         │
  │  sqlite3 ~/.codebuddy-mem/codebuddy-mem.db \     │
  │    "SELECT project, request, completed ..."      │
  │  ```                                             │
  │                                                  │
  │  ### Step 2: Group by project, generate markdown │
  │  ### Step 3: Write file to journal/YYYY-MM-DD.md │
  │  ### Step 4: git add / commit / push             │
  └──────────────────────────────────────────────────┘

第 3 段 — 模型按手册执行，用通用工具：
  模型 → tool_use: Bash("sqlite3 ~/.codebuddy-mem/codebuddy-mem.db ...")
  模型 → tool_use: Write("/d/github/journal/2026/05/2026-05-09.md", content)
  模型 → tool_use: Bash("cd /d/github/journal && git add . && git commit -m '...'")
  模型 → tool_use: Bash("git push")
```

**核心要点**：

- skill 内部的 `sqlite3`、`git push` 等脚本**从来没有被注册为 tool**
- 模型读到 SKILL.md 这份"操作手册"后，自己决定用 `Bash` 工具去执行
- 工具列表从头到尾恒定不变

---

## 3. 完整执行时序图

以用户说"帮我写今天的日报"为例：

```text
用户: "帮我写今天的日报"
  │
  │  system prompt 中有 <available_skills> 目录
  │  模型看到 daily-journal 的 description 匹配需求
  │
  ▼
模型 → tool_use: SkillTool(skill_name="daily-journal")
  │
  │  runtime 行为：
  │  1. 从 Skill Registry 找到 daily-journal
  │  2. fs.readFileSync("~/.claude/skills/daily-journal/SKILL.md")
  │  3. 去掉 YAML frontmatter，取 body
  │  4. body 以 user message 注入当前会话
  │  5. 记录到 Invoked skills 缓存（防重复装载）
  │
  ▼
模型现在看到了完整的操作手册（SKILL.md body）
  │
  │  手册说"Step 1: 用 sqlite3 查询数据库"
  │
  ▼
模型 → tool_use: Bash(command="sqlite3 /c/Users/.../codebuddy-mem.db \"SELECT ...\"")
  │
  │  runtime 执行 bash，返回查询结果
  │
  ▼
模型拿到数据，按手册 Step 2 组织 Markdown
  │
  ▼
模型 → tool_use: Write(file="/d/github/journal/2026/05/2026-05-09.md", content="...")
  │
  ▼
模型 → tool_use: Bash(command="cd /d/github/journal && git add . && git commit -m 'journal: 2026-05-09'")
  │
  ▼
模型 → tool_use: Bash(command="git push")
  │
  ▼
模型 → "已生成今天的日报并推送到 GitHub。"
```

整个过程中，模型用到的工具只有三个：`SkillTool`、`Bash`、`Write`。这三个工具是**一直存在**的通用工具，不是为 daily-journal 专门注册的。

---

## 4. 为什么 Claude Code 选方案 C

### 4.1 工具列表恒定 → prompt cache 命中率高

```text
方案 A/B：
  每新增一个 skill → 工具列表变长 → system prompt 变化
  → prompt cache 的稳定前缀被打破 → 缓存失效 → token 成本上升

方案 C：
  工具列表始终是 Bash, Read, Write, SkillTool, ...
  → system prompt 稳定前缀不变
  → prompt cache 持续命中
```

Claude Code 通过 `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` 标记划分静态/动态边界，确保工具定义在稳定区：

```text
[稳定区 — 可 cache]
  身份规则、工具定义（含 SkillTool）、行为规范
__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__
[动态区 — 不 cache]
  cwd、日期、CLAUDE.md、skill 目录、memory
```

### 4.2 Skill 数量不影响工具 schema 大小

```text
方案 A（50 个 skill × 5 个脚本 = 250 个工具）：
  每个工具的 JSON Schema 约 200-500 token
  250 × 350 = ~87,500 token 只用于工具定义

方案 C：
  通用工具数量恒定（约 15-20 个）
  skill 目录每条约 30-50 token
  50 个 skill 的目录 ≈ 2,000 token
```

### 4.3 模型有充分灵活性

SKILL.md 是自然语言的"操作手册"，模型可以：

- 根据实际情况调整执行顺序
- 跳过不需要的步骤
- 处理手册没覆盖的边缘情况
- 出错时自行诊断和重试

如果把每个步骤固化为独立 tool，模型的灵活性反而受限。

### 4.4 渐进披露节省 token

```text
第 1 段：50 个 skill × 40 token/条 = 2,000 token（常驻）
第 2 段：被选中的 1 个 skill body ≈ 500-1,000 token（按需）
第 3 段：body 引用的资源文件（按需读取）

vs 方案 A：
  50 × 5 × 350 = 87,500 token（全部常驻）
```

---

## 5. 方案对比总结

| 维度 | 方案 A：全部注册 | 方案 B：激活后注册 | 方案 C：不注册（Claude Code） |
|------|-----------------|-------------------|-------------------------------|
| **工具列表** | 随 skill 数线性增长 | 会话中途动态变化 | 恒定不变 |
| **prompt cache** | 频繁失效 | 激活时失效 | 稳定命中 |
| **token 开销** | 极高（所有 schema 常驻） | 中等 | 低（目录 + 按需 body） |
| **实现复杂度** | 需要为每个脚本定义 schema | 需要动态注册/注销机制 | 只需一个 SkillTool + 通用工具 |
| **模型灵活性** | 受限于 tool 参数定义 | 受限于 tool 参数定义 | 自然语言手册，灵活度最高 |
| **扩展性** | 差（工具爆炸） | 中等 | 好（加 skill 只加一行目录） |
| **调试** | 每个 tool 独立日志 | 需要追踪动态注册 | Bash 命令直接可见 |

---

## 6. 什么时候方案 C 不适用

方案 C 也不是万能的，以下场景可能需要方案 A 或 B：

| 场景 | 原因 |
|------|------|
| **脚本需要严格的参数校验** | function call 的 JSON Schema 可以在 API 层强制校验参数类型和必填字段，Bash 执行没有这种保障 |
| **脚本执行需要特殊权限或沙箱** | 独立 tool 可以在 runtime 层做权限控制，Bash 权限控制粒度较粗 |
| **脚本不是 CLI 可执行的** | 如果"脚本"是一个 REST API 调用或数据库操作，用 Bash 不如直接注册为 tool 自然 |
| **需要隐藏实现细节** | function call tool 只暴露接口，不暴露内部命令；Bash 方式下模型看到完整命令 |

Claude Code 的做法是：对于这类需求，走 **MCP Server** 或 **Hook** 而不是把脚本注册为 tool：

```text
需要严格接口 → MCP Server（标准 function call，但由外部服务提供）
需要确定性执行 → Hook（runtime 旁路，不进 LLM 上下文）
需要灵活执行 → Skill body + Bash（模型自主决策）
```

---

## 7. 对 agent 框架设计的启示

如果你在设计 agent 框架，Claude Code 的实践给出的建议是：

1. **默认不注册**：skill 内的脚本默认不注册为 function call tool，用 Bash/Read 等通用工具执行
2. **Skill = 操作手册**：SKILL.md 是给模型看的自然语言指令，不是给 runtime 解析的接口定义
3. **渐进披露**：只把 name/description 放入常驻上下文，body 按需加载
4. **工具列表恒定**：通用工具（Bash/Read/Write/SkillTool）+ MCP Server 覆盖所有需求
5. **确定性逻辑走 Hook**：校验、重试、通知等工程性流程放 Hook，不放 Skill body

---

## 8. 关键源码索引

| 文件 | 职责 |
|------|------|
| `src/tools/SkillTool.ts` | SkillTool 工具实现：接收 skill_name，读取 body 注入会话 |
| `src/skills/*` | Skill Registry：扫描、索引、按需加载 |
| `src/utils/systemPrompt.ts` | system prompt 构建，包含 `<available_skills>` 目录注入 |
| `src/agentLoop/prefetch.ts` | `startSkillDiscoveryPrefetch`：Haiku 推荐相关 skill |
| `src/constants/prompts.ts` | `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 标记 |
| `src/runAgent.ts` | Subagent 的 `skillsToPreload` 预加载循环 |

---

## 参考文档

- [Claude Code Skill 系统设计与 Hooks 协同](./claudecode-skill系统设计与hooks协同.md)
- [Claude Code Agent Loop 工具调用与提示词设计](./claudecode-agentloop-工具调用与提示词设计.md)
- [Claude Code System Prompt 编排语言与格式分析](./claudecode-system-prompt编排语言与格式分析.md)
- [Claude Code Cache 缓存体系深度分析](./claudecode-cache缓存体系深度分析.md)
