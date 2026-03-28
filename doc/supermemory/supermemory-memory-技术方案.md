# Supermemory Memory 系统技术方案

> **项目定位**：Supermemory 是面向 AI 的**记忆与上下文引擎**（19k+ stars），从对话与多模态内容中自动抽取事实、维护用户画像、用混合检索同时覆盖「记忆」和「文档 RAG」，在 LongMemEval / LoCoMo / ConvoMem 三个主要 benchmark 排名第一。

**注意**：Supermemory 的核心引擎（事实抽取器、矛盾消解、混合检索打分）为托管服务，不在开源仓库中。以下分析基于公开文档、API schema 与 SDK 代码逆向推导。

---

## 1. 整体架构

```
┌──────────────┐
│   Client     │
│  (App/Agent/ │
│   MCP/插件)  │
└──────┬───────┘
       │ REST API
       ▼
┌──────────────────────────────────┐
│     Supermemory API Layer        │
│  ┌──────────┐  ┌──────────────┐ │
│  │/v3/documents│ │  /v4/search │ │
│  │(摄取内容) │  │  (记忆检索)  │ │
│  ├──────────┤  ├──────────────┤ │
│  │/v4/memories│ │  /v4/profile │ │
│  │(直接写记忆)│ │  (用户画像)  │ │
│  └──────────┘  └──────────────┘ │
└──────────┬───────────────────────┘
           ▼
┌──────────────────────────────────┐
│    Processing Pipeline           │
│  ┌────────┐ ┌────────┐ ┌──────┐│
│  │Extract │→│ Chunk  │→│Embed ││
│  └────────┘ └────────┘ └──┬───┘│
│                            ▼    │
│  ┌────────────────────────────┐ │
│  │ Index + Build Relationships│ │
│  └────────────────────────────┘ │
└──────────┬───────────────────────┘
           ▼
┌──────────────────────────────────┐
│     Knowledge Graph Storage      │
│  ┌──────────┐  ┌──────────────┐ │
│  │ Vectors  │  │ Relationships│ │
│  │ (HNSW)   │  │   (Graph)    │ │
│  └──────────┘  └──────────────┘ │
└──────────────────────────────────┘
```

---

## 2. 核心数据模型

### 2.1 文档（Document）

```typescript
// packages/validation/schemas.ts
const DocumentStatusEnum = z.enum([
  "unknown",
  "queued",       // 已入队
  "extracting",   // 正在抽取
  "chunking",     // 正在分块
  "embedding",    // 正在嵌入
  "indexing",     // 正在建索引
  "done",         // 完成
  "failed",       // 失败
]);

const DocumentTypeEnum = z.enum([
  "text", "webpage", "pdf", "image", "video",
  "audio", "markdown", "email", "csv", ...
]);
```

### 2.2 块（Chunk）

```typescript
const ChunkSchema = z.object({
  id: z.string(),
  documentId: z.string(),
  content: z.string(),
  embeddedContent: z.string().nullable(),
  type: ChunkTypeEnum,        // "text" | "image" | "code" | ...
  position: z.number(),

  // 多模型 Embedding（支持迁移与演进）
  embedding: z.array(z.number()).nullable(),
  embeddingModel: z.string().nullable(),
  embeddingNew: z.array(z.number()).nullable(),
  embeddingNewModel: z.string().nullable(),
  matryokshaEmbedding: z.array(z.number()).nullable(),  // Matryoshka 嵌入
  matryokshaEmbeddingModel: z.string().nullable(),

  createdAt: z.coerce.date(),
});
```

### 2.3 记忆条目（MemoryEntry）— 核心数据结构

```typescript
const MemoryEntrySchema = z.object({
  id: z.string(),
  memory: z.string(),              // 记忆正文

  // === 版本控制 ===
  version: z.number().default(1),
  isLatest: z.boolean().default(true),
  parentMemoryId: z.string().nullable(),   // 上一版本
  rootMemoryId: z.string().nullable(),     // 版本链根

  // === 记忆关系 ===
  memoryRelations: z.record(MemoryRelationEnum).default({}),
  // MemoryRelationEnum: "updates" | "extends" | "derives"

  // === 来源追踪 ===
  sourceCount: z.number().default(1),

  // === 状态标志 ===
  isInference: z.boolean().default(false),  // 推理生成的
  isForgotten: z.boolean().default(false),  // 已遗忘（软删除）
  isStatic: z.boolean().default(false),     // 静态事实（画像用）
  forgetAfter: z.coerce.date().nullable(),  // 定时遗忘
  forgetReason: z.string().nullable(),      // 遗忘原因

  // === Embedding ===
  memoryEmbedding: z.array(z.number()).nullable(),
  memoryEmbeddingNew: z.array(z.number()).nullable(),

  // === 空间隔离 ===
  spaceId: z.string(),
  orgId: z.string(),
  userId: z.string().nullable(),

  createdAt: z.coerce.date(),
  updatedAt: z.coerce.date(),
});
```

### 2.4 记忆-文档溯源

```typescript
const MemoryDocumentSourceSchema = z.object({
  memoryEntryId: z.string(),
  documentId: z.string(),
  relevanceScore: z.number().default(100),
  addedAt: z.coerce.date(),
});
```

---

## 3. 图记忆与关系系统（核心技术）

### 3.1 三种关系类型

```
┌─────────────────────────────────────────────────────┐
│                  Memory Graph                        │
│                                                      │
│  Memory A: "Alex works at Google"                    │
│      │                                               │
│      │ UPDATES (矛盾替代)                             │
│      ▼                                               │
│  Memory B: "Alex started at Stripe as PM"            │
│      │ (isLatest: true)                              │
│      │                                               │
│      │ EXTENDS (信息扩展)                             │
│      ▼                                               │
│  Memory C: "Alex leads the payments team at Stripe"  │
│      │                                               │
│      │ DERIVES (推理派生)                             │
│      ▼                                               │
│  Memory D: "Alex has PM experience at tech companies"│
│      (isInference: true)                             │
│                                                      │
└─────────────────────────────────────────────────────┘
```

| 关系 | 含义 | 触发场景 |
|------|------|----------|
| **`updates`** | 新信息**矛盾替代**旧信息 | "Alex 换工作了" |
| **`extends`** | 新信息**扩展补充**已有信息 | "Alex 还负责支付团队" |
| **`derives`** | 从已有信息**推理派生**新事实 | "Alex 有多家科技公司 PM 经验" |

### 3.2 版本链

```
rootMemoryId ←── parentMemoryId ←── parentMemoryId ←── 当前版本
(v1, isLatest:false)                                 (v3, isLatest:true)
```

检索时通过 `isLatest: true` 过滤，只返回最新版本，同时保留完整历史。

---

## 4. 矛盾检测与知识更新策略（核心技术）

### 4.1 自动矛盾处理

```
新信息："Alex just started at Stripe"
    │
    ▼
系统检测到与现有记忆矛盾：
    已有："Alex works at Google as a software engineer"
    │
    ▼
创建新 MemoryEntry:
    memory: "Alex just started at Stripe as a PM"
    version: 2
    isLatest: true
    parentMemoryId: 旧记忆 id
    memoryRelations: {旧记忆id: "updates"}
    │
    ▼
旧记忆更新:
    isLatest: false
```

### 4.2 遗忘机制

```typescript
// 定时遗忘
{
  isForgotten: false,
  forgetAfter: "2026-06-27T00:00:00Z",  // 3个月后过期
  forgetReason: null
}

// 主动遗忘（POST .../forget）
{
  isForgotten: true,
  forgetReason: "User requested deletion"
}
```

遗忘后不从数据库物理删除，而是**软删除**：`isForgotten: true` 后不再出现在搜索结果中。

---

## 5. 用户画像系统（核心技术）

### 5.1 画像组成

```
User Profile
    │
    ├──→ Static Facts（长期稳定）
    │      "用户是后端开发者"
    │      "偏好 TypeScript"
    │      "在上海工作"
    │
    └──→ Dynamic Context（近期上下文）
           "正在研究 AI 记忆系统"
           "最近在看 OpenViking 文档"
```

### 5.2 画像更新流程

```
内容摄入
    │
    ▼
AI 分析提取与用户相关事实
    │
    ▼
生成 Profile 操作（add / update / remove）
    │
    ▼
实时更新画像
    │
    ▼
下次 /v4/profile 调用时返回最新画像
```

### 5.3 画像在 Agent 中的注入

```typescript
// packages/tools/src/shared/memory-client.ts
async function supermemoryProfileSearch(containerTag, queryText, baseUrl, apiKey) {
  const response = await fetch(`${baseUrl}/v4/profile`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${apiKey}` },
    body: JSON.stringify({ q: queryText, containerTag })
  });

  // 返回结构：
  // {
  //   profile: { static: [...], dynamic: [...] },
  //   searchResults: [...]
  // }

  // → 去重后转 Markdown 注入系统提示
}
```

---

## 6. 写入流程

### 6.1 路径 A：摄取原始内容（异步管线）

```
POST /v3/documents
    │ (文件/URL/文本)
    ▼
Document created (status: "queued")
    │
    ▼ extracting
多模态提取
    │  PDF → 文本 + OCR
    │  图片 → OCR + 视觉描述
    │  视频 → 转写 + 关键帧
    │  网页 → Markdowner 正文抽取
    │
    ▼ chunking
语义分块（句级 + 2句 overlap）
    │
    ▼ embedding
向量化
    │
    ▼ indexing
建索引 + 构建关系图
    │
    ▼ done
Document status: "done"
多条 Memory entries 生成
```

### 6.2 路径 B：直接写入结构化记忆

```
POST /v4/memories
    │ { content: "用户喜欢 TypeScript", isStatic: true }
    ▼
立即嵌入并可搜
```

### 6.3 路径 C：版本化更新

```
PATCH /v4/memories/{id}
    │ { content: "用户现在更喜欢 Rust" }
    ▼
创建新版本：version +1, isLatest: true
旧版本：isLatest: false
```

---

## 7. 检索流程

### 7.1 三种检索入口

| 入口 | 适用场景 | 返回内容 |
|------|----------|----------|
| `POST /v4/search` (memories) | 个性化记忆检索 | MemoryEntry 列表 |
| `POST /v4/search` (hybrid) | 记忆 + 文档混合 | Memory 和 Chunk 混合列表 |
| `POST /v3/search` | 纯文档 RAG | Chunk 列表（支持 rerank/rewrite） |
| `POST /v4/profile` | 画像 + 可选检索 | Profile + SearchResults |

### 7.2 混合搜索模式（Hybrid Search）

```
query: "Alex 在哪里工作？"
searchMode: "hybrid"
    │
    ▼
Step 1: 搜索记忆
    │  Memory: "Alex started at Stripe as PM" (score: 0.95)
    │  Memory: "Alex leads payments team" (score: 0.82)
    │
    ▼
Step 2: 回退搜索文档 chunks
    │  Chunk: "...Stripe 2026 年组织架构..." (score: 0.71)
    │
    ▼
Step 3: 合并与去重
    │  按相似度分数统一排序
    │  去重（memory 和 chunk 可能来自同一事实）
    │
    ▼
返回: [
  { memory: "Alex started at Stripe as PM", score: 0.95 },
  { memory: "Alex leads payments team", score: 0.82 },
  { chunk: "...Stripe 2026 年组织架构...", score: 0.71 },
]
```

### 7.3 记忆 vs RAG 的区别

```
                Memory                          RAG (Document Chunks)
定义          从内容中提取的事实               原始内容的分块
粒度          细粒度（单个事实/偏好）          粗粒度（文本段落）
更新          会随新信息演进和替代             静态不变
个性化        高（关联用户画像）               低（通用检索）
典型用途      "用户喜欢什么？"                "文档里怎么说的？"
```

---

## 8. 多模态提取支持

| 类型 | 处理方式 |
|------|----------|
| **纯文本** | 直接分块嵌入 |
| **PDF** | 文本结构提取 + 扫描件 OCR |
| **图片** | OCR 文字识别 + AI 视觉描述 |
| **视频** | 语音转写 + 关键帧提取 |
| **网页 URL** | Markdowner 正文抽取 |
| **邮件** | 结构化解析 |
| **CSV** | 表格解析 |
| **代码** | 语法感知分块 |

类型自动检测，基于 URL/MIME/扩展名/内容结构。

---

## 9. 空间隔离（Multi-tenant）

```typescript
const SpaceSchema = z.object({
  id: z.string(),
  orgId: z.string(),
  containerTag: z.string(),  // 隔离标签
  description: z.string().nullable(),
  createdAt: z.coerce.date(),
});
```

- **containerTag**：不同应用/场景/用户群的记忆隔离
- 同一组织下可有多个 Space
- 搜索和画像都在 Space 范围内执行

---

## 10. Embedding 演进策略

Chunk 和 Memory 上都保留多路 embedding 字段：

```typescript
{
  embedding: [...],           // 初始模型
  embeddingModel: "v1",
  embeddingNew: [...],        // 新模型
  embeddingNewModel: "v2",
  matryokshaEmbedding: [...], // Matryoshka 嵌入（可变维度）
  matryokshaEmbeddingModel: "matryoshka-v1",
}
```

支持**渐进式迁移**：新模型上线后并行写入新字段，查询时可切换，避免一次性全量重建。

---

## 11. 关键技术亮点

1. **记忆 vs RAG 双轨统一检索**：hybrid 模式同时搜记忆和文档 chunk，按分数合并
2. **图式关系 + 版本链**：`updates/extends/derives` 三种关系 + `isLatest` 版本控制，自动处理矛盾
3. **用户画像作为一等公民**：static/dynamic 分离，自动维护，直接注入 Agent 系统提示
4. **多模态自动提取**：PDF/图片/视频/网页统一管线
5. **可观测摄入管道**：`DocumentStatusEnum` 七阶段状态机
6. **Embedding 演进**：多路 embedding 字段支持平滑模型迁移
7. **遗忘机制**：定时遗忘 + 主动遗忘 + 软删除语义
8. **空间隔离**：containerTag 实现多租户

---

## 12. 与其他系统的对比

| 维度 | Supermemory | Mem0 | OpenViking |
|------|-------------|------|------------|
| 引擎开源 | 否（托管服务） | 是 | 是 |
| 记忆粒度 | 事实级 | 事实级 | 目录/文件级 |
| 关系图谱 | updates/extends/derives | 实体-关系三元组 | 目录树结构 |
| 画像系统 | 内置一等公民 | 无（需自建） | 无 |
| RAG 融合 | 原生 hybrid 模式 | 独立 | L0/L1/L2 渐进 |
| 矛盾处理 | 自动 + 版本链 | LLM 决策 | 无显式机制 |
| 遗忘机制 | forgetAfter + isForgotten | delete | 无显式机制 |
| 多模态 | PDF/图片/视频/网页 | 可选 vision | 多模态 + AST |
