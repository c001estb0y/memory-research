# 公共踩坑记忆库 — Memory 方案选型分析

> 从 SourceMem 蒸馏的结构化工程经验，需要一个存储 + 实时召回方案，在 AI 助手对话中注入历史踩坑经验。
> 本文档对 memory-research 仓库中的 11 个 memory 方案逐一分析，给出推荐和不推荐的理由。

## 1. 需求画像


| 维度       | 结论                                                              |
| -------- | --------------------------------------------------------------- |
| **数据流**  | SourceMem DB → 蒸馏管线（已有 `doc/mem-distillation/`）→ 结构化经验存储 + 实时召回 |
| **消费方式** | 实时注入 AI 助手对话上下文（不需要生成可读文档）                                      |
| **写入来源** | 蒸馏管线产出的 `EngineeringExperience` JSON，批量导入                       |
| **数据规模** | 几千条经验，每周/每天自动追加（中等规模 + 中频更新）                                    |
| **隔离粒度** | 按项目隔离（DPAR、Shadow-Folk、Tank 等各自独立）                              |
| **消费端**  | Cursor、CodeBuddy、Claude Code 等多客户端，需通用 MCP/API 接口               |


### 蒸馏管线已有的数据模型

蒸馏管线（`doc/mem-distillation/memory-distillation-design.md`）产出的核心结构为 `EngineeringExperience`，包含以下关键字段：

```
issue_context / root_cause / solution / rationale     — 经验核心四要素
experience_type                                        — 7 种类型枚举
scope (architecture / engineering / environment)       — 适用范围分级
environment_conditions                                 — 环境级经验的触发条件
trigger_patterns                                       — 泛化触发模式（用于扩大匹配范围）
related_components                                     — 关联模块/技术栈
confidence / recall_score / hit_count / miss_count     — 置信度与反馈闭环字段
```

选型的核心约束：**存储方案必须能原生或低成本地承载这个数据模型**。

---

## 2. 仓库版本快照

分析基于以下版本（2026-04-11 全部更新到最新）：


| 方案            | 版本         | 分支                   | 最新 Commit  |
| ------------- | ---------- | -------------------- | ---------- |
| claude-mem    | v12.1.0    | main                 | 2026-04-08 |
| ClawXMemory   | v0.1.5     | main                 | 2026-04-03 |
| codebuddy-mem | —          | feat/ux-optimization | 2026-04-01 |
| graphiti      | —          | main                 | 2026-03-31 |
| mem0          | v1.0.8+    | main                 | 2026-04-11 |
| MemOS         | v2.0.13    | main                 | 2026-04-10 |
| mempalace     | v3.1.0     | main                 | 2026-04-10 |
| memU          | v1.5.1     | main                 | 2026-03-23 |
| openclaw      | v2026.4.10 | main                 | 2026-04-11 |
| OpenViking    | v0.3.5     | main                 | 2026-04-11 |
| supermemory   | —          | main                 | 2026-04-10 |


---

## 3. 逐方案分析

### 3.1 claude-mem — 不推荐

**定位**：Claude Code 专属的持久化记忆压缩系统。

**存储后端**：SQLite（会话/观察/摘要）+ ChromaDB（语义索引）+ FTS5（全文检索）

**架构特点**：

- 独立观察者 Agent 在后台监听 Claude Code 会话，自动提取结构化 observation（XML 格式）
- 按 `project` 字段隔离，每个工作区独立记忆
- 设计重点是会话压缩（高压缩比保留关键信息），而非知识库管理

**局限性**：

1. **强绑定 Claude Code 生命周期** — 观察者 Agent 作为 Claude Code 的子进程运行，依赖 Claude Code 的 hook 系统触发，无法独立于 Claude Code 使用
2. **无通用 API/MCP 接口** — 没有暴露 HTTP API 或 MCP server，Cursor、CodeBuddy 无法直接接入
3. **单用户单机设计** — 没有多用户、多项目的共享概念，`project` 字段仅做本地隔离
4. **写入模型不匹配** — 其 observation 格式（XML 结构化，面向会话压缩）与蒸馏管线的 `EngineeringExperience`（面向工程经验）数据模型差异大，适配成本高
5. **检索设计面向"回忆上次做了什么"** — 而非"匹配当前问题的历史经验"，检索逻辑不适合踩坑经验召回

**结论**：定位为 Claude Code 的个人记忆压缩器，不是通用知识库方案。

---

### 3.2 codebuddy-mem — 不推荐

**定位**：为 CodeBuddy Agent 和 Cursor 提供跨会话持久化记忆的插件系统。

**存储后端**：SQLite + 文件系统，按 workspace 隔离

**架构特点**：

- 自动记录 Shell 命令、MCP 调用、文件编辑等操作
- AI 压缩为结构化摘要
- 新会话自动注入历史关键信息
- 提供 Web 可视化界面

**局限性**：

1. **绑定 CodeBuddy / Cursor 插件生态** — 作为特定 IDE 的插件运行，无法被 Claude Code 等其他客户端使用
2. **面向操作记录而非知识沉淀** — 记录的是"做了什么操作"（Shell 命令、文件编辑），不是"学到了什么经验"
3. **无跨项目/跨用户共享设计** — 每个 workspace 独立，无法构建公共知识库
4. **无标准 API** — 没有独立的 HTTP API 或 MCP server 供外部调用
5. **数据模型不匹配** — 操作日志压缩模型与 `EngineeringExperience` 的结构化经验模型差异大

**结论**：定位为 IDE 操作记忆插件，适合"回忆上次编辑了哪些文件"，不适合"检索历史踩坑经验"。

---

### 3.3 mem0 — 不推荐

**定位**：通用 AI 记忆层，提供 `add / search / update / delete` 的 CRUD API。

**存储后端**：可插拔向量库（Qdrant / Chroma / Pinecone 等）+ 可选图库（Neo4j）+ SQLite 审计历史

**架构特点**：

- 原生多租户：`user_id`、`agent_id`、`run_id` 作为 payload 字段参与过滤
- LLM 驱动的记忆提取：从对话中自动抽取"原子 fact"
- 支持向量检索 + 可选图检索

**局限性**：

1. **记忆提取的"原子 fact"粒度过细** — mem0 将对话分解为极细粒度的三元组（如 "user prefers Postgres"），丢失了上下文和排查过程。蒸馏管线已经产出了结构化的 `EngineeringExperience`，再经过 mem0 的 LLM 提取会**二次损耗信息**
2. **垃圾记忆率高** — 此前在本仓库的测试中已确认（见 `doc/mem0/mem0-社区评价与局限性分析.md`），mem0 的 LLM 提取会产出大量低价值记忆，需要额外清洗
3. **图记忆设计缺陷** — 三元组语义表达弱于 Graphiti 的时序图模型，关系建模能力不足
4. **无"公共只读知识库"概念** — 多租户靠字段过滤实现，没有"所有人可读的共享经验池"的一等公民设计
5. **需要外部向量库** — Qdrant 或 Pinecone 等，增加部署复杂度
6. **蒸馏经验的 metadata 丰富度受限** — mem0 的 payload 字段设计偏简单（hash、时间、scope），不如 ChromaDB metadata 灵活，`trigger_patterns`（列表类型）等复杂字段难以原生支持
7. **LLM 调用成本** — 每次 `add` 都会触发 LLM 提取，但蒸馏管线已经完成了提取，这一步是冗余的

**结论**：mem0 适合"从原始对话中提取记忆"的场景，但我们的蒸馏管线已经完成了这一步。用 mem0 存储已蒸馏的经验属于"用大炮打蚊子"——LLM 提取层是冗余开销，且会引入二次信息损耗。

---

### 3.4 graphiti — 不推荐

**定位**：构建 AI Agent 的时序上下文图（Temporal Context Graph）。

**存储后端**：图数据库（Neo4j / FalkorDB / Kuzu）必选，向量存储在图/边属性上

**架构特点**：

- Episodic / Entity / Community 等节点类型
- EntityEdge 上存自然语言 fact + 时间窗（valid_at / invalid_at / expired_at）
- `group_id` 分区检索
- BM25 + 向量混合检索（RRF）+ cross-encoder 重排
- 时序建模：事实可被软作废，保留历史版本

**局限性**：

1. **图数据库是硬依赖** — Neo4j / FalkorDB 必选，部署和运维成本高。对于"存储几千条扁平经验条目"的需求来说过重
2. **蒸馏经验本质上是扁平条目，不需要图遍历** — 每条 `EngineeringExperience` 是独立的、自包含的经验记录，没有实体间的复杂关系链需要图遍历来发现。经验之间的关联（同项目、同类型）通过 metadata 过滤即可解决
3. **时序建模能力对本场景价值有限** — Graphiti 的强项是"事实随时间演化"（如"用户从 Postgres 切换到 MySQL"），但踩坑经验一旦蒸馏完成就是稳定的，不需要 valid_at/invalid_at 时序窗口
4. **写入适配复杂** — 需要将 `EngineeringExperience` 转换为 Episode + Entity + Edge 的图结构，映射关系不自然
5. **检索路径过重** — BFS 图遍历 + cross-encoder 重排对于"按项目过滤 + 语义相似度匹配"的简单需求来说是过度设计
6. **本仓库 fork 为自建 fork** — remote 指向 `c001estb0y/graphiti` 而非上游 `getzep/graphiti`，后续更新维护需自行同步

**结论**：Graphiti 是最强的时序图记忆方案，但对于"按项目隔离的扁平经验条目存储 + 语义检索"这个需求来说，图数据库是过度设计，部署成本与收益不成比例。

---

### 3.5 MemOS — 不推荐

**定位**：记忆操作系统（Memory Operating System），统一管理明文/KV/LoRA/偏好等多种记忆类型。

**存储后端**：Neo4j 类图库 + 向量库 + Pickle（激活记忆）；LoRA 适配器占位

**架构特点**：

- MemCube 容器聚合多种记忆类型（Working / LongTerm / Skill 等九种）
- 图游走 + 向量 + BM25 多路并行检索 + rerank
- 多 Cube 并行搜索、同 cube 跨任务 Skill 共享
- 支持记忆的合并、遗忘、激活等生命周期操作

**局限性**：

1. **概念体系过于庞大** — 九种 memory_type（Working / Episodic / Semantic / Procedural / Skill / Parametric / LoRA / Preference / Profile）、MemCube、MemThread、MemAPI 等，学习曲线陡峭。我们只需要一种记忆类型："蒸馏后的工程经验"
2. **部署最重** — Neo4j + 向量库 + Python 服务，是所有方案中部署复杂度最高的
3. **面向"AI Agent 认知架构"而非"知识库"** — MemOS 试图模拟人类记忆系统（工作记忆、长期记忆、程序性记忆等），这个抽象层对于"存取踩坑经验"来说完全不必要
4. **非传统 namespace/ACL 权限模型** — 强调 MemCube 组合和类型系统，而非项目级隔离。按项目隔离需要创建多个 MemCube 并自行管理映射关系
5. **LoRA 适配器等功能与需求无关** — 大量设计用于"将记忆注入模型参数"的功能，对于"检索经验注入 prompt"的场景没有价值
6. **MCP 接口不成熟** — 文档提到 OpenClaw 本地插件集成（`dev-20260407-v2.0.13` 分支），但作为独立 MCP server 供多客户端使用的能力未稳定

**结论**：MemOS 是一个学术前沿的记忆操作系统原型，概念宏大但对本需求而言过度复杂。用它来存几千条踩坑经验，就像用操作系统内核来管理一个 TODO 列表。

---

### 3.6 memU — 不推荐

**定位**：个人 AI 记忆助手，支持多模态记忆管理。

**存储后端**：SQLite + Rust 核心（Cargo.toml）+ Python 服务层

**架构特点**：

- Rust + Python 混合栈，Rust 处理核心数据结构和性能敏感路径
- 支持文本、图片等多模态记忆
- 个人向设计，强调隐私和本地存储

**局限性**：

1. **Rust + Python 混合技术栈编译复杂** — 需要 Rust 工具链（Cargo）来编译核心组件，部署门槛高于纯 Python 或纯 Node.js 方案
2. **个人记忆助手定位** — 设计面向单个用户的个人记忆管理，无多项目/多用户共享概念
3. **无 MCP server 实现** — 没有标准化的外部接口供 AI 助手接入
4. **社区活跃度较低** — 最新版本 v1.5.1 停留在 2026-03-23，近期无更新
5. **多模态能力与需求不匹配** — 踩坑经验是纯文本结构化数据，不需要图片/音频等多模态支持
6. **文档可读性问题** — README 文件存在编码/权限问题（测试中读取被拒绝），说明项目维护状态一般

**结论**：偏个人向的多模态记忆助手，技术栈重且与需求场景不匹配。

---

### 3.7 openclaw — 不推荐（但值得关注）

**定位**：个人 AI 助手，以 Markdown 为 SSOT（Single Source of Truth）的记忆系统。

**存储后端**：工作区 Markdown 文件 + 每 Agent SQLite（sqlite-vec + FTS5）；可选 QMD sidecar

**架构特点**：

- "Markdown 即记忆"：`MEMORY.md`、`memory/YYYY-MM-DD.md` 为记忆的原始存储
- memory-core 索引层：对 Markdown 做 chunk 切分 → sqlite-vec 向量索引 + FTS5 全文索引
- FallbackMemoryManager：QMD 优先，SQLite 回退
- BM25 + 向量混合检索
- 每 Agent 独立索引

**局限性**：

1. **Markdown 为 SSOT 的设计与结构化经验不匹配** — 蒸馏管线产出的是高度结构化的 JSON（带 confidence、trigger_patterns、scope 等字段），如果转为 Markdown 再被 openclaw 重新 chunk + 索引，会**丢失结构化 metadata**，检索时无法按 scope/experience_type 精确过滤
2. **每 Agent 独立索引，无跨 Agent 共享** — 公共踩坑经验需要被多个 Agent 共同访问，openclaw 的设计是每个 Agent 各管各的
3. **检索粒度为 Markdown 行块（~400 tokens）** — 与蒸馏经验的"一条完整的 EngineeringExperience"粒度不匹配，可能把一条经验拆成多个 chunk 导致召回不完整
4. **部署虽轻但架构复杂** — Markdown + SQLite + FTS5 + sqlite-vec + 可选 QMD，多层 fallback 机制增加了理解和调试成本

**值得关注的原因**：openclaw 的 BM25 + 向量混合检索 + FTS5 的本地方案设计精巧，如果未来需要更强的全文检索能力（比如按错误信息关键词精确匹配），其 FTS5 方案值得借鉴。

**结论**：Markdown-first 的设计理念与结构化经验存储场景不兼容，强行使用会丢失蒸馏管线精心设计的 metadata 体系。

---

### 3.8 supermemory — 不推荐

**定位**：State-of-the-art 的记忆与上下文引擎，面向 AI 应用开发者。

**存储后端**：托管服务（HNSW 向量 + 关系图），核心引擎闭源

**架构特点**：

- Space / Org / User 多层隔离
- 文档摄取 → chunk → 向量化 + 关系图构建
- MemoryEntry 事实级写入
- `/v4/search`（memories + hybrid）、`/v4/profile` 画像接口
- 版本链、遗忘字段、画像 static/dynamic

**局限性**：

1. **核心引擎闭源，自建不完整** — supermemory 的完整能力（混合检索、关系图、画像系统）依赖托管服务，自建只能使用开源的子集
2. **依赖外部 SaaS 服务** — 数据需要上传到 supermemory 的服务端，对于内部项目的踩坑经验可能有数据安全/合规顾虑
3. **API 变化频繁** — 从 v3 到 v4 经历了较大的 API 重构（`/v3/documents` → `/v4/memories`），集成后可能需要频繁适配
4. **成本不可控** — 托管服务按用量计费，持续写入和检索的成本随数据增长线性增加
5. **离线不可用** — 依赖网络连接，无法在断网环境下使用

**结论**：supermemory 是一个优秀的托管记忆服务，但核心引擎闭源 + 外部依赖使其不适合作为内部知识库的基座。

---

### 3.9 OpenViking — 候选方案（推荐度：★★★★☆）

**定位**：面向 AI Agent 的开源上下文数据库，火山引擎出品。

**存储后端**：AGFS（Agent File System，内容 SSOT）+ HNSW 向量索引（URI + 向量 + 元数据）

**架构特点**：

- 文件系统范式：`viking://{scope}/{path}` 统一管理记忆/资源/技能
- L0/L1/L2 三层分级：`.abstract.md` / `.overview.md` / 原文，按 token 预算按需加载
- 目录递归检索 + 分数传播 + 可选 rerank
- 可视化检索轨迹，便于调试
- Session 管理：`viking://session/{id}/`
- Python + Go + Rust 多语言实现

**适合本需求的方面**：

- URI 体系天然支持项目隔离：`viking://user/dpar/experiences/`
- L0/L1/L2 分层可以为经验建立摘要层，先粗筛再精搜
- 检索轨迹可视化对调试召回质量很有帮助
- HTTP API 可供多客户端调用
- 社区活跃（v0.3.5，火山引擎持续维护）

**局限性**：

1. **部署复杂度最高（候选方案中）** — 需要 Python 3.10+ / Go 1.22+ / GCC 9+ 或 Clang 11+，三种语言的编译环境。在 Windows 上配置 Go + C++ 编译链尤其麻烦
2. **AGFS 概念偏重** — 文件系统范式对于"存取结构化 JSON 条目"来说有些 over-abstraction，需要把每条经验映射为一个"文件"
3. **与蒸馏管线的数据模型有 gap** — OpenViking 的记忆条目格式（URI + level + context_type）与 `EngineeringExperience` 的丰富字段（trigger_patterns、recall_score 等）需要写适配层
4. **VLM/Embedding 模型依赖** — 需要配置 VLM 提供者（OpenAI/Azure/本地）和 Embedding 模型，增加外部依赖
5. **面向"Agent 运行时上下文管理"而非"知识库"** — 其 Session 管理、自动压缩等功能是为 Agent 实时运行设计的，静态知识库只是其能力的一个子集

**结论**：检索能力最强的候选方案（分层 + 目录递归 + 可视化），但部署成本高，适合对检索质量有极致要求且有 Linux 服务器环境的场景。

---

### 3.10 ClawXMemory — 候选方案（推荐度：★★★☆☆）

**定位**：多层级记忆系统，用于 AI 长期上下文建模，OpenBMB 出品。

**存储后端**：SQLite（层级记录），纯本地

**架构特点**：

- 五层记忆：L0 原始会话 → L1 话题窗口摘要 → L2 时间/项目索引 → GlobalProfile 单例
- LLM 驱动的多跳推理检索（L2→L1→L0 逐级下钻）
- 后台自动 indexing 和 dreaming（记忆整理）
- MCP server 原生支持（OpenClaw 插件生态）
- 可视化 dashboard（canvas + list 视图）

**适合本需求的方面**：

- 部署轻量：纯 SQLite，npm 包即装即用
- MCP 原生支持，可被 Cursor / Claude Code 接入
- L2 项目索引可自动聚合同项目经验
- 可视化 dashboard 便于浏览和管理

**局限性**：

1. **检索依赖 LLM 调用** — 每次召回都触发 LLM 多跳推理（L2→L1→L0），意味着每次检索都有 token 成本和延迟（数秒级）。对于 DPAR 这种场景，一次对话可能触发多次经验检索，累计成本可观
2. **面向"个人+单插件"设计，非共享知识库** — 文档中无跨用户共享或公共知识库的专门设计，`sessionKey` / `projectKey` 是标签级隔离，不是真正的命名空间
3. **GlobalProfile 是单例用户画像，不是公共经验池** — GlobalProfile 面向"这个用户喜欢什么"，不是"这个项目踩过什么坑"
4. **自动 dreaming 可能干扰蒸馏经验** — ClawXMemory 后台会自动整理和聚合记忆（"dreaming"），可能会把已经精心蒸馏好的结构化经验重新改写或合并，破坏原始数据的完整性
5. **项目较新（v0.1.5）** — 2026-04-03 发布，生态和文档尚在早期，生产稳定性未经充分验证
6. **数据模型适配需要工作量** — `EngineeringExperience` 的字段（trigger_patterns、recall_score、environment_conditions 等）需要映射到 ClawXMemory 的 Memory Fragment 格式，部分复杂字段（如列表类型的 trigger_patterns）可能需要序列化为字符串

**结论**：LLM 驱动推理检索是亮点也是最大短板——质量可能更高但每次都有成本。适合数据量小、检索频率低、对推理深度有要求的场景，不太适合高频、低延迟的实时召回。

---

### 3.11 MemPalace — 推荐方案（推荐度：★★★★★）

**定位**：基于空间隐喻（Wing / Room / Drawer）的 AI 记忆系统，LongMemEval 96.6% R@5。

**存储后端**：ChromaDB（向量 + 元数据）+ SQLite（轻量知识图谱：entities / triples）

**架构特点**：

- 空间隐喻层级：Wing（域/人/项目）→ Room（主题）→ Drawer（单条记忆 chunk）
- Tunnel（隧道）：同一 Room 出现在多个 Wing 时形成跨域桥接
- 四级检索：L0 预载 → L1 元数据过滤 → L2 语义向量搜索 → Palace Graph BFS
- 纯本地部署：ChromaDB + SQLite，零外部 LLM 依赖
- MCP server 原生支持

**适合本需求的方面**：

1. **Wing/Room 结构天然匹配项目/经验类型隔离**
  - `dpar` wing → debugging room / configuration room / deployment room
  - `shadowfolk` wing → architecture room / tooling room
  - 无需额外设计隔离机制
2. **与蒸馏管线数据模型适配成本最低**
  - ChromaDB metadata 支持任意 KV，`EngineeringExperience` 的所有字段可 1:1 映射：
    - `experience_type` → room 名称
    - `scope` / `confidence` / `recall_score` → metadata 过滤字段
    - `trigger_patterns` → 可序列化为 JSON 字符串存入 metadata
    - `issue_context` + `solution` → 文档主体文本（用于向量化）
  - 不需要格式转换或信息损耗
3. **检索零成本零延迟**
  - 纯向量搜索 + 元数据过滤，不消耗 LLM token
  - 千级数据量下 ChromaDB 检索延迟在毫秒级
4. **部署最轻**
  - `pip install mempalace` 即可，零外部服务依赖
  - ChromaDB 嵌入式运行，SQLite 本地文件
  - Windows / macOS / Linux 均可运行
5. **MCP 原生支持多客户端**
  - Cursor：`mcp add mempalace`
  - Claude Code：同上
  - CodeBuddy 等：通过包装轻量 HTTP API 接入
6. **recall_score 反馈机制可直接落地**
  - 蒸馏管线设计的 `recall_score` / `hit_count` / `miss_count` / `last_hit_at` 可作为 ChromaDB metadata 存储
  - 每次召回后更新 metadata 即可，无需额外存储层

**局限性（必须说明）**：

1. **ChromaDB 在万级以上规模未经验证** — 当前 LongMemEval benchmark 是 500 题级别，千级经验条目应该没问题，但如果未来扩展到万级需要关注检索性能
2. **单机部署，无多人并发写入** — ChromaDB 嵌入式模式不支持多进程同时写入，如果多个蒸馏管线实例并发写入同一个库需要加锁或改用 ChromaDB client-server 模式
3. **轻量 KG（SQLite entities/triples）能力有限** — 相比 Neo4j 图遍历能力弱，但本需求不需要图遍历
4. **AAAK 压缩模式有争议** — v3.0.0 README 中承认 AAAK 压缩会导致 R@5 从 96.6% 降到 84.2%，但我们使用 raw mode 即可，不受影响
5. **社区较新（2026-04 才到 v3.x）** — 项目起步较晚，生态深度不如 mem0 / graphiti，但更新活跃（几乎每天有 commit）
6. **无内置 HTTP API** — MCP 是原生的，但如果 CodeBuddy 等客户端需要 HTTP API 调用，需要自行包装一层轻量 HTTP server

---

## 4. 横向对比总表


| 维度              | claude-mem | codebuddy-mem | mem0          | graphiti | MemOS   | memU | openclaw     | supermemory | OpenViking | ClawXMemory | **MemPalace** |
| --------------- | ---------- | ------------- | ------------- | -------- | ------- | ---- | ------------ | ----------- | ---------- | ----------- | ------------- |
| **部署复杂度**       | 中          | 低             | 中高            | 高        | 高       | 中高   | 低            | 外部SaaS      | 高          | 低           | **低**         |
| **项目隔离**        | project字段  | workspace     | user/agent_id | group_id | MemCube | —    | 每Agent独立     | Space/Org   | URI原生      | 标签          | **Wing原生**    |
| **与蒸馏管线适配**     | 差          | 差             | 差(二次损耗)       | 中(需转图)   | 差       | 差    | 差(丢metadata) | 中           | 中(需转格式)    | 中(需转格式)     | **好(1:1映射)**  |
| **检索延迟**        | 快          | 快             | 快             | 快        | 快       | 快    | 快            | 快           | 快          | 慢(LLM)      | **快**         |
| **检索成本**        | 零          | 零             | 零             | 零        | 零       | 零    | 零            | 按量付费        | 零          | 每次LLM       | **零**         |
| **MCP原生**       | ✗          | ✗             | ✗             | ✗        | ✗       | ✗    | ✗            | ✗           | 需包装        | ✓           | **✓**         |
| **多客户端通用**      | ✗          | ✗             | HTTP API      | HTTP API | ✗       | ✗    | ✗            | HTTP API    | HTTP API   | 需包装HTTP     | **需包装HTTP**   |
| **metadata丰富度** | 中          | 低             | 低             | 高(时序)    | 高       | 低    | 低(chunk级)    | 高           | 中          | 中           | **高(任意KV)**   |
| **社区活跃度**       | 活跃         | 低             | 活跃            | 活跃       | 活跃      | 低    | 活跃           | 活跃          | 活跃         | 较新          | **活跃**        |


---

## 5. 最终推荐

**推荐方案：MemPalace**

核心理由排序：

1. **适配成本最低** — 蒸馏管线的 `EngineeringExperience` 字段可 1:1 映射为 ChromaDB metadata，不需要格式转换、不会信息损耗
2. **Wing/Room 天然对应项目/经验类型** — 零额外设计的隔离机制
3. **部署最轻** — `pip install`，零外部依赖，Windows/macOS/Linux 均可
4. **检索零成本零延迟** — 纯向量 + 元数据，不消耗 LLM token
5. **MCP 原生** — Cursor / Claude Code 开箱即用
6. **反馈闭环可直接落地** — recall_score 机制在 ChromaDB metadata 上即可实现

**需要额外实现的部分**：

- Load 适配层：蒸馏管线 JSON → MemPalace `palace.store()` 调用
- 轻量 HTTP API 包装（如果需要支持非 MCP 客户端）
- recall_score 反馈更新逻辑

**备选方案**：如果后续对检索质量有更高要求（分层检索 + 目录递归），可考虑 OpenViking，但需要接受更高的部署成本。

---

> 文档生成时间：2026-04-11
> 基于 memory-research 仓库 11 个方案源码分析 + 蒸馏管线 v1 设计文档

