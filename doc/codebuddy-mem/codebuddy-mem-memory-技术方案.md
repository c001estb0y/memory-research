# CodeBuddy-Mem Memory 系统技术方案

> **项目定位**：CodeBuddy-Mem 是基于 claude-mem 架构衍生的**本地持久化编码记忆系统**，通过 Worker Service + 观察者 AI 自动蒸馏编码会话，用 SQLite + FTS5 本地存储，并通过 MCP Server 向 Cursor / CodeBuddy 等多 IDE 提供三层渐进式记忆检索。

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│              IDE Layer (Cursor / CodeBuddy)                  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │              Lifecycle Hooks                           │  │
│  │  SessionStart → BeforeSubmitPrompt → PostToolUse      │  │
│  │                                   → PreCompact        │  │
│  │                                   → Stop              │  │
│  └────────────────────┬──────────────────────────────────┘  │
└───────────────────────┼─────────────────────────────────────┘
                        │ HTTP (fire-and-forget)
                        ▼
┌─────────────────────────────────────────────────────────────┐
│         Worker Service (Express, 动态端口如 3847)            │
│                                                              │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ Session    │  │ SDK Agent    │  │ Search / Context    │ │
│  │ Manager    │  │ (观察者 AI)  │  │ Generation          │ │
│  └─────┬──────┘  └──────┬───────┘  └──────────┬──────────┘ │
│        │                │                      │            │
│  ┌─────▼────────────────▼──────────────────────▼──────────┐ │
│  │                   Storage Layer                         │ │
│  │  ┌──────────────┐  ┌────────────────┐                  │ │
│  │  │   SQLite     │  │    FTS5        │                  │ │
│  │  │ observations │  │  全文检索索引   │                  │ │
│  │  │ summaries    │  │  + LIKE 检索   │                  │ │
│  │  │ sessions     │  │  (中文优化)    │                  │ │
│  │  └──────────────┘  └────────────────┘                  │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              MCP Server (记忆检索接口)                  │ │
│  │  search / search_like / timeline / get_observations    │ │
│  │  get_context / get_summaries / get_stats               │ │
│  │  list_projects / list_sessions                         │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 实际运行数据（来自当前实例）

| 指标 | 数值 |
|------|------|
| **observations 总量** | 11,046 条 |
| **session summaries** | 1,083 条 |
| **sessions 总数** | 173 个 |
| **覆盖项目数** | 19 个项目 |
| **运行时长** | ~2.1 天（185,533 秒） |
| **状态** | healthy |

---

## 3. 数据模型（Observation 结构）

每条 observation 是经过**观察者 AI 蒸馏**后的结构化记忆：

```json
{
  "id": 6898,
  "memory_session_id": "mem-1773066181894-2qiufn",
  "project": "d:/github/shadow-folk",
  "type": "discovery",
  "title": "当前工作分支状态说明",
  "subtitle": "Git 分支：merge/auth-and-org-architecture",
  "text": "Agent 说明当前检出的分支为 merge/auth-and-org-architecture...",
  "facts": "当前在 `merge/auth-and-org-architecture` 分支。\n该分支在本次会话开始时创建...",
  "narrative": "Agent 说明当前检出的分支为...",
  "concepts": "git, branch, merge, auth, org-architecture",
  "files_read": null,
  "files_modified": null,
  "meta_intent": "【意图类型】：向协作者同步当前会话所在分支...",
  "prompt_number": 0,
  "discovery_tokens": 0,
  "created_at": "2026-03-10T11:36:26.159Z"
}
```

### 核心字段说明

| 字段 | 说明 |
|------|------|
| `type` | 记忆类型：discovery / investigation / debugging / configuration / documentation / feature / learning |
| `title` / `subtitle` | 简洁描述，用于索引层展示 |
| `facts` | 从工具调用中提取的关键事实列表 |
| `narrative` | 操作的上下文和意义叙事 |
| `concepts` | 概念标签（逗号分隔），支持概念检索 |
| `files_read` / `files_modified` | 涉及的文件（支持文件维度检索） |
| `meta_intent` | **中文意图标注**（CodeBuddy-Mem 特有），标注该操作的深层意图 |
| `discovery_tokens` | 蒸馏消耗的 token 量 |

### Session Summary 结构

```json
{
  "id": 1200,
  "project": "d:/github/cursor-langfuse",
  "request": "用户想知道如何在 CodeBuddy 里测试 hook 功能",
  "investigated": "确认了 hook 注册方式和事件映射...",
  "learned": "已发现并修复事件名标准化映射缺失问题...",
  "completed": "完成了多 IDE adapter 架构与 CodeBuddy 支持...",
  "next_steps": "建议用户先在 CodeBuddy 中实际发起一次对话...",
  "files_read": "test-trace.js, .cursor/hooks/...",
  "files_edited": "d:\\GitHub\\cursor-langfuse\\.cursor\\hooks\\...",
  "meta_intent": "用户希望在真实环境中验证追踪链路..."
}
```

Summary 比 observation 更高层，是**会话级**的结构化摘要，包含：请求(request) → 调查(investigated) → 学到(learned) → 完成(completed) → 下一步(next_steps)。

---

## 4. MCP 工具与三层检索（核心技术）

### 4.1 三层渐进式检索工作流

```
Layer 1: search(query) 或 search_like(query)
    │     返回索引：ID + 类型 + 标题 (~50-100 tokens/条)
    │     ⚡ 10x token 节省
    ▼
Layer 2: timeline(anchor=ID)
    │     获取某条 observation 前后的时间上下文
    │     理解操作的先后因果
    ▼
Layer 3: get_observations([ID1, ID2, ...])
    │     仅对筛选后的 ID 拉取全文
    │     按需加载，避免灌满上下文
```

这个工作流通过 `__IMPORTANT` 伪工具强制注入到 AI 的意识中：

```
"3-LAYER WORKFLOW (ALWAYS FOLLOW):
1. search(query) -> Get index with IDs (~50-100 tokens/result)
2. timeline(anchor=ID) -> Get context around interesting results
3. get_observations([IDs]) -> Fetch full details ONLY for filtered IDs
NEVER fetch full details without filtering first. 10x token savings."
```

### 4.2 完整 MCP 工具集（10 个）

| 工具 | 层级 | 说明 |
|------|------|------|
| `search` | L1 | FTS5 全文搜索，返回索引 |
| `search_like` | L1 | SQL LIKE 模糊搜索（**中文/短关键词优化**） |
| `timeline` | L2 | 以某条 observation 为锚点，获取前后上下文 |
| `get_observations` | L3 | 按 ID 数组拉取完整内容 |
| `get_summaries` | L3 | 按 ID 数组拉取会话摘要 |
| `get_context` | 注入 | 获取项目级记忆上下文（session start 注入用） |
| `list_projects` | 管理 | 列出所有记忆中的项目 |
| `list_sessions` | 管理 | 列出会话（支持项目过滤、分页） |
| `get_stats` | 管理 | 数据库健康统计 |
| `__IMPORTANT` | 元指令 | 强制 AI 遵循三层工作流 |

### 4.3 中文搜索优化

`search_like` 是 CodeBuddy-Mem 相比 claude-mem 的重要增强：

```json
{
  "name": "search_like",
  "description": "Search memory using SQL LIKE (better for Chinese/short keywords)",
  "arguments": {
    "query": "SQL LIKE %query%",
    "type": "observations | summaries | all"
  }
}
```

FTS5 对中文分词支持较弱，`search_like` 用 SQL LIKE 模糊匹配弥补。

---

## 5. 上下文注入机制

`get_context` 返回结构化的项目记忆上下文（约 900 tokens），包含：

```xml
<memory_context>
  <instructions>
    以下是用户在本项目中的真实历史任务记录...
    当用户询问"最近做了什么任务"时，必须优先参考此处数据...
  </instructions>

  <recent_sessions>
    <session date="2026-03-26">
      <request>用户希望将代码变更提交并推送...</request>
      <learned>当前 shadow 提交受权限限制...</learned>
      <completed>完成了差异审查与排查...</completed>
    </session>
    ...最近 3 个 session
  </recent_sessions>

  <observations>
    <observation type="debugging" date="2026-03-26">
      <title>Shadow push script failed with HTTP 403</title>
    </observation>
    ...最近 5 条 observation
  </observations>
</memory_context>
```

---

## 6. 记忆写入流程

与 claude-mem 相同的**观察者 AI 蒸馏架构**：

```
IDE 中的工具调用（Write/Edit/Bash/Search...）
    │
    ▼ PostToolUse Hook → HTTP POST 到 Worker
    │
    ▼ 入队 → SDK Agent 消费
    │
    ┌──────────────────────────────────────┐
    │        观察者 Claude (无工具)         │
    │  输入：tool_name + tool_input +      │
    │        tool_output + cwd             │
    │  输出：结构化 XML                     │
    │    <observation>                      │
    │      <type>discovery</type>          │
    │      <title>...</title>              │
    │      <facts>...</facts>              │
    │      <narrative>...</narrative>       │
    │      <concepts>...</concepts>        │
    │    </observation>                     │
    └──────────────────────┬───────────────┘
                           │
                    parseObservations()
                           │
                    ┌──────▼──────┐
                    │   SQLite    │
                    │ + FTS5 索引 │
                    └─────────────┘
```

### CodeBuddy-Mem 特有增强：`meta_intent`

蒸馏结果中额外包含中文意图标注：

```
meta_intent: "【意图类型】：了解当前账户可访问的项目范围、身份角色与
各项目可用能力，为后续在正确项目上下文中进行记忆操作做准备。"
```

这比 claude-mem 的纯英文 type/title 多了一层**中文意图理解**，让后续检索时 AI 能更好地判断相关性。

---

## 7. 与 Claude-Mem 的详细对比

### 7.1 架构同源对比

| 维度 | Claude-Mem | CodeBuddy-Mem |
|------|-----------|---------------|
| **定位** | Claude Code 官方生态插件 | 多 IDE 适配的衍生版 |
| **Stars** | 40k+ | 私有项目 |
| **IDE 支持** | Claude Code 为主 | Cursor + CodeBuddy + Claude Code |
| **Worker 端口** | 固定 37777 | 动态端口（如 3847） |
| **安装方式** | `/plugin marketplace add` | 手动配置 |

### 7.2 存储层差异

| 维度 | Claude-Mem | CodeBuddy-Mem |
|------|-----------|---------------|
| **主存储** | SQLite | SQLite |
| **全文索引** | FTS5（可选） | FTS5 + SQL LIKE（中文优化） |
| **向量检索** | Chroma（语义检索） | **无 Chroma**（纯文本检索） |
| **检索模式** | FTS5 + Chroma 混合 | FTS5 + LIKE 双路 |
| **语义理解** | 向量相似度 | 依赖蒸馏时的 concepts/narrative |

**关键差异**：Claude-Mem 有 Chroma 做语义向量检索（"意思相近"就能找到），CodeBuddy-Mem 只有文本匹配（需要关键词命中）。但 CodeBuddy-Mem 通过 `meta_intent` 和丰富的 `concepts` 标签弥补了部分语义能力。

### 7.3 检索接口差异

| 维度 | Claude-Mem | CodeBuddy-Mem |
|------|-----------|---------------|
| **检索入口** | MCP + Skill + Hook 注入 | MCP（10 个工具） |
| **渐进式披露** | search → timeline → get_observations | 相同三层架构 |
| **中文搜索** | 仅 FTS5（中文弱） | FTS5 + `search_like`（LIKE 补充） |
| **上下文注入** | SessionStart Hook 生成索引 | `get_context` 返回 XML 结构化上下文 |
| **Web Viewer** | 有（React + SSE） | 无（纯 MCP 接口） |

### 7.4 蒸馏差异

| 维度 | Claude-Mem | CodeBuddy-Mem |
|------|-----------|---------------|
| **蒸馏引擎** | Claude Agent SDK（观察者 AI） | 相同架构 |
| **输出格式** | XML: type/title/facts/narrative/concepts | 相同 + **meta_intent** |
| **意图标注** | 无 | 有（中文意图理解） |
| **Session Summary** | request/investigated | request/investigated/learned/completed/**next_steps** |
| **summary 字段** | 较少 | 更多（含 `meta_intent`） |

### 7.5 隐私与部署差异

| 维度 | Claude-Mem | CodeBuddy-Mem |
|------|-----------|---------------|
| **数据存储** | 100% 本地 | 100% 本地 |
| **隐私标签** | `<private>` 标签剥离 | 未见类似机制 |
| **云依赖** | 无（纯本地 SQLite + Chroma） | 无（纯本地 SQLite） |
| **蒸馏 API 调用** | 调 Anthropic API（观察者 AI） | 调 Anthropic API（观察者 AI） |

---

## 8. 核心技术亮点

### 8.1 与 Claude-Mem 共享的优势

1. **观察者 AI 蒸馏**：独立 Claude Agent 压缩工具轨迹，高压缩比
2. **双进程架构**：Hook 轻量 fire-and-forget，不阻塞 IDE
3. **三层渐进式披露**：索引 → 时间线 → 全文，10x token 节省
4. **双 ID 会话模型**：content_session_id vs memory_session_id 隔离

### 8.2 CodeBuddy-Mem 独有优势

1. **中文 `meta_intent` 意图标注**：每条 observation 都有中文意图理解，提升中文场景下的检索相关性
2. **`search_like` 中文搜索**：SQL LIKE 弥补 FTS5 对中文分词的不足
3. **多 IDE 适配**：Cursor + CodeBuddy + Claude Code，通过 adapter 层统一事件模型
4. **更丰富的 Summary 结构**：request → investigated → learned → completed → next_steps 五阶段

### 8.3 Claude-Mem 独有优势

1. **Chroma 语义向量检索**：支持"意思相近"的模糊语义搜索
2. **Web Viewer UI**：实时记忆流可视化
3. **`<private>` 隐私标签**：用户级隐私控制
4. **Skill 生态**：mem-search Skill 引导 AI 使用三层工作流
5. **记忆经济学**：discovery_tokens 追踪与 ROI 量化
6. **社区与文档**：40k+ stars，完善的文档站

---

## 9. 适用场景建议

| 场景 | 推荐 |
|------|------|
| **主用 Claude Code** | Claude-Mem（原生集成，社区支持好） |
| **主用 Cursor / CodeBuddy** | CodeBuddy-Mem（多 IDE 适配，中文优化） |
| **需要语义搜索** | Claude-Mem（Chroma 向量检索） |
| **中文开发场景** | CodeBuddy-Mem（meta_intent + search_like） |
| **需要可视化** | Claude-Mem（Web Viewer） |
| **多 IDE 混用** | CodeBuddy-Mem（adapter 架构） |
