# OpenClaw Memory 系统技术方案

> **项目定位**：OpenClaw 是在本机运行的个人 AI 助手框架（337k+ stars），记忆以**工作空间 Markdown 文件为唯一事实来源**，由默认插件 `memory-core` 提供向量/全文混合检索与工具集成。

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────┐
│                  Agent Runtime                       │
│  ┌───────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │memory_search│ │ memory_get   │  │ Prompt Section│ │
│  │  (Tool)    │  │  (Tool)      │  │  (引导先搜索)  │ │
│  └─────┬──────┘  └──────┬───────┘  └──────────────┘ │
│        │                │                            │
│  ┌─────▼────────────────▼──────────────────────┐    │
│  │         Memory Search Manager               │    │
│  │  ┌─────────────────────────────────────┐    │    │
│  │  │ FallbackMemoryManager               │    │    │
│  │  │  ├─ Primary: QmdMemoryManager       │    │    │
│  │  │  └─ Fallback: MemoryIndexManager    │    │    │
│  │  └─────────────────────────────────────┘    │    │
│  └──────────────────────┬──────────────────────┘    │
│                         │                            │
│  ┌──────────────────────▼──────────────────────┐    │
│  │          Storage Layer (per Agent)          │    │
│  │  ┌──────────┐ ┌──────────┐ ┌────────────┐  │    │
│  │  │sqlite-vec│ │  FTS5    │ │ Embed Cache│  │    │
│  │  │(向量索引) │ │(全文索引) │ │ (嵌入缓存) │  │    │
│  │  └──────────┘ └──────────┘ └────────────┘  │    │
│  └─────────────────────────────────────────────┘    │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │        Workspace Files (事实来源)             │   │
│  │  MEMORY.md │ memory/YYYY-MM-DD.md │ extraPaths│  │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

### 核心分层

| 层次 | 职责 | 关键文件 |
|------|------|----------|
| **事实层** | `MEMORY.md`（长期偏好）、`memory/YYYY-MM-DD.md`（日记）及 `extraPaths` 下 `.md` | `docs/concepts/memory.md` |
| **插件层** | 注册工具、提示词片段、压缩前 flush、嵌入 Provider | `extensions/memory-core/index.ts` |
| **内置检索后端** | 每 Agent 一个 SQLite：chunk 元数据 + sqlite-vec + FTS5 | `packages/memory-host-sdk/src/host/memory-schema.ts` |
| **QMD 后端（可选）** | BM25 + 向量 + Rerank 的外部 sidecar | `extensions/memory-core/src/memory/qmd-manager.ts` |
| **压缩前 flush** | 接近上下文上限时静默一轮，让模型将内容写入日记文件 | `src/auto-reply/reply/agent-runner-memory.ts` |

---

## 2. 核心数据结构与存储模型

### 2.1 SQLite Schema（内置索引）

```sql
-- 键值元数据（索引版本、模型等）
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 已索引文件状态追踪
CREATE TABLE files (
  path   TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'memory',
  hash   TEXT NOT NULL,
  mtime  INTEGER NOT NULL,
  size   INTEGER NOT NULL
);

-- 核心分块表
CREATE TABLE chunks (
  id         TEXT PRIMARY KEY,
  path       TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT 'memory',
  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,
  hash       TEXT NOT NULL,
  model      TEXT NOT NULL,
  text       TEXT NOT NULL,
  embedding  TEXT NOT NULL,      -- 序列化向量
  updated_at INTEGER NOT NULL
);

-- 嵌入缓存（减少重复调用）
CREATE TABLE embedding_cache (
  provider     TEXT NOT NULL,
  model        TEXT NOT NULL,
  provider_key TEXT NOT NULL,
  hash         TEXT NOT NULL,
  embedding    TEXT NOT NULL,
  PRIMARY KEY (provider, model, provider_key, hash)
);

-- FTS5 虚拟表（BM25）
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, id UNINDEXED, path, ...);

-- sqlite-vec 向量表
CREATE VIRTUAL TABLE chunks_vec USING vec0(...);
```

### 2.2 分块策略

按行累积字符，默认约 400 tokens（`maxChars = tokens * 4`），重叠 80 tokens（`overlapChars = overlap * 4`）：

```typescript
// packages/memory-host-sdk/src/host/internal.ts
export function chunkMarkdown(
  content: string,
  chunking: { tokens: number; overlap: number },
): MemoryChunk[] {
  const maxChars = Math.max(32, chunking.tokens * 4);
  const overlapChars = Math.max(0, chunking.overlap * 4);
  // 按行累积，到 maxChars 时 flush 一个 chunk
  // 保留 overlapChars 的尾部作为下一 chunk 开头
}
```

---

## 3. Memory 写入流程（核心技术）

**OpenClaw 的记忆写入与大多数系统不同——没有独立的「记忆提取管线」，而是由 LLM 直接操作 Markdown 文件。**

### 3.1 常规写入

文档约定：
- **`MEMORY.md`**：长期偏好、身份信息、工作流指引（bootstrap 文件，只读保护）
- **`memory/YYYY-MM-DD.md`**：日记类记忆（仅追加）
- 用户可通过「记住这个」等指令促使模型使用编辑工具写入磁盘

### 3.2 压缩前 Flush（关键创新）

当会话 token 接近上下文窗口上限时，系统触发**额外一轮静默运行**：

```
对话进行中... → token 接近 softThreshold → 触发 memory flush
  ↓
模型被指示：
  1. 将本次对话的持久信息写入 memory/2026-03-27.md
  2. 仅追加，不覆盖
  3. 不得修改 MEMORY.md、SOUL.md 等 bootstrap 文件
  ↓
flush 完成 → 执行正常的上下文压缩
```

Flush 护栏提示（`extensions/memory-core/src/flush-plan.ts`）：

```typescript
const MEMORY_FLUSH_TARGET_HINT =
  "Store durable memories only in memory/YYYY-MM-DD.md";
const MEMORY_FLUSH_APPEND_ONLY_HINT =
  "If memory/YYYY-MM-DD.md already exists, APPEND new content only";
const MEMORY_FLUSH_READ_ONLY_HINT =
  "Treat MEMORY.md, SOUL.md, TOOLS.md, AGENTS.md as read-only during flush";
```

防重复 flush 机制：通过 `compactionCount` / `memoryFlushCompactionCount` 配合，避免同一压缩周期重复 flush。

### 3.3 索引同步

chokidar 监视文件变更（去抖动），触发索引同步：

- **增量同步**：对比文件 hash/mtime，只重新分块和嵌入变化的文件
- **全量重建**：当 model、provider、chunk 参数等变化时，走 safe reindex（临时库 + 交换）

```typescript
const needsFullReindex =
  !meta ||
  meta.model !== this.provider.model ||
  meta.provider !== this.provider.id ||
  meta.chunkTokens !== this.settings.chunking.tokens ||
  meta.chunkOverlap !== this.settings.chunking.overlap ||
  (vectorReady && !meta?.vectorDims);
```

---

## 4. Memory 检索流程（核心技术）

### 4.1 混合检索架构

```
query
  │
  ├──→ BM25 (FTS5)  ──→ textScore (rank 映射)
  │                               │
  ├──→ Vector (sqlite-vec / JS)  ──→ vectorScore (余弦相似度)
  │                               │
  └──→ mergeHybridResults  ←──────┘
         │
         ├─ 加权融合：vectorWeight * vectorScore + textWeight * textScore
         ├─ 可选 Temporal Decay（时间衰减）
         ├─ 可选 MMR（最大边际相关性去重）
         │
         ↓
       排序后 Top-K 结果
```

### 4.2 检索实现细节

```typescript
// extensions/memory-core/src/memory/manager.ts
async search(query, opts) {
  // 1. 若索引脏，异步触发 sync
  void this.warmSession(opts?.sessionKey);

  // 2. 无嵌入 Provider 时：纯 FTS + extractKeywords 多词搜索
  if (!this.provider) {
    return this.searchKeywordOnly(cleaned, opts);
  }

  // 3. BM25 检索
  const keywordResults = hybrid.enabled && this.fts.available
    ? await this.searchKeyword(cleaned, candidates)
    : [];

  // 4. 向量检索（优先 sqlite-vec，否则 JS 余弦相似度）
  const queryVec = await this.embedQueryWithTimeout(cleaned);
  const vectorResults = await this.searchVector(queryVec, candidates);

  // 5. 混合打分
  return this.mergeHybridResults({
    vector: vectorResults,
    keyword: keywordResults,
    vectorWeight: hybrid.vectorWeight,
    textWeight: hybrid.textWeight,
    mmr: hybrid.mmr,
    temporalDecay: hybrid.temporalDecay,
  });
}
```

### 4.3 向量检索路径

- **sqlite-vec 路径**：使用 `vec_distance_cosine` 加速
- **JS 回退路径**：加载所有 chunk embedding 做内存中余弦相似度计算
- 兼容无扩展环境

### 4.4 Agent 提示词引导

```typescript
// extensions/memory-core/src/prompt-section.ts
if (hasMemorySearch && hasMemoryGet) {
  toolGuidance =
    "Before answering anything about prior work, decisions, dates, " +
    "people, preferences, or todos: run memory_search on MEMORY.md + " +
    "memory/*.md; then use memory_get to pull only the needed lines.";
}
```

---

## 5. 双后端容错设计

```typescript
// extensions/memory-core/src/memory/search-manager.ts
const wrapper = new FallbackMemoryManager(
  {
    primary: QmdMemoryManager,     // QMD sidecar (BM25+向量+rerank)
    fallbackFactory: async () => {
      return MemoryIndexManager.get(params);  // 内置 SQLite
    },
  }
);
```

- **QMD 后端**：外部高性能检索引擎（BM25 + 向量 + Rerank），支持 `retentionDays` 配置
- **内置 SQLite**：轻量、零依赖，任何环境可用
- **FallbackMemoryManager**：QMD 失败时自动回退到 SQLite

---

## 6. 记忆生命周期管理

| 能力 | 实现方式 |
|------|----------|
| **创建** | LLM 写 Markdown 文件 → 索引同步 → 分块嵌入入库 |
| **更新** | LLM 编辑文件 → chokidar 检测变更 → 重新分块嵌入 |
| **过期** | 无统一 TTL；QMD 侧可配 `retentionDays` |
| **蒸馏** | 依赖模型编辑 Markdown（无自动蒸馏服务） |
| **压缩** | flush 机制 + 用户策略（长期精炼放 MEMORY.md，日常放 memory/*.md） |
| **嵌入缓存** | embedding_cache 表 + maxEntries 配置 |
| **去重** | 检索侧按 chunk id 合并向量与 BM25 候选 |

---

## 7. 关键技术亮点

1. **Markdown 为唯一事实来源（SSOT）**：记忆是人可读、可编辑的文件，索引是可再生缓存
2. **混合检索（向量 + BM25）**：语义理解 + 精确匹配（ID/错误串/符号名等）互补
3. **sqlite-vec 加速 + JS 回退**：兼容所有环境
4. **双后端容错**：QMD 高性能 + SQLite 轻量，自动切换
5. **压缩前 flush 调度**：在上下文压缩前保存易失信息到持久文件
6. **多模态记忆**（可选）：非 `.md` 文件可构造结构化嵌入输入
7. **批量嵌入**：支持 OpenAI/Gemini 等 Batch API
8. **无需独立记忆提取服务**：利用 LLM + 文件工具完成写入，架构极简

---

## 8. 与 mem0 等系统的核心差异

| 维度 | OpenClaw | mem0 等 |
|------|----------|---------|
| 事实来源 | Markdown 文件 | 向量数据库 |
| 写入方式 | LLM 写文件 | LLM 抽取 → 管线写入 |
| 可审计性 | 天然（文本文件） | 需额外查询 |
| 蒸馏/合并 | 依赖模型行为 | 自动管线 |
| 部署复杂度 | 极低（文件+SQLite） | 需向量数据库 |
