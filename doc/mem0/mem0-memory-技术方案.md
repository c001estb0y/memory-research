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

---

## 9. 实战示例：codebuddy-mem 记忆数据 → Mem0 存储与召回

> 以下基于 `memory-research` 项目中 codebuddy-mem MCP 的**真实记忆数据**，演示如果切换到 Mem0，数据会如何存储、建立图关系以及召回。

### 9.1 codebuddy-mem 原始数据结构

codebuddy-mem 有两种核心记录：

**Observation（观察记录）** — 事件级粒度：

```json
{
  "id": 11818,
  "type": "documentation",
  "title": "编辑 Claude Code 子代理配置与编排机制文档",
  "subtitle": "修改 doc/claudecode 下的中文机制说明文档",
  "narrative": "本次会话记录到一次文档文件编辑行为，目标是 Claude Code 子代理的配置与编排机制说明文档...",
  "facts": "在 2026-03-31T13:25:07.752Z 发生了一次 file_edit 操作。被修改的文件路径为 d:\\GitHub\\memory-research\\doc\\claudecode\\claudecode-subagent-配置与编排机制.md。",
  "concepts": "claudecode, subagent, orchestration, configuration, markdown, knowledge-base",
  "meta_intent": "【意图类型】：完善或更新 Claude Code 子代理的配置与编排机制说明",
  "files_modified": "d:\\GitHub\\memory-research\\doc\\claudecode\\claudecode-subagent-配置与编排机制.md",
  "created_at": "2026-03-31T13:25:16.424Z"
}
```

**Summary（会话摘要）** — 会话级粒度：

```json
{
  "id": 1395,
  "request": "用户想确认这些知识整理内容是否已经在仓库中，并帮忙提交到版本库。",
  "learned": "相关知识文档已经存在并在当前仓库中维护，提交过程中需要注意命令要适配 PowerShell 环境。",
  "completed": "完成了仓库状态核查、文档变更暂存，成功创建了一次文档提交。",
  "meta_intent": "用户希望把整理好的知识沉淀到代码仓库，形成可追踪、可共享的文档资产。",
  "files_edited": "claudecode-搜索工具体系与实现机制.md, .gitignore, ...",
  "created_at": "2026-04-01T10:52:17.799Z"
}
```

### 9.2 Mem0 三段式管线处理过程

以 Summary id=1390 为例，原始数据：

```
request: "用户想搞清楚 Claude Code 里搜索 Markdown 和搜索代码分别该走什么流程"
learned: "Markdown 搜索主要依赖 Grep/Glob；代码搜索可结合 LSP 做符号查询"
completed: "完成了搜索体系归纳，产出了搜索工具说明文档"
```

#### 第一段：LLM 事实抽取（`USER_MEMORY_EXTRACTION_PROMPT`）

Mem0 会将 summary 的对话内容发给 LLM，提取结构化事实：

```json
{
  "facts": [
    "用户正在研究 Claude Code 的搜索工具体系",
    "用户发现 Markdown 搜索主要依赖 Grep/Glob 本地检索",
    "用户发现代码搜索可结合 LSP 做符号级查询",
    "用户完成了一份搜索工具说明文档",
    "用户的运行环境是 PowerShell（非 Bash）"
  ]
}
```

#### 第二段：向量近邻检索

对每个 fact 计算 embedding，在已有记忆库中检索：

```python
# fact: "用户正在研究 Claude Code 的搜索工具体系"
existing_memories = vector_store.search(
    vectors=embed("用户正在研究 Claude Code 的搜索工具体系"),
    limit=5,
    filters={"user_id": "minusjiang"}
)
# → 可能找到: ["用户正在研究 Claude Code 的子代理编排机制"] (score=0.82)
```

#### 第三段：LLM 冲突决策（`DEFAULT_UPDATE_MEMORY_PROMPT`）

LLM 对比新 fact 和已有记忆，做出 ADD/UPDATE/DELETE/NONE 决策：

```json
{
  "memory": [
    {
      "id": "0",
      "text": "用户正在研究 Claude Code 的架构（包括搜索工具体系和子代理编排机制）",
      "event": "UPDATE",
      "old_memory": "用户正在研究 Claude Code 的子代理编排机制"
    },
    {
      "id": "new",
      "text": "Markdown 搜索主要依赖 Grep/Glob 本地检索",
      "event": "ADD"
    },
    {
      "id": "new",
      "text": "代码搜索可结合 LSP 做符号、定义与引用级查询",
      "event": "ADD"
    },
    {
      "id": "new",
      "text": "用户的运行环境是 PowerShell，不能用 Bash heredoc",
      "event": "ADD"
    }
  ]
}
```

### 9.3 向量库最终存储

经过三段管线后，向量库中的 payload 如下：

```python
# 记忆 1（UPDATE 后）
{
    "data": "用户正在研究 Claude Code 的架构（包括搜索工具体系和子代理编排机制）",
    "hash": "md5(...)",
    "user_id": "minusjiang",
    "agent_id": "memory-research",
    "created_at": "2026-03-31T13:25:16Z",
    "updated_at": "2026-04-01T06:18:51Z"
}

# 记忆 2（ADD）
{
    "data": "Markdown 搜索主要依赖 Grep/Glob 本地检索",
    "hash": "md5(...)",
    "user_id": "minusjiang",
    "agent_id": "memory-research",
    "created_at": "2026-04-01T06:18:51Z"
}

# 记忆 3（ADD）
{
    "data": "代码搜索可结合 LSP 做符号、定义与引用级查询",
    "hash": "md5(...)",
    "user_id": "minusjiang",
    "agent_id": "memory-research",
    "created_at": "2026-04-01T06:18:51Z"
}

# 记忆 4（ADD）
{
    "data": "用户的运行环境是 PowerShell，不能用 Bash heredoc",
    "hash": "md5(...)",
    "user_id": "minusjiang",
    "agent_id": "memory-research",
    "created_at": "2026-04-01T10:52:17Z"
}
```

### 9.4 图关系构建（Neo4j）

Mem0 图轨会并行对同一段文本执行实体+关系抽取。以多条记忆累积后的图为例：

#### 实体抽取（`extract_entities` tool call）

LLM 从对话中提取实体：

```json
[
  {"name": "minusjiang", "type": "person"},
  {"name": "claude_code", "type": "software"},
  {"name": "grep", "type": "tool"},
  {"name": "glob", "type": "tool"},
  {"name": "lsp", "type": "tool"},
  {"name": "powershell", "type": "environment"},
  {"name": "memory_research", "type": "project"},
  {"name": "openclaw", "type": "software"},
  {"name": "subagent", "type": "concept"},
  {"name": "venus_proxy", "type": "service"}
]
```

#### 关系抽取（`EXTRACT_RELATIONS_PROMPT` + `establish_relationships` tool call）

LLM 基于实体建立**关系三元组**（Relation Triple）。

> **什么是关系三元组？**
>
> 关系三元组是知识图谱的**最小语义单元**，格式为 `(Source, Relationship, Destination)`，即"主体—关系—客体"。
> 
> ```
> (minusjiang,  researches,  claude_code)
>     ↑             ↑             ↑
>    主体          关系           客体
>   (Node)       (Edge)        (Node)
> ```
>
> 等价自然语言："minusjiang 正在研究 claude_code"。三个元素缺一不可：
>
> | 元素 | 英文 | 角色 | 图结构 |
> |------|------|------|--------|
> | Source | Subject | 主体/起点 | 节点 (Node) |
> | Relationship | Predicate | 关系/动作 | 边 (Edge) |
> | Destination | Object | 客体/终点 | 节点 (Node) |
>
> **为什么需要三元组？** 向量搜索只能衡量"这段话和那段话像不像"（模糊语义匹配），三元组则能精确回答"A 和 B 是什么关系"并支持**多跳推理**——沿着边遍历可以连接多个间接关联的实体。例如，沿 `minusjiang → works_on → memory_research → documents → claude_code → has_component → subagent` 这条路径，可以推理出"你在 memory-research 项目中研究了 Claude Code 的子代理机制"。
>
> | 维度 | 向量 (Embedding) | 三元组 (Triple) |
> |------|------------------|-----------------|
> | 存什么 | 一段话的"语义指纹" | 两个实体间的明确关系 |
> | 查什么 | "这段话和那段话像不像" | "A 和 B 是什么关系" |
> | 擅长 | 模糊语义匹配 | 精确关系查询、路径推理 |
> | 弱点 | 无法表达结构化关系 | 依赖实体归一化质量 |
>
> Mem0 采用**双轨并行**：向量负责"模糊想起来"，三元组负责"精确推理出来"，两者互补。

Mem0 源码中通过 `EXTRACT_RELATIONS_PROMPT`（`mem0/graphs/utils.py`）指导 LLM 抽取三元组，核心指令为：
- 只抽取文本中**明确陈述**的信息
- 使用一致、通用、无时态的关系类型（如用 `professor` 而非 `became_professor`）
- 用户自称"I/me/my"时，用 `user_id` 替代

LLM 通过 `establish_relationships` tool call 返回三元组列表：

```
minusjiang -- researches --> claude_code
minusjiang -- works_on --> memory_research
minusjiang -- uses --> powershell
claude_code -- has_tool --> grep
claude_code -- has_tool --> glob
claude_code -- has_tool --> lsp
claude_code -- compared_with --> openclaw
claude_code -- has_component --> subagent
memory_research -- documents --> claude_code
memory_research -- documents --> openclaw
minusjiang -- configured --> venus_proxy
venus_proxy -- proxies --> claude_code
grep -- searches --> markdown
lsp -- provides --> symbol_query
```

#### 三元组抽取的完整机制：LLM + Function Calling

> **三元组抽取是纯代码还是 LLM？**
>
> **100% 由 LLM 完成**，不是正则、NER 库或规则代码。整个图轨管线涉及 **4 步**，前 3 步都是 LLM 调用：
>
> | 步骤 | 源码方法 | 做什么 | 实现方式 |
> |------|---------|--------|----------|
> | 1. 实体抽取 | `_retrieve_nodes_from_data()` | 找出文本中的实体 | LLM + `extract_entities` tool call |
> | 2. 关系抽取 | `_establish_nodes_relations_from_data()` | 建立三元组 | LLM + `establish_relationships` tool call |
> | 3. 冲突检测 | `_get_delete_entities_from_search_output()` | 判断哪些旧边要软删 | LLM + `delete_graph_memory` tool call |
> | 4. 图写入 | `_add_entities()` | 写入 Neo4j | **纯代码**（Cypher MERGE） |

**LLM 如何输出结构化三元组？** Mem0 不是让 LLM 直接吐文本再正则解析，而是用 **Function Calling（Tool Use）** 机制。以关系抽取为例，`RELATIONS_TOOL`（源码 `mem0/graphs/tools.py`）定义了 JSON Schema：

```json
{
  "name": "establish_relationships",
  "description": "Establish relationships among the entities based on the provided text.",
  "parameters": {
    "type": "object",
    "properties": {
      "entities": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "source": {"type": "string", "description": "The source entity of the relationship."},
            "relationship": {"type": "string", "description": "The relationship between the source and destination entities."},
            "destination": {"type": "string", "description": "The destination entity of the relationship."}
          },
          "required": ["source", "relationship", "destination"]
        }
      }
    }
  }
}
```

LLM 被要求调用这个 tool，返回的就是 **JSON 结构化的三元组数组**，不需要人工解析。这比直接输出文本再正则匹配可靠得多。

**Mem0 使用什么 LLM？** 从源码 `graph_memory.py` 第 58-71 行：

```python
# Default to openai if no specific provider is configured
self.llm_provider = "openai"
if self.config.llm and self.config.llm.provider:
    self.llm_provider = self.config.llm.provider
if self.config.graph_store and self.config.graph_store.llm and self.config.graph_store.llm.provider:
    self.llm_provider = self.config.graph_store.llm.provider
```

**默认使用 OpenAI**（GPT-4o 系列），但支持 16 种 LLM 后端，且**图轨和向量轨可以配置不同的 LLM**（`graph_store.llm` 优先级最高 → 全局 `config.llm` → fallback `openai`）：

| Provider | 说明 | Provider | 说明 |
|----------|------|----------|------|
| `openai` | **默认**，GPT-4o 等 | `anthropic` | Claude 系列 |
| `azure_openai` | Azure 托管 OpenAI | `gemini` | Google Gemini |
| `deepseek` | DeepSeek | `ollama` | 本地模型（Llama、Qwen） |
| `groq` | Groq 加速推理 | `together` | Together AI |
| `aws_bedrock` | AWS Bedrock | `litellm` | 统一代理层 |
| `xai` | xAI Grok | `lmstudio` | LM Studio 本地 |
| `vllm` | vLLM 推理引擎 | `langchain` | LangChain 封装 |
| `minimax` | MiniMax | `sarvam` | Sarvam AI |

整个流程本质是：**Prompt 指导语义理解 → Function Calling 约束输出格式 → 代码写入图数据库**。LLM 负责"理解"，代码负责"存储"。

#### Neo4j 简介与在 Mem0 中的角色

> **什么是 Neo4j？**
>
> Neo4j 是全球最流行的**原生图数据库**（Graph Database），专门为存储和查询**节点（Node）与关系（Relationship）** 构成的图结构数据而设计。与传统关系型数据库（MySQL、PostgreSQL）通过 JOIN 关联表不同，Neo4j 将关系作为**一等公民**直接存储在磁盘上，使得关系遍历的时间复杂度为 **O(1)**（常数时间），而非 SQL JOIN 的 O(n)。
>
> **核心概念：**
>
> | 概念 | 说明 | 类比 SQL |
> |------|------|----------|
> | **Node（节点）** | 图中的实体，可带标签（Label）和属性 | 表中的一行 |
> | **Relationship（关系）** | 节点之间的有向连接，有类型和属性 | 外键 JOIN |
> | **Label（标签）** | 节点的分类标记，如 `:Person`、`:Software` | 表名 |
> | **Property（属性）** | 节点或关系上的键值对，如 `name: 'grep'` | 列值 |
> | **Cypher** | Neo4j 的声明式查询语言 | SQL |
>
> **Cypher 语法直觉：**
>
> ```cypher
> -- 创建/匹配节点：用圆括号 ()
> (alice:Person {name: 'Alice'})
>
> -- 创建/匹配关系：用箭头 -[]->, 中括号放关系类型
> (alice)-[:KNOWS]->(bob)
>
> -- 查询：MATCH + RETURN
> MATCH (p:Person)-[:WORKS_AT]->(c:Company)
> WHERE c.name = 'Tencent'
> RETURN p.name
> ```
>
> **为什么 Mem0 选择 Neo4j？**
>
> 1. **原生图存储**：三元组 `(source)-[relationship]->(destination)` 天然映射为 Neo4j 的 Node-Relationship-Node 结构，无需额外转换
> 2. **向量索引**：Neo4j 5.x 内置向量相似度搜索（`vector.similarity.cosine`），Mem0 用它在图上做实体对齐——给每个节点存 embedding，新实体先在图中找"最像"的已有节点，避免重复实体
> 3. **MERGE 语义**：`MERGE` 是"有则匹配，无则创建"的原子操作，天然支持幂等写入，适合 Mem0 的增量更新模式
> 4. **多跳遍历**：沿关系路径查询是 Neo4j 的核心强项，如 `MATCH path = (a)-[*1..3]->(b)` 可以在 3 跳内找到所有关联实体
>
> **Mem0 中的替代方案：** Neo4j 是默认图后端，但 Mem0 也支持其他图数据库：
>
> | Provider | 说明 |
> |----------|------|
> | `neo4j`（default） | 最成熟，支持向量索引 + Cypher |
> | `memgraph` | 兼容 Cypher 的高性能内存图库 |
> | `kuzu` | 嵌入式图数据库，无需独立部署 |
> | `apache_age` | PostgreSQL 的图扩展插件 |
> | `neptune` / `neptunedb` | AWS 托管图数据库 |

#### Neo4j 写入（Cypher）

```cypher
-- 节点 MERGE（带 embedding 向量属性）
MERGE (n:Entity {name: 'minusjiang'})
SET n.entity_type = 'person', n.updated_at = datetime()
CALL db.create.setNodeVectorProperty(n, 'embedding', $embedding)

MERGE (n:Entity {name: 'claude_code'})
SET n.entity_type = 'software', n.updated_at = datetime()

-- 关系 MERGE
MERGE (s:Entity {name: 'minusjiang'})
MERGE (d:Entity {name: 'claude_code'})
MERGE (s)-[r:RESEARCHES]->(d)
ON CREATE SET r.created_at = datetime(), r.valid = true, r.mentions = 1
ON MATCH SET r.mentions = r.mentions + 1, r.updated_at = datetime()

MERGE (s:Entity {name: 'claude_code'})
MERGE (d:Entity {name: 'grep'})
MERGE (s)-[r:HAS_TOOL]->(d)
ON CREATE SET r.created_at = datetime(), r.valid = true, r.mentions = 1
```

#### 最终图结构可视化

```
                        ┌──────────┐
                   ┌────│ OpenClaw │
                   │    └──────────┘
            compared_with    ▲
                   │     documents
                   ▼         │
┌──────────┐  researches ┌──────────────┐  has_tool  ┌──────┐  searches  ┌──────────┐
│minusjiang│────────────→│  Claude Code │───────────→│ Grep │───────────→│ Markdown │
└──────────┘             └──────────────┘            └──────┘            └──────────┘
     │                        │                 has_tool
     │ uses                   │ has_component     │
     ▼                        ▼                   ▼
┌────────────┐          ┌──────────┐         ┌──────┐ provides ┌──────────────┐
│ PowerShell │          │ Subagent │         │ LSP  │─────────→│ Symbol Query │
└────────────┘          └──────────┘         └──────┘          └──────────────┘
     │                                            
     │ works_on                               has_tool
     ▼                                          │
┌─────────────────┐                        ┌──────┐
│ Memory Research │                        │ Glob │
└─────────────────┘                        └──────┘
```

### 9.5 召回过程演示

#### 场景 1：语义召回 —— "Claude Code 的搜索能力有哪些？"

**向量检索**：

```python
results = m.search("Claude Code 的搜索能力有哪些？", user_id="minusjiang")
```

1. 计算查询 embedding
2. 在 `user_id=minusjiang` 过滤下，向量近邻 top-K：

```json
{
  "results": [
    {"memory": "Markdown 搜索主要依赖 Grep/Glob 本地检索", "score": 0.91},
    {"memory": "代码搜索可结合 LSP 做符号、定义与引用级查询", "score": 0.88},
    {"memory": "用户正在研究 Claude Code 的架构（包括搜索工具体系和子代理编排机制）", "score": 0.82}
  ]
}
```

**图检索**（并行）：

1. LLM 从 query 抽取实体：`["claude_code", "搜索"]`
2. Neo4j 查找 `claude_code` 的出边 + 入边：

```cypher
MATCH (n:Entity {name: 'claude_code'})-[r]->(m) WHERE r.valid = true RETURN n, r, m
UNION
MATCH (n)-[r]->(m:Entity {name: 'claude_code'}) WHERE r.valid = true RETURN n, r, m
```

3. BM25 重排三元组，返回：

```json
{
  "relations": [
    {"source": "claude_code", "relationship": "has_tool", "destination": "grep"},
    {"source": "claude_code", "relationship": "has_tool", "destination": "lsp"},
    {"source": "claude_code", "relationship": "has_tool", "destination": "glob"},
    {"source": "grep", "relationship": "searches", "destination": "markdown"},
    {"source": "lsp", "relationship": "provides", "destination": "symbol_query"}
  ]
}
```

**最终合并**：向量结果提供自然语言描述，图结果补充结构化关系 → 模型获得**语义+关系**双重上下文。

#### 场景 2：关系追踪 —— "我之前在哪个项目里研究过子代理机制？"

**向量检索**返回：

```json
[
  {"memory": "用户正在研究 Claude Code 的架构（包括搜索工具体系和子代理编排机制）", "score": 0.85}
]
```

**图检索**（关键补充）：

```json
{
  "relations": [
    {"source": "minusjiang", "relationship": "works_on", "destination": "memory_research"},
    {"source": "minusjiang", "relationship": "researches", "destination": "claude_code"},
    {"source": "claude_code", "relationship": "has_component", "destination": "subagent"},
    {"source": "memory_research", "relationship": "documents", "destination": "claude_code"}
  ]
}
```

→ 模型可以推理出：`minusjiang → works_on → memory_research → documents → claude_code → has_component → subagent`，完整回答"你在 memory-research 项目中研究了 Claude Code 的子代理机制"。

#### 场景 3：冲突更新 —— 用户换了运行环境

```python
m.add("我现在改用 WSL2 + Bash 了，不再用 PowerShell", user_id="minusjiang")
```

三段式管线：
1. **抽取** → `["用户改用 WSL2 + Bash 环境"]`
2. **近邻** → 找到旧记忆 `"用户的运行环境是 PowerShell，不能用 Bash heredoc"` (score=0.87)
3. **决策** →

```json
{
  "memory": [
    {
      "id": "4",
      "text": "用户的运行环境已切换为 WSL2 + Bash",
      "event": "UPDATE",
      "old_memory": "用户的运行环境是 PowerShell，不能用 Bash heredoc"
    }
  ]
}
```

图侧同步：
- 软删旧边：`minusjiang -- uses --> powershell` → `SET r.valid = false`
- 新增边：`minusjiang -- uses --> wsl2_bash`

### 9.6 codebuddy-mem vs Mem0 能力对比

| 维度 | codebuddy-mem | Mem0 |
|------|---------------|------|
| **存储粒度** | Observation（事件级）+ Summary（会话级） | Fact（原子事实级） |
| **存储方式** | SQLite FTS5 全文索引 | 向量库 embedding + 图数据库 |
| **去重机制** | 无自动去重，LLM 重复抽取相同文件编辑 | 三段式管线自动 ADD/UPDATE/DELETE 去重 |
| **冲突处理** | 无冲突检测，旧记忆和新记忆并存 | LLM 语义比较 → UPDATE 覆盖旧信息 |
| **关系建模** | 无显式关系，依赖 concepts 标签关联 | Neo4j 图：实体节点 + 关系边 |
| **检索方式** | FTS 全文搜索 + timeline 时序 | 向量语义近邻 + 图三元组 BM25 + 可选 reranker |
| **跨会话能力** | 按 project 隔离，可搜索所有会话 | 按 user_id/agent_id 隔离，语义召回 |
| **可解释性** | narrative + facts + meta_intent 丰富 | 记忆正文简洁，图关系直观 |
| **典型问题** | 同一文件多次编辑产生大量近似记录 | 自动合并，但 LLM 调用成本高 |

### 9.7 Mem0 的核心优势与局限

**优势**：
- **自动去重**：codebuddy-mem 中同一文件 `claudecode-subagent-配置与编排机制.md` 被编辑 8 次，产生了 8 条几乎相同的 observation；Mem0 会在第 2 次写入时自动 UPDATE（合并），只保留一条
- **冲突解决**：当用户从 PowerShell 切换到 WSL2 时，Mem0 自动更新旧记忆；codebuddy-mem 会同时保留两条矛盾记录
- **关系推理**：图结构可以回答"X 和 Y 是什么关系"这类关联查询，codebuddy-mem 只能靠文本匹配

**局限**：
- **LLM 调用成本**：每条记忆写入需要 2 次 LLM 调用（事实抽取 + 冲突决策），图轨再加 2 次（实体抽取 + 关系抽取）
- **上下文丢失**：Mem0 只存原子 fact，丢失了 codebuddy-mem 中 narrative、meta_intent 等丰富的上下文信息
- **时序能力弱**：Mem0 没有 codebuddy-mem 的 timeline 功能，无法按时间线浏览"之前发生了什么"
- **实体漂移**：图中的实体名需要严格归一化（`claude_code` vs `Claude Code` vs `claudeCode`），否则同一实体可能产生多个节点
