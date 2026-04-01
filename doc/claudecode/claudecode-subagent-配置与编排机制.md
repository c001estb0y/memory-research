# Claude Code Subagent 配置与编排机制

基于 Claude Code 开源快照源码的深度分析。

---

## 一、核心概念：Agent 工具与用户的关系

在阅读本文档前，需要理解一个核心概念：**本文中所有 `Agent({...})` 代码块展示的，都是主模型（AI）自动生成的 tool_use 调用，不是用户需要手写的内容。**

### 1.1 用户视角

用户只需用自然语言描述需求：

```
用户："这个分支还差什么才能发版？"
```

### 1.2 AI 内部发生的事

主模型收到用户的自然语言后，通过推理决定是否需要调用 Agent 工具，并自动生成调用参数：

```
用户说的 ──→ "这个分支还差什么才能发版？"
               │
               ↓
主模型想的 ──→ "这是个调查性问题，fork 出去比较好，
               不需要把中间工具输出留在我的上下文里"
               │
               ↓
主模型生成 ──→ tool_use: Agent({
的 JSON        name: "ship-audit",
               description: "Branch ship-readiness audit",
               prompt: "审查这个分支发版前还差什么..."
             })
               │
               ↓
Claude Code ──→ 解析 tool_use → Fork 路径 → 子 agent 执行
运行时           → 返回结果给主模型
               │
               ↓
主模型汇总 ──→ "发版前还有 3 个问题：1. 没有测试..."
               │
               ↓
用户看到的 ──→ "发版前还有 3 个问题：1. 没有测试..."
```

### 1.3 Agent 就是一个"工具"

`Agent` 和 `Read`（读文件）、`Grep`（搜索）、`Edit`（编辑）一样，是主模型可以调用的工具之一。在 API 返回的 JSON 中，它长这样：

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "thinking",
      "thinking": "用户想知道发版前还差什么，我 fork 出去调查..."
    },
    {
      "type": "tool_use",
      "id": "toolu_xyz789",
      "name": "Agent",
      "input": {
        "name": "ship-audit",
        "description": "Branch ship-readiness audit",
        "prompt": "审查这个分支发版前还差什么。检查：未提交变更、相对 main 领先的 commit、测试覆盖、CI 配置。报告 punch list，200 字以内。"
      }
    }
  ],
  "stop_reason": "tool_use"
}
```

主模型"决定"调用哪个工具、传什么参数——这个决策过程就是 LLM 的推理，由提示词（`prompt.ts` 中的 `whenToUse` 描述和使用示例）引导。

> 因此，后文中所有 `Agent({...})` 的展示，都是在解释**AI 内部的决策和工具调用机制**，不是用户操作指南。

---

## 二、Subagent 架构总览

Claude Code 的 subagent 系统由三层构成：

```
用户（自然语言）
 ↓
主模型（Agent Loop） ← 决定是否需要 subagent、选择哪种模式
 ├── Agent 工具 → spawn subagent（同步/异步）
 ├── Fork → 继承上下文的分身（prompt cache 共享）
 ├── Coordinator 模式 → 编排多个 worker
 └── forkedAgent → 内部静默 fork（extractMemories / autoDream / sessionMemory）
```

---

## 三、Subagent 类型全览

### 3.1 六种内置 Agent

| agentType | 角色定位 | 模型 | 后台 | 只读 | 何时使用 |
|-----------|----------|------|------|------|----------|
| **general-purpose** | 通用任务执行 | 默认 | 否 | 否 | 复杂检索、多步任务、没把握的搜索 |
| **Explore** | 代码库快速搜索 | haiku（ant 用 inherit） | 否 | 是 | 找文件、搜关键词、了解架构 |
| **Plan** | 方案设计 | inherit | 否 | 是 | 实现策略规划、架构决策 |
| **verification** | 对抗性验证 | inherit | 是 | 是 | 非平凡实现后的独立验证 |
| **claude-code-guide** | 产品文档问答 | haiku | 否 | 否 | 关于 Claude Code CLI/SDK/API 的问题 |
| **statusline-setup** | 状态栏配置 | sonnet | 否 | 否 | 配置 Claude Code 状态栏 |

### 3.2 Agent 定义的完整配置字段

每个 Agent 定义（`BaseAgentDefinition`）支持以下配置：

```typescript
{
  agentType: string           // 类型标识，如 "Explore"、"verification"
  whenToUse: string           // 何时使用的描述（展示给主模型）
  tools?: string[]            // 允许使用的工具列表（"*" = 全部）
  disallowedTools?: string[]  // 禁用的工具列表
  skills?: string[]           // 可用技能
  mcpServers?: McpServerSpec[]// 可用的 MCP 服务器
  hooks?: object              // 生命周期钩子
  color?: string              // UI 展示颜色
  model?: string              // 模型（"inherit" / "sonnet" / "opus" / "haiku"）
  effort?: string             // 推理强度
  permissionMode?: string     // 权限模式
  maxTurns?: number           // 最大回合数
  background?: boolean        // 是否后台运行
  isolation?: string          // 隔离模式（worktree / remote）
  memory?: object             // 持久记忆配置
  omitClaudeMd?: boolean      // 是否省略 CLAUDE.md
  criticalSystemReminder_EXPERIMENTAL?: string  // 每轮注入的关键提醒
  requiredMcpServers?: string[]  // 必须可用的 MCP 服务器
  initialPrompt?: string      // 首轮预置提示词
}
```

### 3.3 Agent 来源分类

Agent 可来自六种不同来源，按优先级合并（高→低）：

| 来源 | 说明 | 定义方式 |
|------|------|----------|
| **built-in** | 内置代理 | TypeScript 代码 |
| **plugin** | 插件提供 | 插件目录 |
| **userSettings** | 用户级自定义 | `~/.claude/agents/*.md` |
| **projectSettings** | 项目级自定义 | `.claude/agents/*.md` |
| **flagSettings** | CLI 参数 | 命令行标志 |
| **policySettings** | 管理策略 | 组织策略配置 |

同名 `agentType` 的定义，高优先级覆盖低优先级。用户可以用 Markdown frontmatter 自定义 agent。

### 3.4 自定义 Agent 示例（Markdown 格式）

用户在 `.claude/agents/` 下创建 Markdown 文件即可定义自定义 agent：

```markdown
---
agentType: code-reviewer
whenToUse: 对代码变更进行安全审查
model: sonnet
tools:
  - Read
  - Grep
  - Glob
  - Bash
disallowedTools:
  - FileWrite
  - FileEdit
background: true
---

你是一个代码安全审查专家。审查提供的代码变更，关注：
1. SQL 注入
2. XSS 漏洞
3. 敏感信息泄露
4. 权限绕过
```

---

## 四、四种编排模式

### 4.1 Spawn（标准 subagent 启动）

最常见的模式——主模型通过 Agent 工具 spawn 一个子代理。

**主模型生成的 tool_use**（用户无需手写，由 AI 自动决策和生成）：

```
Agent({
  description: "搜索认证相关代码",
  prompt: "在代码库中找到所有处理 JWT 验证的文件...",
  subagent_type: "Explore"
})
```

**执行流程**：

```
主模型 → Agent 工具
  ├── 解析 subagent_type → 匹配 AgentDefinition
  ├── 解析模型（inherit / 指定模型）
  ├── 创建 agentId
  ├── 构建子上下文（createSubagentContext）
  ├── 组装工具池（按 tools/disallowedTools 过滤）
  ├── 注入 system prompt + user context
  └── 进入 query loop（子 agent 的 agent loop）
       ├── 模型推理
       ├── 工具调用
       └── 返回结果给主模型
```

**同步 vs 异步**：

| 维度 | 同步（sync） | 异步（background） |
|------|-------------|-------------------|
| **触发方式** | 默认 | `run_in_background: true` 或 agent 定义 `background: true` |
| **AbortController** | 共享父级 | 独立新建 |
| **AppState 共享** | 是 | 否 |
| **权限提示** | 可弹出 | 默认避免（静默执行） |
| **结果返回** | 工具结果直接返回 | `task-notification` 消息异步通知 |

**具体例子——同步 Explore**：

```
用户: "项目里哪些文件处理用户认证？"

主模型 → Agent({
  description: "Search auth files",
  subagent_type: "Explore",
  prompt: "Find all files that handle user authentication. 
           Check for JWT validation, session management, 
           login/logout endpoints. Report file paths and 
           a one-line summary of each. Medium thoroughness."
})

Explore Agent:
  1. Glob: **/auth*.{ts,js}
  2. Grep: "jwt|session|login|logout" 
  3. Read: src/middleware/auth.ts（确认内容）
  4. 返回: "找到 5 个认证相关文件: ..."

主模型: 收到结果，继续回答用户
```

**具体例子——异步 Verification**：

```
用户: "给用户注册 API 添加邮箱验证"

主模型: 实现完成（修改了 4 个文件）
        ↓
主模型 → Agent({
  description: "Verify registration API",
  subagent_type: "verification",
  prompt: "原始任务：添加邮箱验证到用户注册 API。
           改动文件：routes/auth.ts, models/user.ts, 
           services/email.ts, tests/auth.test.ts。
           方法：注册后发送验证链接，24h 过期。"
})
        ↓
主模型: [继续其他工作或等待]
        ↓ [稍后收到 task-notification]
Verification Agent: VERDICT: FAIL（验证链接在并发请求下可能生成重复 token）
        ↓
主模型: 修复并 resume Agent 重新验证
```

### 4.2 Fork（上下文继承分身）

Fork 是 Claude Code 独有的编排模式——子代理**继承父级的完整对话上下文**，共享 prompt cache。

**与 Spawn 的核心区别**：

| 维度 | Spawn（指定 subagent_type） | Fork（省略 subagent_type） |
|------|---------------------------|--------------------------|
| **上下文** | 从零开始，只有 prompt 内容 | 继承父级完整对话 |
| **Prompt Cache** | 独立缓存 | 共享父级缓存 |
| **System Prompt** | 子 agent 自己的 | 父级的已渲染 prompt |
| **Prompt 风格** | 完整背景说明 | 简短指令（因为上下文已有） |
| **适用场景** | 专业任务 | 研究、实现分支 |

**主模型生成的 tool_use**（省略 `subagent_type`，AI 自动选择 Fork）：

```
Agent({
  name: "ship-audit",
  description: "Branch ship-readiness audit",
  prompt: "审查这个分支发版前还差什么。检查：未提交变更、
          相对 main 领先的 commit、测试覆盖、CI 配置。
          报告 punch list，200 字以内。"
})
```

**主模型生成的 prompt 写法差异**（提示词模板中教 AI 如何区分两种风格）：

```
// Spawn：需要完整背景
Agent({
  subagent_type: "Explore",
  prompt: "我们正在做一个电商项目，使用 Next.js + Prisma。
           最近用户反馈结账流程偶尔 500 错误。
           请搜索 checkout 相关的错误处理代码，
           特别是 payment 和 order 创建的事务处理。"
})

// Fork：简短指令（上下文已有）
Agent({
  name: "checkout-fix",
  prompt: "修复 checkout 事务问题。
           把 payment 和 order 创建包在同一个事务里。"
})
```

**Fork 的约束**：

- **不要偷看**：fork 返回前会提供 `output_file` 路径，主模型不能 Read 它（会把 fork 的工具噪音拉入自己的上下文）
- **不要猜测**：fork 未返回前，不能编造或预测其结果
- **不要设模型**：不同模型无法复用父级 cache
- **不可递归**：fork 内不能再 fork（通过 `isInForkChild` 检测 `<FORK_BOILERPLATE_TAG>` 防止）

**具体例子——并行 Fork 研究**：

```
用户: "这个 PR 改了认证和支付两个模块，帮我审查"

主模型: 这两块独立，我并行 fork 审查。

// 单条 assistant 消息中发起两个并行 fork
Agent({
  name: "auth-review", 
  description: "Review auth changes",
  prompt: "审查 PR 中认证模块的变更。关注 session 处理、
          token 过期逻辑、CSRF 防护。报告问题列表。"
})
Agent({
  name: "payment-review",
  description: "Review payment changes", 
  prompt: "审查 PR 中支付模块的变更。关注金额计算精度、
          退款幂等性、Webhook 签名验证。报告问题列表。"
})

[两个 fork 并行执行，各自返回 notification]

主模型: 综合两份审查结果，给出最终报告
```

### 4.3 Resume（恢复已完成的 Agent）

Resume 允许向已结束的 agent 发送后续消息，保留其完整上下文继续工作。

**主模型生成的 tool_use**：

```
Agent({
  resume: "verify-registration",   // 之前的 agent name/ID
  prompt: "已修复邮箱验证的并发问题。
           在 services/email.ts 中添加了分布式锁。
           请重新验证。"
})
```

**Resume 的内部流程**：

```
1. 从磁盘读取之前 agent 的 transcript + metadata
2. 清洗消息：
   ├── 过滤空白 assistant 消息
   ├── 过滤孤立 thinking 消息
   └── 过滤未完成的 tool_use
3. 重建 contentReplacementState（保证 prompt cache 对齐）
4. 如果有 worktreePath，验证目录仍存在
5. 追加新的 user 消息（resume prompt）
6. 通过 runAgent（isAsync: true）恢复执行
```

**典型场景——FAIL→修复→Resume→PASS**：

```
第一轮:
  主模型: 实现注册 API
  → Verification Agent: VERDICT: FAIL（重复邮箱未处理）

第二轮:
  主模型: 添加唯一约束，捕获 IntegrityError
  → Agent({ resume: "verify-reg", prompt: "已修复重复邮箱问题..." })
  → Verification Agent: 重新运行幂等性探针
  → VERDICT: PASS

第三轮:
  主模型: 抽检 Verification Agent 报告中的命令
  → 确认一致，向用户报告完成
```

### 4.4 Coordinator 模式（多 Worker 编排）

Coordinator 是 Claude Code 的「指挥官模式」——主模型变成纯编排者，不直接执行任务。

**架构**：

```
Coordinator（主线程，专注编排）
├── Agent({ subagent_type: "worker", prompt: "研究 X" })
├── Agent({ subagent_type: "worker", prompt: "实现 Y" })
├── Agent({ subagent_type: "worker", prompt: "验证 Z" })
├── SendMessage({ to: "worker-1", content: "补充信息" })
└── TaskStop({ agentId: "worker-2" })
```

**Coordinator 的三个工具**：

| 工具 | 作用 |
|------|------|
| `Agent` | 启动新 worker |
| `SendMessage` | 向运行中的 worker 发消息 |
| `TaskStop` | 停止一个 worker |

**Coordinator 的典型编排模式**：

```
Research → Synthesis → Implementation → Verification

1. Research Phase:
   并行 spawn 多个 Explore worker 调查代码库不同部分

2. Synthesis Phase:
   收集 research 结果，综合分析，制定计划

3. Implementation Phase:
   spawn 实现 worker，可能用 worktree 隔离

4. Verification Phase:
   spawn Verification Agent 做对抗性验证
```

**与 Fork 的互斥关系**：Coordinator 模式下 fork 被禁用（`isForkSubagentEnabled` 在 coordinator 下返回 false）。因为 coordinator 有自己的多 worker 编排机制，不需要 fork。

---

## 五、工具过滤机制

每个 subagent 看到的工具池经过严格过滤：

### 5.1 三层过滤

```
全量工具池
  ↓ 第一层：agent 定义的 tools（白名单）
  ↓ 第二层：agent 定义的 disallowedTools（黑名单）
  ↓ 第三层：全局禁用列表（ALL_AGENT_DISALLOWED_TOOLS）
  ↓ 第四层：异步 agent 额外限制（ASYNC_AGENT_ALLOWED_TOOLS）
  ↓
子 agent 可见工具
```

### 5.2 各内置 Agent 的工具权限

| Agent | 可用工具 | 禁用工具 |
|-------|----------|----------|
| **general-purpose** | `*`（全部） | 全局禁用列表 |
| **Explore** | 全部 - 禁用列表 | Agent, ExitPlanMode, FileEdit, FileWrite, NotebookEdit |
| **Plan** | 同 Explore | 同 Explore |
| **verification** | 全部 - 禁用列表 | Agent, ExitPlanMode, FileEdit, FileWrite, NotebookEdit |
| **claude-code-guide** | Glob/Grep/Read/WebFetch/WebSearch（或 Bash+Read+Web） | 其余全部 |
| **statusline-setup** | Read, Edit | 其余全部 |

### 5.3 工具通配符

`tools: ['*']` 表示「使用全量工具池再减去 disallowedTools」。未设置 `tools` 字段等同于 `['*']`。

---

## 六、Isolation（隔离模式）

Subagent 支持两种隔离模式，用于并行修改同一代码库时避免冲突：

### 6.1 Worktree 隔离

```
Agent({
  description: "Implement feature X",
  prompt: "...",
  isolation: "worktree"
})
```

系统会创建一个临时 Git worktree，agent 在隔离的工作副本中操作。完成后可以合并回主分支。

### 6.2 Remote 隔离（CCR）

```
Agent({
  description: "Complex implementation",
  prompt: "...",
  isolation: "remote"
})
```

将任务发送到远程 Claude Code Remote 环境执行，完全独立的沙箱。

---

## 七、Agent Memory（代理记忆）

Agent 可配置持久化记忆，跨会话积累知识：

**目录结构**：

```
~/.claude/agent-memory/<agentType>/
├── user/       # 用户级记忆
│   └── MEMORY.md
├── project/    # 项目级记忆
│   └── MEMORY.md
└── local/      # 本地级记忆
    └── MEMORY.md
```

**Memory Snapshot**：项目可以在 `.claude/agent-memory-snapshots/<agentType>/` 下放置种子记忆，agent 首次运行时会同步到本地。

---

## 八、内部静默 Fork（forkedAgent）

除了用户可见的 Agent/Fork，Claude Code 还有一条内部 fork 通道——`runForkedAgent`。这些 fork 不走 Agent 工具，而是直接调用 `query()` 函数，用于后台静默任务：

| 使用者 | querySource | 目的 |
|--------|-------------|------|
| **extractMemories** | `extract_memories` | 回合结束后提取记忆 |
| **SessionMemory** | `session_memory` | 更新会话记忆 |
| **autoDream** | `auto_dream` | 后台记忆整理 |
| **AgentSummary** | `agent_summary` | 子 agent 进度摘要（UI 用） |
| **compact** | 压缩相关 | 上下文压缩摘要 |

**与 Agent 工具 Fork 的区别**：

| 维度 | Agent Fork | forkedAgent |
|------|-----------|-------------|
| **触发方** | 主模型主动 | 系统 hooks 自动 |
| **用户可见** | 是（UI 显示、transcript） | 否（skipTranscript） |
| **工具集** | 按定义过滤 | 受限工具集 |
| **Prompt Cache** | 共享父级 | 共享 cacheSafeParams |
| **目的** | 完成用户任务 | 后台维护（记忆、压缩） |

---

## 九、完整编排示例

### 场景：用户要求实现一个完整功能

```
用户: "给博客系统添加评论功能，要支持回复和点赞"
```

**Phase 1: 规划**

```
主模型 → EnterPlanMode()
  → [进入规划阶段，工具限制为只读]

主模型 → Agent({
  subagent_type: "Explore",
  prompt: "搜索博客系统的数据模型、API 路由、前端组件结构。
          特别关注文章详情页、已有的数据库 schema、
          API 路由约定。报告架构概况。"
})
Explore Agent: "数据库用 Prisma，路由在 src/api/，
               前端用 React，文章模型在 prisma/schema.prisma..."

主模型 → 写出 plan 文件 → ExitPlanMode()
用户: "方案没问题，开始实现"
```

**Phase 2: 并行实现**

```
// 单条消息中并行 fork
主模型 → Agent({
  name: "db-schema",
  description: "Add comment schema",
  isolation: "worktree",
  prompt: "在 prisma/schema.prisma 中添加 Comment 模型，
          支持嵌套回复（parentId 自引用）和点赞计数。
          运行 prisma generate。"
})

主模型 → Agent({
  name: "api-routes",
  description: "Add comment API",
  prompt: "在 src/api/ 下创建评论 CRUD 端点：
          POST /comments（创建）、
          POST /comments/:id/reply（回复）、
          POST /comments/:id/like（点赞）、
          GET /posts/:id/comments（分页获取）。"
})

[等待两个 fork 完成通知]
```

**Phase 3: 前端实现**

```
主模型 → Agent({
  name: "frontend",
  description: "Add comment UI",
  prompt: "在文章详情页添加评论组件。
          包含：评论列表（支持嵌套）、
          发表评论表单、回复按钮、点赞按钮。
          使用已有的 UI 组件库风格。"
})
```

**Phase 4: 验证**

```
主模型 → Agent({
  subagent_type: "verification",
  prompt: "任务：给博客系统添加评论功能（创建、回复、点赞）。
          改动文件：prisma/schema.prisma, src/api/comments.ts,
          src/components/Comments.tsx 等 8 个文件。
          方法：Prisma 模型 + REST API + React 组件。"
})

Verification Agent:
  ✓ Build 通过
  ✓ 测试通过
  ✓ 评论创建 200
  ✓ 回复创建 201
  ✗ [对抗性] 并发点赞：race condition，点赞计数少计
  VERDICT: FAIL

主模型: 修复点赞的原子操作
  → Agent({ resume: "verify-...", prompt: "已改为 Prisma atomic increment..." })
  → VERDICT: PASS

主模型: 抽检命令确认 → 向用户报告完成
```

---

## 十、代码控制 vs 提示词控制：完整对照

Claude Code 的 subagent 系统中，有些行为由 TypeScript 代码在运行时强制执行（"硬约束"），有些行为仅通过提示词引导模型自觉遵守（"软约束"）。二者的边界如下。

### 10.1 代码硬控制（运行时强制，模型无法绕过）

**工具池过滤**

这是最核心的代码控制手段——子代理能看到哪些工具完全由代码决定，模型没有任何绕过方式。

| 控制点 | 源码位置 | 机制 |
|--------|----------|------|
| 全局禁用列表 | `constants/tools.ts` → `ALL_AGENT_DISALLOWED_TOOLS` | `Set` 硬编码，所有 subagent 禁用 TaskOutput/EnterPlanMode/AskUserQuestion 等 |
| 异步 agent 工具白名单 | `constants/tools.ts` → `ASYNC_AGENT_ALLOWED_TOOLS` | `Set` 硬编码，异步 agent 只能用白名单内的工具 |
| 三层过滤管线 | `agentToolUtils.ts` → `filterToolsForAgent()` + `resolveAgentTools()` | `tools` 白名单 → `disallowedTools` 黑名单 → 全局禁用 → 异步过滤 |
| 自定义 agent 额外禁用 | `constants/tools.ts` → `CUSTOM_AGENT_DISALLOWED_TOOLS` | 非 built-in 来源的 agent 被额外限制 |

```typescript
// agentToolUtils.ts — 代码强制过滤，模型看不到被过滤掉的工具
if (ALL_AGENT_DISALLOWED_TOOLS.has(tool.name)) {
  return false
}
if (!isBuiltIn && CUSTOM_AGENT_DISALLOWED_TOOLS.has(tool.name)) {
  return false
}
if (isAsync && !ASYNC_AGENT_ALLOWED_TOOLS.has(tool.name)) {
  return false
}
```

> **场景：Explore Agent 发现 bug 想顺手修复**
>
> 用户说「帮我理解 auth 模块的登录流程」，主模型启动一个 Explore Agent。Agent 在阅读代码时发现了一个空指针问题，想调用 `FileEditTool` 顺手修一下：
>
> ```
> Explore Agent → 调用 FileEditTool
>                   ↓
>               工具池中找不到（已被代码移除）→ 失败
> ```
>
> 即使模型完全无视 system prompt 中 "READ-ONLY MODE" 的声明，也无法修改文件——工具根本不存在于它的工具池中。提示词引导意图，代码强制执行。

**模型选择与 Thinking**

| 控制点 | 源码位置 | 机制 |
|--------|----------|------|
| 模型解析 | `runAgent.ts` → `getAgentModel()` | 根据 `model` 字段（inherit/haiku/sonnet）映射真实模型名 |
| Thinking 禁用 | `runAgent.ts` → `agentOptions.thinkingConfig` | 同步 subagent 的 thinking 被代码强制设为 `{ type: 'disabled' }`，fork 继承父级 |
| Effort 覆盖 | `runAgent.ts` → `agentGetAppState()` | `agentDefinition.effort` 覆盖 `state.effortValue` |

```typescript
// runAgent.ts — 同步 subagent 禁用 thinking（省 token），这不是提示词建议
thinkingConfig: useExactTools
  ? toolUseContext.options.thinkingConfig
  : { type: 'disabled' as const },
```

**权限与安全**

| 控制点 | 源码位置 | 机制 |
|--------|----------|------|
| 权限模式覆盖 | `runAgent.ts` → `agentGetAppState()` | 代码强制设置 `permissionMode`，同时不覆盖 `bypassPermissions`/`acceptEdits` |
| 异步静默权限 | `runAgent.ts` → `shouldAvoidPermissionPrompts` | 异步 agent 自动设为不弹权限提示（代码硬设） |
| 工具权限隔离 | `runAgent.ts` → `allowedTools` | 用 `alwaysAllowRules.session` 限制子 agent 可免确认的工具范围 |
| MCP 安全策略 | `runAgent.ts` → `initializeAgentMcpServers()` | 当 `isRestrictedToPluginOnly('mcp')` 时，代码阻止非信任来源的 agent 使用 MCP |
| Hook 权限 | `runAgent.ts` → `hooksAllowedForThisAgent` | 非信任来源 agent 的 frontmatter hooks 被代码阻止注册 |

> **场景：后台 Agent 静默运行，不弹权限确认**
>
> 用户说「后台帮我跑全量测试并整理报告」，主模型用 `async: true` 启动后台 Agent。Agent 执行 `BashTool`（运行 `npm test`），正常流程会弹出「允许执行吗？」的确认弹窗，但：
>
> ```
> 后台 Agent → 执行 BashTool
>                ↓
>            shouldAvoidPermissionPrompts = true（代码硬设）
>                ↓
>            自动批准，用户不会被打断
> ```
>
> 这是纯代码控制——后台 Agent 不能打断前台工作流，不依赖模型配合；反过来，同步 Agent 也无法让自己变成静默模式。

**上下文隔离**

| 控制点 | 源码位置 | 机制 |
|--------|----------|------|
| AbortController 隔离 | `runAgent.ts` | 异步 agent → `new AbortController()`（独立），同步 → 共享父级 |
| AppState 隔离 | `runAgent.ts` → `createSubagentContext()` | `shareSetAppState: !isAsync` —— 异步 agent 不共享状态 |
| 文件状态隔离 | `runAgent.ts` → `agentReadFileState` | fork → 克隆父级，spawn → 全新空缓存 |
| 省略 CLAUDE.md | `runAgent.ts` → `shouldOmitClaudeMd` | Explore/Plan 的 `omitClaudeMd: true`，代码跳过注入 |
| 省略 gitStatus | `runAgent.ts` → `resolvedSystemContext` | Explore/Plan 代码移除 `gitStatus`，节省 token |
| maxTurns 限制 | `runAgent.ts` → `query({ maxTurns })` | 代码传入 `maxTurns`（如 fork 为 200），超限则 break |

**Fork 递归防护**

| 控制点 | 源码位置 | 机制 |
|--------|----------|------|
| Fork 递归检测 | `forkSubagent.ts` → `isInForkChild()` | 扫描消息中是否存在 `<FORK_BOILERPLATE_TAG>`，存在则拒绝再次 fork |
| Coordinator 互斥 | `forkSubagent.ts` → `isForkSubagentEnabled()` | Coordinator 模式下代码返回 false，直接禁用 fork |

```typescript
// forkSubagent.ts — 代码级递归防护
export function isInForkChild(messages: MessageType[]): boolean {
  return messages.some(m => {
    if (m.type !== 'user') return false
    const content = m.message.content
    return content.some(
      block => block.type === 'text' &&
        block.text.includes(`<${FORK_BOILERPLATE_TAG}>`),
    )
  })
}
```

> **场景：Fork 子 Agent 试图再次 fork**
>
> 主模型 fork 了一个子 Agent 处理模块 A 的重构。子 Agent 发现任务复杂，想再 fork 一层：
>
> ```
> 主模型 → fork 子 Agent（消息中注入 <FORK_BOILERPLATE_TAG>）
>                ↓
>          子 Agent 想再 fork
>                ↓
>          isInForkChild() 扫描消息 → 发现标记 → 拒绝
>                ↓
>          子 Agent 只能自己完成任务
> ```
>
> 提示词说「你是 fork 的 worker，不要再 fork」让模型知道不该这样做；代码层的标记检测确保即使模型忽略提示词也无法递归，防止资源无限消耗。

**生命周期管理**

| 控制点 | 源码位置 | 机制 |
|--------|----------|------|
| Transcript 记录 | `runAgent.ts` → `recordSidechainTranscript()` | 每条消息持久化到磁盘 |
| Hook 注册/清理 | `runAgent.ts` → `registerFrontmatterHooks()` / `clearSessionHooks()` | 代码在 agent 启动时注册、结束时清理 |
| MCP 连接清理 | `runAgent.ts` → `mcpCleanup()` | finally 块中清理 agent 特有的 MCP 连接 |
| 进程清理 | `runAgent.ts` → `killShellTasksForAgent()` | 清理 agent 启动的所有后台 shell 任务 |
| 技能预加载 | `runAgent.ts` → skillsToPreload 循环 | 代码强制将 frontmatter 中声明的 skills 以 user message 注入 |

### 10.2 提示词软控制（引导模型行为，理论上可被忽略）

**何时选择哪种 Agent**

主模型如何决定用 Explore、Plan、还是 Fork，完全由提示词引导：

| 提示词机制 | 源码位置 | 作用 |
|-----------|----------|------|
| `whenToUse` 字段 | 各 agent 定义 | 注入到 Agent 工具描述中，告诉主模型什么场景用什么 agent |
| "When to fork" 章节 | `prompt.ts` → `whenToForkSection` | 教主模型区分 fork vs spawn 的场景 |
| "When NOT to use" 章节 | `prompt.ts` → `whenNotToUseSection` | 告诉主模型简单搜索不需要 spawn agent |
| "Writing the prompt" 章节 | `prompt.ts` → `writingThePromptSection` | 教主模型如何写好给 agent 的 prompt |
| Fork 示例 | `prompt.ts` → `forkExamples` | 提供 fork 用法的 few-shot 示例 |
| 使用说明 | `prompt.ts` → Usage notes 列表 | 告诉主模型：包含描述、agent 结果不可见给用户、并行技巧等 |

```typescript
// prompt.ts 中写给主模型的提示词（模型可以不遵守）
"Fork yourself (omit subagent_type) when the intermediate tool output 
isn't worth keeping in your context..."
"Brief the agent like a smart colleague who just walked into the room..."
"Never delegate understanding."
```

> **场景 A：用户让 Claude 重构 10 个文件，主模型决定是否 fork**
>
> 用户说「把项目中所有 var 替换成 const/let」，涉及 10 个文件。主模型读到提示词中的 `whenToForkSection` 后自主判断：fork 3 个子 Agent 并行处理，每个负责 3-4 个文件。但代码不会强制模型 fork——如果模型选择自己逐个处理，结果一样正确，只是更慢。这类编排策略完全是提示词引导，代码不干预。
>
> **场景 B：用户问「数据库连接池怎么配的？」，主模型选 Agent 类型**
>
> 主模型读到各 Agent 的 `whenToUse` 描述后判断这是"理解代码"场景，选择 Explore Agent（只读、轻量）。选择本身靠提示词引导（可能选错），但选择之后的能力边界由代码硬控制——选了 Explore 就一定没有写工具，不可能"意外"改代码。**策略层软，执行层硬。**

**Agent 内部行为约束**

子 agent 的行为本身主要靠 system prompt 引导：

| 提示词 | Agent | 约束内容 |
|--------|-------|----------|
| "READ-ONLY MODE" 段落 | Explore / Plan | 告诉 agent 不能创建/修改/删除文件 |
| "DO NOT MODIFY THE PROJECT" 段落 | Verification | 告诉 agent 不能修改项目文件（但可写 /tmp） |
| 验证策略 | Verification | 按变更类型（前端/后端/CLI/基础设施…）的检查方法 |
| 反合理化清单 | Verification | "The code looks correct" 不是验证——运行它 |
| 对抗性探针清单 | Verification | 并发/边界值/幂等/孤立操作 |
| 输出格式要求 | Verification | `### Check` + Command run + Output observed + `VERDICT:` |
| Fork 子进程规则 | Fork 子代理 | "你是 fork 的 worker，不要再 fork" 等 10 条规则 |
| Fork 输出格式 | Fork 子代理 | Scope → Result → Key files → Files changed → Issues |
| `criticalSystemReminder` | Verification | 每轮注入："你不能编辑文件，必须以 VERDICT 结尾" |

这些是"软约束"——Explore 的 system prompt 说"你不能修改文件"，但 Explore 看到的工具池里 FileEdit/FileWrite 已经被代码移除了（见 9.1），所以真正的保障是代码层。

> **场景：Verification Agent 发现 bug，想用 Bash 偷偷修**
>
> 主模型完成代码修改后启动 Verification Agent 检验。Agent 发现一个明显的 bug：
>
> ```
> Verification Agent 发现 bug
>          ↓
>      FileEditTool 已被移除 → 不能直接编辑
>          ↓
>      但 BashTool 还在！Agent 想执行：
>        echo "const fixed = true;" >> src/config.ts
>          ↓
>      提示词说 "DO NOT MODIFY THE PROJECT"
>          ↓
>      模型遵守了 → 没有执行（但如果模型忽略提示词，Bash 可以成功写文件）
> ```
>
> 这是一个**有缺口的双重保险**：代码堵住了"正门"（FileEdit/FileWrite），但 Bash 这个"侧门"只靠提示词把守。实际风险较低——模型通常遵守提示词，且 Bash 执行本身还有独立的权限检查链（规则匹配 → 分类器 → 用户确认）。

**关键洞察**：提示词和代码形成**双重保险**——

```
提示词："你是只读的，不能修改文件"     ← 引导意图
代码：disallowedTools 移除了写入工具    ← 强制执行
```

| Agent | 提示词说"只读" | 代码移除写工具 | 真正的只读来源 |
|-------|---------------|--------------|--------------|
| Explore | 是 | 是（FileEdit/FileWrite/NotebookEdit） | 代码 |
| Plan | 是 | 是（同上） | 代码 |
| Verification | 是 | 是（同上） | 代码 |
| Fork 子代理 | 部分（"不要再 fork"） | 部分（`isInForkChild()` 检测阻止递归 fork） | 混合 |

**主模型的编排决策**

以下决策完全由提示词引导，代码不干预：

- 是否要 spawn 一个 agent（vs 自己做）
- 选择 Explore 还是 general-purpose
- 选择 spawn 还是 fork
- 是否要 resume 之前的 agent
- 是否要并行发起多个 agent
- 给 agent 写什么 prompt
- 何时向用户报告 agent 结果
- 是否在实现后触发 verification

### 10.3 总结：控制边界一览

```
┌─────────────────────────────────────────────────────────────────┐
│                     代码硬控制（不可绕过）                         │
│                                                                 │
│  工具池过滤    模型选择     权限模式     AbortController           │
│  Thinking      maxTurns    MCP 安全    文件状态隔离               │
│  Fork 递归防护  生命周期清理  CLAUDE.md 省略  gitStatus 省略       │
│  Hook 注册/清理  技能预加载   Transcript 记录                     │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                    提示词软控制（引导性）                          │
│                                                                 │
│  何时用哪种 agent    spawn vs fork 选择    prompt 写法规范         │
│  agent 内部行为规范  验证策略/输出格式       fork 子进程规则        │
│  "只读"行为宣告      对抗性探针清单          反合理化清单          │
│  并行 fork 时机      resume 时机              编排模式选择        │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                      双重保险区域                                │
│                                                                 │
│  "只读"约束 = 提示词声明 + 代码移除写工具（真正保障在代码）         │
│  "不要递归 fork" = 提示词告知 + isInForkChild() 代码检测           │
│  criticalSystemReminder = 提示词每轮注入（但验证行为靠提示词）      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**设计哲学**：Claude Code 对安全关键行为（工具访问、权限、隔离）使用代码强制，对需要灵活性的行为（编排策略、检查方法、输出风格）使用提示词引导，对高风险+需要 LLM 配合的行为（只读约束、递归防护）采用双重保险。一句话总结——**策略层可以软，但执行层必须硬**。

---

## 十一、核心源码文件索引

| 文件 | 职责 |
|------|------|
| `src/tools/AgentTool/AgentTool.tsx` | Agent 工具定义、输入 schema、sync/async 分支 |
| `src/tools/AgentTool/runAgent.ts` | Agent 执行核心（上下文构建、query loop、清理） |
| `src/tools/AgentTool/resumeAgent.ts` | Agent 恢复机制（transcript 读取、消息清洗） |
| `src/tools/AgentTool/forkSubagent.ts` | Fork 模式定义（FORK_AGENT、消息构建） |
| `src/tools/AgentTool/prompt.ts` | Agent 工具提示词（使用说明、示例） |
| `src/tools/AgentTool/loadAgentsDir.ts` | Agent 定义类型、多来源加载与合并 |
| `src/tools/AgentTool/builtInAgents.ts` | 内置 Agent 注册逻辑 |
| `src/tools/AgentTool/agentToolUtils.ts` | 工具过滤、异步生命周期、进度追踪 |
| `src/tools/AgentTool/agentMemory.ts` | Agent 持久记忆（三级目录） |
| `src/tools/AgentTool/agentMemorySnapshot.ts` | Agent 记忆快照同步 |
| `src/tools/AgentTool/built-in/exploreAgent.ts` | Explore Agent 定义 |
| `src/tools/AgentTool/built-in/planAgent.ts` | Plan Agent 定义 |
| `src/tools/AgentTool/built-in/verificationAgent.ts` | Verification Agent 定义 |
| `src/tools/AgentTool/built-in/generalPurposeAgent.ts` | General Purpose Agent 定义 |
| `src/tools/AgentTool/built-in/claudeCodeGuideAgent.ts` | Claude Code Guide Agent 定义 |
| `src/tools/AgentTool/built-in/statuslineSetup.ts` | Statusline Setup Agent 定义 |
| `src/utils/forkedAgent.ts` | 内部静默 fork（CacheSafeParams、createSubagentContext） |
| `src/coordinator/coordinatorMode.ts` | Coordinator 模式（系统提示、用户上下文） |
| `src/tools/shared/spawnMultiAgent.ts` | Teammate spawn（多进程/in-process） |
