# ClawXMemory 架构分析与 Claude Code 借鉴关系

基于 ClawXMemory 开源仓库（OpenBMB/ClawXMemory, 2026-04-01）源码的深度分析，对比 Claude Code ExtractMemories 系统。

---

## 一、项目概述

ClawXMemory 是 THUNLP（清华大学）、中国人民大学、AI9Stars、OpenBMB、面壁智能联合研发的**多层记忆系统**，作为 OpenClaw 的 `memory` 插件运行。核心目标是解决 AI Agent 跨会话的长期上下文建模问题。

**仓库地址**：`https://github.com/OpenBMB/ClawXMemory`

**技术栈**：TypeScript, SQLite, OpenClaw Plugin SDK, LLM-driven extraction

---

## 二、核心架构：五层记忆模型

ClawXMemory 的核心创新是引入了一个**严格分层的记忆结构**，从原始对话到高层抽象逐级聚合：

```
┌─────────────────────────────────────────────────────┐
│  GlobalProfile（全局用户画像）                        │ ← 单例，持续重写
├─────────────────────────────────────────────────────┤
│  L2 ProjectIndex（项目记忆）                         │ ← 按项目维度聚合
│  L2 TimeIndex（时间线记忆）                          │ ← 按日期维度聚合
├─────────────────────────────────────────────────────┤
│  L1 WindowRecord（记忆片段）                         │ ← 一个已关闭话题的结构化摘要
├─────────────────────────────────────────────────────┤
│  L0 SessionRecord（原始对话）                        │ ← 最底层原始消息记录
└─────────────────────────────────────────────────────┘
```

### 2.1 L0 — 原始对话记录

```typescript
interface L0SessionRecord {
  l0IndexId: string;
  sessionKey: string;
  timestamp: string;
  messages: MemoryMessage[];  // { role, content }
  source: string;             // "openclaw" | "skill" | "import"
  indexed: boolean;           // 是否已被 heartbeat 消费
  createdAt: string;
}
```

L0 是最底层的原始数据，通过 `agent_end` 钩子在每轮对话结束时自动捕获。

### 2.2 L1 — 记忆片段（已关闭话题）

```typescript
interface L1WindowRecord {
  l1IndexId: string;
  sessionKey: string;
  timePeriod: string;
  startedAt: string;
  endedAt: string;
  summary: string;              // LLM 生成的话题摘要
  facts: FactCandidate[];       // 提取的事实（key-value + confidence）
  situationTimeInfo: string;    // 时间上下文
  projectTags: string[];        // 关联项目标签
  projectDetails: ProjectDetail[];  // 结构化项目信息
  l0Source: string[];           // 来源 L0 ID 列表
  createdAt: string;
}
```

L1 是系统的核心单元。当检测到**话题切换**时，当前 Active Topic Buffer 被关闭，触发 LLM 提取生成一条 L1 记录。每条 L1 包含：摘要、事实、项目信息、时间信息。

### 2.3 L2 — 宏观索引

**L2 TimeIndex**：按日期聚合 L1，每天一条时间线摘要。

```typescript
interface L2TimeIndexRecord {
  l2IndexId: string;
  dateKey: string;        // "2026-04-01"
  summary: string;        // LLM 重写的当日摘要
  l1Source: string[];
  updatedAt: string;
}
```

**L2 ProjectIndex**：按项目维度聚合 L1，每个项目一条记录。

```typescript
interface L2ProjectIndexRecord {
  l2IndexId: string;
  projectKey: string;
  projectName: string;
  summary: string;
  currentStatus: "planned" | "in_progress" | "done";
  latestProgress: string;
  l1Source: string[];
  updatedAt: string;
}
```

### 2.4 GlobalProfile — 全局用户画像

```typescript
interface GlobalProfileRecord {
  recordId: "global_profile_record";  // 硬编码单例 ID
  profileText: string;                // LLM 持续重写的用户画像
  sourceL1Ids: string[];
  updatedAt: string;
}
```

这是一个**单例记录**，每次有新 L1 产生时都会被 LLM 重写更新。

---

## 三、两大核心流程

### 3.1 后台索引管线（Heartbeat Pipeline）

入口：`src/core/pipeline/heartbeat.ts` — `HeartbeatIndexer`

```
用户对话结束
    │
    ▼
agent_end hook → 捕获 L0 → 写入 SQLite（indexed=false）
    │
    ▼
scheduleIdleIndex (debounce) → runHeartbeat
    │
    ▼
listUnindexedL0Sessions → 按 session 分组
    │
    ▼
processPendingSession:
    ├─ 无 Active Topic Buffer → 创建新 Buffer
    ├─ 有 Buffer + 话题未变 → 扩展 Buffer
    └─ 有 Buffer + 话题切换 → 关闭 Buffer → 创建新 Buffer
                │
                ▼
          closeTopicBuffer:
          ├─ extractL1FromWindow (LLM) → 生成 L1
          ├─ canonicalizeL1Projects (LLM) → 项目标识统一
          ├─ rewriteRollingProjectMemories (LLM) → 更新 L2 Project
          ├─ rewriteDailyTimeSummary (LLM) → 更新 L2 Time
          └─ rewriteGlobalProfile (LLM) → 更新 GlobalProfile
```

**话题切换检测**：通过 `extractor.judgeTopicShift()` 调用 LLM 判断当前对话是否切换了话题。输入为当前话题摘要 + 最近 8 条用户消息 + 新入消息，输出为 `{ topicChanged: boolean, topicSummary: string }`。

**触发时机**：
- 空闲 debounce（消息捕获后延迟触发）
- 定时器（`autoIndexIntervalMinutes`）
- 会话边界（sessionKey 切换）
- 手动 flush（`memory_flush` 工具）
- `before_reset` 钩子

### 3.2 模型驱动的多跳检索（Reasoning Retrieval）

入口：`src/core/retrieval/reasoning-loop.ts` — `ReasoningRetriever`

这是 ClawXMemory 的核心亮点 — 不用传统向量检索，而是让 LLM **沿着记忆层级主动推理**：

```
用户查询
    │
    ▼
before_prompt_build hook → retrieve(query)
    │
    ▼
Hop 1: 意图路由
    输入: 用户查询 + GlobalProfile 摘要
    输出: intent (time/project/fact/general) + 是否需要记忆 + lookup queries
    │
    ▼
本地构建 L2 候选列表（code-side）
    │
    ▼
Hop 2: L2 筛选
    输入: 查询 + L2 Time/Project 候选
    输出: enoughAt=l2 → 返回 | enoughAt=descend_l1 → 继续
    │
    ▼
解析候选 L1（仅从选中的 L2 来源）
    │
    ▼
Hop 3: L1/L0 筛选
    输入: 选中的 L2 + 候选 L1 + L0 头部预览
    输出: enoughAt=l1 → 返回 | enoughAt=descend_l0 → 加载完整 L0
    │
    ▼
(可选) Hop 4: 精细 L0 选择
    │
    ▼
渲染最终上下文 → prependSystemContext 注入
```

**关键设计**：
- **缓存机制**：30 秒 TTL 的 recall cache，避免短时间内重复检索
- **预算控制**：recall budget 限制每层最大检索条数
- **本地回退**：当 LLM 检索超时或失败时，用本地 BM25/FTS 搜索兜底
- **渐进式深入**：只有当上层信息不够时才向下钻取，避免过度检索

---

## 四、Dream 重构机制

入口：`src/core/review/dream-review.ts` — `DreamRewriteRunner`

Dream 是一个**离线全局重构**流程，类似于人类睡眠时的记忆整理：

1. 收集所有 L1 → 按项目 key 聚类
2. 检测重复话题、冲突信息、孤立片段
3. LLM 审查 → 生成重建计划
4. 重写 L2 Project 索引（合并/删除/更新）
5. 重写 GlobalProfile
6. 清理过期引用

**触发条件**：
- 手动触发
- 定时触发（`autoDreamIntervalMinutes`）
- 阈值门控：新 L1 数量需超过 `autoDreamMinNewL1`

---

## 五、插件集成机制

ClawXMemory 作为 OpenClaw `memory` 插件，通过以下钩子接入生命周期：

| 钩子 | 用途 | 优先级 |
|------|------|--------|
| `before_prompt_build` | 检索记忆 → 注入系统上下文 | 60 |
| `before_message_write` | 过滤启动标记、命令消息、空消息 | 80 |
| `before_tool_call` | 追踪工具调用事件（Case Trace） | 60 |
| `after_tool_call` | 记录工具结果 | — |
| `agent_end` | 捕获 L0 → 触发后台索引 | — |
| `before_reset` | flush 当前 pending → 关闭 topic | — |
| `message:received` | 提前追踪入站消息（去重辅助） | — |
| `command:*` | 过滤命令消息不入记忆 | — |

安装后，ClawXMemory 会**接管 OpenClaw 的原生记忆系统**：
- 禁用 `memory-core` 插件
- 禁用 `session-memory` 钩子
- 禁用 `memorySearch` 和 `memoryFlush`
- 将 `plugins.slots.memory` 指向自身

---

## 六、提供的工具

| 工具 | 说明 |
|------|------|
| `memory_search` | 多跳推理检索，返回 recall context + 各层级 ID |
| `memory_overview` | 返回记忆统计、运行状态、recall 诊断 |
| `memory_list` | 按层级浏览记忆（支持分页、搜索） |
| `memory_get` | 按 ID 精确读取任意层级记录 |
| `memory_flush` | 手动触发索引 flush |

---

## 七、与 Claude Code 记忆系统的对比

### 7.1 核心架构差异

| 维度 | ClawXMemory | Claude Code ExtractMemories |
|------|-------------|----------------------------|
| **记忆层级** | 4 层（L0 → L1 → L2 → Profile） | 扁平（4 类 .md 文件） |
| **记忆结构** | 分层索引树 + 关系链接 | 独立文件 + MEMORY.md 索引 |
| **存储方式** | SQLite 数据库 | 文件系统（Markdown + YAML frontmatter） |
| **检索方式** | LLM 多跳推理（4-hop reasoning） | 系统提示注入 + 工具搜索（Grep/Read） |
| **索引机制** | 话题切换检测 → 逐层聚合 | fork agent 分析最近消息 → 写文件 |
| **用户画像** | GlobalProfile 单例（LLM 持续重写） | `user` 类型记忆文件 |
| **项目跟踪** | L2 ProjectIndex（结构化状态机） | `project` 类型记忆文件 |
| **离线重构** | Dream（全局重写 + 去重 + 冲突检测） | 无等价机制 |

### 7.2 明确借鉴的设计理念

根据源码分析，ClawXMemory 在以下方面明确借鉴了 Claude Code 的记忆机制设计：

#### 借鉴 1：后台 fork 式记忆提取

**Claude Code**：ExtractMemories 在每轮查询结束后，通过 `runForkedAgent`（共享 prompt cache 的 fork 子代理）在后台提取记忆，不阻塞主对话。

**ClawXMemory**：在 `agent_end` 钩子中捕获 L0，然后通过 `scheduleIdleIndex` + debounce 异步触发后台 `HeartbeatIndexer`，同样不阻塞主对话。

```
Claude Code: query loop end → handleStopHooks → runForkedAgent(extract_memories)
ClawXMemory: agent_end hook → captureL0 → scheduleIdleIndex → HeartbeatIndexer.runHeartbeat
```

**借鉴程度**：架构模式相同（后台异步提取），但实现方式不同（fork agent vs hook + timer）。

#### 借鉴 2：记忆四分类 → 四维信息提取

**Claude Code** 的记忆四分类法：

| 类型 | 说明 |
|------|------|
| `user` | 用户角色、偏好、知识背景 |
| `feedback` | 工作方式纠正和确认 |
| `project` | 项目上下文、进度、决策 |
| `reference` | 外部系统资源指引 |

**ClawXMemory** 的 L1 提取四维信息：

| 维度 | 说明 |
|------|------|
| `summary` | 话题核心摘要 |
| `facts` | 结构化事实（技术栈、偏好、计划） |
| `projectDetails` | 项目信息（key, name, status, summary, latestProgress） |
| `situationTimeInfo` | 时间上下文 |

**对应关系**：
- Claude Code `user` → ClawXMemory `GlobalProfile`（用户画像持续重写）
- Claude Code `feedback` → ClawXMemory `facts`（偏好、习惯等 key-value 事实）
- Claude Code `project` → ClawXMemory `L2 ProjectIndex`（结构化项目状态机）
- Claude Code `reference` → ClawXMemory `facts`（外部资源作为 fact 存储）

**借鉴程度**：概念对应但实现差异大。Claude Code 用扁平文件 + 四类型标签；ClawXMemory 将四维信息拆解为不同层级的结构化字段。

#### 借鉴 3：「不保存可推导信息」的原则

**Claude Code** 在 `WHAT_NOT_TO_SAVE_SECTION` 中明确规定不保存代码模式、架构、git 历史、调试方案等可从当前项目状态推导的信息。

**ClawXMemory** 在 extraction-rules.json 中通过正则规则限制提取范围，聚焦于事实（技术栈、偏好、计划、活动）而非代码细节。同时 `summaryLimits` 限制摘要长度，避免存储冗余信息。

**借鉴程度**：设计原则一致，但 ClawXMemory 的过滤机制偏向正则规则，不如 Claude Code 的自然语言规则明确。

#### 借鉴 4：记忆的「先验证后使用」原则

**Claude Code** 在 `TRUSTING_RECALL_SECTION` 中要求模型在推荐记忆内容前必须验证（文件是否存在、函数是否还在、状态是否过时）。

**ClawXMemory** 在 `context-template.md` 的注入尾部有类似指导：

```
Treat the selected evidence above as authoritative historical memory for this turn when it is relevant.
If the needed answer is already shown above, do not claim that memory is missing or that this is a fresh conversation.
```

**借鉴程度**：方向一致但力度不同。Claude Code 的验证要求更严格（grep 检查、文件存在性检查），ClawXMemory 更侧重于「不要否认记忆的存在」。

#### 借鉴 5：记忆与压缩解耦

**Claude Code**：ExtractMemories（跨会话记忆）和 AutoCompact（会话内压缩）是两个完全独立的系统，各自有独立的触发机制。

**ClawXMemory**：通过禁用 OpenClaw 原生的 `memoryFlush`（压缩前 flush）和 `session-memory`，完全接管记忆管理。记忆索引（heartbeat）和用户回复（recall 注入）是两个独立流程。

**借鉴程度**：相同的设计哲学 — 记忆提取和上下文压缩是正交关注点。

### 7.3 超越 Claude Code 的创新

#### 创新 1：层级化记忆树 vs 扁平文件

Claude Code 的记忆是扁平的 `.md` 文件集合，通过 `MEMORY.md` 索引管理。检索依赖 Grep/Read 工具扫描文件内容。

ClawXMemory 构建了一棵**可导航的记忆树**（L0 → L1 → L2 → Profile），检索时 LLM 从高层开始逐级下钻。这种设计在记忆量增大时具有显著的效率优势 — 不需要扫描所有文件，只需沿着索引路径定位。

#### 创新 2：话题切换驱动的自动分段

Claude Code 的 ExtractMemories 在每轮结束时运行，不关心话题是否切换。多轮讨论同一话题可能产生多条重复或冗余的记忆文件。

ClawXMemory 通过 LLM 判断话题是否切换（`judgeTopicShift`），只有话题真正关闭时才生成 L1，避免了碎片化。Active Topic Buffer 机制确保同一话题的多轮对话被聚合为一条完整的记忆片段。

#### 创新 3：Dream 离线记忆重构

Claude Code 没有等价的离线重构机制。随着时间推移，记忆文件可能出现重复、冲突或过时的信息。

ClawXMemory 的 Dream 机制定期执行全局审查：
- 检测重复话题的 L1 片段
- 识别冲突信息
- 重建 L2 Project 索引（合并、删除、更新）
- 重写 GlobalProfile
- 清理过期引用（pruneL1Refs）

这类似于人类睡眠时大脑的记忆整合过程。

#### 创新 4：多跳推理检索 vs 工具搜索

Claude Code 的记忆检索依赖模型自行调用 Grep/Read 工具搜索记忆文件，本质上是文本匹配。

ClawXMemory 的 `ReasoningRetriever` 实现了 4-hop 的推理链：
1. Hop 1：意图识别 + 路由决策
2. Hop 2：L2 层级筛选
3. Hop 3：L1/L0 层级筛选
4. Hop 4：精细选择

每一跳都是一次 LLM 推理调用，模型主动判断需要什么信息、在记忆树的哪个位置。这比简单的文本搜索更接近人类的回忆过程。

#### 创新 5：项目状态机

Claude Code 的 `project` 类型记忆是自由文本，缺少结构化的状态管理。

ClawXMemory 为每个项目维护结构化的状态记录：

```typescript
interface ProjectDetail {
  key: string;              // 唯一标识
  name: string;             // 显示名
  status: "planned" | "in_progress" | "done";  // 状态机
  summary: string;          // 累积摘要
  latestProgress: string;   // 最新进展
  confidence: number;       // 置信度
}
```

支持项目标识统一（`resolveProjectIdentities`）、摘要合并（`mergeDistinctProjectText`）、状态演进（`preferProjectStatus`）。

#### 创新 6：可视化 Dashboard

Claude Code 的记忆系统没有可视化界面，用户只能通过文件系统查看 `.md` 文件。

ClawXMemory 内置了本地 Web Dashboard（`http://127.0.0.1:39393/clawxmemory/`），提供：
- 画布视图 + 列表视图
- 记忆层级可视化浏览
- Recall Trace 追踪（查看每次检索的多跳推理过程）
- Case Trace（查看每次交互的完整生命周期）
- 设置管理、导入导出

---

## 八、设计哲学总结

### ClawXMemory：「结构化索引 + 模型推理」

- 记忆不是文本片段的堆积，而是一棵可导航的索引树
- 检索不是文本匹配，而是模型沿树结构逐层推理
- 话题切换是记忆分段的自然边界
- Dream 机制确保记忆随时间演化而非积累冗余
- 用户画像是持续重写的单例，而非独立片段的集合

### Claude Code ExtractMemories：「类型化文件 + 工具搜索」

- 记忆是 4 类独立的 Markdown 文件（user/feedback/project/reference）
- 检索依赖模型自行调用 Grep/Read 工具
- 每轮结束自动提取，不区分话题边界
- 强调「不保存可推导信息」的排除规则
- 验证优先：推荐记忆内容前必须验证其当前有效性

### 核心区别

| 理念 | ClawXMemory | Claude Code |
|------|-------------|-------------|
| **组织方式** | 层级化索引树（L0→L1→L2→Profile） | 扁平类型化文件 |
| **检索哲学** | 模型主动推理定位 | 模型调用工具搜索 |
| **分段策略** | 话题切换驱动 | 每轮自动提取 |
| **演化机制** | Dream 全局重构 | 无 |
| **可观测性** | Web Dashboard + Trace | 无可视化 |
| **信任策略** | 信任记忆存在性 | 验证后才推荐 |

---

## 九、核心源码文件索引

| 文件 | 职责 |
|------|------|
| `src/index.ts` | 插件入口，注册 service/tools/hooks/prompt |
| `src/runtime.ts` | 运行时核心（L0 捕获、队列调度、recall 注入、Case Trace） |
| `src/hooks.ts` | 生命周期钩子注册（6 个 plugin hooks + 2 个 internal hooks） |
| `src/tools.ts` | 5 个用户工具定义（search/overview/list/get/flush） |
| `src/core/types.ts` | 所有数据类型定义（L0-L2, Profile, Trace, Dream） |
| `src/core/pipeline/heartbeat.ts` | 后台索引管线（L0 → L1 → L2 → Profile） |
| `src/core/retrieval/reasoning-loop.ts` | 多跳推理检索（4-hop reasoning chain） |
| `src/core/skills/llm-extraction.ts` | LLM 调用封装（提取、路由、重写、Dream 审查） |
| `src/core/indexers/l1-extractor.ts` | L1 片段提取器 |
| `src/core/indexers/l2-builder.ts` | L2 索引构建器（Time + Project） |
| `src/core/review/dream-review.ts` | Dream 离线重构流程 |
| `src/core/storage/sqlite.ts` | SQLite 存储层 |
| `src/ui-server.ts` | Dashboard 本地服务 |
| `skills/extraction-rules.json` | 事实提取正则规则 |
| `skills/intent-rules.json` | 意图分类关键词 |
| `skills/context-template.md` | recall 注入模板 |
