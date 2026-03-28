# Claude-Mem Memory 系统技术方案

> **项目定位**：Claude-Mem 是面向 Claude Code 的**持久化记忆插件**（40k+ stars），在编码会话中自动捕获工具调用与用户提示，由独立「观察者 AI」蒸馏为结构化记忆，写入 SQLite + Chroma，并在未来会话中通过渐进式披露注入相关上下文。

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Claude Code IDE                           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │              5 Lifecycle Hooks                         │  │
│  │  SessionStart → UserPromptSubmit → PostToolUse        │  │
│  │                                 → Stop(Summarize)     │  │
│  │                                 → SessionEnd          │  │
│  └────────────────────┬──────────────────────────────────┘  │
└───────────────────────┼─────────────────────────────────────┘
                        │ HTTP (fire-and-forget)
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              Worker Service (Express, port 37777)            │
│                                                              │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ Session    │  │ SDK Agent    │  │ Search / Context    │ │
│  │ Manager    │  │ (观察者 AI)  │  │ Generation          │ │
│  └─────┬──────┘  └──────┬───────┘  └──────────┬──────────┘ │
│        │                │                      │            │
│  ┌─────▼────────────────▼──────────────────────▼──────────┐ │
│  │                   Storage Layer                         │ │
│  │  ┌──────────────┐  ┌────────────────┐  ┌────────────┐ │ │
│  │  │   SQLite     │  │  Chroma        │  │   FTS5     │ │ │
│  │  │ (结构化数据) │  │ (向量嵌入)     │  │ (全文索引) │ │ │
│  │  └──────────────┘  └────────────────┘  └────────────┘ │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              Web Viewer UI (React + SSE)               │ │
│  │              http://localhost:37777                      │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 核心设计原则

- **双进程架构**：Hook 轻量 fire-and-forget，重活（AI 蒸馏、检索）在 Worker 进程
- **不阻塞 IDE**：Hook 失败时降级 exit 0，Worker 不可用不影响编码体验
- **观察者模式**：用独立的 AI Agent（禁用所有工具）压缩记忆，避免干扰主对话

---

## 2. 五个生命周期钩子（核心流程）

```
会话开始
  │
  ▼
┌─────────────┐     ┌───────────────────┐     ┌──────────────┐
│ SessionStart│────→│ UserPromptSubmit  │────→│ PostToolUse  │──┐
│ (注入上下文)│     │ (初始化会话)      │     │ (捕获工具调用)│  │
└─────────────┘     └───────────────────┘     └──────────────┘  │
                                                    ↑            │
                                                    └────────────┘
                                                    (每次工具调用)
                                                         │
                                              会话即将结束│
                                                         ▼
                                              ┌──────────────────┐
                                              │  Stop/Summarize  │
                                              │  (生成会话摘要)   │
                                              └────────┬─────────┘
                                                       │
                                                       ▼
                                              ┌──────────────────┐
                                              │   SessionEnd     │
                                              │  (资源回收)       │
                                              └──────────────────┘
```

### 2.1 SessionStart → `context`（注入历史记忆）

```typescript
// src/cli/handlers/context.ts
export const contextHandler: EventHandler = {
  async execute(input: NormalizedHookInput): Promise<HookResult> {
    const workerReady = await ensureWorkerRunning();
    // 调用 Worker API 获取注入上下文
    const apiPath = `/api/context/inject?projects=${encodeURIComponent(projectsParam)}`;
    // 返回紧凑索引注入到 Claude 的 additionalContext
  }
};
```

**职责**：
1. 确保 Worker Service 运行
2. 调用 `/api/context/inject` 获取历史记忆的紧凑索引
3. 将索引注入到 `hookSpecificOutput.additionalContext`
4. 可选显示 Web Viewer 链接

### 2.2 UserPromptSubmit → `session-init`（初始化会话）

```typescript
// src/cli/handlers/session-init.ts
// POST /api/sessions/init
// 创建/复用 sdk_sessions
// 计算 promptNumber
// 脱敏后写入 user_prompts
// 启动/续跑 SDK Agent
```

**职责**：
1. 创建或复用 `sdk_sessions` 记录
2. 用户提示脱敏后写入 `user_prompts` 表
3. 若非 `<private>` 内容，启动 SDK Agent

### 2.3 PostToolUse → `observation`（捕获工具调用 — 最高频触发）

```typescript
// src/services/worker/http/routes/SessionRoutes.ts
private handleObservationsByClaudeId = this.wrapHandler((req, res) => {
  // 过滤跳过的工具（CLAUDE_MEM_SKIP_TOOLS）
  // 隐私检查 + 标签剥离
  // 入队
  this.sessionManager.queueObservation(sessionDbId, {
    tool_name,
    tool_input: cleanedToolInput,
    tool_response: cleanedToolResponse,
    prompt_number: promptNumber,
    cwd
  });
  // 确保 SDK Agent 在运行
  this.ensureGeneratorRunning(sessionDbId, 'observation');
});
```

**职责**：
1. 接收每次工具调用的名称、参数、返回值
2. 隐私过滤与标签剥离
3. 入队等待 SDK Agent 消费

### 2.4 Stop → `summarize`（生成会话摘要）

```typescript
// src/sdk/prompts.ts
export function buildSummaryPrompt(session, mode) {
  return `--- MODE SWITCH: PROGRESS SUMMARY ---
Do NOT output <observation> tags. This is a summary request.
Your response MUST use <summary> tags ONLY.
Any <observation> output will be discarded.`;
}
```

**职责**：
1. 从 transcript 取最后一条 assistant 消息
2. 触发 SDK Agent 的 Summary 模式
3. 生成结构化会话摘要

### 2.5 SessionEnd → `session-complete`（资源回收）

**职责**：将会话从活跃 Map 移除，配合 orphan reaper 进行资源回收。

---

## 3. 记忆蒸馏机制（核心技术 — 观察者 AI）

### 3.1 设计理念

**不是简单截断工具输出，而是用独立的「观察者 Claude」蒸馏出结构化知识。**

```
原始工具调用                    观察者 AI 蒸馏结果
┌──────────────────┐           ┌──────────────────────┐
│ tool: Write      │           │ <observation>         │
│ input: {         │           │   type: file_change   │
│   path: app.ts   │  ──AI──→ │   title: 重构认证模块  │
│   content: ...   │           │   facts:              │
│   (500行代码)    │           │     - 将 JWT 迁移到    │
│ }                │           │       session-based    │
│ output: success  │           │   narrative: ...       │
└──────────────────┘           │   files_modified:      │
                               │     - src/auth/app.ts  │
                               └──────────────────────┘

压缩比：500行 → ~10行结构化 XML
```

### 3.2 SDK Agent 实现

```typescript
// src/services/worker/SDKAgent.ts
class SDKAgent {
  // 关键：禁用所有工具，强制只输出 XML
  private disallowedTools = ['*'];

  async processQueue() {
    while (queue.length > 0) {
      const message = queue.shift();

      if (message.type === 'observation') {
        const obsPrompt = buildObservationPrompt({
          tool_name: message.tool_name,
          tool_input: JSON.stringify(message.tool_input),
          tool_output: JSON.stringify(message.tool_response),
          cwd: message.cwd
        });
        // 发给观察者 Claude，无工具可用，只能输出结构化 XML
        const response = await this.query(obsPrompt);
        // 解析 XML → 存储
        processAgentResponse(response);
      }
    }
  }
}
```

### 3.3 结构化 XML 输出

观察者 AI 被要求输出以下结构：

```xml
<observation>
  <type>file_change|command|search|configuration|...</type>
  <title>简洁的操作描述</title>
  <subtitle>补充说明</subtitle>
  <facts>
    <fact>从对话中提取的关键事实 1</fact>
    <fact>从对话中提取的关键事实 2</fact>
  </facts>
  <narrative>操作的上下文和意义</narrative>
  <concepts>相关概念标签</concepts>
  <files_modified>修改的文件列表</files_modified>
  <files_read>读取的文件列表</files_read>
</observation>
```

### 3.4 响应处理与存储

```typescript
// src/services/worker/agents/ResponseProcessor.ts
const observations = parseObservations(text, session.contentSessionId);
const summary = parseSummary(text, session.sessionDbId);

// 同一事务写入
const result = sessionStore.storeObservations(
  session.memory_session_id,
  session.project,
  observations,
  summaryForStore,
  session.lastPromptNumber,
  discoveryTokens
);

// 异步：同步到 Chroma 向量库 + SSE 广播到 Viewer
```

### 3.5 记忆经济学（Discovery Token 追踪）

SDK Agent 记录每次蒸馏消耗的 token（`discovery_tokens`），与后续检索时节省的 token 对比：

```
蒸馏成本：~200 tokens（调用观察者 AI）
原始数据：~5000 tokens（工具调用全文）
注入成本：~50 tokens（结构化摘要）
→ 净节省：~4750 tokens/次
```

---

## 4. 存储模型

### 4.1 SQLite Schema

```sql
-- 会话表
CREATE TABLE sdk_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_session_id TEXT UNIQUE NOT NULL,  -- Claude 会话 ID
  memory_session_id TEXT UNIQUE,             -- SDK 子会话 ID
  project TEXT NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  completed_at DATETIME
);

-- 观察记录（蒸馏后的结构化记忆）
CREATE TABLE observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  text TEXT NOT NULL,           -- 原始 XML
  type TEXT NOT NULL,           -- file_change/command/search/...
  title TEXT,                   -- 操作标题
  subtitle TEXT,                -- 补充说明
  facts TEXT,                   -- JSON: 提取的事实列表
  narrative TEXT,               -- 操作叙事
  concepts TEXT,                -- 概念标签
  files_modified TEXT,          -- 修改的文件
  files_read TEXT,              -- 读取的文件
  prompt_number INTEGER,
  discovery_tokens INTEGER,     -- 蒸馏消耗的 token
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 会话摘要
CREATE TABLE session_summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_session_id TEXT UNIQUE NOT NULL,
  project TEXT NOT NULL,
  request TEXT,        -- 用户请求了什么
  investigated TEXT,   -- 调查了什么
  -- ...更多结构化字段
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 用户提示（脱敏后）
CREATE TABLE user_prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_session_id TEXT NOT NULL,
  prompt_number INTEGER NOT NULL,
  prompt_text TEXT NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- FTS5 全文索引（平台支持时）
CREATE VIRTUAL TABLE user_prompts_fts USING fts5(
  prompt_text,
  content='user_prompts',
  content_rowid='id'
);
```

### 4.2 双 ID 会话模型

```
Claude Code 会话                    SDK 观察者会话
┌──────────────────┐              ┌──────────────────┐
│content_session_id│──1:1映射──→ │memory_session_id │
│(用户可见)        │              │(内部使用)        │
└──────────────────┘              └──────────────────┘
```

**关键设计**：两套 ID 分离，避免把记忆系统的消息写进用户的 transcript。

### 4.3 Chroma 向量存储

```typescript
// src/services/sync/ChromaSync.ts
// Collection 命名：cm__<project_name>
// 一条 observation 拆成多条向量文档：

formatObservationDocs(obs) {
  return [
    { text: obs.narrative, metadata: { field_type: "narrative", sqlite_id: obs.id } },
    { text: obs.text, metadata: { field_type: "text", sqlite_id: obs.id } },
    // 每条 fact 单独一条向量
    ...obs.facts.map(f => ({
      text: f, metadata: { field_type: "fact", sqlite_id: obs.id }
    }))
  ];
}
```

---

## 5. 记忆检索与注入流程（核心技术）

### 5.1 SessionStart 自动注入

```
新会话开始
    │
    ▼
GET /api/context/inject?projects=my-project
    │
    ▼
generateContext()
    │
    ├──→ 查询最近的 observations（按项目过滤）
    ├──→ 查询 session_summaries
    ├──→ TokenCalculator 估算每条记忆的 token 成本
    ├──→ 按重要性/时间排序
    │
    ▼
生成紧凑索引（Markdown 格式）
    │
    ┌─────────────────────────────────────────────┐
    │ ## Recent Memory (47 observations)          │
    │                                              │
    │ | ID | Time | Type | Title | Est. Tokens |  │
    │ |----|------|------|-------|-------------|   │
    │ | 1  | 2h ago | file_change | 重构认证 | ~120 │
    │ | 2  | 3h ago | command | 部署测试 | ~80    │
    │ | ...                                        │
    │                                              │
    │ Use mem-search skill to query details.       │
    └─────────────────────────────────────────────┘
    │
    ▼
注入到 Claude 的 additionalContext
```

### 5.2 渐进式披露（Progressive Disclosure）

```
Layer 1: 紧凑索引（SessionStart 自动注入）
    │     ID + 时间 + 类型 + 标题 + 预估 token
    │     成本：~500 tokens（覆盖整个项目历史）
    │
    ▼  Claude 根据用户问题决定是否深入
Layer 2: Timeline 检索（MCP / mem-search）
    │     按时间范围查询详细 observation
    │     成本：按需，每条 ~50-200 tokens
    │
    ▼  Claude 发现需要具体某条记忆的全文
Layer 3: 全文拉取（get_observations）
    │     获取完整的 observation 内容
    │     成本：按需，每条完整内容
```

**核心优势**：把上下文预算的决策权交给模型，而非一次性灌满。

### 5.3 混合检索策略

```typescript
// HybridSearchStrategy.ts
class HybridSearchStrategy {
  async search(query) {
    // 1. SQLite 元数据过滤（项目、时间范围、类型）
    const metadataResults = await sqliteSearch(query.filters);

    // 2. Chroma 语义检索
    const semanticResults = await chromaSearch(query.text);

    // 3. FTS5 全文检索（平台支持时）
    const ftsResults = await ftsSearch(query.text);

    // 4. 合并 + 去重 + 排序
    return mergeAndRank(metadataResults, semanticResults, ftsResults);
  }
}
```

### 5.4 MCP 工具与 Skill

**MCP Server**（`src/servers/mcp-server.ts`）暴露的工具：

| 工具 | 映射端点 | 用途 |
|------|----------|------|
| `search` | `/api/search` | 语义 + 关键词混合搜索 |
| `timeline` | `/api/timeline` | 按时间线浏览 |
| `get_observations` | `/api/observations` | 拉取完整 observation |

**mem-search Skill**（`plugin/skills/mem-search/SKILL.md`）定义三步工作流：

```
1. search → 发现相关记忆 ID
2. timeline → 了解时间上下文
3. get_observations → 获取需要的完整内容
```

---

## 6. 隐私控制机制

### 6.1 `<private>` 标签

```typescript
// src/utils/tag-stripping.ts
// 用户可在提示中用 <private>...</private> 标记敏感内容
// 在到达 Worker/DB 前被完全剥离

stripPrivateTags(input) → 移除 <private> 包裹的内容
stripMemoryTagsFromJson(json) → 从工具参数中递归剥离
```

如果整段提示只剩 `<private>` 内容，session-init 返回 `{skipped: true, reason: 'private'}`。

### 6.2 系统级过滤

```typescript
// 剥离 claude-mem 自身的上下文标签（防递归存储）
// 剥离 system_instruction（防泄漏系统提示）
// CLAUDE_MEM_SKIP_TOOLS：配置需要跳过的工具
// CLAUDE_MEM_EXCLUDED_PROJECTS：排除特定项目
```

### 6.3 PrivacyCheckValidator

在 observation/summarize 路径上，根据用户 prompt 判断整个会话是否敏感。

---

## 7. Web Viewer UI

```
http://localhost:37777
    │
    ├──→ 实时记忆流（SSE 推送）
    │      new_observation → 新记忆卡片
    │      new_summary → 会话摘要
    │      new_prompt → 用户提示
    │
    ├──→ 记忆浏览与搜索
    │      按项目/时间/类型筛选
    │      查看结构化字段
    │
    └──→ 队列状态监控
           待处理 observation 数量
           SDK Agent 运行状态
```

**人机共看同一数据源**：开发者在 Viewer 看到的记忆，和 Claude 在新会话中被注入的索引，来自同一个 SQLite。

---

## 8. 关键技术亮点

### 8.1 观察者 AI 蒸馏（最核心创新）

- 用独立的 Claude Agent（禁用所有工具）压缩工具轨迹
- 输出结构化 XML（type/title/facts/narrative/concepts/files）
- 压缩比高：500 行代码变更 → ~10 行结构化摘要
- 与主对话完全隔离，不污染用户 transcript

### 8.2 双进程 + Fire-and-Forget

- Hook 进程轻量（仅 HTTP POST），不阻塞 IDE
- Worker 进程独立运行（Express + SDK Agent）
- Hook 失败降级 exit 0，不影响编码体验

### 8.3 渐进式披露

- 不一次灌满上下文，而是先给索引
- 模型自主决定需要哪些记忆的全文
- 每条记忆标注预估 token 成本，辅助决策

### 8.4 双 ID 会话模型

- `content_session_id`（Claude 侧）vs `memory_session_id`（SDK 侧）
- 避免记忆系统消息污染用户 transcript

### 8.5 记忆经济学

- 追踪 `discovery_tokens`（蒸馏成本）
- 对比原始数据 token vs 注入 token
- 量化记忆系统的 ROI

### 8.6 混合检索

- SQLite 结构化查询（项目、时间、类型）
- Chroma 向量语义检索
- FTS5 全文关键词检索
- 三路合并排序

---

## 9. 与其他记忆系统的对比

| 维度 | Claude-Mem | Mem0 | OpenClaw Memory |
|------|-----------|------|-----------------|
| **记忆来源** | 自动捕获工具调用 | 需手动调用 add() | LLM 写 Markdown |
| **压缩方式** | 独立 AI Agent 蒸馏 | LLM 事实抽取 | 无自动压缩 |
| **存储** | SQLite + Chroma | 向量库 + 图库 | Markdown + SQLite |
| **注入方式** | 渐进式披露（索引→按需） | search() 返回 | 混合检索返回 |
| **粒度** | 工具调用级（operation） | 事实级（fact） | 文件级（chunk） |
| **自动化** | 全自动（hook 驱动） | 需代码集成 | 半自动（flush） |
| **隔离性** | 双进程 + 双 ID | 单进程 | 单进程 |

---

## 10. 数据流全景

```
用户在 Claude Code 中编码
    │
    ├── SessionStart ──→ Worker: /api/context/inject
    │                         │
    │                    generateContext()
    │                         │
    │                    ┌────▼────┐
    │                    │ SQLite  │──→ 紧凑索引 ──→ 注入 Claude
    │                    └─────────┘
    │
    ├── UserPromptSubmit ──→ Worker: /api/sessions/init
    │                              │
    │                         创建 session + 存 prompt
    │                              │
    │                         启动 SDK Agent
    │
    ├── PostToolUse (×N) ──→ Worker: /api/sessions/observations
    │                              │
    │                         入队 ──→ SDK Agent 消费
    │                                      │
    │                              ┌───────▼────────┐
    │                              │ 观察者 Claude   │
    │                              │ (无工具, XML)   │
    │                              └───────┬────────┘
    │                                      │
    │                              parseObservations()
    │                                      │
    │                              ┌───────▼────────┐
    │                              │ SQLite + Chroma │
    │                              └───────┬────────┘
    │                                      │
    │                              SSE ──→ Web Viewer
    │
    ├── Stop ──→ Worker: /api/sessions/summarize
    │                  │
    │            SDK Agent Summary 模式
    │                  │
    │            session_summaries 表
    │
    └── SessionEnd ──→ Worker: /api/sessions/complete
                             │
                        资源回收
```
