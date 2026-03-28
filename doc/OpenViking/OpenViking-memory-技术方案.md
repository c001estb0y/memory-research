# OpenViking Memory 系统技术方案

> **项目定位**：OpenViking 是字节跳动（火山引擎）开源的**AI Agent 上下文数据库**（19k+ stars），用虚拟文件系统范式统一管理 Memory / Resource / Skill，并在向量索引上叠加以目录为导向的**层级检索**与 **L0/L1/L2 渐进式加载**，大幅降低 token 消耗。

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                     Client Layer                         │
│              find() / search() / read()                  │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                   Service Layer                          │
│  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌─────────┐ │
│  │FSService │ │SearchService│ │SessionSvc│ │ResourceSvc│ │
│  └────┬─────┘ └─────┬──────┘ └────┬─────┘ └────┬────┘ │
└───────┼─────────────┼─────────────┼─────────────┼──────┘
        │             │             │             │
┌───────▼─────────────▼─────────────▼─────────────▼──────┐
│                  Retrieve Layer                          │
│  ┌──────────────┐  ┌─────────────────────────────────┐ │
│  │IntentAnalyzer│  │   HierarchicalRetriever         │ │
│  │(意图分析)    │  │   (层级递归检索)                 │ │
│  └──────┬───────┘  └──────────────┬──────────────────┘ │
│         │                         │                     │
│  ┌──────▼─────────────────────────▼──────────────────┐ │
│  │                  Reranker                          │ │
│  └────────────────────────────────────────────────────┘ │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                  Storage Layer                           │
│  ┌───────────────────┐  ┌────────────────────────────┐ │
│  │    AGFS (内容)    │  │   Vector Index (索引)      │ │
│  │  L0/L1/L2 全文    │  │   URI+向量+元数据          │ │
│  │  + 多媒体文件     │  │   HNSW / 余弦相似度       │ │
│  └───────────────────┘  └────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### 核心设计理念

**不把上下文当成平面切片，而是映射到 `viking://` 下的目录树**：

- Agent 可用类似 `ls`/`tree` 的方式确定性地定位信息
- 再辅以语义检索做模糊匹配
- 目录结构本身就是信息的组织方式

---

## 2. `viking://` 协议与 URI 设计

### 2.1 URI 格式

```
viking://{scope}/{path}
```

### 2.2 Scope 类型

| Scope | 说明 | 示例 |
|-------|------|------|
| `resources` | 文档/知识库 | `viking://resources/docs/api.md` |
| `user` | 用户维度的记忆 | `viking://user/{space}/memories/` |
| `agent` | Agent 维度的记忆和技能 | `viking://agent/{space}/skills/code-review/` |
| `session` | 会话上下文 | `viking://session/{id}/` |
| `queue` | 处理队列 | `viking://queue/...` |
| `temp` | 临时文件 | `viking://temp/...` |

### 2.3 URI 安全规范化

```python
# openviking/storage/viking_fs.py
def _normalize_uri(uri):
    # 短格式归一化为 viking://
    # 禁止 .. 路径遍历
    # 禁止反斜杠和 Windows 盘符
    # 确保 canonical 格式
```

---

## 3. 三层内容抽象 L0/L1/L2（核心技术）

### 3.1 层级定义

```python
# openviking/core/context.py
class ContextLevel(int, Enum):
    ABSTRACT = 0  # L0: ~100 tokens，极短摘要
    OVERVIEW = 1  # L1: ~1k-2k tokens，导航要点
    DETAIL   = 2  # L2: 原始文件全文
```

### 3.2 具体实现

| 层级 | 载体文件 | Token 量级 | 用途 |
|------|----------|------------|------|
| **L0 Abstract** | `.abstract.md` | ~100 tokens | 快速向量召回，判断相关性 |
| **L1 Overview** | `.overview.md` | ~1k-2k tokens | 导航与要点概览，决定是否深入 |
| **L2 Detail** | 原始文件与子目录 | 完整内容 | 按需 `read` 获取全文 |

### 3.3 示例：一个目录的 L0/L1/L2

```
viking://resources/api-docs/
├── .abstract.md          # L0: "API 文档，包含认证、用户、支付三个模块"
├── .overview.md          # L1: 三个模块的概要说明 + 关键端点列表
├── authentication.md     # L2: 认证模块完整文档
├── users.md             # L2: 用户模块完整文档
└── payments.md          # L2: 支付模块完整文档
```

### 3.4 L0/L1 生成流程

**自底向上**：子目录的 L0 汇聚到父目录的 L1，由 `SemanticProcessor` / `SemanticQueue` 异步完成。

```python
# openviking/storage/queuefs/embedding_msg_converter.py
# 根据 URI 后缀推断 level
uri = context_data.get("uri", "")
if uri.endswith("/.abstract.md"):
    resolved_level = ContextLevel.ABSTRACT    # L0
elif uri.endswith("/.overview.md"):
    resolved_level = ContextLevel.OVERVIEW    # L1
else:
    resolved_level = ContextLevel.DETAIL      # L2
```

### 3.5 Token 预算管理

默认只用 L0/L1 做导航与召回，L2 按需 `read`：

```
传统 RAG：query → 返回所有相关 chunk (高 token 消耗)

OpenViking：
  query → L0 向量召回 (100 tokens/条)
        → L1 确认相关性 (1-2k tokens/条)
        → 仅对确认相关的做 L2 read (按需)

效果：输入 token 降低 83-96%
```

---

## 4. 核心数据结构

### 4.1 Context 对象

```python
# openviking/core/context.py
@dataclass
class Context:
    uri: str                    # viking:// URI
    parent_uri: str             # 父目录 URI
    context_type: ContextType   # MEMORY / RESOURCE / SKILL
    level: ContextLevel         # L0 / L1 / L2
    abstract: str               # 摘要文本
    content: str                # 内容
    is_leaf: bool              # 是否叶子节点
    # 租户/空间字段...
```

### 4.2 双层存储

| 存储层 | 存什么 | 特点 |
|--------|--------|------|
| **AGFS** | L0/L1/L2 全文与多媒体 | 单一事实来源（SSOT） |
| **Vector Index** | URI + 向量 + 元数据 | 不存文件正文，只存引用 |

---

## 5. 写入流程

### 5.1 Resource 写入管线

```
输入内容（文件/URL/文本）
    │
    ▼
ResourceProcessor.process()
    │
    ├──→ Parser 解析到 temp URI
    │      │ 智能切分：按标题拆段、小段合并、大段建子目录
    │      │ 代码文件：tree-sitter AST 骨架摘要
    │      ▼
    ├──→ TreeBuilder 迁移到 AGFS
    │      │ 建立目录树结构
    │      │ 写入各层级文件
    │      ▼
    └──→ SemanticQueue 异步处理
           │ 生成 L0 (.abstract.md)
           │ 生成 L1 (.overview.md)
           │ Embedding 向量化
           ▼
       Vector Index 入库
```

### 5.2 Memory 写入

```
会话记忆抽取
    │
    ▼
viking://user/{space}/memories/ 下创建条目
    │
    ├──→ L0: 记忆摘要
    ├──→ L1: 记忆概述
    └──→ L2: 完整记忆内容
    │
    ▼
SemanticQueue → 向量化入库
```

### 5.3 Skill 写入

```
viking://agent/{space}/skills/{skill-name}/
    │
    ├──→ .abstract.md     # L0 向量
    ├──→ .overview.md     # 技能概述
    ├──→ SKILL.md         # 完整技能定义
    └──→ scripts/...      # 关联脚本
```

---

## 6. 检索流程（核心技术 — 层级递归检索）

### 6.1 两种检索模式

| 模式 | 入口 | 特点 |
|------|------|------|
| **`find()`** | 单查询 + 可选 `target_uri` | 不做意图分析，直接检索 |
| **`search()`** | 带 session_info | IntentAnalyzer 生成 0-5 条 TypedQuery，并发检索 |

### 6.2 IntentAnalyzer（意图分析）

```
用户查询 + 会话上下文
    │
    ▼
IntentAnalyzer.analyze()
    │  LLM 分析意图
    ▼
TypedQuery[] (0-5 条)
    │  每条含：query_text, context_type, target_directories
    ▼
并发调用 HierarchicalRetriever.retrieve()
```

### 6.3 HierarchicalRetriever（层级递归检索 — 核心算法）

```
                    ┌─────────────────────┐
                    │  确定起始 root_uris  │
                    │  (按 context_type)   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ 全局向量检索 (L0/L1)│
                    │ search_global_roots  │
                    │ TopK = 5            │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ 合并起始锚点         │
                    │ level≠2 → 目录入口   │
                    │ level=2 → 叶子候选   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ 优先队列递归搜索     │
                    │ (Priority Queue)    │
                    │                     │
                    │ 对每个目录：         │
                    │  search_children()  │
                    │  子项得分 =          │
                    │   α × embedding_score│
                    │   + (1-α) × parent_score│
                    │                     │
                    │ 非 L2 → 继续入队递归 │
                    │ L2 → 加入候选集     │
                    │                     │
                    │ MAX_CONVERGENCE = 3  │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Hotness 混合打分    │
                    │  active_count       │
                    │  + updated_at       │
                    │  + semantic_score   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Rerank (可选)      │
                    │  对 abstract 文本    │
                    │  batch rerank       │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  返回 Top-K 结果    │
                    └─────────────────────┘
```

### 6.4 分数传播公式

```
child_score = α × embedding_similarity(query, child) + (1 - α) × parent_score
```

其中 `SCORE_PROPAGATION_ALPHA = 0.5`，意味着子项得分由自身语义相似度和父目录得分各占一半。

### 6.5 多租户根目录映射

```python
# openviking/retrieve/hierarchical_retriever.py
def _get_root_uris_for_type(self, context_type, ctx):
    if context_type is None:  # 全局搜索
        return [
            f"viking://user/{user_space}/memories",
            f"viking://agent/{agent_space}/memories",
            "viking://resources",
            f"viking://agent/{agent_space}/skills",
        ]
    elif context_type == ContextType.MEMORY:
        return [
            f"viking://user/{user_space}/memories",
            f"viking://agent/{agent_space}/memories",
        ]
    elif context_type == ContextType.RESOURCE:
        return ["viking://resources"]
    elif context_type == ContextType.SKILL:
        return [f"viking://agent/{agent_space}/skills"]
```

---

## 7. 检索轨迹可视化与可观测性

### 7.1 溯源结构

```python
# openviking_cli/retrieve/types.py
class FindResult:
    def to_dict(self, include_provenance=True):
        return {
            "results": [...],
            "provenance": {
                "searched_directories": [...],  # 搜索过的目录
                "matched_contexts": [...],       # 匹配的上下文
                "thinking_trace": {...},          # 推理轨迹
            }
        }
```

### 7.2 ThinkingTrace

结构化记录目录推理、分数分布、收敛过程，用于调试检索行为。

### 7.3 Prometheus 指标

`RetrievalStatsCollector` + `RetrievalObserver` 暴露聚合指标。

---

## 8. Token 优化策略（核心优势）

| 策略 | 效果 |
|------|------|
| **分层加载（L0/L1/L2）** | 默认只用 ~100 tokens 的 L0 做召回，L2 按需加载 |
| **智能切分** | 按标题拆段、小段合并、大段建子目录 |
| **代码 AST 摘要** | 大文件用 tree-sitter 骨架替代 LLM 全文处理 |
| **目录递归剪枝** | 层级检索天然剪枝，不相关的子树不会被展开 |

### 实测效果（OpenClaw 插件实验）

| 指标 | 改进 |
|------|------|
| 任务完成率 | +43-49% |
| 输入 Token 成本 | -83-96% |
| 对比 LanceDB | +17% 任务完成率 |

---

## 9. 关键技术亮点

1. **文件系统范式的统一命名空间**：Memory / Resource / Skill 同构为 `viking://` 树，权限、路径、CLI 心智模型一致
2. **L0/L1/L2 三层渐进式加载**：向量库中显式区分层级，全局搜目录锚点、子树内递归，天然节省 token
3. **目录递归检索 + 分数传播**：父目录得分影响子项排序，实现结构化的语义导航
4. **存储与索引分离**：AGFS 是 SSOT，向量库只存引用与语义，同步/删除/迁移简单可靠
5. **两阶段检索**：简单 `find` vs 带意图分析的 `search`，兼顾延迟与复杂任务
6. **多模态与技能同管道**：L0/L1 始终为文本描述，统一处理不同类型内容
7. **Hotness 混合排序**：语义分 + 活跃度 + 更新时间，热门内容优先

---

## 10. 与传统 RAG 的核心差异

| 维度 | 传统 RAG | OpenViking |
|------|----------|------------|
| 组织方式 | 平面 chunk 列表 | 目录树（文件系统） |
| 检索粒度 | 单一粒度 chunk | L0/L1/L2 渐进式 |
| Token 消耗 | 返回所有 chunk | 先摘要判断再按需加载 |
| 上下文类型 | 通常只有文档 | Memory + Resource + Skill 统一 |
| 可解释性 | 相似度分数 | 目录路径 + 检索轨迹 |
| 导航方式 | 只能搜 | 可搜 + 可浏览（ls/tree） |
