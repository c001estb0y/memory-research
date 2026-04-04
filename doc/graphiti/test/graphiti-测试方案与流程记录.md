# Graphiti 知识图谱构建测试方案与流程记录

## 一、测试目标

使用 Graphiti 原生管线处理 CodeBuddy-Mem 导出的 `dpar_export.db` 中的 session_summary 数据，自动提取实体和关系，构建知识图谱，并生成交互式可视化 HTML。

验证 Graphiti 的核心能力：
- LLM 驱动的实体抽取（Entity Extraction）
- 跨 episode 的实体解析与合并（Entity Resolution）
- 事实三元组的时序管理（Temporal Facts）
- 知识图谱的可视化呈现

## 二、测试环境

| 组件 | 版本/配置 |
|------|----------|
| OS | macOS ARM64 (Darwin 23.5.0) |
| Python | 3.12.13（通过 Miniforge conda 环境） |
| graphiti-core | 0.28.2 |
| Kuzu（嵌入式图数据库） | 0.11.3 |
| sentence-transformers | 5.3.0 |
| PyTorch | 2.11.0 |
| openai SDK | 2.30.0 |

> **环境约束**：无 Docker、无 sudo、无 Homebrew，通过 Miniforge 创建独立 conda 环境 `graphiti` 解决依赖。

## 三、组件选型与适配

Graphiti 默认依赖 OpenAI 全家桶（GPT + OpenAI Embeddings + OpenAI Reranker）+ Neo4j/FalkorDB。本次测试环境受限，对三个核心组件进行了替换适配：

### 3.1 LLM：Venus API（Claude Sonnet 4.6）

| 配置项 | 值 |
|--------|---|
| API 端点 | `http://v2.open.venus.oa.com/llmproxy/chat/completions` |
| 模型 | `claude-sonnet-4-6` |
| 认证 | Bearer Token（Venus 格式） |

**适配问题**：Graphiti v0.28.2 的 `OpenAIClient` 使用 OpenAI 新版 Responses API（`/responses` 端点），Venus API 仅支持 `/chat/completions`。

**解决方案**：自定义 `VenusOpenAIClient`，继承 `OpenAIClient`，重写两个方法：
- `_create_structured_completion`：改用 `chat.completions.create` + `response_format={"type": "json_object"}`，将 Pydantic schema 注入到 system prompt 中引导 LLM 输出结构化 JSON
- `_handle_structured_response`：适配 `chat/completions` 的返回格式（`choices[0].message.content`）

### 3.2 Embedding：本地模型 BAAI/bge-small-zh-v1.5

| 配置项 | 值 |
|--------|---|
| 模型 | `BAAI/bge-small-zh-v1.5`（HuggingFace） |
| 框架 | sentence-transformers |
| 向量维度 | 512 |
| 运行方式 | 本地 CPU 推理 |

**选型理由**：
- Venus API 的 embedding 端点被网络安全策略拦截（403），无法使用远程 embedding
- `bge-small-zh-v1.5` 是专门针对中文优化的轻量级 embedding 模型，适合中文语料场景
- 模型体积小（~100MB），本地推理延迟低

**实现**：自定义 `LocalEmbedder` 类，实现 `EmbedderClient` 接口的 `create` 和 `create_batch` 方法。

### 3.3 Cross-Encoder（重排器）：本地模型 ms-marco-MiniLM-L-6-v2

| 配置项 | 值 |
|--------|---|
| 模型 | `cross-encoder/ms-marco-MiniLM-L-6-v2`（HuggingFace） |
| 框架 | sentence-transformers CrossEncoder |
| 运行方式 | 本地 CPU 推理 |

**选型理由**：Graphiti 默认使用 `OpenAIRerankerClient`，同样需要 OpenAI API。本地 cross-encoder 可完全离线运行。

**实现**：自定义 `LocalCrossEncoder` 类，实现 `CrossEncoderClient` 接口的 `rank` 方法。

### 3.4 图数据库：Kuzu（嵌入式）

| 配置项 | 值 |
|--------|---|
| 驱动 | `graphiti_core.driver.kuzu_driver.KuzuDriver` |
| 存储路径 | `./kuzu_db/`（本地文件，约 43MB） |
| 运行方式 | 嵌入式，无需独立服务 |

**选型历程**：
1. 首先尝试 FalkorDB Lite（嵌入式 Redis + FalkorDB），但其二进制为 Linux x86-64 架构，在 macOS ARM64 上运行失败（`Exec format error`）
2. 切换到 Kuzu 嵌入式图数据库，原生支持 macOS ARM64

**额外适配**：
- KuzuDriver 的 `build_indices_and_constraints()` 是空实现，需要手动创建全文索引（FTS Index）
- 手动执行 4 条 `CREATE_FTS_INDEX` 语句以支持 Graphiti 的全文检索

## 四、数据源

### 4.1 来源

`dpar_export.db`（SQLite），从 CodeBuddy-Mem 系统导出的记忆数据。

### 4.2 使用的数据

仅使用 `content_type = 'session_summary'` 的记录，跳过 observation（噪音大、空结果多）。

| 指标 | 值 |
|------|---|
| 原始记录数 | 9 条 |
| 去重后 | 8 条（hughes 3/25 有 1 条完全重复） |
| 涉及人员 | ziyadyao（5 条）、hughes（3 条） |
| 时间范围 | 2026-03-23 ~ 2026-04-03 |

详细内容见 [dpar-session-summaries.md](./dpar-session-summaries.md)。

## 五、测试流程

### 5.1 整体流程

```
dpar_export.db (SQLite)
    │
    ▼ load_summaries() — 读取 + 去重
8 条 session_summary
    │
    ▼ graphiti.add_episode() × 8
    │   ├── LLM 实体抽取 (Venus/Claude)
    │   ├── 实体解析 & 合并 (LLM + Embedding 相似度)
    │   ├── 事实/关系抽取 (LLM)
    │   └── 写入 Kuzu 图数据库
    │
    ▼ Kuzu Cypher 查询导出
    │   ├── Entity 节点 (uuid, name, summary, group_id)
    │   ├── RelatesToNode_ 中间节点 (name, fact, valid_at, invalid_at)
    │   └── Episodic 节点 (uuid, name, content, valid_at)
    │
    ▼ generate_html() — D3.js 力导向图可视化
dpar-knowledge-graph.html
```

### 5.2 执行命令

```bash
# 1. 创建 conda 环境
conda create -n graphiti python=3.12 -y
conda activate graphiti

# 2. 安装依赖
pip install graphiti-core kuzu sentence-transformers

# 3. 运行完整管线（灌入 + 导出 + 可视化）
python run_graphiti.py

# 4. 仅导出（数据已在 Kuzu 中时）
python run_graphiti.py --export-only
```

### 5.3 Graphiti add_episode 内部流程

每条 summary 作为一个 episode 灌入时，Graphiti 内部执行：

1. **extract_nodes** — LLM 从文本中提取实体列表（名称、类型、摘要）
2. **entity resolution** — 对提取的实体与已有图中的实体做匹配（embedding 相似度 + LLM 判断），决定是新建还是合并
3. **extract_edges** — LLM 从文本中提取实体间的关系/事实三元组
4. **edge deduplication** — 对新关系与已有关系做去重与合并
5. **persist** — 将节点和边写入 Kuzu 图数据库

## 六、测试结果

### 6.1 图谱规模

| 指标 | 数量 |
|------|------|
| Entity 节点 | 85 |
| 关系边（RelatesToNode_） | 102 |
| Episode 节点 | 8 |

### 6.2 节点类型分布

| 类型 | 数量 | 说明 |
|------|------|------|
| person | 2 | ziyadyao, hughes |
| tech | 19 | SVN, Unreal, Cook, Pak 等技术组件 |
| document | 15 | 脚本、配置文件、报告等 |
| concept | 49 | DPAR, UGC 玩法等概念/方案 |

### 6.3 关系类型 Top 10

| 关系 | 数量 |
|------|------|
| FIXED | 6 |
| GENERATED_AND_ARRANGED | 4 |
| CONTAINS | 3 |
| INCLUDED_IN | 3 |
| INVESTIGATED | 3 |
| ANALYZED | 2 |
| CONFIGURED | 2 |
| CONNECTED_TO | 2 |
| REVIEWED_DECISIONS_FOR | 2 |
| WORKED_ON | 2 |

### 6.4 跨人员实体融合验证

ziyadyao 和 hughes 之间有 **3 个共享实体**：

| 共享实体 | ziyadyao 的视角 | hughes 的视角 |
|---------|----------------|--------------|
| **DPAR** | 排查打包流程、对比构建逻辑 | 完成流水线转换、理解完整构建流程 |
| **SVN** | 资产引用修复脚本、路径修复提交 | 认证配置、路径不匹配修复 |
| **UGC 玩法** | 排查本地 Cook 后无法进入的问题 | 分析地图崩溃问题（蓝图与 C++ 类迁移兼容性） |

这验证了 Graphiti 的核心能力：不同人、不同时间提到的同一实体，被自动识别并合并，各自的事实信息汇聚到同一节点的 summary 中。

### 6.5 人员连接度

| 人员 | 直接连接实体数 |
|------|-------------|
| ziyadyao | 29 |
| hughes | 17 |

## 七、产物清单

| 文件 | 说明 |
|------|------|
| `run_graphiti.py` | Graphiti 管线主脚本（灌入 + 导出 + 可视化） |
| `build_graph.py` | 早期自定义脚本（已弃用，仅保留参考） |
| `kuzu_db/` | Kuzu 嵌入式图数据库（43MB） |
| `graphiti_graph_data.json` | Graphiti 导出的图数据 JSON（70KB） |
| `graph_data.json` | 早期脚本生成的图数据（已弃用） |
| `dpar-knowledge-graph.html` | 交互式力导向图可视化（D3.js） |
| `dpar-session-summaries.md` | 原始 session summary 数据整理 |

## 八、遇到的问题与解决

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| FalkorDB Lite 启动失败 | Linux x86-64 二进制 vs macOS ARM64 | 切换到 Kuzu 嵌入式图数据库 |
| Venus embedding API 403 | 网络安全策略拦截 | 使用本地 `bge-small-zh-v1.5` 模型 |
| OpenAI Responses API 404 | Venus 不支持 `/responses` 端点 | 自定义 `VenusOpenAIClient`，回退到 `/chat/completions` |
| Claude 不遵守 Pydantic schema | `beta.chat.completions.parse` 不兼容 | 将 JSON schema 注入 system prompt + `json_object` 格式约束 |
| Kuzu 全文索引缺失 | KuzuDriver 的 `build_indices_and_constraints` 是空实现 | 手动执行 `CREATE_FTS_INDEX` 创建 4 个索引 |
| KuzuDriver 缺少 `_database` 属性 | 基类声明了但 Kuzu 未初始化 | 手动设置 `driver._database = "default"` |
