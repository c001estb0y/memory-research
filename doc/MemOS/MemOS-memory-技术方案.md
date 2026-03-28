# MemOS Memory 系统技术方案

> **项目定位**：MemOS 是面向 LLM 与 AI Agent 的**记忆操作系统**（7.9k stars），用操作系统隐喻统一管理明文记忆、激活记忆（KV Cache）、参数记忆（LoRA）三种形态，支持跨任务 Skill 记忆的持久化与演进。

---

## 1. 整体架构（操作系统隐喻）

```
┌───────────────────────────────────────────────────────────┐
│                    MemOS API Layer                         │
│              /search_memories  /add_memories               │
└─────────────────────┬─────────────────────────────────────┘
                      │
┌─────────────────────▼─────────────────────────────────────┐
│                  MemScheduler（调度器）                     │
│  ┌──────────────┐  ┌──────────────────┐  ┌────────────┐  │
│  │MemoryManager │  │ActivationMemMgr  │  │TaskHandlers│  │
│  │(写入/精化)   │  │(KV Cache 管理)   │  │(异步任务)  │  │
│  └──────┬───────┘  └────────┬─────────┘  └─────┬──────┘  │
│         │                   │                   │         │
│  ┌──────▼───────────────────▼───────────────────▼──────┐  │
│  │              MemCube（记忆立方体）                    │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │  │
│  │  │ text_mem │ │ act_mem  │ │ para_mem │ │pref_mem│ │  │
│  │  │(明文树)  │ │(KV Cache)│ │(LoRA)    │ │(偏好)  │ │  │
│  │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └───┬────┘ │  │
│  └───────┼─────────────┼────────────┼───────────┼──────┘  │
└──────────┼─────────────┼────────────┼───────────┼─────────┘
           │             │            │           │
    ┌──────▼──────┐ ┌────▼────┐ ┌────▼────┐ ┌────▼────┐
    │ Neo4j/图库  │ │ Pickle  │ │ LoRA    │ │ Neo4j   │
    │ + 向量索引  │ │ 文件    │ │ adapter │ │ + 向量  │
    └─────────────┘ └─────────┘ └─────────┘ └─────────┘
```

### 核心设计理念

MemOS 将 AI 记忆类比为操作系统中的存储层级：

| OS 概念 | MemOS 对应 | 说明 |
|---------|------------|------|
| CPU 寄存器 | Activation Memory (KV Cache) | 最快速的上下文，当前推理直接使用 |
| 内存 | Working Memory | 当前任务的临时上下文 |
| 磁盘 | Long-Term / User Memory | 持久化的长期记忆 |
| 固件 | Parametric Memory (LoRA) | 写入模型权重的知识 |

**记忆生命周期**：`Generated → Activated → Merged → Archived → Frozen`

---

## 2. MemCube：记忆容器（核心抽象）

### 2.1 MemCube 定义

```python
# src/memos/mem_cube/base.py
class BaseMemCube(ABC):
    def __init__(self, config: BaseMemCubeConfig):
        self.text_mem: BaseTextMemory      # 明文记忆（图+向量）
        self.act_mem: BaseActMemory        # 激活记忆（KV Cache）
        self.para_mem: BaseParaMemory      # 参数记忆（LoRA）
        self.pref_mem: BaseTextMemory      # 偏好记忆
```

### 2.2 工厂初始化

```python
# src/memos/mem_cube/general.py
class GeneralMemCube(BaseMemCube):
    def __init__(self, config):
        self._text_mem = MemoryFactory.from_config(config.text_mem) \
            if config.text_mem.backend != "uninitialized" else None
        self._act_mem = MemoryFactory.from_config(config.act_mem) \
            if config.act_mem.backend != "uninitialized" else None
        self._para_mem = MemoryFactory.from_config(config.para_mem) \
            if config.para_mem.backend != "uninitialized" else None
        self._pref_mem = MemoryFactory.from_config(config.pref_mem) \
            if config.pref_mem.backend != "uninitialized" else None
```

### 2.3 多 Cube 组合

`CompositeCubeView.search_memories` 对每个 cube 并行搜索并合并结果，支持多智能体共享记忆。

---

## 3. 记忆类型分类（九种一等类型）

```python
# src/memos/memories/textual/item.py
class TreeNodeTextualMemoryMetadata:
    memory_type: Literal[
        "WorkingMemory",          # 当前任务临时上下文
        "LongTermMemory",         # 长期事实与知识
        "UserMemory",             # 用户视角记忆
        "OuterMemory",            # 外部检索（联网等）
        "ToolSchemaMemory",       # 工具模式定义
        "ToolTrajectoryMemory",   # 工具调用轨迹
        "RawFileMemory",          # 文档/KB chunks
        "SkillMemory",            # 可复用技能模板
        "PreferenceMemory",       # 偏好信息
    ]
```

### 3.1 记忆类型间的转换关系

```
                    ┌──────────────┐
对话/任务执行 ──→  │WorkingMemory │ ──(异步精化)──→ LongTermMemory
                    └──────┬───────┘                    │
                           │                      (合并 merged_from)
                           ▼                            │
                    ┌──────────────┐              ┌─────▼──────┐
                    │ SkillMemory  │              │ Archived   │
                    │(跨任务复用)  │              │(旧版本归档) │
                    └──────────────┘              └────────────┘
                           │
                     (检索激活)
                           ▼
                    ┌──────────────┐
                    │ KV Cache     │ ←── 明文 → KV Cache 转化
                    │(激活记忆)    │
                    └──────────────┘
```

---

## 4. 明文树记忆（TreeTextMemory）— 核心存储

### 4.1 数据模型

```python
# src/memos/memories/textual/item.py
class TextualMemoryItem:
    id: str
    content: str
    metadata: TreeNodeTextualMemoryMetadata
        # memory_type: 上述九种之一
        # status: "activated" | "resolving" | "archived" | "deleted"
        # tags: List[str]
        # sources: List[SourceMessage]   # 溯源
        # history: List[ArchivedTextualMemory]  # 版本历史
        # evolve_to: Optional[str]       # 演进目标
        # merged_from: List[str]         # 合并来源
        # embedding: Optional[List[float]]
        # background: Optional[str]      # 产生背景
```

### 4.2 图存储结构

明文记忆存储在 Neo4j 等图数据库中：
- **节点**：每条 `TextualMemoryItem` 为一个节点
- **边**：记忆间的关联（合并、演进、溯源等）
- **向量索引**：节点上的 `embedding` 字段用于语义检索
- **BM25/全文索引**：支持关键词检索

---

## 5. Memory 写入流程（核心技术 — 双阶段管线）

### 5.1 第一阶段：快写（Fast Path）

```
对话/任务结果
       │
       ▼
  MemReader.get_memory(mode="fast")
       │
       ▼
  LLM 抽取结构化记忆 ──→ TextualMemoryItem[]
       │
       ▼
  MemoryManager.add()
       │
       ├──→ WorkingMemory 镜像（临时，带 working_binding 标签）
       │
       └──→ 目标类型（LTM/UM/Skill/Tool/...）入图库
```

关键实现（`src/memos/memories/textual/tree_text_memory/organize/manager.py`）：

```python
def add(self, memories, mode="fast"):
    for memory in memories:
        working_id = memory.id or str(uuid.uuid4())

        # 同时写入 WorkingMemory 镜像
        if memory.metadata.memory_type in ("WorkingMemory", "LongTermMemory", "UserMemory", "OuterMemory"):
            working_metadata = memory.metadata.model_copy(
                update={"memory_type": "WorkingMemory"}
            )
            working_nodes.append(...)

        # 写入目标类型
        if memory.metadata.memory_type in ("LongTermMemory", "UserMemory", "SkillMemory", ...):
            if "mode:fast" in tags:
                metadata["background"] = f"[working_binding:{working_id}] direct built from raw inputs"
            target_nodes.append(...)
```

### 5.2 第二阶段：异步精化（Fine Transfer）

```
MemScheduler 触发 MemReadMessageHandler
       │
       ▼
  拉取第一阶段写入的记忆节点（by mem_ids）
       │
       ▼
  MemReader.fine_transfer_simple_mem()
       │  LLM 精化：补充背景、优化表述、合并重复
       ▼
  text_mem.add(精化后的记忆)
       │
       ▼
  旧节点标为 archived（若有 merged_from）
       │
       ▼
  清理 WorkingMemory 中的 working_binding 临时节点
```

**双阶段的意义**：
- **Fast Path** 保证低延迟（先落库再说）
- **Fine Transfer** 保证质量（异步 LLM 精化、合并、去重）

---

## 6. Memory 检索流程（核心技术 — 多路并行）

### 6.1 检索管线

```
User Query
    │
    ▼
TaskGoalParser（意图解析）
    │  fast: 分词/token
    │  fine: LLM → ParsedTaskGoal(topic/keys/tags)
    ▼
_retrieve_paths（多路并行检索）
    │
    ├──→ Path A: WorkingMemory（当前任务上下文）
    ├──→ Path B: LongTerm + UserMemory（长期记忆）
    ├──→ Path C: Internet（外部检索，可选）
    ├──→ Path D: Keyword（关键词检索，可选）
    ├──→ Path E: SkillMemory（技能记忆，可选）
    ├──→ Path F: PreferenceMemory（偏好记忆，可选）
    └──→ Path G: ToolMemory（工具记忆，可选）
    │
    ▼
GraphMemoryRetriever（每路内部混合检索）
    │
    ├──→ _graph_recall（图上游走）
    ├──→ _vector_recall（向量相似度）
    ├──→ BM25（关键词匹配，可选）
    └──→ 全文检索（可选）
    │
    ▼
按 id 去重合并
    │
    ▼
Reranker（重排）
    │
    ▼
Reasoner（推理后处理）
    │
    ▼
MOSSearchResult
```

### 6.2 GraphMemoryRetriever 实现

```python
# src/memos/memories/textual/tree_text_memory/retrieve/recall.py
def retrieve(self, query, memory_scope, top_k, ...):
    if memory_scope == "WorkingMemory":
        return self._get_working_memories(top_k)

    # 三路并行
    with ContextThreadPoolExecutor(max_workers=3) as executor:
        future_graph = executor.submit(self._graph_recall, ...)
        future_vector = executor.submit(self._vector_recall, ...)
        future_bm25 = executor.submit(self._bm25_recall, ...) if bm25_enabled else None

    # 按 id 去并
    combined = {item.id: item for item in graph_results + vector_results + bm25_results}
    return list(combined.values())
```

### 6.3 检索结果结构

```python
# src/memos/types/general_types.py
class MOSSearchResult(TypedDict):
    text_mem: list[dict[str, str | list[TextualMemoryItem]]]
    act_mem: list[dict[str, str | list[ActivationMemoryItem]]]
    para_mem: list[dict[str, str | list[ParametricMemoryItem]]]
```

---

## 7. Skill Memory（技能记忆）— 特色功能

### 7.1 技能抽取

通过专用 prompt 从对话中抽象可复用方法论：

```python
# src/memos/templates/skill_mem_prompt.py
SKILL_MEMORY_EXTRACTION_PROMPT = """
从对话中提取可复用的技能模板，包括：
- trigger: 什么情况下触发
- 方法论/步骤
- 示例
- 脚本纲要
支持与已有 skill 的 update/old_memory_id 合并
"""
```

### 7.2 技能检索

```python
# src/memos/memories/textual/tree_text_memory/retrieve/searcher.py
def _retrieve_from_skill_memory(self, ...):
    if memory_type not in ["All", "SkillMemory"]:
        return []

    items = self.graph_retriever.retrieve(
        memory_scope="SkillMemory", ...
    )
    return self.reranker.rerank(items, ...)
```

### 7.3 跨任务复用

不同 session 共享同一 `mem_cube_id` 即可共享 Skill 记忆，实现跨任务知识复用。

---

## 8. Activation Memory（激活记忆 — KV Cache）

### 8.1 原理

将检索到的明文记忆转化为 LLM 的 KV Cache，实现：
- 减少重复的 prompt token 消耗
- 加速推理（前缀复现）

### 8.2 实现

```python
# src/memos/memories/activation/kv.py
class KVCacheMemory(BaseActMemory):
    def extract(self, text: str) -> KVCacheItem:
        kv_cache = self.llm.build_kv_cache(text)
        return KVCacheItem(
            memory=kv_cache,
            metadata={"source_text": text, "extracted_at": datetime.now().isoformat()},
        )

# src/memos/mem_scheduler/memory_manage_modules/activation_memory_manager.py
class ActivationMemoryManager:
    def update_activation_memory(self, new_memories, ...):
        # 1. 把明文记忆拼成模板文本
        new_text = MEMORY_ASSEMBLY_TEMPLATE.format(
            memory_text="".join([f"{i+1}. {s.strip()}\n" for i, s in enumerate(memories)])
        )
        # 2. 生成 KV Cache
        cache_item = act_mem.extract(new_text)
        # 3. 持久化
        act_mem.add([cache_item])
        act_mem.dump(self.act_mem_dump_path)  # → activation_memory.pickle
```

---

## 9. 记忆生命周期管理

| 阶段 | 状态 | 说明 |
|------|------|------|
| Generated | `activated` | 从对话/任务中新生成 |
| Activated | `activated` | 被检索激活，可能转为 KV Cache |
| Merged | `archived`(旧) + `activated`(新) | 多条记忆合并为一条 |
| Archived | `archived` | 被新记忆替代，保留历史 |
| Frozen | `deleted` | 不再参与检索 |

```python
# 合并流程
if memory.merged_from:
    for old_id in memory.merged_from:
        text_mem.update_status(old_id, status="archived")
```

---

## 10. 关键技术亮点

1. **MemCube 多形态记忆容器**：明文 + KV Cache + LoRA + 偏好，统一配置与工厂路由
2. **双阶段写入（Fast + Fine）**：低延迟落库 + 异步 LLM 精化，兼顾体验与质量
3. **多路并行检索**：图 + 向量 + BM25 + 全文 + 外网，七条路径并发
4. **可治理、可溯源的记忆**：`SourceMessage`、`status`、`history`、`merged_from` 支撑全生命周期
5. **Skill Memory 作为一等类型**：支持跨任务复用方法论，且与已有 skill 自动合并
6. **KV Cache 加速**：检索到的明文记忆可转化为 KV Cache，减少 token 消耗
7. **九种记忆类型的细粒度分类**：不同类型有独立的写入/检索路径和生命周期策略

---

## 11. 当前局限

- **LoRA Memory**：参数记忆模块当前为占位实现（`dump` 写 placeholder），尚未构成可用训练管线
- **文档外迁**：主文档已迁至 [MemTensor/MemOS-Docs](https://github.com/MemTensor/MemOS-Docs)
- **论文与实现差距**：部分论文中描述的「明文蒸馏入权重」尚未在代码中落地
