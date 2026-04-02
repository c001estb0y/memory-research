# Dynamic Graph Memory 特性解析

> **核心问题**：AI Agent 的记忆如何不只是"记住"，还能"理解关系"并"随时间演化"？Dynamic Graph Memory 就是这个方向的关键技术。

---

## 1. 什么是 Dynamic Graph Memory？

Dynamic Graph Memory（动态图记忆）并非某个项目的专有术语，而是一类**设计范式**：将 AI Agent 的记忆从"扁平文本片段 + 向量检索"升级为"带有关系结构、可随时间增量更新的知识图谱"。

**核心特征**：

| 特征 | 说明 |
|------|------|
| **图结构存储** | 记忆以节点（实体）和边（关系）的形式存储，而非扁平文本块 |
| **动态更新** | 新信息实时写入图，旧信息自动失效/更新，无需批量重建 |
| **时序感知** | 事实带有有效期窗口（何时为真、何时失效），可查询任意时间点的状态 |
| **溯源追踪** | 每个推导出的事实都能追溯到原始数据来源（Episode） |
| **关系推理** | 支持多跳遍历，能回答"A 的同事的上级是谁"这类链式关系问题 |

"Dynamic"的核心含义是：**图不是一次性构建的静态快照，而是持续生长、自我修正的活系统。**

---

## 2. 为什么需要 Dynamic Graph Memory？

### 2.1 传统 RAG 的瓶颈

传统 RAG（Retrieval-Augmented Generation）工作流是：文档 → 分块 → embedding → 向量检索 → 拼入 prompt。

这套方案有三个根本局限：

| 局限 | 表现 |
|------|------|
| **无关系感知** | 向量搜索只衡量"这段话和那段话语义像不像"，无法表达"A works_at B"这种结构化关系 |
| **无时序追踪** | 无法区分"用户上个月住北京"和"用户本月搬到上海"——两条记忆并存，互相矛盾 |
| **无多跳推理** | 无法回答"我同事的经理是谁"——需要连续跳转 `我 → works_with → 同事 → reports_to → 经理` |

### 2.2 从向量到图的跨越

```
向量记忆                           图记忆
┌─────────────────┐               ┌─────────────────────────────────┐
│ "Alice在Stripe"  │──embedding──→│ (Alice)-[:works_at]->(Stripe)   │
│ "Alice喜欢React" │──embedding──→│ (Alice)-[:prefers]->(React)     │
│ "Bob管理Alice"   │──embedding──→│ (Bob)-[:manages]->(Alice)       │
└─────────────────┘               └─────────────────────────────────┘
    ↓ 查询                            ↓ 查询
 "Alice的上级？"                    "Alice的上级？"
    ↓                                  ↓
 返回3条相似文本                    沿边遍历: Alice ← manages ─ Bob
 需要人工推理                       直接返回: Bob
```

---

## 3. 两种实现路径：Mem0 vs Graphiti

当前主流有两种 Dynamic Graph Memory 的实现方式，代表了**不同的设计取舍**：

### 3.1 Mem0：向量为主 + 图关系增强

**设计哲学**：向量记忆是主路径，图是可选的增强层。

```
┌───────────────────────────────────────────┐
│              Memory.add()                  │
│          ThreadPoolExecutor 并行           │
├───────────────────┬───────────────────────┤
│   向量轨（主路径） │   图轨（可选增强）    │
│                   │                       │
│ 1. LLM 事实抽取   │ 1. LLM 实体抽取      │
│ 2. 向量近邻检索   │ 2. LLM 关系抽取      │
│ 3. LLM 冲突决策   │ 3. 向量实体对齐      │
│    ADD/UPDATE/    │ 4. LLM 冲突检测      │
│    DELETE/NONE    │ 5. 软删旧边 + MERGE  │
│ 4. 写入向量库     │    新边写入图库       │
└───────────────────┴───────────────────────┘
```

**Mem0 图写入的核心流程**：

```python
# mem0/memory/graph_memory.py - MemoryGraph.add()
def add(self, data, filters):
    # Step 1: LLM 抽取实体和类型
    entity_type_map = self._retrieve_nodes_from_data(data, filters)
    
    # Step 2: LLM 建立关系三元组
    to_be_added = self._establish_nodes_relations_from_data(data, filters, entity_type_map)
    
    # Step 3: 在图中搜索已有关系（向量余弦相似度 ≥ threshold 做实体对齐）
    search_output = self._search_graph_db(node_list=list(entity_type_map.keys()), filters=filters)
    
    # Step 4: LLM 判断哪些旧边需要失效
    to_be_deleted = self._get_delete_entities_from_search_output(search_output, data, filters)
    
    # Step 5: 执行图操作
    deleted_entities = self._delete_entities(to_be_deleted, filters)  # 软删: SET r.valid = false
    added_entities = self._add_entities(to_be_added, filters, entity_type_map)  # MERGE 新节点和边
    
    return {"deleted_entities": deleted_entities, "added_entities": added_entities}
```

**Mem0 的"动态"体现**：
- 写入时自动检测冲突关系，**软删除旧边**（`SET r.valid = false, r.invalidated_at = datetime()`）
- 通过 `MERGE` 语义实现幂等写入——同一实体不会重复创建
- 用向量余弦相似度做**实体对齐**——`claude_code` 和 `Claude Code` 会被识别为同一实体

**Mem0 的检索**：向量结果 + 图关系并行返回：

```python
results = memory.search("Claude Code 有哪些搜索工具？", user_id="minusjiang")
# 返回:
# results: [向量匹配的记忆文本...]
# relations: [
#   {"source": "claude_code", "relationship": "has_tool", "destination": "grep"},
#   {"source": "claude_code", "relationship": "has_tool", "destination": "lsp"},
#   {"source": "grep", "relationship": "searches", "destination": "markdown"}
# ]
```

### 3.2 Graphiti：时序上下文图（Temporal Context Graph）

**设计哲学**：图是一等公民，时间是核心维度。

Graphiti（由 Zep 团队开源，3k+ stars）把自己定位为"为 AI Agent 构建时序上下文图的框架"。相比 Mem0 的图增强思路，Graphiti 引入了几个关键的一级抽象：

#### Episode（情节）—— 溯源的基本单元

```python
# graphiti_core/nodes.py
class EpisodicNode(Node):
    source: EpisodeType       # message / json / text
    source_description: str   # 数据源描述
    content: str              # 原始数据内容
    valid_at: datetime        # 原始文档的创建时间（参考时间）
    entity_edges: list[str]   # 该 episode 关联的实体边列表
```

每次写入的原始数据作为 Episode 保存，所有从中抽取的实体和事实都**追溯到这个 Episode**。这意味着任何一条事实边，你都能找到"它是从哪段原始对话/文档中来的"。

#### EntityEdge（事实边）—— 带时间窗口的关系

```python
# graphiti_core/edges.py
class EntityEdge(Edge):
    name: str                          # 关系名称
    fact: str                          # 自然语言描述的事实
    fact_embedding: list[float] | None # 事实的向量表示
    episodes: list[str]                # 溯源：关联的 episode ID 列表
    valid_at: datetime | None          # 事实何时开始为真
    invalid_at: datetime | None        # 事实何时不再为真
    expired_at: datetime | None        # 节点何时被标记为失效
    attributes: dict[str, Any]         # 自定义属性
```

**这是 Graphiti 最核心的设计**——每条事实边不只是 `(A)-[关系]->(B)`，还带有完整的时间语义：

| 字段 | 含义 | 示例 |
|------|------|------|
| `valid_at` | 事实从何时开始为真 | 2026-01-15（Alice 从1月15日起在 Stripe 工作） |
| `invalid_at` | 事实从何时不再为真 | 2026-06-01（Alice 6月离开了 Stripe） |
| `expired_at` | 系统标记该边失效的时间 | 2026-06-02（系统在6月2日处理了这条矛盾） |

这套**双时态（Bi-temporal）**模型意味着：你可以查询"2026年3月时，Alice 在哪里工作？"——系统会找到 `valid_at ≤ 2026-03 AND (invalid_at IS NULL OR invalid_at > 2026-03)` 的边。

#### 矛盾检测与边消解（Contradiction Resolution）

当新信息与已有事实矛盾时（比如"Alice 从 Stripe 跳到了 Google"），Graphiti 不是简单删除旧边，而是通过时间窗口**保留完整历史**：

```python
# graphiti_core/utils/maintenance/edge_operations.py
def resolve_edge_contradictions(
    resolved_edge: EntityEdge, 
    invalidation_candidates: list[EntityEdge]
) -> list[EntityEdge]:
    invalidated_edges = []
    for edge in invalidation_candidates:
        # 如果旧边的有效期与新边的有效期有时间重叠
        # 且旧边早于新边生效
        if edge_valid_at < resolved_edge_valid_at:
            # 将旧边的 invalid_at 设为新边的 valid_at
            edge.invalid_at = resolved_edge.valid_at
            edge.expired_at = utc_now()
            invalidated_edges.append(edge)
    return invalidated_edges
```

**效果**：

```
时间线：
  2026-01 ──────── 2026-06 ──────── 2026-12
       │              │
 (Alice)-[works_at {valid_at: 01, invalid_at: 06}]->(Stripe)   ← 旧边，被标记为失效
 (Alice)-[works_at {valid_at: 06, invalid_at: null}]->(Google)  ← 新边
```

两条边都保留在图中，但通过时间窗口可以区分：Alice 1-6月在 Stripe，6月起在 Google。

#### Episode 上下文窗口

```python
# graphiti_core/utils/maintenance/graph_data_operations.py
EPISODE_WINDOW_LEN = 3

async def retrieve_episodes(
    driver, reference_time, last_n=EPISODE_WINDOW_LEN, group_ids=None, ...
) -> list[EpisodicNode]:
    # 取 reference_time 之前的最近 N 个 episode 作为上下文
    # 帮助 LLM 在抽取实体/关系时理解时序背景
```

写入新 Episode 时，Graphiti 会取出最近的几个 Episode 作为 LLM 抽取的上下文，让实体抽取和关系建立更准确。

#### 混合检索

Graphiti 的检索结合三种策略：

| 策略 | 作用 |
|------|------|
| **语义向量搜索** | 基于 embedding 的相似度匹配 |
| **关键词搜索（BM25）** | 精确的文本匹配 |
| **图遍历（BFS）** | 沿关系路径发现间接关联的实体 |

并且支持基于 `SearchFilters` 做时间维度过滤：

```python
# graphiti_core/search/search_filters.py
class SearchFilters(BaseModel):
    node_labels: list[str] | None       # 按节点标签过滤
    edge_types: list[str] | None        # 按边类型过滤
    valid_at: list[list[DateFilter]]    # 按事实有效期过滤
    invalid_at: list[list[DateFilter]]  # 按失效时间过滤
    created_at: list[list[DateFilter]]  # 按创建时间过滤
    expired_at: list[list[DateFilter]]  # 按过期时间过滤
```

---

## 4. Dynamic 的六大核心特性

综合 Mem0 和 Graphiti 的实现，Dynamic Graph Memory 的"动态"体现在以下维度：

### 4.1 增量构建（Incremental Construction）

**不做批量重算，新数据实时写入图。**

传统 GraphRAG（如微软的方案）需要对全量文档跑一次完整的图构建 pipeline：分块 → 实体抽取 → 社区检测 → 摘要生成。每次有新数据，理论上需要重跑。

Dynamic Graph Memory 的做法是：每条新消息/文档独立处理，通过 `MERGE` 语义写入图——有则更新，无则创建。

```
静态图构建：        文档全集 ──→ 批量 pipeline ──→ 图（一次性）
动态增量构建：      新消息 ──→ 抽取+对齐+MERGE ──→ 图持续演化
```

### 4.2 自动冲突解决（Contradiction Handling）

**矛盾信息不丢弃，通过时间窗口或软删除维护一致性。**

| 方案 | Mem0 | Graphiti |
|------|------|---------|
| 机制 | 软删除旧边 `valid=false` | 时间窗口：旧边 `invalid_at` = 新边 `valid_at` |
| 历史保留 | 旧边标记为 invalid，可查 | 完整双时态历史，支持时间点查询 |
| 决策方式 | LLM 判定是否冲突 | LLM 去重 + 时间区间计算 |

### 4.3 实体对齐（Entity Resolution）

**防止同一个现实世界实体在图中出现多个节点。**

当 LLM 抽取出 `"Claude Code"` 和已有节点 `"claude_code"` 时，系统需要识别它们是同一实体。

- **Mem0**：计算新实体 embedding，与图中已有节点做向量余弦相似度比较（阈值默认 0.9），超过阈值则复用已有节点
- **Graphiti**：同样使用 embedding 向量匹配，加上 LLM 去重（`EdgeDuplicate` prompt 判断 `duplicate_facts` / `contradicted_facts`）

### 4.4 溯源追踪（Provenance）

**每条推导出的事实都能追溯到原始数据。**

| 方案 | 实现 |
|------|------|
| **Graphiti** | `EpisodicNode`（原始数据）→ `EpisodicEdge`（MENTIONS）→ `EntityNode`；`EntityEdge.episodes[]` 记录所有关联的 Episode ID |
| **Mem0** | 向量轨有 SQLite 审计日志（`history` 表：memory_id, old_memory, new_memory, event）；图轨无独立溯源机制 |

Graphiti 的溯源模型更完备：你可以从一条事实边出发，找到所有贡献过这条边的原始 Episode（对话、文档），这对可解释性至关重要。

### 4.5 时序感知（Temporal Awareness）

**知道事实"什么时候是真的"。**

这是 Graphiti 最显著的特性，也是其论文（[arXiv:2501.13956](https://arxiv.org/abs/2501.13956)）的核心贡献。

```
普通图：     (Alice)-[:works_at]->(Stripe)           ← 现在还在 Stripe 吗？不知道
时序图：     (Alice)-[:works_at {
                valid_at: 2026-01,
                invalid_at: 2026-06
             }]->(Stripe)                              ← 明确：1月到6月在 Stripe
             (Alice)-[:works_at {
                valid_at: 2026-06,
                invalid_at: null
             }]->(Google)                              ← 明确：6月起在 Google
```

Mem0 的图没有显式的时间有效期语义——`invalidated_at` 只是"何时被系统标记为 invalid"，不是"事实何时在现实中不再为真"。

### 4.6 LLM 驱动的图操作（LLM-Powered Graph Ops）

**实体抽取、关系建立、冲突判定全部由 LLM 完成，代码只负责存储。**

两个项目都不使用传统 NER/规则引擎，而是完全依赖 LLM + Function Calling：

```
Mem0 图轨单次写入的 LLM 调用:
  1. extract_entities tool call     → 实体列表
  2. establish_relationships tool   → 三元组列表
  3. delete_graph_memory tool       → 待删边列表
  合计: 3 次 LLM 调用

Graphiti 单次 add_episode 的 LLM 调用:
  1. 实体抽取（extract_nodes）      → 实体列表
  2. 关系/事实抽取（extract_edges） → 事实边列表（含 valid_at/invalid_at）
  3. 去重+矛盾判定（resolve）       → 合并/失效决策
  4. 实体摘要更新                   → 随时间演化的实体描述
  合计: 4+ 次 LLM 调用
```

---

## 5. Mem0 vs Graphiti 全维度对比

| 维度 | Mem0 Graph Memory | Graphiti |
|------|-------------------|---------|
| **图的角色** | 可选增强层（向量是主路径） | 一等公民（图是主存储） |
| **时序模型** | 软删除 `valid=false/true` | 双时态 `valid_at` / `invalid_at` / `expired_at` |
| **溯源** | SQLite 审计日志（向量轨） | Episode 一等对象，`MENTIONS` 边链接到实体 |
| **矛盾处理** | LLM 判定 + 软删旧边 | LLM 去重 + 时间区间消解，保留完整历史 |
| **实体对齐** | 向量余弦相似度（阈值 0.9） | 向量匹配 + LLM 去重 |
| **检索方式** | 向量近邻 + BM25 图三元组重排 | 语义 + BM25 + 图遍历（BFS）混合 |
| **时间查询** | 不支持"某时间点的状态" | 完整支持，`SearchFilters` 可按时间窗口过滤 |
| **自定义本体** | 无预定义 schema | Pydantic 模型定义实体/边类型 |
| **图后端** | Neo4j, Memgraph, Kuzu, AGE, Neptune | Neo4j, FalkorDB, Kuzu, Neptune |
| **向量存储** | 20+ 种向量库 | 内置于图节点/边（`name_embedding`, `fact_embedding`） |
| **LLM 调用数** | 向量轨 2 次 + 图轨 3 次 ≈ 5 次/写入 | 4+ 次/Episode |
| **适用场景** | 通用 AI 助手长期记忆 | 需要时序感知的复杂 Agent 系统 |

---

## 6. 具体场景对比

### 场景 1：用户换工作

**输入**："我从 Stripe 跳到 Google 了"

**Mem0 处理**：
```
向量轨: "用户在 Stripe 做 PM" → LLM 决策 → UPDATE → "用户在 Google 做 PM"（旧记忆被覆盖）
图  轨: (user)-[:works_at]->(Stripe) → SET r.valid = false
        新增 (user)-[:works_at]->(Google)
```
→ 旧记忆被更新/标记无效，**无法查询"之前在哪工作"**（向量轨旧值已覆盖）

**Graphiti 处理**：
```
旧边: (user)-[:works_at {valid_at: 2025-01, invalid_at: null}]->(Stripe)
  → 更新为: invalid_at = 2026-04（新信息的 valid_at）

新边: (user)-[:works_at {valid_at: 2026-04, invalid_at: null}]->(Google)
```
→ **两条边都保留**，查询"2025年6月在哪"返回 Stripe，查询"现在在哪"返回 Google

### 场景 2：多跳关系查询

**查询**："Alice 的同事的经理是谁？"

**Mem0**：向量检索返回相关文本片段 + 图关系补充

```json
{
  "results": ["Alice 和 David 一起做移动端项目", "David 向 Rachel 汇报"],
  "relations": [
    {"source": "alice", "relationship": "works_with", "destination": "david"},
    {"source": "david", "relationship": "reports_to", "destination": "rachel"}
  ]
}
```
→ 模型可以从 `relations` 推理，但 **Mem0 自身不做路径遍历**，需要上层 LLM 拼接

**Graphiti**：混合检索直接做图遍历

```python
results = await graphiti.search("Alice's teammate's manager")
# 图遍历: alice → works_with → david → reports_to → rachel
# 返回事实边: "David reports to Rachel" (fact)
```
→ 图遍历直接找到路径，无需 LLM 二次推理

### 场景 3：时间点查询

**查询**："2025年6月时，团队的技术栈是什么？"

**Mem0**：无法处理。没有时间窗口概念，只能返回当前最新状态。

**Graphiti**：

```python
from graphiti_core.search.search_filters import SearchFilters, DateFilter

filters = SearchFilters(
    valid_at=[[DateFilter(before=datetime(2025, 7, 1))]],
    invalid_at=[[DateFilter(after=datetime(2025, 6, 1))]]  # 或 null（仍然有效）
)
results = await graphiti.search("team tech stack", search_filter=filters)
```
→ 返回在2025年6月有效的所有技术栈相关事实

---

## 7. 技术局限与权衡

### 7.1 共同局限

| 局限 | 说明 |
|------|------|
| **LLM 调用成本** | 每次写入 3-5 次 LLM 调用，大规模场景下成本显著 |
| **实体漂移** | 同一实体可能因不同表述（`Claude Code` / `claude_code` / `ClaudeCode`）产生多个节点 |
| **LLM 抽取质量** | 关系抽取完全依赖 LLM，小模型容易产生错误三元组或遗漏关键关系 |
| **延迟** | 图操作增加写入延迟，尤其在图规模增长后 |

### 7.2 Mem0 特有局限

- **时序能力弱**：没有事实有效期窗口，无法做历史状态查询
- **图是附属**：图关系不参与主排序，仅作为 `relations` 附加返回
- **溯源缺失（图轨）**：图中的关系无法追溯到原始对话

### 7.3 Graphiti 特有局限

- **复杂度高**：双时态模型、Episode 链、矛盾消解 pipeline，理解和调试门槛更高
- **向量存储耦合**：embedding 存在图节点/边属性中，不如 Mem0 灵活（20+ 向量库可选）
- **需要 Neo4j 5.26+**：对图数据库版本有要求

---

## 8. 如何选择？

```
你的 Agent 需要什么？
    │
    ├── 只需要"记住用户偏好"，不关心时间和关系
    │   → 纯向量记忆就够了（Mem0 默认模式）
    │
    ├── 需要理解"谁和谁有什么关系"
    │   → Mem0 Graph Memory（向量 + 图增强，上手简单）
    │
    └── 需要时间感知 + 矛盾追踪 + 完整溯源
        → Graphiti（时序上下文图，适合复杂 Agent 系统）
```

### 决策矩阵

| 场景 | 推荐方案 | 理由 |
|------|----------|------|
| 个人 AI 助手 | Mem0（向量 + 可选图） | 记忆偏好为主，关系简单，成本可控 |
| 企业知识管理 | Graphiti | 需要时序追踪、合规审计、多用户隔离 |
| 客服/CRM | Mem0 Graph Memory | 需要关系但不需要精细时序 |
| 金融/医疗 Agent | Graphiti | 事实有效期至关重要，需要完整溯源 |
| 研究/知识库 | Graphiti | 知识持续演化，需要查询历史状态 |

---

## 9. 总结

Dynamic Graph Memory 是 AI Agent 从"鹦鹉学舌"到"真正理解世界"的关键基础设施。它的核心价值在于：

1. **关系 > 片段**：让 Agent 理解实体间的结构化关系，而不只是模糊的语义相似
2. **演化 > 快照**：记忆是活的——新信息持续融入，旧信息自动失效，不需要重建
3. **时间 > 瞬间**：知道事实"什么时候是真的"，支持历史状态回溯
4. **溯源 > 黑盒**：每条推导出的事实都能追踪到原始数据来源

Mem0 和 Graphiti 代表了两种不同的工程取舍——前者更务实（向量为主，图为辅），后者更前沿（时序图为核心）。选择哪个取决于你的场景对时序精度、溯源完整性和系统复杂度的容忍度。

---

## 参考资料

- [Mem0 官方文档 - Graph Memory](https://docs.mem0.ai/open-source/features/graph-memory)
- [Graphiti GitHub](https://github.com/getzep/graphiti) — Build Temporal Context Graphs for AI Agents
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/abs/2501.13956)
- [Mem0 源码 - graph_memory.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/graph_memory.py)
- [Graphiti 源码 - edge_operations.py](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py)
