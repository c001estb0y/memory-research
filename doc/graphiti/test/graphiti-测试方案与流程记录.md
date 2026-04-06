# Graphiti 知识图谱构建测试方案与流程记录

## 一、测试目标

使用 Graphiti 原生管线处理 CodeBuddy-Mem 导出的 session_summary 数据，自动提取实体和关系，构建知识图谱，并生成交互式可视化 HTML。

验证 Graphiti 的核心能力：
- LLM 驱动的实体抽取（Entity Extraction）
- 跨 episode 的实体解析与合并（Entity Resolution）
- 事实三元组的时序管理（Temporal Facts）
- 知识图谱的可视化呈现

## 二、通用测试环境

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

### 通用组件适配

Graphiti 默认依赖 OpenAI 全家桶（GPT + OpenAI Embeddings + OpenAI Reranker）+ Neo4j/FalkorDB。本次测试环境受限，对以下组件进行了统一替换，两轮测试共用：

#### Embedding：本地模型 BAAI/bge-small-zh-v1.5

| 配置项 | 值 |
|--------|---|
| 模型 | `BAAI/bge-small-zh-v1.5`（HuggingFace） |
| 框架 | sentence-transformers |
| 向量维度 | 512 |
| 运行方式 | 本地 CPU 推理 |

选型理由：Venus API 的 embedding 端点被网络安全策略拦截（403）；`bge-small-zh-v1.5` 针对中文优化，体积小（~100MB），本地推理延迟低。

#### Cross-Encoder（重排器）：本地模型 ms-marco-MiniLM-L-6-v2

| 配置项 | 值 |
|--------|---|
| 模型 | `cross-encoder/ms-marco-MiniLM-L-6-v2`（HuggingFace） |
| 框架 | sentence-transformers CrossEncoder |
| 运行方式 | 本地 CPU 推理 |

#### 图数据库：Kuzu（嵌入式）

| 配置项 | 值 |
|--------|---|
| 驱动 | `graphiti_core.driver.kuzu_driver.KuzuDriver` |
| 存储 | 本地文件（嵌入式，无需独立服务） |

首先尝试 FalkorDB Lite，因 Linux x86-64 二进制与 macOS ARM64 不兼容而放弃，切换到 Kuzu。额外需手动创建 4 条 `CREATE_FTS_INDEX` 语句。

---

## 三、第一轮测试：DPAR 项目

### 3.1 数据源

`dpar_export.db`（SQLite），从 CodeBuddy-Mem 系统导出的记忆数据。仅使用 `content_type = 'session_summary'` 的记录。

| 指标 | 值 |
|------|---|
| 原始记录数 | 9 条 |
| 去重后 | 8 条（hughes 3/25 有 1 条完全重复） |
| 涉及人员 | ziyadyao（5 条）、hughes（3 条） |
| 时间范围 | 2026-03-23 ~ 2026-04-03 |

详细内容见 [dpar-session-summaries.md](./dpar-session-summaries.md)。

### 3.2 LLM 配置

| 配置项 | 值 |
|--------|---|
| API 服务 | Venus API |
| API 端点 | `http://v2.open.venus.oa.com/llmproxy/chat/completions` |
| 模型 | `claude-sonnet-4-6`（medium 和 small 均使用同一模型） |
| 认证 | Bearer Token（Venus 格式） |

**适配问题**：Graphiti v0.28.2 的 `OpenAIClient` 使用 OpenAI 新版 Responses API（`/responses` 端点），Venus API 仅支持 `/chat/completions`。

**解决方案**：自定义 `VenusOpenAIClient`，继承 `OpenAIClient`，重写：
- `_create_structured_completion`：改用 `chat.completions.create` + `response_format={"type": "json_object"}`，将 Pydantic schema 注入 system prompt
- `_handle_structured_response`：适配 `choices[0].message.content` 返回格式

### 3.3 执行流程

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

```bash
# 运行完整管线
python run_graphiti.py

# 仅导出（数据已在 Kuzu 中时）
python run_graphiti.py --export-only
```

### 3.4 测试结果

#### 图谱规模

| 指标 | 数量 |
|------|------|
| Entity 节点 | 85 |
| 关系边（RelatesToNode_） | 102 |
| Episode 节点 | 8 |
| 处理耗时 | ~10 分钟 |
| 成功率 | 100%（8/8） |

#### 节点类型分布

| 类型 | 数量 | 说明 |
|------|------|------|
| person | 2 | ziyadyao, hughes |
| tech | 19 | SVN, Unreal, Cook, Pak 等技术组件 |
| document | 15 | 脚本、配置文件、报告等 |
| concept | 49 | DPAR, UGC 玩法等概念/方案 |

#### 关系类型 Top 10

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

#### 跨人员实体融合验证

ziyadyao 和 hughes 之间有 **3 个共享实体**：

| 共享实体 | ziyadyao 的视角 | hughes 的视角 |
|---------|----------------|--------------|
| **DPAR** | 排查打包流程、对比构建逻辑 | 完成流水线转换、理解完整构建流程 |
| **SVN** | 资产引用修复脚本、路径修复提交 | 认证配置、路径不匹配修复 |
| **UGC 玩法** | 排查本地 Cook 后无法进入的问题 | 分析地图崩溃问题（蓝图与 C++ 类迁移兼容性） |

这验证了 Graphiti 的核心能力：不同人、不同时间提到的同一实体，被自动识别并合并，各自的事实信息汇聚到同一节点的 summary 中。

#### 人员连接度

| 人员 | 直接连接实体数 |
|------|-------------|
| ziyadyao | 29 |
| hughes | 17 |

### 3.5 遇到的问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| FalkorDB Lite 启动失败 | Linux x86-64 二进制 vs macOS ARM64 | 切换到 Kuzu |
| Venus embedding API 403 | 网络安全策略拦截 | 本地 `bge-small-zh-v1.5` |
| OpenAI Responses API 404 | Venus 不支持 `/responses` | 自定义 `VenusOpenAIClient` |
| Claude JSON 输出不规范 | 返回 markdown 包裹或截断 | `_parse_json_robust` 多策略解析 |
| Kuzu 全文索引缺失 | KuzuDriver 的 `build_indices_and_constraints` 是空实现 | 手动 `CREATE_FTS_INDEX` |
| LLM 输出截断 | Venus API 默认 max_tokens 过低 | 强制 `max_tokens=8192` + `finish_reason` 检查 |
| Venus API 限流（429） | 高频调用超出公共服务限额 | 指数退避重试 + 每条间隔 2s |

### 3.6 产物清单

| 文件 | 说明 |
|------|------|
| `run_graphiti.py` | Graphiti 管线主脚本（灌入 + 导出 + 可视化） |
| `kuzu_db/` | Kuzu 嵌入式图数据库（43MB） |
| `graphiti_graph_data.json` | 导出的图数据 JSON（70KB） |
| `dpar-knowledge-graph.html` | 交互式力导向图可视化（D3.js） |
| `dpar-session-summaries.md` | 原始 session summary 数据整理 |

---

## 四、第二轮测试：ShadowFolk 项目

### 4.1 数据源

`minusjiang_shadowfolk_export.db`（SQLite），从 CodeBuddy-Mem 系统导出。

| 指标 | 值 |
|------|---|
| 原始记录数 | 815 条 |
| 去重后 | 783 条 |
| 涉及人员 | minusjiang（全部） |
| 时间范围 | 2026-02-26 ~ 2026-03-19（约 3 周） |
| 内容 | ShadowFolk 项目开发记录（MCP、CLI、前后端、Mem0 等） |

### 4.2 LLM 配置：TimiAPI（双模型分层）

本轮测试更换了 LLM 服务和模型。Graphiti 内部将 LLM 分为 **medium** 和 **small** 两级（详见 `graphiti-架构原理与codebuddy-mem存储召回分析.md` 3.6-3.7 节），不同步骤使用不同级别的模型：

| 模型级别 | 模型名 | 具体用途 | 调用频率 |
|---------|--------|---------|---------|
| **medium** | `gpt-5-nano` | 实体抽取（从文本识别人/工具/概念等） | 每条 episode 1 次 |
| **medium** | `gpt-5-nano` | 实体解析（判断新实体是否已在图中存在） | 每条 episode 0-1 次 |
| **medium** | `gpt-5-nano` | 关系/事实抽取（抽取实体间的事实三元组 + 时间标注） | 每条 episode 1 次 |
| **small** | `gpt-4o-mini` | 边去重 + 矛盾检测（逐条边判断是否重复或与旧事实矛盾） | 每条 episode N 次（N=抽取的边数） |
| **small** | `gpt-4o-mini` | 实体摘要生成（根据新边更新实体 summary） | 每条 episode 1 次 |

> 设计理念：需要深度理解的步骤（实体/关系抽取）用较强的 gpt-5-nano；高频但判定简单的步骤（边去重、摘要）用更快更便宜的 gpt-4o-mini。

| 配置项 | 值 |
|--------|---|
| API 服务 | TimiAPI |
| API 端点 | `http://api.timiai.woa.com/ai_api_manage/llmproxy/chat/completions` |
| medium 模型 | `gpt-5-nano` |
| small 模型 | `gpt-4o-mini` |
| 认证 | Bearer Token |

**适配方案**：自定义 `TimiOpenAIClient`，继承 `OpenAIClient`，重写 `_create_structured_completion` 和 `_create_completion` 两个方法，将 Pydantic schema 注入 system prompt 并使用 `response_format={"type": "json_object"}`。

**TimiAPI 特殊问题**：不带 `stream` 参数时默认返回流式 SSE 响应，导致 openai SDK 将响应解析为 `str` 而非 `ChatCompletion` 对象。解决方案：在所有 `create` 调用中显式传入 `stream=False`。

### 4.3 执行过程与耗时

#### 单条验证

首先用 1 条 summary 验证 TimiAPI + Graphiti 管线的连通性：

| 指标 | 值 |
|------|---|
| 输入 | "我完成了 SVN 资产引用修复脚本的增强（v3），新增了 MaterialFunction 类型的专门处理逻辑" |
| 抽取实体 | 3 个（minusjiang, SVN 资产引用修复脚本 v3, MaterialFunction） |
| 抽取边 | 2 条（IMPROVED, DEVELOPED） |
| 耗时 | 43 秒 |

验证通过。

#### 全量评估与决策

按 783 条 × 43 秒/条估算：

| 指标 | 值 |
|------|---|
| 预计 LLM 总调用次数 | ~6000+ 次（每条 6-13 次 LLM 调用） |
| 预计总耗时 | **~9.4 小时**（理想情况） |
| 实际预计 | **~20 小时**（随图增大，实体解析和边去重搜索空间增大，单条处理时间递增） |

考虑到：
1. 处理时间过长（预计 20 小时以上）
2. API 限流风险（Venus API 此前已遇到 429 限流）
3. 本轮测试目的是验证管线可行性而非生产级数据处理

**决策**：仅处理前 **78 条**（约 1/10），最终实际成功处理 **30 条**（28 成功 / 2 失败）后因累计耗时已达 46 分钟而手动终止，导出已有数据生成可视化。

#### 实际执行统计

| 阶段 | 耗时 | 成功/失败 | 说明 |
|------|------|----------|------|
| 初始化（模型加载 + Kuzu建表 + FTS索引） | ~10 秒 | — | |
| Episode 1-10 | 870 秒（14.5 分钟） | 9/1 | 平均 87 秒/条 |
| Episode 11-20 | 911 秒（15.2 分钟） | 9/0 | 平均 91 秒/条 |
| Episode 21-30 | 967 秒（16.1 分钟） | 10/1 | 平均 97 秒/条，图增大后略慢 |
| **合计** | **2748 秒（~46 分钟）** | **28/2** | 平均 92 秒/条 |

可观察到随着图中实体增多，单条处理时间从 87 秒增长到 97 秒，主要因为实体解析和边去重需要在更大的候选集中搜索。

### 4.4 测试结果

#### 图谱规模

| 指标 | 数量 |
|------|------|
| Entity 节点 | **248** |
| 关系边（RelatesToNode_） | **280** |
| Episode 节点 | 33 |
| 有效边 | 232 |
| 已失效边（temporal invalidation） | 48（17%） |

#### 节点类型分布

| 类型 | 数量 | 说明 |
|------|------|------|
| concept | 201 | MVP、前端开发、后端架构、需求等概念/方案 |
| tech | 31 | MCP、CLI、Git、Drizzle、HTML5 Canvas 等 |
| document | 10 | 脚本、配置文件等 |
| issue | 5 | 各类问题与 bug |
| person | 1 | minusjiang |

#### 关系类型 Top 15

| 关系 | 数量 |
|------|------|
| USES | 23 |
| INCLUDES | 15 |
| IMPROVES | 13 |
| INTEGRATES_WITH | 13 |
| SUPPORTS | 7 |
| REQUIRES | 5 |
| COMPATIBLE_WITH | 5 |
| ENHANCES | 5 |
| PART_OF | 5 |
| HAS_CAPABILITY | 5 |
| PUSHES_TO | 5 |
| REQUIRES_INPUT | 4 |
| WORKS_ON | 4 |
| ENABLES | 4 |
| INTEGRATED_WITH | 3 |

#### 核心节点连接度 Top 10

| 节点 | 连接边数 | 角色 |
|------|---------|------|
| minusjiang | 39 | 项目开发者 |
| MCP | 27 | 核心通信协议 |
| MVP | 9 | 产品里程碑 |
| 前端开发 | 8 | 开发领域 |
| HTML5 Canvas | 8 | 前端渲染技术 |
| CLI | 7 | 命令行工具 |
| Workspace | 7 | 工作区管理 |
| 后端 | 7 | 开发领域 |
| Mem0 | 7 | 记忆系统 |
| MVP范围 | 6 | 需求范围 |

#### 时序失效验证

48 条已失效边（`invalid_at` 非空，占总边数 17%）验证了 Graphiti 的时序推理能力——当新信息与旧信息矛盾时，旧边被自动标记失效而非删除，保留了完整的演进历史。例如项目早期的架构决策被后续迭代覆盖时，旧的事实边会被设置 `invalid_at` 和 `expired_at`，但仍可追溯。

### 4.5 遇到的问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| TimiAPI 默认流式响应 | 不带 `stream` 参数时返回 SSE | 显式 `stream=False` |
| openai SDK 返回 str 而非对象 | 流式响应被错误解析 | 同上 |
| gpt-5-nano 不支持 temperature | reasoning model 限制 | 检测 `gpt-5` 前缀，跳过 temperature 参数 |
| Kuzu DB 损坏 `IndexError` | 前次进程被 kill 导致 WAL 损坏 | 删除 `kuzu_db` + `.wal` 后重建 |
| 处理时间过长 | 783 条预计 20+ 小时 | 仅处理 1/10 |

### 4.6 产物清单

| 文件 | 说明 |
|------|------|
| `shadowfolk/run_graphiti_shadowfolk.py` | 适配 TimiAPI 的管线脚本 |
| `shadowfolk/kuzu_db` | Kuzu 嵌入式图数据库（30 条数据） |
| `shadowfolk/shadowfolk_graph_data.json` | 导出的图数据 JSON（248 节点 + 280 边） |
| `shadowfolk/shadowfolk-knowledge-graph.html` | 交互式力导向图可视化（D3.js） |

---

## 五、两轮测试对比总结

| 维度 | DPAR（第一轮） | ShadowFolk（第二轮） |
|------|--------------|-------------------|
| 数据量 | 8 条（全量） | 30 条（783 条中取 1/10） |
| LLM 服务 | Venus API | TimiAPI |
| LLM 模型 | claude-sonnet-4-6（单模型） | gpt-5-nano + gpt-4o-mini（双模型分层） |
| 实体数 | 85 | 248 |
| 边数 | 102 | 280 |
| 每条平均产出 | 10.6 实体 + 12.8 边 | 8.3 实体 + 9.3 边 |
| 平均处理时间/条 | ~75 秒 | ~92 秒 |
| 失效边占比 | 未统计 | 17%（48/280） |
| 成功率 | 100% | 93%（28/30） |

**发现**：
- gpt-5-nano 在结构化抽取任务上表现良好，首条 summary 即抽取了 29 个实体 + 16 条边
- 双模型分层（medium/small）是合理的成本优化策略——边去重这种高频简单任务用 gpt-4o-mini 足够
- 随着图中实体增多，实体解析和边去重的搜索空间增大，单条处理时间从 87 秒增长到 97 秒
- 大规模数据处理（783 条）需要考虑：分批处理、提升 API 配额、或实现并发调用

---

## 六、踩坑记录

### 6.1 孤立节点与断裂子图

**现象**：ShadowFolk 可视化图谱中出现了多个孤立的小簇和完全没有边的孤立节点。例如"回填脚本"、"中文摘要"、"约100条中文摘要"三个节点只有彼此间的 `GENERATES` 边，与主图完全断开。

**数据统计**：248 个实体中有 **48 个完全孤立节点**（无任何边连接），如"分支名"、"Agent"、"Profile"、"全局可视化"等。

**原因分析**：

源头是这条 session_summary（2026-02-27）：

> "回填脚本成功批量生成中文摘要，证明管道可用…运行回填生成约 100 条中文摘要"

Graphiti 处理这条 summary 时，LLM（gpt-5-nano）的行为：

1. **实体抽取**：正确提取了"回填脚本"、"中文摘要"、"约100条中文摘要"
2. **关系抽取**：只抽取了两条内部关系 `回填脚本 --[GENERATES]--> 中文摘要` 和 `回填脚本 --[GENERATES]--> 约100条中文摘要`
3. **遗漏的关键边**：没有抽取 `minusjiang --[DEVELOPED]--> 回填脚本` 或 `回填脚本 --[PART_OF]--> ShadowFolk` 这样的边，导致这组节点无法连回主图

这是 **LLM 关系抽取不完整** 的典型表现——当一段文本涉及的实体较多时，LLM 可能只抽取了部分关系，遗漏了将子图连回中心节点的关键边。48 个完全孤立的节点也是同样原因：被实体抽取步骤识别出来，但关系抽取步骤没有为它们生成任何边。

**Graphiti 源码注释也提到**："works best with OpenAI and Gemini"，不同模型在结构化抽取的完整性上存在差异。

**可能的改善方向**：
- 在 `add_episode` 时传入 `custom_extraction_instructions`，明确要求 LLM "确保每个实体至少与一个已知实体建立关系"
- 后处理阶段检测孤立节点，尝试补充关系或合并到已有实体
- 使用更强的 medium 模型（如 gpt-4.1 或 gpt-5）提升抽取完整度

### 6.2 Venus API 限流导致处理中断

**现象**：第一轮 DPAR 测试后尝试用 Venus API 处理 ShadowFolk（783 条），在处理数条后即触发 HTTP 429（Rate Limit Exceeded），多次指数退避重试后仍无法恢复。

**原因**：Venus API 是公共代理服务，有全局限流策略（约 50 次/分钟），而 Graphiti 每条 episode 需要 6-13 次 LLM 调用，在 2 秒间隔下仍超出限额。前序 DPAR 测试的频繁重试消耗了大量配额。

**解决**：更换为 TimiAPI，限流更宽松。同时在脚本中增加了指数退避重试（30/60/90s）和每条 episode 间 2 秒固定间隔。

**教训**：大规模 Graphiti 灌入任务对 LLM API 的 QPS 要求远超一般应用，需提前评估 API 配额或使用专用 API Key。

### 6.3 TimiAPI 默认流式响应导致 SDK 解析失败

**现象**：使用 TimiAPI 时，openai SDK 的 `client.chat.completions.create()` 返回 `str` 类型而非 `ChatCompletion` 对象，导致 `'str' object has no attribute 'choices'` 错误。

**原因**：TimiAPI 代理在请求体中不包含 `stream` 字段时，默认返回 SSE（Server-Sent Events）流式响应。openai SDK v2.30.0 在处理非预期的流式响应时，将整个 SSE 数据流解析为一个字符串。

**验证方法**：
```python
# 不带 stream → 返回 str（错误）
resp = await client.chat.completions.create(model="gpt-4o-mini", messages=[...])
print(type(resp))  # <class 'str'>

# 显式 stream=False → 返回 ChatCompletion（正确）
resp = await client.chat.completions.create(model="gpt-4o-mini", messages=[...], stream=False)
print(type(resp))  # <class 'openai.types.chat.chat_completion.ChatCompletion'>
```

**解决**：在 `TimiOpenAIClient` 的 `_create_structured_completion` 和 `_create_completion` 中都显式传入 `stream=False`。

### 6.4 Kuzu 数据库进程异常退出后损坏

**现象**：多次运行或强制 kill 进程后，再次启动脚本报 `IndexError: unordered_map::at: key not found`。

**原因**：Kuzu 使用 WAL（Write-Ahead Log）机制。进程被 kill 时 WAL 未正确刷写，留下损坏的 `kuzu_db.wal` 文件。下次启动时 Kuzu 尝试从损坏的 WAL 恢复，触发 key not found 错误。

**解决**：删除 `kuzu_db`（文件或目录）和 `kuzu_db.wal` 后重建。数据会全部丢失，需要重新灌入。

**教训**：Kuzu 嵌入式数据库在异常退出场景下不够健壮，长时间运行的灌入任务建议增加定期 checkpoint 或断点续传机制。

### 6.5 gpt-5-nano 不支持 temperature 参数

**现象**：使用 gpt-5-nano 作为 medium 模型时，API 返回错误（reasoning model 不支持 temperature）。

**原因**：gpt-5 系列是 reasoning model，与 o1/o3 系列类似，不接受 `temperature` 参数。Graphiti 的 OpenAIClient 默认会传 temperature。

**解决**：在 `_create_structured_completion` 和 `_create_completion` 中检测模型前缀（`gpt-5`、`o1`、`o3`），对 reasoning model 跳过 temperature 参数。

### 6.6 Graphiti OpenAIClient 默认使用 Responses API

**现象**：Graphiti v0.28.2 的 `OpenAIClient._create_structured_completion` 调用 `self.client.responses.parse()`，命中 `/responses` 端点。Venus API 和 TimiAPI 均不支持此端点，返回 404。

**原因**：Graphiti 紧跟 OpenAI SDK 新版，采用了 Responses API（替代旧版 chat/completions + structured output）。但企业内部 API 代理通常只支持 `/chat/completions`。

**解决**：自定义 Client 子类，将 `_create_structured_completion` 回退到 `self.client.chat.completions.create` + `response_format={"type": "json_object"}` + system prompt 中注入 JSON schema 的方式。同时需要自行实现 `_parse_json_robust` 处理 LLM 输出中的 markdown 包裹、尾逗号等格式问题。
