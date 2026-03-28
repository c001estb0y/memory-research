# Mem0 Memory 系统技术方案

> **项目定位**：Mem0 是面向 AI 助手/智能体的**通用长期记忆层**（51k+ stars），用 LLM 从对话中抽取事实，经向量检索与可选图存储，实现可检索、可更新、多租户隔离的个性化记忆。

---

## 1. 整体架构

```
┌──────────────────────────────────────────────────────────┐
│                      Memory API                          │
│            add() / search() / update() / delete()        │
└────────────────────────┬─────────────────────────────────┘
                         │
          ┌──────────────▼──────────────┐
          │      Memory (main.py)       │
          │    编排层 · 策略层 · 入口    │
          └──────┬──────────────┬───────┘
                 │              │
    ┌────────────▼───┐   ┌─────▼────────────┐
    │  向量轨 (主)   │   │  图轨 (可选)     │
    │ ThreadPool 并行 │   │ ThreadPool 并行  │
    └───────┬────────┘   └────────┬─────────┘
            │                     │
   ┌────────▼────────┐   ┌───────▼─────────┐
   │ LLM 事实抽取    │   │ LLM 实体/关系   │
   │ (Prompt A)      │   │ 抽取            │
   └────────┬────────┘   └───────┬─────────┘
            │                     │
   ┌────────▼────────┐   ┌───────▼─────────┐
   │ 向量近邻检索    │   │ Cypher 写入     │
   │ + LLM 冲突决策  │   │ + BM25 检索     │
   │ (Prompt B)      │   │                  │
   └────────┬────────┘   └───────┬─────────┘
            │                     │
   ┌────────▼────────┐   ┌───────▼─────────┐
   │  Vector Store   │   │  Graph Store    │
   │ (Qdrant/FAISS/  │   │ (Neo4j/Memgraph │
   │  Milvus/PG...)  │   │  /Neptune/...)  │
   └─────────────────┘   └─────────────────┘
            │
   ┌────────▼────────┐
   │  SQLite History │
   │ (审计 ADD/UPDATE│
   │  /DELETE)       │
   └─────────────────┘
```

---

## 2. 核心数据结构与存储模型

### 2.1 向量轨（主路径）

每条记忆在向量库中的 payload：

```python
{
    "data": "用户喜欢深色主题",          # 记忆正文
    "hash": "md5(data)",                  # 用于变更检测
    "created_at": "2026-03-27T...",       # 创建时间
    "updated_at": "2026-03-27T...",       # 更新时间（仅更新时）
    "user_id": "user_123",               # 作用域
    "agent_id": "agent_456",             # 作用域（可选）
    "run_id": "run_789",                 # 作用域（可选）
}
```

向量存储抽象接口（`mem0/vector_stores/base.py`）：

```python
class VectorStoreBase(ABC):
    @abstractmethod
    def insert(self, vectors, ids, payloads): ...
    @abstractmethod
    def search(self, query, vectors, limit, filters): ...
    @abstractmethod
    def update(self, vector_id, vector, payload): ...
    @abstractmethod
    def delete(self, vector_id): ...
    @abstractmethod
    def get(self, vector_id): ...
    @abstractmethod
    def list(self, filters, limit): ...
```

### 2.2 图轨（可选）

- **存储**：Neo4j/Memgraph 等图数据库
- **节点**：实体（人、组织、概念等）
- **边**：关系（如 "works_at"、"likes" 等）
- **检索**：BM25 在三元组 `(source, relationship, destination)` 上排序

### 2.3 历史审计轨

```python
# mem0/memory/storage.py - SQLite history 表
# 字段：memory_id, old_memory, new_memory, event, is_deleted, actor_id, role
```

---

## 3. Memory 写入流程（核心技术 - 三段式管线）

**Mem0 的核心创新：「抽取 → 近邻 → LLM 决策」三段式写入。**

### 3.1 第一段：LLM 事实抽取

```
用户对话 → get_fact_retrieval_messages() → LLM → {"facts": [...]}
```

提示词区分两种模式：
- **用户记忆**（`USER_MEMORY_EXTRACTION_PROMPT`）：从 user 消息中抽取偏好、经历等
- **代理记忆**（`AGENT_MEMORY_EXTRACTION_PROMPT`）：从 assistant 消息中抽取知识、决策等

```python
# mem0/configs/prompts.py
USER_MEMORY_EXTRACTION_PROMPT = """
You are an expert at extracting structured memories from conversations.
Extract facts about the user's preferences, experiences, and important details.
Output: {"facts": ["fact1", "fact2", ...]}
"""
```

### 3.2 第二段：向量近邻检索

对每个新 fact：
1. 计算 embedding
2. 在相同 `user_id`/`agent_id`/`run_id` 过滤下，`vector_store.search` 取 top-K 相似旧记忆
3. **将旧记忆 id 临时映射为整数**，避免 LLM 伪造 UUID

```python
# mem0/memory/main.py
for new_fact in new_facts:
    embeddings = self.embedding_model.embed(new_fact)
    existing_memories = self.vector_store.search(
        vectors=embeddings, limit=top_k, filters=scope_filters
    )
    # 将 UUID 映射为临时整数 id
    temp_id_map = {i: mem.id for i, mem in enumerate(existing_memories)}
```

### 3.3 第三段：LLM 冲突决策（关键创新）

```
新 fact + 旧记忆列表 → get_update_memory_messages() → LLM → 操作列表
```

LLM 被要求输出结构化操作：

```json
{
  "memory": [
    {
      "id": "0",
      "text": "用户在 Stripe 担任 PM",
      "event": "UPDATE",
      "old_memory": "用户在 Google 担任软件工程师"
    },
    {
      "id": "new",
      "text": "用户喜欢用 TypeScript",
      "event": "ADD"
    },
    {
      "id": "2",
      "text": "",
      "event": "DELETE"
    }
  ]
}
```

**四种操作**：

| 操作 | 含义 | 执行 |
|------|------|------|
| `ADD` | 全新事实 | `_create_memory()` → 新向量 + SQLite ADD |
| `UPDATE` | 更新已有记忆 | `_update_memory()` → 更新向量和 payload + SQLite UPDATE |
| `DELETE` | 过时/错误信息 | `_delete_memory()` → 删除向量 + SQLite DELETE |
| `NONE` | 已存在无需更改 | 仅可选更新 session 字段 |

### 3.4 完整写入流程

```python
# mem0/memory/main.py - add() 方法
def add(self, messages, user_id=None, agent_id=None, run_id=None, ...):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # 向量轨和图轨并行执行
        future_vec = executor.submit(self._add_to_vector_store, messages, ...)
        future_graph = executor.submit(self._add_to_graph, messages, ...)
        concurrent.futures.wait([future_vec, future_graph])

    # _add_to_vector_store 内部流程：
    # 1. LLM 抽取 facts
    # 2. 对每个 fact：embedding → 向量近邻 → LLM 决策
    # 3. 执行 ADD/UPDATE/DELETE/NONE
```

### 3.5 `infer=False` 模式

跳过 LLM 合并，逐条消息直接 `embed + _create_memory`。适用于已结构化数据的导入。

---

## 4. Memory 检索流程

### 4.1 search() 方法

```python
def search(self, query, user_id=None, agent_id=None, limit=100, ...):
    # 1. 构建过滤条件（至少需要 user_id/agent_id/run_id 之一）
    filters = self._build_filters_and_metadata(user_id, agent_id, run_id)

    # 2. 并行执行向量检索和图检索
    with ThreadPoolExecutor() as executor:
        future_vec = executor.submit(self._search_vector_store, query, filters, limit)
        future_graph = executor.submit(self.graph.search, query, filters) if self.enable_graph else None

    # 3. 向量检索核心
    embeddings = self.embedding_model.embed(query, "search")
    memories = self.vector_store.search(query=query, vectors=embeddings,
                                        limit=limit, filters=filters)

    # 4. 可选 Reranker 重排
    if self.reranker and rerank:
        memories = self.reranker.rerank(query, memories)

    return {"results": vec_results, "relations": graph_results}
```

### 4.2 图检索机制

```python
# mem0/memory/graph_memory.py
def search(self, query, filters, limit=100):
    # 1. LLM 从 query 抽取实体
    entity_type_map = self._retrieve_nodes_from_data(query, filters)

    # 2. 在图中搜索相关三元组
    search_output = self._search_graph_db(node_list=list(entity_type_map.keys()))

    # 3. BM25 重排三元组
    bm25 = BM25Okapi(search_outputs_sequence)
    tokenized_query = query.split(" ")
    reranked_results = bm25.get_top_n(tokenized_query, search_outputs_sequence, n=5)

    return search_results
```

---

## 5. 记忆生命周期管理

| 能力 | 实现方式 |
|------|----------|
| **创建** | `_create_memory()` + SQLite `ADD` 记录 |
| **更新** | `_update_memory()`：重算向量与 hash，保留 scope 字段 + SQLite `UPDATE` |
| **删除** | `_delete_memory()`：可选先清理图实体 + SQLite `DELETE` |
| **去重与冲突** | 三段式管线：向量近邻 + LLM 结构化决策（非规则去重） |
| **审计** | `history(memory_id)` → SQLite 完整变更记录 |
| **批量清理** | `delete_all(user_id=..., agent_id=..., run_id=...)` |
| **全量重置** | `reset()`：清 history + 重建 vector collection |

---

## 6. 支持的存储后端

### 6.1 向量数据库（VectorStoreFactory）

| 类别 | 支持后端 |
|------|----------|
| **专用向量库** | Qdrant（默认）、Pinecone、Milvus、Weaviate、FAISS、Turbopuffer |
| **通用数据库** | PostgreSQL (pgvector)、MongoDB、Redis/Valkey、Elasticsearch、OpenSearch |
| **云服务** | AWS S3 Vectors、Azure AI Search、Azure MySQL、Google Vertex AI、Supabase |
| **其他** | Chroma、LangChain、Cassandra、Databricks、Upstash、百度 |

### 6.2 图数据库（GraphStoreFactory）

Neo4j（默认）、Memgraph、Neptune/NeptuneDB、Kuzu、Apache AGE

### 6.3 嵌入模型（EmbedderFactory）

OpenAI、Ollama、HuggingFace、Gemini、Bedrock、FastEmbed 等

### 6.4 重排器（RerankerFactory）

Cohere、Sentence-Transformer、LLM-based 等

---

## 7. 关键技术亮点

### 7.1 三段式写入管线

**这是 Mem0 最核心的设计**：不是简单地向向量库追加记忆，而是：

1. **LLM 抽取**：从对话中提炼结构化事实
2. **向量近邻**：找到可能冲突/重复的已有记忆
3. **LLM 决策**：由 LLM 判断是 ADD/UPDATE/DELETE/NONE

好处：
- 自动去重，不会存储大量重复信息
- 自动处理矛盾（如「用户换了工作」→ UPDATE 旧记忆）
- 临时整数 id 映射避免 LLM 幻觉出 UUID

### 7.2 向量与图解耦并行

- 向量负责**语义召回**（"什么与 X 相关？"）
- 图负责**关系补充**（"X 和 Y 是什么关系？"）
- 两者并行执行，互不阻塞

### 7.3 用户记忆 vs 代理记忆

同一套管线，按是否存在 `agent_id` 与 assistant 消息切换抽取策略：

```python
if agent_id and any(m["role"] == "assistant" for m in messages):
    # 代理记忆模式：从 assistant 消息中抽取
    prompt = AGENT_MEMORY_EXTRACTION_PROMPT
else:
    # 用户记忆模式：从 user 消息中抽取
    prompt = USER_MEMORY_EXTRACTION_PROMPT
```

### 7.4 可插拔存储后端

通过工厂模式，一行配置切换：

```python
config = MemoryConfig(
    vector_store={"provider": "qdrant", "config": {...}},
    graph_store={"provider": "neo4j", "config": {...}},
    llm={"provider": "openai", "config": {...}},
    embedder={"provider": "openai", "config": {...}},
)
memory = Memory.from_config(config)
```

---

## 8. 配置示例

```python
from mem0 import Memory

config = {
    "llm": {
        "provider": "openai",
        "config": {"model": "gpt-4o-mini", "temperature": 0}
    },
    "embedder": {
        "provider": "openai",
        "config": {"model": "text-embedding-3-small"}
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {"collection_name": "memories", "host": "localhost", "port": 6333}
    },
    "graph_store": {
        "provider": "neo4j",
        "config": {"url": "neo4j://localhost:7687", "username": "neo4j", "password": "xxx"}
    }
}

m = Memory.from_config(config)

# 写入
m.add("我刚换到 Stripe 做 PM 了", user_id="alice")

# 检索
results = m.search("alice 在哪里工作？", user_id="alice")
# → [{"memory": "alice 在 Stripe 担任 PM", "score": 0.95}]
```
