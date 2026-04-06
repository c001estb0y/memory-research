# Graphiti 架构原理与 codebuddy-mem 存储召回分析

> 基于 Graphiti 源码（`graphiti_core/`）分析其时序知识图谱架构，并以 codebuddy-mem 的真实记忆数据为示例，演示在 Graphiti 中的存储和召回过程。

---

## 1. Graphiti 是什么

Graphiti 是 Zep 开源的**时序上下文图引擎**（Temporal Context Graph Engine），专为 AI Agent 设计。与 Mem0 的"向量+可选图"双轨架构不同，Graphiti 将**所有数据统一存储在图数据库中**（Neo4j / FalkorDB / Kuzu），且每条事实都带有**时间有效窗口**——记录它何时为真、何时被推翻。

核心理念：**一切皆 Episode → 一切皆图 → 一切有时间**。

---

## 2. 数据模型：四种节点 + 五种边

### 2.1 节点类型

源码 `graphiti_core/nodes.py`：

| 节点类型 | 标签 | 核心字段 | 说明 |
|----------|------|----------|------|
| **EpisodicNode** | `Episodic` | `content`, `source`, `source_description`, `valid_at`, `entity_edges[]` | 原始数据的忠实记录，一切溯源的起点 |
| **EntityNode** | 自定义标签 | `name`, `name_embedding`, `summary`, `attributes{}` | 实体（人、项目、工具等），摘要随图演化 |
| **CommunityNode** | `Community` | `name`, `name_embedding`, `summary` | 实体聚类的社区摘要 |
| **SagaNode** | `Saga` | `name` | 串联有序 Episode 序列 |

**关键设计**：`EpisodicNode` 是 Graphiti 与 Mem0 最大的区别——**原始数据被完整保存为图中的一等公民**，而非像 Mem0 那样只存抽取后的原子 fact。

```python
# 源码 nodes.py EpisodicNode
class EpisodicNode(Node):
    source: EpisodeType           # 'message' | 'json' | 'text'
    source_description: str       # 数据来源描述
    content: str                  # 原始完整内容
    valid_at: datetime            # 原文档/事件的业务时间
    entity_edges: list[str]       # 本 episode 关联的事实边 UUID
```

```python
# 源码 nodes.py EntityNode
class EntityNode(Node):
    name_embedding: list[float]   # 名称向量，用于实体解析
    summary: str                  # 随新信息动态更新的摘要
    attributes: dict[str, Any]    # 自定义属性（如 Pydantic 模型定义）
```

### 2.2 边类型

源码 `graphiti_core/edges.py`：

| 边类型 | 关系名 | 核心字段 | 说明 |
|--------|--------|----------|------|
| **EntityEdge** | `RELATES_TO` | `name`, `fact`, `fact_embedding`, `valid_at`, `invalid_at`, `expired_at`, `episodes[]` | **事实边**——Graphiti 的核心 |
| **EpisodicEdge** | `MENTIONS` | 基础字段 | Episode → Entity 的提及关系 |
| **CommunityEdge** | `HAS_MEMBER` | 基础字段 | Community → Entity 的成员关系 |
| **HasEpisodeEdge** | `HAS_EPISODE` | 基础字段 | Saga → Episode |
| **NextEpisodeEdge** | `NEXT_EPISODE` | 基础字段 | Episode 时序链 |

**事实边的时间三字段**（Graphiti 的核心创新）：

```python
# 源码 edges.py EntityEdge
class EntityEdge(Edge):
    name: str                      # 关系类型 (SCREAMING_SNAKE_CASE)
    fact: str                      # 完整自然语言描述
    fact_embedding: list[float]    # fact 的向量
    valid_at: datetime | None      # 事实何时开始为真
    invalid_at: datetime | None    # 事实何时停止为真
    expired_at: datetime | None    # 被系统逻辑作废的时间
    episodes: list[str]            # 溯源：来自哪些 Episode
```

| 字段 | 含义 | 谁设置 |
|------|------|--------|
| `valid_at` | 事实成立时间 | LLM 从文本中提取 |
| `invalid_at` | 事实失效时间 | LLM 提取 或 矛盾检测自动设置 |
| `expired_at` | 被系统标记为过期 | `resolve_edge_contradictions` 自动设置 |

---

## 3. Episode 写入管线（`add_episode`）

### 3.1 七步管线全景

```
输入文本
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 1: 创建 EpisodicNode（保存原始内容 + valid_at）          │
│ Step 2: extract_nodes — LLM 实体抽取                         │
│ Step 3: resolve_extracted_nodes — 实体解析/去重               │
│ Step 4: extract_edges — LLM 关系/事实抽取                    │
│ Step 5: resolve_extracted_edges — 事实去重 + 矛盾检测 + 时间作废 │
│ Step 6: extract_attributes_from_nodes — 实体摘要/属性更新      │
│ Step 7: _process_episode_data — 建 MENTIONS 边，批量写入图     │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Step 2: 实体抽取

源码 `prompts/extract_nodes.py` 的核心 prompt：

```
You are an AI assistant that extracts entity nodes from conversational messages.

Instructions:
1. Speaker Extraction: Always extract the speaker as the first entity node.
2. Entity Identification: Extract all significant entities, concepts, or actors 
   explicitly or implicitly mentioned in the CURRENT MESSAGE.
3. Entity Classification: Use ENTITY TYPES to classify each entity.
4. Exclusions: Do NOT extract relationships, actions, dates, or times.
5. Be explicit and unambiguous in naming entities.
```

LLM 返回结构化 JSON（`ExtractedEntities`），包含 `name` + `entity_type_id`。

### 3.3 Step 3: 实体解析（去重）

这是 Graphiti 比 Mem0 精细的地方——**两级去重**：

1. **快速路径**：对每个新实体名做 `NODE_HYBRID_SEARCH_RRF`（BM25 + 向量），找到候选已有节点后，用 **MinHash/fuzzy 相似度** 判断是否可以直接合并
2. **LLM 路径**：快速路径无法确定时，调用 `dedupe_nodes.nodes` prompt 让 LLM 判断

### 3.4 Step 4: 关系/事实抽取

源码 `prompts/extract_edges.py` 的核心 prompt：

```
You are an expert fact extractor that extracts fact triples from text.
1. Extracted fact triples should also be extracted with relevant date information.
2. Treat the CURRENT TIME as the time the CURRENT MESSAGE was sent.

Extract all factual relationships between the given ENTITIES based on the CURRENT MESSAGE.
Only extract facts that:
- involve two DISTINCT ENTITIES from the ENTITIES list,
- are clearly stated or unambiguously implied in the CURRENT MESSAGE

DATETIME RULES:
- If the fact is ongoing (present tense), set valid_at to REFERENCE_TIME.
- If a change/termination is expressed, set invalid_at to the relevant timestamp.
- Leave both fields null if no explicit or resolvable time is stated.
```

LLM 返回的每条 `Edge` 包含：

```python
class Edge(BaseModel):
    source_entity_name: str
    target_entity_name: str
    relation_type: str       # SCREAMING_SNAKE_CASE
    fact: str                # 完整自然语言描述
    valid_at: str | None     # ISO 8601
    invalid_at: str | None   # ISO 8601
```

**与 Mem0 的关键区别**：Graphiti 的事实边存储的是**完整自然语言 fact**（如 "minusjiang 正在研究 Claude Code 的搜索工具体系"），不是 Mem0 那种只存 `has_tool` 这样的精简标签。

### 3.5 Step 5: 事实去重 + 矛盾检测 + 时间作废

这是 Graphiti 最复杂的步骤，也是其**时序推理能力**的核心：

1. **检索候选边**：对新 fact 做 `EDGE_HYBRID_SEARCH_RRF`（BM25 + 向量）
   - 同端点的命中 → **duplicate 候选**
   - 不同端点的命中 → **invalidation 候选**

2. **LLM 判定**（`dedupe_edges.resolve_edge`）：

```
You will receive TWO lists of facts with CONTINUOUS idx numbering.
EXISTING FACTS are indexed first, followed by FACT INVALIDATION CANDIDATES.

1. DUPLICATE DETECTION: 
   If the NEW FACT represents identical information, return duplicate_facts idx.
2. CONTRADICTION DETECTION:
   Determine which facts the NEW FACT contradicts from either list.
   Return all contradicted idx values in contradicted_facts.
```

3. **时间裁剪**（`resolve_edge_contradictions`，纯代码）：

```python
# 源码 edge_operations.py
if edge_valid_at < resolved_edge_valid_at:
    # 旧边更早成立，新边更晚 → 旧边的 invalid_at = 新边的 valid_at
    edge.invalid_at = resolved_edge.valid_at
    edge.expired_at = utc_now()
```

**关键**：Graphiti 用的是**软作废**（设置 `invalid_at` + `expired_at`），不像 Mem0 那样硬删除。历史关系永远保留在图中。

### 3.6 每步 LLM 调用详解：到底在干什么？

Graphiti 处理**每一条 episode** 时，会进行**多次 LLM 调用**，每次调用有明确的职责。以下逐步拆解：

#### 调用 1: 实体抽取（`extract_nodes`）— 模型: medium

**目的**：从原始文本中识别出所有实体（人、工具、项目、概念等）。

**Prompt 核心**：
```
You are an AI assistant that extracts entity nodes from text.
Instructions:
1. Extract all significant entities, concepts, or actors mentioned.
2. Use ENTITY TYPES to classify each entity.
3. Do NOT extract relationships, actions, dates, or times.
```

**具体例子**：输入 session_summary：
> "我完成了 SVN 资产引用修复脚本的增强（v3），新增了 MaterialFunction 类型的专门处理逻辑"

LLM 返回：
```json
{
  "extracted_entities": [
    {"name": "ziyadyao", "entity_type_id": 0},
    {"name": "SVN 资产引用修复脚本", "entity_type_id": 0},
    {"name": "MaterialFunction", "entity_type_id": 0}
  ]
}
```

#### 调用 2: 实体解析/去重（`resolve_extracted_nodes`）— 模型: medium（仅需要时）

**目的**：判断新抽取的实体是否已存在于图中。例如，"SVN 认证" 和 "SVN" 是不是同一个实体？

**流程**：先用 **embedding 相似度 + MinHash 快速匹配**（不调 LLM），如果无法确定再调 LLM。

**Prompt 核心**：
```
You are a helpful assistant that determines whether ENTITIES extracted 
from a conversation are duplicates of existing entities.
```

**具体例子**：新抽取了 "SVN认证"，图中已有 "SVN"。快速匹配不确定时，LLM 判定：
```json
{"duplicate_name": ""}  // 不是重复，"SVN认证" 是独立概念
```

#### 调用 3: 关系/事实抽取（`extract_edges`）— 模型: medium

**目的**：基于已知实体列表，从文本中抽取实体间的关系和事实，并标注时间。

**Prompt 核心**：
```
You are an expert fact extractor that extracts fact triples from text.
- If the fact is ongoing (present tense), set valid_at to REFERENCE_TIME.
- If a change/termination is expressed, set invalid_at to the relevant timestamp.
```

**具体例子**：基于实体 [ziyadyao, SVN 资产引用修复脚本, MaterialFunction]，LLM 返回：
```json
{
  "edges": [
    {
      "source_entity_name": "ziyadyao",
      "target_entity_name": "SVN 资产引用修复脚本",
      "relation_type": "ENHANCED",
      "fact": "ziyadyao 完成了 SVN 资产引用修复脚本的增强（v3版本）",
      "valid_at": "2026-03-23T12:16:59Z"
    },
    {
      "source_entity_name": "SVN 资产引用修复脚本",
      "target_entity_name": "MaterialFunction",
      "relation_type": "ADDED_HANDLING_FOR",
      "fact": "SVN 资产引用修复脚本 v3 新增了对 MaterialFunction 类型的专门处理逻辑",
      "valid_at": "2026-03-23T12:16:59Z"
    }
  ]
}
```

#### 调用 4~N: 事实去重 + 矛盾检测（`resolve_extracted_edge`）— 模型: small × 每条边

**目的**：对**每条新抽取的边**，检索图中已有的相似事实，判断是重复还是矛盾。**这是调用次数最多的步骤**——抽取了 N 条边，就要调 N 次 LLM。

**Prompt 核心**：
```
You are a helpful assistant that de-duplicates facts and determines 
which existing facts are contradicted by the new fact.
Return: duplicate_facts (idx list) + contradicted_facts (idx list)
```

**具体例子**：新 fact "ziyadyao 完成了脚本增强 v3"，图中已有 "ziyadyao 开发了脚本 v2"。LLM 判定：
```json
{
  "duplicate_facts": [],        // 不是重复（v2 ≠ v3）
  "contradicted_facts": [0]     // v2 被 v3 取代 → 矛盾
}
```
→ 旧边 `invalid_at` 设为新边的 `valid_at`，实现时间裁剪。

#### 调用 N+1: 实体摘要生成（`extract_attributes_from_nodes`）— 模型: small

**目的**：根据新增的事实边更新实体节点的 summary。

**Prompt 核心**：
```
You are a helpful assistant that generates concise entity summaries 
from provided context.
```

**具体例子**：实体 "SVN 资产引用修复脚本" 之前的 summary 为空，新增了两条边后，LLM 生成：
> "ziyadyao 完成了 SVN 资产引用修复脚本的增强（v3版本）。SVN 资产引用修复脚本 v3 新增了对 MaterialFunction 类型的专门处理逻辑。"

### 3.7 推荐模型与调用量分析

#### Graphiti 默认推荐模型

源码 `llm_client/openai_base_client.py`：

| 模型角色 | 默认值 | 用途 |
|---------|--------|------|
| **medium**（`config.model`） | `gpt-4.1-mini` | 实体抽取、实体解析、关系抽取 |
| **small**（`config.small_model`） | `gpt-4.1-nano` | 边去重/矛盾检测、摘要生成、属性抽取 |

> 源码注释："works best with OpenAI and Gemini"。其他 provider 有各自默认值，如 Anthropic 用 `claude-sonnet-4-20250514` / `claude-haiku-4-20250414`。

**设计理念**：计算密集但判定简单的步骤（边去重、摘要）用更便宜的 small 模型；需要深度理解的步骤（实体/关系抽取）用 medium 模型。

#### 单条 Episode 的 LLM 调用量

| 步骤 | 模型 | 调用次数 | 说明 |
|------|------|---------|------|
| 实体抽取 | medium | 1 次 | 固定 |
| 实体解析 | medium | 0-1 次 | 仅相似度无法确定时调用 |
| 关系抽取 | medium | 1 次 | 固定 |
| 边去重+矛盾 | small | **N 次** | N = 抽取的边数（典型 3-10 条） |
| 实体摘要 | small | 1 次 | 批量处理所有新/更新实体 |
| **合计** | | **6-13 次** | medium 2-3 次 + small 4-10 次 |

**关键发现**：处理 783 条 session_summary 时，按平均每条 8 次 LLM 调用计算，总共约需 **6000+ 次 LLM 调用**。这就是为什么大规模数据处理需要数小时，且容易触发 API 限流（Venus 公共服务限流 50次/分钟）。

### 3.8 LLM 调用次数对比（Graphiti vs Mem0）

| 步骤 | Graphiti | Mem0 |
|------|----------|------|
| 实体抽取 | 1 次 (medium) | 1 次（图轨） |
| 实体解析 | 0-1 次 (medium) | 0 次 |
| 关系抽取 | 1 次 (medium) | 1 次（图轨） |
| 事实去重+矛盾 | N 次 (small) | N 次（图轨 delete prompt） |
| 事实提取（向量轨） | **不需要** | 1 次 |
| 冲突决策（向量轨） | **不需要** | 1 次 |
| 实体摘要 | 1 次 (small) | 0 次 |
| **合计（典型）** | **6-13 次** | **4-5 次（向量+图）** |

Graphiti 的 LLM 调用量略高于 Mem0，但关键区别在于：
- Graphiti 的高频调用（边去重）使用 **small 模型**，成本和延迟更低
- 所有结果直接进同一个图，不存在 Mem0 的向量/图不同步问题
- 边去重步骤保证了图的质量——不会有重复边或矛盾边共存

---

## 4. 混合检索机制

### 4.1 三路并行 + 重排

源码 `search/search.py`：

```
查询 "Claude Code 的搜索工具有哪些？"
  │
  ├── BM25 全文检索 ──────────────────┐
  │   (对 fact 文本做关键词匹配)        │
  │                                    │
  ├── Cosine 向量检索 ────────────────┤──→ 合并去重 ──→ Reranker ──→ Top-K
  │   (对 fact_embedding 做近邻)       │
  │                                    │
  └── BFS 图遍历 ─────────────────────┘
      (从已知节点沿边扩展)
```

### 4.2 五种 Reranker

| Reranker | 原理 | 适用场景 |
|----------|------|----------|
| `rrf` | 倒数排名融合（Reciprocal Rank Fusion） | 默认，通用 |
| `cross_encoder` | 对 query-fact 对做交叉编码打分 | 高精度语义排序 |
| `node_distance` | RRF + 图上 BFS 距离排序 | 以某实体为中心的关联查询 |
| `mmr` | 最大边际相关（Maximal Marginal Relevance） | 多样性召回 |
| `episode_mentions` | 按 `len(edge.episodes)` 排序 | 高频事实优先 |

### 4.3 搜索配方（预设组合）

源码 `search/search_config_recipes.py`：

| 配方名 | 检索方法 | Reranker | 用途 |
|--------|----------|----------|------|
| `EDGE_HYBRID_SEARCH_RRF` | BM25 + Cosine | RRF | 通用事实搜索 |
| `EDGE_HYBRID_SEARCH_NODE_DISTANCE` | BM25 + Cosine + BFS | Node Distance | 以某实体为中心 |
| `COMBINED_HYBRID_SEARCH_CROSS_ENCODER` | BM25 + Cosine + BFS | Cross Encoder | 最高精度 |

---

## 5. 溯源机制（Provenance）

Graphiti 的溯源是**双向**的：

```
EpisodicNode（原始数据）
  │
  ├──[MENTIONS]──→ EntityNode（实体）
  │
  └── entity_edges: [edge_uuid_1, edge_uuid_2]  ← 本 episode 产生了哪些事实
  
EntityEdge（事实边）
  │
  └── episodes: [episode_uuid_1, episode_uuid_2]  ← 本事实来自哪些 episode
```

任何一条事实都可以追溯到产生它的原始文本，任何一个 episode 都知道自己贡献了哪些事实。

---

## 6. 实战：codebuddy-mem 记忆数据在 Graphiti 中的存储与召回

### 6.1 原始数据：codebuddy-mem Summary

以真实的 codebuddy-mem 会话摘要为输入：

```json
{
  "id": 1421,
  "request": "用户想搜索并整理社区对 Mem0 的评价及其局限性，输出为一份 Markdown 文档。",
  "learned": "Mem0 的图记忆与关系抽取高度依赖 LLM 多阶段调用，而不是纯规则实现；社区评价整理的重点落在其记忆效果、工程可用性，以及成本、稳定性、可解释性等局限。",
  "completed": "完成了 Mem0 技术方案相关文档的多轮补充，并编辑了关于 Mem0 社区评价与局限性的 Markdown 文档。",
  "meta_intent": "用户希望沉淀一份对 Mem0 的客观调研材料，用于方案选型、技术评估或对外分享。",
  "created_at": "2026-04-02T14:02:17.753Z"
}
```

### 6.2 Step 1: 创建 EpisodicNode

```python
EpisodicNode(
    uuid="ep-2026-0402-1421",
    name="session_1421",
    group_id="memory-research",
    source=EpisodeType.text,
    source_description="codebuddy-mem session summary",
    content="""用户想搜索并整理社区对 Mem0 的评价及其局限性，输出为一份 Markdown 文档。
Mem0 的图记忆与关系抽取高度依赖 LLM 多阶段调用...
完成了 Mem0 技术方案相关文档的多轮补充...""",
    valid_at=datetime(2026, 4, 2, 14, 2, 17),  # 原始事件时间
    created_at=now(),                             # 入库时间
    entity_edges=[]                               # 稍后填充
)
```

### 6.3 Step 2-3: 实体抽取 + 解析

LLM 从 content 中抽取实体：

```json
{
  "extracted_entities": [
    {"name": "minusjiang", "entity_type_id": 1},
    {"name": "Mem0", "entity_type_id": 2},
    {"name": "Mem0 技术方案文档", "entity_type_id": 3},
    {"name": "Mem0 社区评价文档", "entity_type_id": 3},
    {"name": "LLM", "entity_type_id": 4}
  ]
}
```

实体解析：对 `Mem0` 做 `NODE_HYBRID_SEARCH_RRF`，发现图中已有 `EntityNode(name="Mem0", summary="通用 AI Agent 长期记忆开源方案...")`，fuzzy 匹配确认是同一实体 → **合并**，不创建新节点。

### 6.4 Step 4: 事实抽取

LLM 基于实体列表和当前文本，抽取事实三元组：

```json
{
  "edges": [
    {
      "source_entity_name": "minusjiang",
      "target_entity_name": "Mem0",
      "relation_type": "EVALUATES",
      "fact": "minusjiang 正在搜索并整理社区对 Mem0 的评价及其局限性",
      "valid_at": "2026-04-02T14:02:17Z",
      "invalid_at": null
    },
    {
      "source_entity_name": "Mem0",
      "target_entity_name": "LLM",
      "relation_type": "DEPENDS_ON",
      "fact": "Mem0 的图记忆与关系抽取高度依赖 LLM 多阶段调用",
      "valid_at": "2026-04-02T14:02:17Z",
      "invalid_at": null
    },
    {
      "source_entity_name": "minusjiang",
      "target_entity_name": "Mem0 社区评价文档",
      "relation_type": "AUTHORED",
      "fact": "minusjiang 编辑了关于 Mem0 社区评价与局限性的 Markdown 文档",
      "valid_at": "2026-04-02T14:02:17Z",
      "invalid_at": null
    },
    {
      "source_entity_name": "minusjiang",
      "target_entity_name": "Mem0 技术方案文档",
      "relation_type": "UPDATED",
      "fact": "minusjiang 完成了 Mem0 技术方案相关文档的多轮补充",
      "valid_at": "2026-04-02T14:02:17Z",
      "invalid_at": null
    }
  ]
}
```

**注意**：每条 fact 都是**完整自然语言句子**，不是 Mem0 那样的精简标签。

### 6.5 Step 5: 事实去重 + 矛盾检测

假设图中已有旧边：

```
(minusjiang) --[RESEARCHES]--> (Mem0)
fact: "minusjiang 正在研究 Mem0 的三段式写入管线"
valid_at: 2026-04-02T13:47:53Z
```

对新 fact "minusjiang 正在搜索并整理社区对 Mem0 的评价及其局限性" 做混合检索，命中上述旧边。

LLM `resolve_edge` 判定：
```json
{
  "duplicate_facts": [],
  "contradicted_facts": []
}
```

→ **不是重复也不矛盾**（研究管线 ≠ 整理评价），两条边共存。

但如果后续有新信息"minusjiang 已完成 Mem0 调研，转向研究 Graphiti"，则：

```json
{
  "duplicate_facts": [],
  "contradicted_facts": [0]  // 旧边 idx=0 被矛盾
}
```

时间裁剪：旧边 `invalid_at` = 新边 `valid_at`，旧边 `expired_at` = `now()`。

### 6.6 Step 6-7: 实体摘要更新 + 写入

**Mem0 实体摘要更新**：

```
原 summary: "通用 AI Agent 长期记忆开源方案，支持向量+图双轨存储"
新 summary: "通用 AI Agent 长期记忆开源方案，支持向量+图双轨存储。
            图记忆与关系抽取高度依赖 LLM 多阶段调用。社区评价显示
            存在记忆质量、成本和工程可用性方面的局限。"
```

**写入图数据库**：

```cypher
-- Episode 节点
CREATE (ep:Episodic {uuid: 'ep-2026-0402-1421', content: '...', valid_at: datetime('2026-04-02T14:02:17Z')})

-- MENTIONS 边（Episode → Entity）
MATCH (ep:Episodic {uuid: 'ep-2026-0402-1421'})
MATCH (e:Entity {name: 'minusjiang'})
CREATE (ep)-[:MENTIONS]->(e)

-- 事实边（Entity → Entity）
MATCH (s:Entity {name: 'minusjiang'})
MATCH (d:Entity {name: 'Mem0'})
CREATE (s)-[r:RELATES_TO {
    name: 'EVALUATES',
    fact: 'minusjiang 正在搜索并整理社区对 Mem0 的评价及其局限性',
    valid_at: datetime('2026-04-02T14:02:17Z'),
    invalid_at: null,
    expired_at: null,
    episodes: ['ep-2026-0402-1421']
}]->(d)
CALL db.create.setNodeVectorProperty(r, 'fact_embedding', $embedding)
```

### 6.7 最终图结构

多条 codebuddy-mem 会话累积后的图：

```
                          ┌─────────────────────┐
                     ┌────│ Mem0 技术方案文档      │
                     │    └─────────────────────┘
                UPDATED        ▲
                     │    AUTHORED
                     ▼         │
┌──────────┐  EVALUATES  ┌──────┐  DEPENDS_ON  ┌─────┐
│minusjiang│────────────→│ Mem0 │─────────────→│ LLM │
└──────────┘             └──────┘              └─────┘
     │                        ▲
     │ AUTHORED               │ COMPARED_WITH
     ▼                        │
┌─────────────────────┐  ┌──────────┐
│ Mem0 社区评价文档     │  │ Graphiti │
└─────────────────────┘  └──────────┘
     │
     │ RESEARCHES
     ▼
┌──────────┐  HAS_TOOL  ┌──────┐
│Claude Code│───────────→│ Grep │
└──────────┘            └──────┘
     │
     │ HAS_COMPONENT
     ▼
┌──────────┐
│ Subagent │
└──────────┘
```

每条边上都有 `fact`（自然语言）+ `valid_at`/`invalid_at`（时间窗口）。

### 6.8 召回演示

#### 场景 1："Mem0 有什么问题？"

**三路并行检索**：

```python
results = await graphiti.search("Mem0 有什么问题？", group_ids=["memory-research"])
```

1. **BM25**：关键词"Mem0"+"问题"命中：
   - "Mem0 的图记忆与关系抽取高度依赖 LLM 多阶段调用"
   - "minusjiang 正在搜索并整理社区对 Mem0 的评价及其局限性"

2. **Cosine**：query embedding 与 fact_embedding 近邻：
   - "社区评价显示存在记忆质量、成本和工程可用性方面的局限"（来自实体摘要）

3. **BFS**：从 `Mem0` 节点出发，1-2 跳内的事实边：
   - `Mem0 --DEPENDS_ON--> LLM`
   - `Mem0 --COMPARED_WITH--> Graphiti`

**Cross Encoder 重排** → 返回相关性最高的 Top-K 事实。

#### 场景 2：时序查询——"minusjiang 之前在研究什么，现在在研究什么？"

Graphiti 的时间字段让这类查询成为可能：

```cypher
-- 查找 minusjiang 的所有研究活动，按时间排序
MATCH (m:Entity {name: 'minusjiang'})-[r:RELATES_TO]->(target)
WHERE r.name IN ['RESEARCHES', 'EVALUATES', 'ANALYZES']
RETURN r.fact, r.valid_at, r.invalid_at, target.name
ORDER BY r.valid_at
```

结果（按时间线）：

| valid_at | invalid_at | fact | target |
|----------|-----------|------|--------|
| 03-31 13:25 | 04-02 13:47 | 正在研究 Claude Code 的子代理编排机制 | Claude Code |
| 04-02 13:47 | 04-02 14:02 | 正在研究 Mem0 的三段式写入管线 | Mem0 |
| 04-02 14:02 | null (进行中) | 正在搜索并整理社区对 Mem0 的评价 | Mem0 |

→ "minusjiang 之前研究 Claude Code 的子代理机制，然后转向 Mem0 的技术管线，现在在整理 Mem0 的社区评价。"

**这种时序查询在 Mem0 中无法实现**——因为 Mem0 用硬删除覆盖旧记忆，历史状态永久丢失。

#### 场景 3：溯源——"这个结论是从哪来的？"

```cypher
-- 通过事实边的 episodes 字段追溯
MATCH (s)-[r:RELATES_TO]->(d)
WHERE r.fact CONTAINS 'LLM 多阶段调用'
WITH r.episodes AS ep_uuids
UNWIND ep_uuids AS ep_uuid
MATCH (ep:Episodic {uuid: ep_uuid})
RETURN ep.content, ep.valid_at, ep.source_description
```

→ 返回产生这条事实的原始 Episode 全文，可以验证 LLM 是否正确提取。

---

## 7. Graphiti vs Mem0 vs codebuddy-mem 三方对比

| 维度 | Graphiti | Mem0 | codebuddy-mem |
|------|----------|------|---------------|
| **存储架构** | 统一图（Neo4j/FalkorDB） | 向量库 + 可选图（独立运行） | SQLite + FTS5 |
| **数据粒度** | Episode（原始）+ 事实边 + 实体摘要 | 原子 Fact（向量）+ 精简三元组（图） | Observation + Summary |
| **原始数据保留** | 完整保留为 EpisodicNode | 不保留（只存提取后的 fact） | 完整保留（narrative） |
| **时序能力** | 强：valid_at/invalid_at/expired_at | 弱：硬删除，无历史 | 中：有 created_at，可 timeline |
| **事实描述** | 完整自然语言 + embedding | 精简标签（has_tool 等） | 结构化 XML（facts 字段） |
| **矛盾处理** | 软作废（保留历史 + 时间裁剪） | 硬删除（旧边永久丢失） | 无（新旧共存） |
| **去重机制** | 两级：fuzzy 快速 + LLM 确认 | 一级：LLM 判定 | 无自动去重 |
| **检索方式** | BM25 + Cosine + BFS 三路混合 | Cosine + 图 BM25 双路 | FTS 全文搜索 |
| **Reranker** | 5 种（RRF/CrossEncoder/MMR/NodeDist/Episodes） | 可选 reranker | 无 |
| **溯源** | 双向（Episode↔Edge↔Entity） | 无 | 无显式溯源 |
| **LLM 调用/条** | 3-5 次 | 4-5 次 | 1 次（摘要生成） |
| **一致性** | 强（单一图存储） | 弱（向量库和图库可漂移） | 强（单一 SQLite） |

---

## 8. Graphiti 的核心优势与局限

### 优势

1. **时序推理**：每条事实有时间窗口，可以回答"以前 vs 现在"类查询——这是 Mem0 的硬伤
2. **统一存储**：所有数据在同一个图中，不存在 Mem0 的向量/图不同步问题
3. **完整溯源**：任何结论都可以追溯到原始 Episode 文本
4. **软作废**：历史不会丢失，旧事实标记 expired 而非删除
5. **混合检索 5 种 Reranker**：比 Mem0 的 BM25 重排更灵活

### 局限

1. **强依赖图数据库**：必须部署 Neo4j/FalkorDB，运维成本高于 Mem0 的纯向量方案
2. **实体解析的 O(n) 风险**：虽然优于 Mem0 的简单下划线归一化，但候选节点多时 LLM 上下文仍可能溢出
3. **Structured Output 强依赖**：源码注释明确说"works best with OpenAI and Gemini"，其他 LLM 可能输出格式不对
4. **社区规模较小**：Graphiti ~9K Stars，远小于 Mem0 的 47.8K，生态和文档成熟度不及
5. **无向量轨 fallback**：如果图数据库不可用，整个系统不可用；Mem0 至少还有纯向量模式
6. **无记忆衰减与重要度机制（"僵尸记忆"问题）**：详见下节

---

## 9. 僵尸记忆问题：Graphiti 缺失的记忆管理维度

### 9.1 问题描述

当知识图谱持续积累大量记忆后，一个关键问题浮现：**陈旧但未被矛盾覆盖的记忆，会在语义检索中与新记忆平等竞争**。

例如：
- 三个月前："minusjiang 偏好使用 CLI 交互"
- 昨天："minusjiang 正在调试 MCP WebSocket 连接问题"

当用户查询 "minusjiang 最近在做什么" 时，这两条记忆在 Graphiti 的搜索中**权重完全一样**——纯看语义相似度，不考虑时间远近。如果旧记忆碰巧在语义上与查询更匹配，它就会排在新记忆前面。

这类"僵尸记忆"不是错误（`invalid_at` 为空，事实仍然成立），但在实际应用中**召回价值远低于近期记忆**。

### 9.2 Graphiti 源码验证：确实没有处理

对 Graphiti v0.28.2 源码的全面审计结果：

| 能力 | 源码现状 |
|------|---------|
| **记忆重要度评分** | 无。`EntityNode` 和 `EntityEdge` 上没有 importance/priority/strength 字段 |
| **时间衰减权重** | 无。`search.py` 的排序仅按 BM25/cosine/RRF 分数，不读取 `created_at` 或 `valid_at` |
| **访问频率强化** | 无。没有记录"某条记忆被召回过多少次" |
| **自动遗忘/淘汰** | 无。`expired_at` 字段存在但搜索时**默认不过滤**（`SearchFilters()` 默认为空） |
| **新近性偏好** | 无。`graphiti.py` 文档写了"用当前时间作为时间相关性参考点"，但**实现中并未使用** |

唯一相关的 `episode_mentions` reranker 按边被多少个 episode 引用排序（`len(edge.episodes)` 降序），衡量的是"被反复提及的频率"而非重要度或新近性。

**Graphiti 的设计哲学是"一切保留，时间标注"**——通过 `valid_at/invalid_at` 记录事实的时间窗口，通过矛盾检测软作废旧边，但**不主动衰减或遗忘**。这对历史回溯很好，但对实时召回场景会产生僵尸记忆问题。

### 9.3 业界对比：谁解决了这个问题？

#### Supermemory：智能遗忘（Intelligent Forgetting）

Supermemory（LongMemEval 基准 81.6% 准确率，超过 Zep 的 71.2%）实现了受**艾宾浩斯遗忘曲线**启发的记忆管理：

| 机制 | 说明 |
|------|------|
| **Smart Forgetting & Decay** | 记忆有强度值，随时间自然衰减；不重要的信息逐渐变弱直到被自动清除 |
| **Access-based Reinforcement** | 每次被召回的记忆强度增强（"越用越记得，不用就忘"） |
| **Recency & Relevance Bias** | 最近讨论的内容获得更高权重；经常引用的文档保持"置顶" |
| **Hierarchical Memory Layers** | 工作记忆（热）→ 短期记忆（温）→ 长期记忆（冷），不同层级不同检索策略 |
| **Context Rewriting** | 持续更新摘要，将新旧信息融合 |

> "Less relevant information gradually fades while important, frequently-accessed content stays sharp. No more drowning in irrelevant context." — Supermemory Blog

#### Stanford Generative Agents：三维评分公式

Stanford 的 Generative Agents 论文（"25 个 AI 小镇居民"）提出了被广泛引用的记忆检索评分模型：

```
score = α × recency + β × importance + γ × relevance
```

| 维度 | 含义 | 计算方式 |
|------|------|---------|
| **recency**（新近性） | 多久前的记忆 | 指数衰减函数，越旧分越低 |
| **importance**（重要度） | 这条记忆本身有多重要 | 写入时用 LLM 打分 1-10（"吃了早餐"=1，"发现重大 bug"=9） |
| **relevance**（相关性） | 和当前查询多相关 | embedding 余弦相似度 |

这解决了僵尸记忆的核心矛盾：
- 三个月前的 "喜欢 CLI" → recency 低，但 importance 高 + 当前查询相关时仍可被召回
- 昨天的 "调试 MCP" → recency 高，即使 importance 一般也优先召回
- 很久以前的琐碎记忆 → recency 低 + importance 低 → 自然沉底

### 9.4 三方对比

| 维度 | Graphiti | Supermemory | Stanford Generative Agents |
|------|----------|-------------|--------------------------|
| 重要度评分 | 无 | 有（强度值） | 有（LLM 打分 1-10） |
| 时间衰减 | 无 | 有（智能遗忘） | 有（指数衰减） |
| 访问强化 | 无 | 有（召回增强） | 无 |
| 自动清除 | 无（仅软作废矛盾边） | 有（低于阈值淘汰） | 无 |
| 检索公式 | 纯语义（BM25 + cosine + BFS） | 新近性 + 相关性 + 重要度 | α·recency + β·importance + γ·relevance |
| 历史保留 | 完整（软作废） | 部分（弱记忆被清除） | 完整 |

### 9.5 对 Graphiti 的改进方向

Graphiti 已有的 `SearchFilters` 机制提供了扩展空间：

1. **检索时加入 recency 权重**：在 RRF 融合阶段，将 `1/(rank+k)` 乘以时间衰减因子 `e^(-λ·Δt)`，使旧记忆自然降权
2. **写入时增加 importance 评分**：在 `extract_edges` 后用 LLM 对每条 fact 打 importance 分（1-10），存入 `EntityEdge` 的 `attributes` 字段
3. **读取时记录访问频率**：在 `search()` 返回结果后，更新被命中边的访问计数，实现 access-based reinforcement
4. **默认过滤已失效边**：将 `SearchFilters` 默认设置为 `expired_at IS NULL`，除非用户明确要求查看历史

---

## 参考源码索引

| 主题 | 路径 |
|------|------|
| 节点模型 | `graphiti/graphiti_core/nodes.py` |
| 边模型 | `graphiti/graphiti_core/edges.py` |
| 写入编排 | `graphiti/graphiti_core/graphiti.py` |
| 实体抽取 prompt | `graphiti/graphiti_core/prompts/extract_nodes.py` |
| 关系抽取 prompt | `graphiti/graphiti_core/prompts/extract_edges.py` |
| 边去重+矛盾 prompt | `graphiti/graphiti_core/prompts/dedupe_edges.py` |
| 实体解析操作 | `graphiti/graphiti_core/utils/maintenance/node_operations.py` |
| 边解析+时间作废 | `graphiti/graphiti_core/utils/maintenance/edge_operations.py` |
| 搜索引擎 | `graphiti/graphiti_core/search/search.py` |
| 搜索配方 | `graphiti/graphiti_core/search/search_config_recipes.py` |
| LLM 客户端 | `graphiti/graphiti_core/llm_client/client.py` |
