# Cognee — AI Agent Memory 知识引擎 Overview

> 基于 [topoteretes/cognee](https://github.com/topoteretes/cognee) 源码与文档的系统梳理。

---

## 一、项目简介

Cognee 是一个开源的 **Knowledge Engine（知识引擎）**，专为 AI Agent 提供持久化、可演进的记忆能力。它将任意格式的数据摄入后，通过 LLM 结构化抽取构建 **知识图谱 + 向量索引**，再以混合检索方式为 Agent 提供精准上下文。

| 维度 | 说明 |
|------|------|
| **定位** | AI Agent 的知识基础设施层 |
| **Stars** | 16,500+（2026.04） |
| **语言** | Python（3.10–3.13） |
| **License** | Apache-2.0 |
| **核心理念** | 向量检索 + 图数据库 + 认知科学 |
| **官网** | https://www.cognee.ai |

### 一句话概括

> Cognee 把文档变成 AI 的记忆——不是简单的 RAG 检索，而是构建可推理、可演进、可追溯的知识图谱。

---

## 二、背景与设计哲学

### 2.1 解决什么问题

传统 RAG（Retrieval-Augmented Generation）只做"文档 → 向量 → 相似度匹配"，存在三个核心问题：

1. **无关系感知**：向量检索只看语义相似度，无法回答"A 和 B 是什么关系"这类结构化问题
2. **无知识演进**：每次检索都是静态的，不会从交互中学习
3. **无租户隔离**：多用户/多 Agent 场景下缺乏数据隔离机制

Cognee 的回答是：**在向量检索之上叠加知识图谱层**，用 LLM 从数据中抽取实体和关系，构建可遍历、可推理的图结构；同时引入会话记忆、反馈权重和自我改进机制，让知识随使用而增长。

### 2.2 设计取向

- **图 + 向量双模**：不是纯 RAG，图遍历、邻域扩展、Cypher 查询与向量检索组合使用
- **本体约束**：支持 OWL/RDF 本体定义，让抽取结果符合领域模型
- **会话与永久记忆闭环**：短期 session 缓存 ↔ `improve` 反馈权重 ↔ 永久图回写
- **产品化就绪**：多租户/用户隔离、ACL、可观测性（OTEL/Langfuse/Sentry）、分布式部署
- **可扩展流水线**：`Task` + `run_pipeline` 编排，可自定义认知化任务链

---

## 三、核心架构

```
┌─────────────────────────────────────────────────────────────┐
│                        应用层                                │
│  Claude Code Plugin │ MCP Server │ REST API │ Python SDK     │
├─────────────────────────────────────────────────────────────┤
│                      API 层（V1 + V2）                       │
│  V1: add → cognify → search                                 │
│  V2: remember / recall / improve / forget                    │
│  Cloud: serve / disconnect                                   │
├─────────────────────────────────────────────────────────────┤
│                      编排层（Pipeline）                       │
│  Task → run_pipeline → 数据集解析 → 前台/后台执行 → 缓存     │
├─────────────────────────────────────────────────────────────┤
│                      领域任务层（Tasks）                      │
│  ingestion │ classify │ chunk │ graph_extract │ summarize    │
│  add_data_points │ temporal_cognify │ memify                 │
├─────────────────────────────────────────────────────────────┤
│                      检索层（Search/Retrieval）               │
│  向量检索 │ 图遍历 │ Cypher │ LLM 补全 │ 会话检索            │
│  SearchType 路由 │ 授权过滤                                  │
├─────────────────────────────────────────────────────────────┤
│                      引擎与模型层                             │
│  DataPoint │ KnowledgeGraph │ Node/Edge │ 去重 │ 统一引擎     │
├─────────────────────────────────────────────────────────────┤
│                      基础设施层                               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐   │
│  │ 向量存储  │ │ 图存储   │ │ 关系库   │ │ LLM/Embedding │   │
│  │ LanceDB  │ │ Kuzu     │ │ SQLAlch. │ │ LiteLLM      │   │
│  │ pgvector │ │ Neo4j    │ │ aiosqlite│ │ + Instructor  │   │
│  │ ChromaDB │ │ Postgres │ │          │ │ + fastembed   │   │
│  │ Neptune  │ │ Neptune  │ │          │ │ + Ollama      │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────┘   │
├─────────────────────────────────────────────────────────────┤
│                      横切关注点                               │
│  多租户/ACL │ OTEL │ Langfuse │ Sentry │ 遥测 │ 会话缓存    │
└─────────────────────────────────────────────────────────────┘
```

---

## 四、核心 API 与数据流

### 4.1 V1 经典三步：add → cognify → search

这是 Cognee 最基础的使用范式：

```python
import cognee

# 1. 摄入数据
await cognee.add("Cognee turns documents into AI memory.")

# 2. 认知化：构建知识图谱 + 向量索引
await cognee.cognify()

# 3. 检索
results = await cognee.search("What does Cognee do?")
```

**内部数据流**：

```
add:     原始数据 → 解析/加载 → 关系库（数据集、权限、元数据）
           │
cognify: classify_documents → extract_chunks → extract_graph_from_data (LLM)
           │                                         │
           └→ summarize_text → add_data_points ──→ 图引擎写入节点/边
                                    │                  │
                                    └─────────────→ 向量引擎索引节点/边
           │
search:  SearchType 路由 → 向量/图/Cypher/LLM 检索 → 授权过滤 → 返回结果
```

**关键阶段说明**：

| 阶段 | 实现 | 作用 |
|------|------|------|
| `classify_documents` | 文档分类器 | 识别文档类型，决定后续处理策略 |
| `extract_chunks` | 分块器 | 将长文档切分为语义连贯的片段 |
| `extract_graph_from_data` | LLM 结构化抽取 | 从文本中提取实体和关系，构建 KnowledgeGraph |
| `summarize_text` | LLM 摘要 | 生成块级摘要，提升检索效率 |
| `add_data_points` | 统一写入 | 节点/边去重后同时写入图引擎和向量引擎 |

### 4.2 V2 Agent 记忆 API：remember / recall / improve / forget

面向 Agent 长期记忆场景的高级 API：

```python
# 永久记忆：等价于 add + cognify + improve
await cognee.remember("Pedro is CEO of Brex and prefers email.")

# 回忆：混合检索（会话缓存 + 图检索）
results = await cognee.recall("Who is Pedro?")

# 遗忘
await cognee.forget(data_id="...", dataset="...")
```

**remember 的两种模式**：

| 模式 | 触发条件 | 行为 |
|------|---------|------|
| 永久记忆 | 无 `session_id` | `add` → `cognify` → `improve`（triplet 索引等增强） |
| 会话记忆 | 有 `session_id` | 写入 session 缓存（关键词式 Q&A）→ 后台 `improve` 桥接到永久图 |

**recall 的检索策略**：

1. 若有 `session_id` 且未指定数据集：先对会话做 **词级重叠打分** 检索
2. 无命中时回落到图检索
3. `auto_route=True` 时用 **query_router** 自动选择最优 SearchType
4. 结果带 `_source: "session"` 或 `"graph"` 标记来源

**improve 的增强机制**：

- 带 `session_ids` 时：反馈权重 → 将会话 Q&A cognify 进图 → memify enrichment → 可选回写 session
- 无 `session_ids` 时：主要跑 triplet embedding 等 enrichment 任务

---

## 五、存储后端

### 5.1 向量存储

| 后端 | 说明 |
|------|------|
| **LanceDB** | 默认本地向量库 |
| **pgvector** | Postgres 扩展，可与图共用连接 |
| **ChromaDB** | 轻量级向量库 |
| **Neptune Analytics** | AWS 托管，支持向量+图混合 |

### 5.2 图存储

| 后端 | 说明 |
|------|------|
| **Kuzu** | 默认本地图库（文件级） |
| **Neo4j** | 企业级图数据库 |
| **Postgres** | 用关系表模拟图结构 |
| **Neptune / Neptune Analytics** | AWS 托管图服务 |

### 5.3 统一混合模式

设置 `USE_UNIFIED_PROVIDER=pghybrid` 时，用同一 Postgres 连接同时承载 pgvector 向量存储和 Postgres 图适配器，简化部署。

---

## 六、LLM 与 Embedding 支持

### LLM 提供者

通过 **LiteLLM + Instructor** 统一封装，支持结构化输出：

OpenAI / Anthropic / Gemini / Mistral / Azure / Bedrock / Ollama / llama_cpp / Custom

### Embedding 引擎

| 引擎 | 说明 |
|------|------|
| **fastembed** | 本地快速 embedding |
| **Ollama** | 本地模型 embedding |
| **OpenAI-compatible** | 任意兼容 OpenAI 接口的服务 |
| **LiteLLM** | 回退方案，覆盖多种云端 provider |

---

## 七、关键特性总结

| 特性 | 说明 |
|------|------|
| **知识图谱构建** | LLM 自动抽取实体/关系，支持本体约束（OWL/RDF） |
| **混合检索** | 向量相似度 + 图遍历 + Cypher + LLM 补全 |
| **会话记忆** | session 快速缓存 ↔ improve 持久化 ↔ 图回写 |
| **自我改进** | triplet embedding、反馈权重、enrichment 流水线 |
| **多租户隔离** | 用户/租户/数据集级别的 ACL 控制 |
| **可观测性** | OpenTelemetry、Langfuse、Sentry 集成 |
| **可扩展流水线** | Task + Pipeline 编排，可自定义 cognify 任务链 |
| **多部署模式** | 本地 / Docker / Cloud（Modal、Fly、Railway） |
| **Agent 集成** | Claude Code Plugin、MCP Server、REST API |
| **时序认知** | `temporal_cognify` 分支，支持事件/时间线图构建 |

---

## 八、技术栈

| 类别 | 技术 |
|------|------|
| **核心框架** | Python, FastAPI, SQLAlchemy, Pydantic |
| **LLM 集成** | LiteLLM, Instructor, OpenAI SDK |
| **向量存储** | LanceDB, pgvector, ChromaDB |
| **图存储** | Kuzu, Neo4j, Postgres, Neptune |
| **本体/知识** | rdflib, NetworkX |
| **文档处理** | pypdf, filetype, aiohttp |
| **可观测性** | OpenTelemetry, Langfuse, Sentry, structlog |
| **部署** | uvicorn, alembic, Docker, Modal/Fly/Railway |

---

## 九、与同类项目的差异化

| 维度 | Cognee | 纯 RAG 方案 | Mem0 | Graphiti |
|------|--------|------------|------|---------|
| **检索方式** | 向量 + 图 + Cypher | 仅向量 | 向量 + 规则 | 向量 + 时序图 |
| **知识结构** | 本体约束的知识图谱 | 无结构 | 扁平记忆 | 时序知识图 |
| **自我改进** | improve + memify | 无 | 有限 | 无 |
| **多租户** | 原生支持 | 无 | 有 | 无 |
| **会话桥接** | session ↔ 永久图 | 无 | 有 | 无 |
| **本地部署** | 完全支持 | 视实现 | 部分 | 完全支持 |
