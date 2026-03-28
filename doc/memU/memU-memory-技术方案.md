# MemU Memory 系统技术方案

> **项目定位**：MemU 是面向 24/7 主动式 AI Agent 的**智能体记忆框架**（13k+ stars），用「文件系统」隐喻组织三层记忆架构（Resource → Memory Item → Memory Category），支持 RAG 与 LLM 双模式检索、多模态输入、显著性排序与记忆自演化，目标是让 Agent 通过记忆而非更长的上下文窗口来演进能力。

---

## 1. 整体架构

```
┌────────────────────────────────────────────────────────────┐
│              MemoryService（组合根）                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐  │
│  │Memorize  │ │Retrieve  │ │  CRUD    │ │ Workflow     │  │
│  │ Mixin    │ │ Mixin    │ │  Mixin   │ │ Pipeline     │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘  │
│       │            │            │               │          │
│  ┌────▼────────────▼────────────▼───────────────▼───────┐  │
│  │              Workflow Engine                           │  │
│  │  WorkflowStep → PipelineManager → LocalRunner         │  │
│  │  (requires/produces/capabilities + interceptor)       │  │
│  └──────────────────────┬────────────────────────────────┘  │
│                         │                                   │
│  ┌──────────────────────▼────────────────────────────────┐  │
│  │              Database Layer（四个 Repository）          │  │
│  │  ResourceRepo │ MemoryItemRepo │ MemoryCategoryRepo   │  │
│  │                                │ CategoryItemRepo     │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐              │  │
│  │  │ InMemory │ │ SQLite   │ │ Postgres │              │  │
│  │  │(+cosine) │ │(+JSON vec)│ │(+pgvector)│             │  │
│  │  └──────────┘ └──────────┘ └──────────┘              │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │              LLM Layer                                 │  │
│  │  llm_profiles: { default, embedding, ... }            │  │
│  │  backends: OpenAI SDK │ httpx │ LazyLLM               │  │
│  │  providers: OpenAI │ Grok │ OpenRouter │ Doubao       │  │
│  └───────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

### 核心设计理念：Memory as File System

MemU 将 AI 记忆类比为文件系统：

| 文件系统概念 | MemU 对应 | 说明 |
|-------------|-----------|------|
| 文件夹 | Memory Category | 按主题组织的聚合层 |
| 文件 | Memory Item | 最小有意义的独立记忆单元 |
| 符号链接 | Cross-references (`[ref:xxx]`) | 条目间的交叉引用 |
| 挂载资源 | Resource | 原始多模态数据（对话、文档、图片等） |

**核心主张**：有限的上下文窗口应被精炼的、检索到的、精确匹配的记忆填充——而非端到端堆积原始信息。

---

## 2. 三层记忆架构（核心抽象）

```
┌─────────────────────────────────────────────────────┐
│         Memory Category Layer（聚合层）               │
│  结构化文本记忆文件，注入 Agent 上下文的最终形态       │
│  可带 [ref:xxx] 引用链接回 Memory Item               │
│         ▲ 聚合 (LLM summary)     │ 检索入口          │
├─────────┼───────────────────────┼───────────────────┤
│         │                       ▼                    │
│         Memory Item Layer（条目层）                    │
│  最小有意义单元，自然语言句子                          │
│  带 embedding 向量 + memory_type 分类                 │
│  6种类型: profile|event|knowledge|behavior|skill|tool │
│         ▲ 抽取 (LLM)            │ 中间层检索          │
├─────────┼───────────────────────┼───────────────────┤
│         │                       ▼                    │
│         Resource Layer（资源层）                       │
│  原始多模态数据：文本、对话、文档、图片、音频、视频    │
│  重点：完整性 + 可溯源，不做早期抽象                   │
│  caption 的 embedding 用于资源级检索                  │
└─────────────────────────────────────────────────────┘

三层保持双向全链路可溯源：Category → Item → Resource
```

---

## 3. 数据模型

### 3.1 核心模型定义

```python
# src/memu/database/models.py

MemoryType = Literal["profile", "event", "knowledge", "behavior", "skill", "tool"]

class BaseRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime
    updated_at: datetime

class Resource(BaseRecord):
    url: str                           # 原始资源 URL
    modality: str                      # conversation|document|image|audio|video
    local_path: str                    # 本地存储路径
    caption: str | None                # 短摘要（多模态统一文本化后产生）
    embedding: list[float] | None      # caption 的向量，用于资源级检索

class MemoryItem(BaseRecord):
    resource_id: str | None            # 关联的原始资源
    memory_type: str                   # 6种类型之一
    summary: str                       # 自然语言记忆内容
    embedding: list[float] | None      # summary 的向量
    happened_at: datetime | None       # 事件发生时间
    extra: dict[str, Any] = {}         # 扩展字段（见下）

class MemoryCategory(BaseRecord):
    name: str                          # 类目名称
    description: str                   # 类目描述
    embedding: list[float] | None      # name+description 的初始化向量
    summary: str | None                # LLM 合成的聚合文本（持续演化）

class CategoryItem(BaseRecord):
    item_id: str                       # Memory Item ID
    category_id: str                   # Memory Category ID
```

### 3.2 Memory Item 的 extra 字段

`extra` 字典支持多种扩展能力：

| 字段 | 说明 |
|------|------|
| `content_hash` | 内容哈希，用于强化去重 |
| `reinforcement_count` | 强化次数（相同内容重复出现时累加） |
| `last_reinforced_at` | 最后强化时间 |
| `ref_id` | 短 ID，被 Category summary 中的 `[ref:xxx]` 引用 |
| `when_to_use` | 何时应检索此记忆的提示（Tool Memory 专用） |
| `metadata` | 类型特定元数据（如 tool_name、avg_success_rate） |
| `tool_calls` | 工具调用历史（序列化的 ToolCallResult 列表） |

### 3.3 六种记忆类型

| 类型 | 说明 | 示例 |
|------|------|------|
| `profile` | 用户/实体的个人信息 | "用户是一名后端工程师，偏好 Python" |
| `event` | 过去发生的事件 | "2026年3月用户完成了认证模块重构" |
| `knowledge` | 事实性知识 | "项目使用 PostgreSQL 14 数据库" |
| `behavior` | 行为习惯与模式 | "用户总是先写测试再写实现" |
| `skill` | 可复用的技能方法论 | "用户习惯用 git worktree 做并行开发" |
| `tool` | 工具使用经验 | "搜索 API 在关键词少于3个字时效果差" |

### 3.4 多租户：Scoped Models

```python
# 通过 UserConfig.model 动态生成带 scope 字段的模型
class DefaultUserModel(BaseModel):
    user_id: str | None = None

# build_scoped_models(DefaultUserModel) 生成：
# DefaultUserModelResource(user_id, url, modality, ...)
# DefaultUserModelMemoryItem(user_id, resource_id, summary, ...)
# ...
# scope 字段自动成为表列，实现多用户记忆隔离
```

---

## 4. 默认记忆分类（10 个预置 Category）

```python
# src/memu/app/settings.py
_default_memory_categories = [
    {"name": "personal_info",   "description": "Personal information about the user"},
    {"name": "preferences",     "description": "User preferences, likes and dislikes"},
    {"name": "relationships",   "description": "Information about relationships with others"},
    {"name": "activities",      "description": "Activities, hobbies, and interests"},
    {"name": "goals",           "description": "Goals, aspirations, and objectives"},
    {"name": "experiences",     "description": "Past experiences and events"},
    {"name": "knowledge",       "description": "Knowledge, facts, and learned information"},
    {"name": "opinions",        "description": "Opinions, viewpoints, and perspectives"},
    {"name": "habits",          "description": "Habits, routines, and patterns"},
    {"name": "work_life",       "description": "Work-related information and professional life"},
]
```

---

## 5. 记忆写入流程（Memorize Pipeline）

### 5.1 工作流步骤

```
memorize(resource_url, modality, user)
        │
        ▼
Step 1: ingest_resource
        │  LocalFS.fetch() → 本地路径 + 原始文本
        ▼
Step 2: preprocess_multimodal
        │  按 modality 分发预处理：
        │  ├─ conversation → 分段 + 每段 LLM 摘要
        │  ├─ document → LLM 压缩 + caption
        │  ├─ image → Vision API → description + caption
        │  ├─ video → ffmpeg 抽帧 → Vision API
        │  └─ audio → transcribe/读 txt → LLM 处理
        │  输出：[{text, caption}, ...]
        ▼
Step 3: extract_items
        │  对每个 preprocessed 段：
        │  ├─ 按 memory_type 并行构建 prompt
        │  ├─ LLM 并发抽取 → XML 格式
        │  └─ 解析得 [(memory_type, content, [category_names])]
        ▼
Step 4: dedupe_merge
        │  （当前为占位，直接透传）
        ▼
Step 5: categorize_items
        │  ├─ create_resource（caption 做 embed）
        │  ├─ embed(summary) → 批量向量化
        │  ├─ create_item（可选强化去重）
        │  └─ link_item_category（建立多对多关联）
        ▼
Step 6: persist_index
        │  ├─ _update_category_summaries（LLM 合并已有 summary + 新条目）
        │  └─ 可选：_persist_item_references（从 summary 中解析 [ref:xxx]）
        ▼
Step 7: build_response
        │  组装 resource(s) + items + categories + relations
        ▼
返回结果
```

### 5.2 记忆抽取的 XML 格式

LLM 按每种 memory_type 输出结构化 XML：

```xml
<profile>
  <memory>
    <content>User is a backend engineer who prefers Python</content>
    <categories>
      <category>personal_info</category>
      <category>work_life</category>
    </categories>
  </memory>
  <memory>
    <content>User likes dark mode and vim keybindings</content>
    <categories>
      <category>preferences</category>
    </categories>
  </memory>
</profile>
```

### 5.3 Category Summary 持续演化

每次写入新记忆后，受影响的 Category 会用 LLM 重新合成 summary：

```
输入：
  - 原有 category.summary（已有的聚合文本）
  - 新增 memory items（本次写入的条目）
  - target_length（目标长度，默认 400）

LLM 合成任务：
  将原 summary 与新条目融合，输出更新后的聚合文本

可选 enable_item_references：
  summary 中嵌入 [ref:abc123] 引用标记
  → 检索时可沿引用链精确定位 Memory Item
```

---

## 6. 检索流程（Retrieve Pipeline）

MemU 提供两种检索模式，均遵循 **Category → Item → Resource** 的 top-down 层级搜索。

### 6.1 RAG 检索模式（embedding-based）

```
retrieve(queries, where, method="rag")
        │
        ▼
Step 1: route_intention
        │  LLM 判断是否需要检索 + 可选 query 改写
        │  输出：needs_retrieval, rewritten_query
        ▼
Step 2: route_category
        │  embed(active_query) → query_vector
        │  对所有 category 的 summary 文本现场 embed
        │  cosine_topk 匹配 → category_hits
        │  ★ 注意：用 summary 向量而非初始化的 name+desc 向量
        ▼
Step 3: sufficiency_after_category
        │  LLM 判断 category 层结果是否充分
        │  不充分 → 改写 query + 重算 embedding
        ▼
Step 4: recall_items
        │  vector_search_items(query_vector, top_k)
        │  ranking 策略：similarity 或 salience
        ▼
Step 5: sufficiency_after_items
        │  LLM 判断 item 层结果是否充分
        │  不充分 → 继续深入 resource 层
        ▼
Step 6: recall_resources
        │  资源 caption embedding 语料库 → cosine_topk
        ▼
Step 7: build_context
        │  拼装 categories + items + resources（含 score）
        ▼
返回 {categories, items, resources, next_step_query}
```

### 6.2 LLM 检索模式（non-embedding ranking）

```
retrieve(queries, where, method="llm")
        │
        ▼
Step 1: route_intention（同 RAG）
        ▼
Step 2: route_category
        │  将所有 category 信息格式化为文本
        │  → LLM_CATEGORY_RANKER_PROMPT
        │  → LLM 输出 JSON {"categories": [id, ...]}
        │  纯 LLM 排序，无 ANN
        ▼
Step 3: sufficiency_after_category
        │  LLM judger 判断 proceed_to_items
        ▼
Step 4: recall_items
        │  可选 use_category_references：
        │    从命中 category summary 中 extract_references
        │    → list_items_by_ref_ids（精确拉取）
        │  否则全量 items → LLM_ITEM_RANKER_PROMPT
        │  → JSON {items: [id, ...]}
        ▼
Step 5: sufficiency_after_items
        ▼
Step 6: recall_resources
        │  仅考虑与 ranked items 关联的 resource_id
        │  → LLM_RESOURCE_RANKER_PROMPT → JSON
        ▼
Step 7: build_context
        ▼
返回结果（无向量分数，LLM 排序结果）
```

### 6.3 充分性驱动的逐级加深

这是 MemU 检索的独特设计——每层检索后都有一个 **sufficiency check**：

```
                    ┌──────────────────┐
                    │ Category 层检索  │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │ 充分？（LLM 判断）│
                    └────┬────────┬────┘
                    YES  │        │ NO
              ┌──────────▼┐  ┌───▼──────────┐
              │ 直接返回  │  │ 改写 query   │
              │ category  │  │ 重算 embedding│
              │ 结果      │  │ 进入 Item 层 │
              └───────────┘  └───┬──────────┘
                                 │
                        ┌────────▼─────────┐
                        │ Item 层检索      │
                        └────────┬─────────┘
                                 │
                        ┌────────▼─────────┐
                        │ 充分？           │
                        └────┬────────┬────┘
                        YES  │        │ NO
                  ┌──────────▼┐  ┌───▼──────────┐
                  │ 返回      │  │ 进入 Resource│
                  │ items     │  │ 层检索       │
                  └───────────┘  └──────────────┘
```

---

## 7. 显著性排序（Salience Scoring）

当 `RetrieveItemConfig.ranking == "salience"` 时，不单纯按向量相似度排序，而是综合三个因子：

```python
# src/memu/database/inmemory/vector.py
def salience_score(similarity, reinforcement_count, last_reinforced_at, recency_decay_days=30.0):
    reinforcement_factor = math.log(reinforcement_count + 1)  # 对数阻尼
    recency_factor = math.exp(-0.693 * days_ago / recency_decay_days)  # 半衰期衰减
    return similarity * reinforcement_factor * recency_factor
```

| 因子 | 公式 | 说明 |
|------|------|------|
| 相似度 | cosine(query, item) | 语义相关性 |
| 强化因子 | log(reinforcement_count + 1) | 对数阻尼，防止频繁出现的事实过度主导 |
| 时效因子 | exp(-0.693 × days / half_life) | 半衰期指数衰减（默认 30 天半衰） |

**强化去重机制**：`enable_item_reinforcement` 开启后，相同内容（`compute_content_hash`）再次出现时不创建新条目，而是 `reinforcement_count++`，影响后续 salience 排序。

---

## 8. 多模态支持

### 8.1 预处理分发

| Modality | 预处理方式 | 输出 |
|----------|-----------|------|
| `conversation` | `format_conversation_for_preprocess` → LLM 分段 → 每段 `_summarize_segment` | [{text, caption}, ...] 多段 |
| `document` | 模板 format → LLM 压缩 | [{processed_content, caption}] |
| `image` | `llm_client.vision(template, image_path)` | [{description, caption}] |
| `video` | `VideoFrameExtractor.extract_middle_frame` (ffmpeg) → vision API | [{description, caption}] |
| `audio` | `llm_client.transcribe()` 或读 `.txt` → LLM 处理 | [{processed_content, caption}] |

### 8.2 多模态统一为文本

这是 MemU 的刻意工程选择：

- **Resource 层**：保留原始多模态数据（完整性 + 可溯源）
- **Memory Item 层**：全部转化为自然语言文本句子
- **Memory Category 层**：纯文本聚合文件

好处：
1. 当前 LLM 对文本的推理能力最强
2. 高层记忆结构保持一致，无需为不同模态维护并行系统
3. Resource 层原始数据永不丢弃，必要时可回溯

---

## 9. Workflow Engine（工作流引擎）

### 9.1 WorkflowStep 定义

```python
# src/memu/workflow/step.py
@dataclass
class WorkflowStep:
    step_id: str                    # 步骤 ID
    role: str                       # 角色标识
    handler: Callable               # 执行函数
    requires: set[str]              # 前置依赖（state 中必须存在的 key）
    produces: set[str]              # 产出（写入 state 的 key）
    capabilities: set[str]          # 所需能力：{"llm", "db", "vector", "io"}
    config: dict[str, Any] | None   # 步骤配置（如 llm_profile）
```

### 9.2 Pipeline 管理

```python
# src/memu/workflow/pipeline.py
class PipelineManager:
    def register(name, steps)       # 注册命名工作流
    def build(name)                 # 构建可执行管线
    # 支持运行时替换/插入步骤
```

已注册的工作流：
- `memorize`：7 步写入管线
- `retrieve_rag`：7 步 RAG 检索管线
- `retrieve_llm`：7 步 LLM 检索管线

### 9.3 拦截器

```python
# src/memu/workflow/interceptor.py
# 支持在 step 执行前后注入自定义逻辑
```

---

## 10. 存储后端

### 10.1 三种后端

| 后端 | 元数据存储 | 向量索引 | 适用场景 |
|------|-----------|---------|---------|
| InMemory | Python dict | cosine brute-force (numpy) | 开发/测试 |
| SQLite | SQLModel + FTS | JSON 存向量 + brute-force | 单机轻量部署 |
| Postgres | SQLModel + Alembic 迁移 | pgvector（可选 brute-force 回退） | 生产环境 |

### 10.2 配置示例

```python
# InMemory
DatabaseConfig(metadata_store=MetadataStoreConfig(provider="inmemory"))

# SQLite
DatabaseConfig(metadata_store=MetadataStoreConfig(
    provider="sqlite", dsn="sqlite:///./data/memory.db"
))

# Postgres + pgvector
DatabaseConfig(metadata_store=MetadataStoreConfig(
    provider="postgres",
    dsn="postgresql+psycopg://user:pass@localhost:5432/memu",
    ddl_mode="create"  # 或 "validate"
))
```

---

## 11. CRUD 与增量 Patch

除了 `memorize` 批量写入外，MemU 支持单条记忆的 CRUD 操作：

```python
# src/memu/app/crud.py
class CRUDMixin:
    async def create_memory_item(summary, memory_type, category_ids, propagate=True)
    async def update_memory_item(item_id, summary, propagate=True)
    async def delete_memory_item(item_id, propagate=True)
```

当 `propagate=True` 时，CRUD 操作会触发 **Category Patch**——不重算整个 summary，而是用 `CATEGORY_PATCH_PROMPT` 让 LLM 做增量修补：

```
输入：原 category summary + 变更描述（新增/修改/删除了什么）
LLM 输出：{ "need_update": true, "updated_content": "..." }
```

这与 memorize 的「全量合并式 summary」形成两套策略：
- **Memorize**：全量 LLM 合成（适合批量写入）
- **CRUD Patch**：增量 LLM 修补（适合单条操作）

---

## 12. 引用链机制（Item References）

当 `enable_item_references=True` 时：

1. **写入时**：Category summary 中嵌入 `[ref:abc123]` 标记，引用源 Memory Item 的短 ID
2. **检索时**：LLM 模式下 `use_category_references=True`，从命中的 category summary 中提取 `[ref:xxx]`，通过 `list_items_by_ref_ids` 精确拉取关联条目

```
Category Summary 示例：
"用户是一名后端工程师 [ref:a1b2c3]，擅长 Python [ref:d4e5f6]，
 目前在做微服务重构项目 [ref:g7h8i9]。"

检索时：
extract_references(summary) → {"a1b2c3", "d4e5f6", "g7h8i9"}
→ 精确拉取这 3 条 Memory Item 的完整内容
```

---

## 13. LLM 配置与多 Profile

```python
# src/memu/app/settings.py
class LLMConfig(BaseModel):
    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = "OPENAI_API_KEY"
    chat_model: str = "gpt-4o-mini"
    embed_model: str = "text-embedding-3-small"
    client_backend: str = "sdk"  # "sdk" | "httpx" | "lazyllm_backend"

# 多 profile 配置：
MemoryService(
    llm_profiles={
        "default": LLMConfig(chat_model="gpt-4o-mini"),
        "embedding": LLMConfig(embed_model="text-embedding-3-small"),
        "deep": LLMConfig(chat_model="gpt-4o", base_url="..."),
    }
)
```

不同工作流步骤可指定不同 profile（如 `preprocess_llm_profile`、`memory_extract_llm_profile`、`category_update_llm_profile`），实现成本与质量的精细控制。

---

## 14. 生态三件套

| 组件 | 许可证 | 说明 |
|------|--------|------|
| **memU** | Apache 2.0 | 核心算法引擎，记忆抽取/组织/检索 |
| **memU-server** | AGPL-3.0 | 自托管后端服务，CRUD + RBAC + 用量统计 |
| **memU-ui** | - | 前端 Dashboard，可视化记忆浏览和管理 |

云服务：`https://api.memu.so`（v3 REST API）

---

## 15. 关键技术亮点

1. **三层文件系统式记忆架构**：Resource → Item → Category，双向全链路可溯源
2. **双模式检索（RAG + LLM）**：向量搜索快速召回 vs LLM 直读记忆文件深度理解
3. **充分性驱动的逐级加深检索**：每层后 LLM 判断是否继续深入，避免过度检索
4. **Salience 显著性排序**：相似度 × 强化因子 × 时效因子，比纯向量相似度更智能
5. **Category Summary 持续演化**：每次写入后 LLM 重新合成聚合文本，记忆结构随使用自然演化
6. **引用链 [ref:xxx]**：Category → Item 的精确溯源机制
7. **多模态统一为文本**：底层保留原始数据，上层统一文本格式，工程简洁性与完整性兼得
8. **工作流引擎**：带 requires/produces 契约的步骤编排 + 拦截器，支持运行时管线定制
9. **多 LLM Profile**：不同步骤可用不同模型，精细控制成本与质量
10. **强化去重**：相同内容不重复存储，通过 reinforcement_count 累积重要性

---

## 16. 版本演进要点

| 版本 | 关键变更 |
|------|---------|
| 0.3 | 三层架构 + memorize/retrieve 双管线上线 |
| 0.4 | 非 RAG（LLM）检索模式 |
| 0.8 | 工作流引擎 + Postgres 后端 |
| 0.9 | CRUD Patch 工作流 |
| 1.0.0 | BREAKING：完整三件套生态，用户模型/范围/工作流支持 |
| 1.2.0 | SQLite 后端、工作流拦截器、清空记忆 |
| 1.3.0 | `happened_at`/`extra` 字段、LazyLLM/LangGraph/Grok/OpenRouter 集成 |
| 1.4.0 | Category 内联引用、Salience 显著性排序、Tool Memory 类型 |
| 1.5.0 | Patch 非传播选项、HTTP 代理支持、架构文档与 ADR |
| 1.5.1 | Alembic 迁移修复（当前最新） |

---

## 17. 当前局限

- **dedupe_merge 占位**：写入管线中的去重/合并步骤当前为空实现
- **Self-Evolution 未独立成模块**：自演化主要通过 Category summary 重写和 salience 机制间接实现，无显式的"遗忘"或"重组"调度器
- **SQLite/InMemory 向量检索为暴力扫描**：大规模数据场景需依赖 Postgres + pgvector
- **patch.py 与 crud.py 存在重复代码**：`PatchMixin` 未被 `MemoryService` 继承，属于未清理的冗余
- **Python 3.13+ 要求**：较高的 Python 版本门槛
