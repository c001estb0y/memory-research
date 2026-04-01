# Claude Code Memory 系统设计

基于 Claude Code 开源快照源码的深度分析。

---

## 一、Memory 分层架构

Claude Code 的 Memory 系统由五个层级组成，各自有独立的存储位置、生命周期和用途：

| 层级 | 存储位置 | 作用域 | 生命周期 |
|------|----------|--------|----------|
| **Managed Memory** | `/etc/claude-code/CLAUDE.md` | 全局（所有用户） | 管理员管理 |
| **User Memory** | `~/.claude/CLAUDE.md` | 个人（所有项目） | 用户手动管理 |
| **Project Memory** | `CLAUDE.md` / `.claude/CLAUDE.md` / `.claude/rules/*.md` | 项目级（可提交到仓库） | 随仓库版本控制 |
| **Local Memory** | `CLAUDE.local.md` | 个人+项目级（不提交仓库） | 用户手动管理 |
| **Auto Memory** | `~/.claude/projects/<slug>/memory/` | 个人+项目级 | 自动读写+整理 |
| **Team Memory** | `~/.claude/projects/<slug>/memory/team/` | 团队+项目级 | 自动同步到服务端 |
| **Session Memory** | `<projectDir>/<sessionId>/session-memory/summary.md` | 当前会话 | 会话级 |
| **Agent Memory** | 按 agent scope 目录 | 按 agent 类型 | agent 生命周期 |

### 1.1 指令型 Memory（CLAUDE.md 体系）

这是 Claude Code 最早的 memory 形式，本质是「规则文件」——告诉模型如何行事的静态指令。

**加载顺序与优先级**（后加载 = 更高优先级）：

1. **Managed** — `/etc/claude-code/CLAUDE.md`，全局指令
2. **User** — `~/.claude/CLAUDE.md`，个人全局指令
3. **Project** — 项目根目录的 `CLAUDE.md`、`.claude/CLAUDE.md`、`.claude/rules/*.md`
4. **Local** — 项目根目录的 `CLAUDE.local.md`，私有项目指令

这些文件通过 `claudemd.ts` 的发现和加载逻辑被注入到系统提示词的 userContext 中。

特点：
- 手动维护，内容是开发规则、编码规范、项目约定等
- 支持 `@include` 引用其他文件
- 可以沿目录树向上逐级查找（从 CWD 到项目根）
- 随 Git 仓库提交和版本控制

### 1.2 Auto Memory（自动记忆）

这是 Claude Code 的新一代 memory 系统，核心设计是「基于文件的持久化记忆」，由模型自主读写。

**存储路径**：

```
~/.claude/projects/<sanitized-git-root>/memory/
├── MEMORY.md          # 索引文件（自动加载到 prompt）
├── user_role.md       # topic 文件：用户角色
├── feedback_testing.md # topic 文件：测试反馈
├── project_auth.md    # topic 文件：项目认证
└── team/              # 团队记忆目录（可选）
    ├── MEMORY.md
    └── ...
```

路径解析优先级：
1. `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 环境变量（完整路径覆盖）
2. `settings.json` 中的 `autoMemoryDirectory`（仅信任源）
3. `~/.claude/projects/<sanitized-git-root>/memory/`（默认）

Git Worktree 共享：同一仓库的所有 worktree 共享同一个 memory 目录（通过 `findCanonicalGitRoot` 解析）。

---

## 二、Memory 类型分类

Auto Memory 采用四类型分类法，约束模型只存储**不可从当前项目状态推导出的信息**：

### 2.1 user（用户类型）

- **存什么**：用户的角色、目标、职责、知识背景、偏好
- **何时存**：了解到用户角色、偏好、知识背景等信息时
- **如何用**：据此调整协作方式（给高级工程师和初学者不同的回答风格）
- **示例**：「用户是数据科学家，当前关注可观测性/日志系统」

### 2.2 feedback（反馈类型）

- **存什么**：用户对工作方式的指导——包括纠正（避免做什么）和确认（继续做什么）
- **何时存**：用户纠正做法 OR 确认非显而易见的做法成功时
- **格式**：规则本身 + **Why:** + **How to apply:**
- **示例**：「集成测试必须用真实数据库，不能用 mock。原因：之前 mock/prod 差异导致线上迁移失败」

### 2.3 project（项目类型）

- **存什么**：不可从代码或 Git 历史推导的项目上下文（进行中的工作、目标、事件、决策动机）
- **何时存**：了解谁在做什么、为什么、截止何时
- **格式**：事实/决策 + **Why:** + **How to apply:**
- **注意**：相对日期必须转换为绝对日期（如「周四」→「2026-03-05」）
- **示例**：「2026-03-05 起冻结非关键合并 — 移动端团队要切发布分支」

### 2.4 reference（引用类型）

- **存什么**：外部系统中信息位置的指针
- **何时存**：了解到外部资源及其用途时
- **示例**：「pipeline bugs 在 Linear 项目"INGEST"中追踪」

### 2.5 不应该存的内容

系统提示词中明确约束以下内容不应存为 memory：

- 代码模式、架构、文件结构（可通过 grep/git 获取）
- Git 历史、最近变更（`git log`/`git blame` 是权威来源）
- 调试方案（修复已在代码中，context 在 commit message 中）
- 已在 CLAUDE.md 中记录的内容
- 临时任务细节、当前会话状态

即使用户明确要求保存 PR 列表等也要询问「其中什么是令人意外或非显而易见的」。

---

## 三、Memory 存储格式

### 3.1 MEMORY.md（索引文件）

MEMORY.md 是 auto memory 目录的入口文件，作为索引被加载到系统提示词中。

**限制**：
- 最大 200 行（`MAX_ENTRYPOINT_LINES`）
- 最大 25,000 字节（`MAX_ENTRYPOINT_BYTES`）
- 超限时截断并附加警告

**格式约定**：
- 每条索引一行，不超过 ~150 字符
- 格式：`- [Title](file.md) — one-line hook`
- 不含 frontmatter
- 不直接写 memory 内容（只写指向 topic 文件的链接）

### 3.2 Topic 文件（记忆文件）

每个记忆条目是一个带 YAML frontmatter 的 Markdown 文件：

```markdown
---
name: {{记忆名称}}
description: {{一行描述 — 用于未来会话判断相关性}}
type: {{user, feedback, project, reference}}
---

{{记忆内容 — feedback/project 类型结构为：规则/事实 + Why: + How to apply:}}
```

**命名约定**：按语义主题命名（如 `user_role.md`、`feedback_testing.md`），而非按时间。

### 3.3 KAIROS 日志模式

长生命周期的 Assistant 会话（Proactive/KAIROS 模式）使用不同的写入策略：

- 不维护 MEMORY.md 索引，改为追加日志到 `logs/YYYY/MM/YYYY-MM-DD.md`
- 日志条目为带时间戳的短条目（append-only）
- MEMORY.md 由每晚的 `/dream` 流程从日志中蒸馏生成

---

## 四、Memory 写入机制

### 4.1 主模型直接写入

当用户明确要求「记住这个」或模型判断需要保存信息时，主模型使用 FileWrite/FileEdit 工具直接写入 auto memory 目录。

系统提示词中的指导：

```
If the user explicitly asks you to remember something, save it immediately 
as whichever type fits best. If they ask you to forget something, find and 
remove the relevant entry.
```

保存是两步过程：
1. 将记忆写入独立的 topic 文件
2. 在 MEMORY.md 中添加索引指针

### 4.2 Extract Memories（后台提取代理）

在每个交互回合结束时（stop hooks 阶段），系统 fork 一个后台子代理来提取记忆：

**触发条件**：
- `EXTRACT_MEMORIES` feature flag 开启
- `isExtractModeActive()` 返回 true
- 非交互式会话也可能触发（通过 GrowthBook 配置）

**互斥机制**：`hasMemoryWritesSince()` 检查主模型是否已经在当前轮次写过 auto memory，如果写过则跳过提取（避免重复）。

**执行方式**：
- 通过 `runForkedAgent` fork 子代理执行
- `querySource: 'extract_memories'`
- `maxTurns: 5`（最多 5 轮工具调用）
- 使用受限工具集（`createAutoMemCanUseTool`），只允许写 auto memory 目录
- 预注入 `scanMemoryFiles` 结果（避免子代理花一轮执行 `ls`）
- 不写 transcript（`skipTranscript: true`）

### 4.3 Session Memory（会话记忆）

Session Memory 是当前会话内的笔记系统，记录在 `summary.md` 中。

**触发条件**：通过 `postSamplingHook` 注册，在模型采样完成后异步执行。

**更新阈值**：基于 token 消耗量和工具调用次数（可配置的 `SessionMemoryConfig`）。

**写入流程**：
1. `shouldExtractMemory()` 检查是否满足更新条件
2. `runForkedAgent` fork 子代理
3. 使用 `buildSessionMemoryUpdatePrompt` 构造更新提示
4. 写入 `<projectDir>/<sessionId>/session-memory/summary.md`

**与压缩的关系**：Session Memory 可被 `sessionMemoryCompact.ts` 作为轻量级压缩方案使用——用 session memory 替代长上下文的全量摘要。

### 4.4 Team Memory Sync（团队记忆同步）

Team Memory 实现了跨团队成员的记忆共享：

**存储**：`~/.claude/projects/<slug>/memory/team/` 目录

**同步语义**：
- **Pull**：服务端覆盖本地（server wins per-key）
- **Push**：只上传 content hash 不同的 key（增量上传）
- 通过 OAuth 认证，按 Git remote hash 标识仓库

**安全措施**：
- `teamMemSecretGuard.ts` — 写入前进行密钥扫描
- `secretScanner.ts` — 检测并阻止包含 API key、密码等敏感信息的写入
- 文件变更监视 + 节流同步（`watcher.ts`）

### 4.5 Auto Dream（自动记忆整理）

Auto Dream 是记忆的后台整理/蒸馏服务：

**触发条件**：
- 回合结束时与 Extract Memories 一起在 stop hooks 中 fire-and-forget
- 需满足时间门槛 + 会话数量条件
- `consolidationLock` 防止并发执行

**执行方式**：
- `buildConsolidationPrompt` 构造整理提示
- `runForkedAgent` fork 子代理执行
- `querySource: 'auto_dream'`
- 合并/去重/整理现有 memory 文件

---

## 五、Memory 检索与加载机制

### 5.1 系统提示词注入

`loadMemoryPrompt()` 在系统提示词构建时被调用，负责将 memory 指令和 MEMORY.md 内容注入 prompt：

```
系统提示词
├── ... 其他段落 ...
├── # auto memory
│   ├── 记忆目录说明 + 类型分类 + 写入指导
│   ├── 何时访问记忆 + 信任召回指导
│   └── ## MEMORY.md
│       └── [MEMORY.md 内容，截断到 200 行/25KB]
└── ... 其他段落 ...
```

### 5.2 相关记忆检索（findRelevantMemories）

当用户发送消息时，系统会从 memory 目录中检索相关的 topic 文件并作为 attachment 注入对话：

**检索流程**：

1. `scanMemoryFiles(memoryDir)` — 递归扫描目录下所有 `.md` 文件（排除 MEMORY.md）
2. 读取每个文件的 frontmatter（前 30 行），提取 `name`、`description`、`type`
3. 按修改时间倒序排列，取前 200 个
4. `formatMemoryManifest()` — 格式化为文本清单
5. 通过 `sideQuery` 调用 Sonnet 模型，从清单中选择最相关的文件（最多 5 个）
6. 返回选中文件的路径和修改时间

**选择器提示词**：

```
You are selecting memories that will be useful to Claude Code as it 
processes a user's query. Return a list of filenames for the memories 
that will clearly be useful (up to 5). Only include memories that you 
are certain will be helpful. If you are unsure, do not include.
```

**优化**：
- `alreadySurfaced` 参数排除已展示过的文件，避免重复
- `recentTools` 参数排除正在使用的工具的文档型记忆（但保留警告/已知问题类）
- 使用 JSON schema 输出格式确保结构化返回

### 5.3 记忆预取

在 `query.ts` 的 agent loop 中，记忆检索是预取式的：

1. 循环入口处启动 `startRelevantMemoryPrefetch`（异步不阻塞）
2. 模型流式输出和工具执行期间，预取在后台进行
3. 工具执行完毕后，检查预取是否完成：
   - 完成 → 过滤已读文件（通过 `readFileState` 去重），作为 attachment 注入
   - 未完成 → 跳过，下一轮迭代再试
4. 预取结果在整个回合期间有效（不随迭代重置）

### 5.4 搜索过往上下文

系统提示词中还指导模型如何搜索过往记忆和 transcript：

```
## Searching past context

When looking for past context:
1. Search topic files in your memory directory:
   Grep with pattern="<search term>" path="<memoryDir>" glob="*.md"
2. Session transcript logs (last resort — large files, slow):
   Grep with pattern="<search term>" path="<projectDir>/" glob="*.jsonl"
```

### 5.5 记忆信任与验证

系统提示词包含完善的「记忆信任」指导：

**记忆可能过时**：
- 记忆记录的是「某个时间点的事实」，不是当前事实
- 基于记忆行动前应验证当前状态
- 如果记忆与当前信息冲突，信任当前观察，并更新/删除过时记忆

**推荐前验证**：
- 记忆中的文件路径 → 检查文件是否存在
- 记忆中的函数/标志 → grep 搜索确认
- 用户即将基于建议行动 → 先验证
- 「记忆说 X 存在」≠「X 现在存在」

**忽略指令**：如果用户说「忽略记忆」，当 MEMORY.md 为空来处理，不引用、不比较、不提及记忆内容。

---

## 六、Memory 生命周期管理

### 6.1 启用/禁用控制

Auto Memory 的开关优先级链：

1. `CLAUDE_CODE_DISABLE_AUTO_MEMORY` 环境变量（1/true → 关闭，0/false → 开启）
2. `CLAUDE_CODE_SIMPLE`（`--bare` 模式）→ 关闭
3. CCR 无持久存储（无 `CLAUDE_CODE_REMOTE_MEMORY_DIR`）→ 关闭
4. `settings.json` 中的 `autoMemoryEnabled`
5. 默认：开启

### 6.2 更新与整理

| 操作 | 触发方 | 时机 |
|------|--------|------|
| **直接写入** | 主模型 | 用户明确要求 / 模型判断需要 |
| **后台提取** | extractMemories 子代理 | 每轮结束，stop hooks |
| **会话记忆更新** | SessionMemory 子代理 | postSampling hook，达到阈值 |
| **记忆整理** | autoDream 子代理 | 回合结束，满足时间+会话数条件 |
| **团队同步** | teamMemorySync | 文件变更监视 + 节流 |

### 6.3 安全与权限

- **路径验证**：`validateMemoryPath()` 拒绝相对路径、根路径、UNC 路径、含 null 字节的路径
- **写入范围限制**：extractMemories 子代理通过 `createAutoMemCanUseTool` 限制只能写 auto memory 目录
- **团队记忆密钥扫描**：写入 team memory 前扫描 API key、密码等敏感信息
- **projectSettings 安全**：`.claude/settings.json` 中的 `autoMemoryDirectory` 不被信任（防止恶意仓库重定向到 `~/.ssh` 等敏感目录）

---

## 七、与 OpenClaw Memory 系统对比

| 维度 | Claude Code | OpenClaw |
|------|-------------|----------|
| **存储格式** | Markdown 文件 + YAML frontmatter | Markdown 文件 |
| **索引机制** | MEMORY.md 文本索引 + Sonnet 模型选择 | sqlite-vec 向量 + FTS5 全文 |
| **写入方式** | 主模型 + extractMemories 子代理 | LLM 直接写 Markdown |
| **检索方式** | Sonnet sideQuery 从 manifest 选（最多 5 个） | 向量 + BM25 混合检索 |
| **类型分类** | 四类型分类（user/feedback/project/reference） | 无固定分类 |
| **压缩前保存** | extractMemories 后台提取 | Memory Flush（静默轮写日记） |
| **团队共享** | Team Memory + 服务端同步 | 无 |
| **整理/蒸馏** | autoDream 后台整理 | 无自动蒸馏 |
| **嵌入/向量** | 无向量索引 | sqlite-vec / JS 余弦相似度 |
| **事实来源** | Markdown 文件（类似 OpenClaw） | Markdown 文件 |
| **跨项目记忆** | 每个 Git 仓库独立 | 每个 Agent 独立 |

### 关键差异分析

**检索方式**：Claude Code 使用 Sonnet 模型做语义选择（每次最多 5 个文件），依赖 description frontmatter 的质量；OpenClaw 使用向量 + BM25 混合检索，在大量记忆时可能更精确。

**记忆提取**：Claude Code 的 extractMemories 是后台子代理，可以做多轮工具调用（最多 5 轮）来提取和保存记忆；OpenClaw 的 Memory Flush 是压缩前的一轮静默运行，让模型将信息追加到日记文件。

**类型系统**：Claude Code 有严格的四类型分类和详细的「什么不该存」指导，通过提示词工程约束模型行为；OpenClaw 没有固定分类。

**团队协作**：Claude Code 原生支持 Team Memory 和服务端同步；OpenClaw 不支持。

**整理能力**：Claude Code 的 autoDream 可以自动整理和合并记忆；OpenClaw 依赖用户手动编辑 Markdown。

---

## 八、核心源码文件索引

| 文件 | 职责 |
|------|------|
| `src/memdir/paths.ts` | Memory 路径解析（开关、目录、安全校验） |
| `src/memdir/memdir.ts` | Memory prompt 构建（MEMORY.md 加载、指令生成、KAIROS 日志模式） |
| `src/memdir/memoryTypes.ts` | 四类型分类定义 + 提示词段落 |
| `src/memdir/memoryScan.ts` | 目录扫描 + frontmatter 解析 |
| `src/memdir/findRelevantMemories.ts` | Sonnet sideQuery 检索相关记忆 |
| `src/memdir/memoryAge.ts` | 记忆新鲜度计算 |
| `src/memdir/teamMemPaths.ts` | Team Memory 路径与校验 |
| `src/memdir/teamMemPrompts.ts` | Team + Auto 联合提示词构建 |
| `src/utils/claudemd.ts` | CLAUDE.md 体系发现与加载 |
| `src/utils/memoryFileDetection.ts` | 区分不同类型的 memory 文件 |
| `src/services/extractMemories/extractMemories.ts` | 后台记忆提取子代理 |
| `src/services/extractMemories/prompts.ts` | 提取提示词构建 |
| `src/services/SessionMemory/sessionMemory.ts` | 会话记忆初始化与更新 |
| `src/services/SessionMemory/prompts.ts` | 会话记忆模板与更新提示词 |
| `src/services/SessionMemory/sessionMemoryUtils.ts` | 会话记忆配置与状态管理 |
| `src/services/autoDream/autoDream.ts` | 后台记忆整理 |
| `src/services/autoDream/consolidationPrompt.ts` | 整理提示词 |
| `src/services/autoDream/consolidationLock.ts` | 整理锁与会话追踪 |
| `src/services/teamMemorySync/index.ts` | 团队记忆 HTTP 同步 |
| `src/services/teamMemorySync/watcher.ts` | 文件变更监视与节流同步 |
| `src/services/teamMemorySync/teamMemSecretGuard.ts` | 敏感信息扫描 |
| `src/services/compact/sessionMemoryCompact.ts` | 基于 Session Memory 的轻量压缩 |
| `src/tools/AgentTool/agentMemory.ts` | Agent 专属记忆管理 |
| `src/utils/attachments.ts` | 记忆预取注入到对话 |
