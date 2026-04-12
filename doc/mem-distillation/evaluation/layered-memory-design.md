# 分层踩坑记忆系统 — 技术方案

> 基于蒸馏管线产出 + MemPalace 存储 + 级联检索调度的三层架构设计。

## 1. 问题与动机

### 1.1 当前现状

蒸馏管线已产出 636 条 L1 结构化经验 + 49 条 L2 叙事，但存在两个核心缺陷：

1. **无溯源**：无法从一条蒸馏经验反查到它来自哪些 summary/observation
2. **无分层检索**：所有经验平铺在 JSON 文件中，没有按信息密度分级的检索策略

### 1.2 目标场景

**场景 A — 按事搜索**（新人遇到问题）：
> C 在 DPAR 项目遇到 NeverCook 问题 → 搜索 "NeverCook" →
> 先看到蒸馏后的精华结论（L0）→ 不够再展开 hughesli/ziyad 的原始排查记录（L1→L2）

**场景 B — 按人搜索**（回顾个人贡献）：
> PM 想看 hughesli 上周解决了哪些问题 → 搜索 `author=hughesli, date>=2026-03-10` →
> 按时间线展示该人在各 Room 下的经验和排查记录

**场景 C — 按人+事交叉搜索**（比较不同人的解法）：
> "hughesli 和 ziyad 各自是怎么处理 NeverCook 的？" → 搜索 `room=NeverCook配置` →
> 结果按 author 分组展示，每人各自的 L0→L1→L2 链路独立呈现

### 1.3 设计原则

- **双入口检索**：既支持以"事"（问题/关键词）为入口检索，也支持以"人"（author + 时间线）为入口检索
- **逐层深入**：先给结论（L0），再给上下文（L1），再给原始证据（L2）
- **多人聚合与分离**：按事搜索时，多人经验按 confidence 并列展示；按人搜索时，聚焦单人按时间排列
- **借鉴 MemPalace**：用 Wing/Room 概念组织知识，用 ChromaDB 做向量检索和 metadata 过滤
- **不改 MemPalace 源码**：MemPalace 只做存储层，级联逻辑在外部实现

## 2. 架构总览

```
                    ┌─────────────┐   ┌──────────────┐
                    │ 按事搜索     │   │ 按人搜索      │
                    │ "NeverCook" │   │ author=hughes │
                    └──────┬──────┘   └──────┬───────┘
                           │                  │
                           ▼                  ▼
┌──────────────────────────────────────────────────────┐
│                 检索调度层 (新建)                       │
│                                                      │
│  search_by_issue(query, wing,     search_by_author(  │
│    room?, author?)                  author, wing,    │
│                                     time_range?)     │
│  ┌──────────┐                     ┌──────────┐      │
│  │ L0 搜索   │  级联 L0→L1→L2     │ L0 过滤   │ 按人  │
│  │ 向量+排序  │  按 similarity     │ metadata  │ 按时间 │
│  ├──────────┤  × confidence      ├──────────┤ 按Room │
│  │ L1 搜索   │  多人混排           │ L1 过滤   │ 分组   │
│  ├──────────┤                    ├──────────┤      │
│  │ L2 搜索   │                    │ L2 按需   │      │
│  └──────────┘                    └──────────┘      │
├──────────────────────────────────────────────────────┤
│                 索引层 (新建)                          │
│  traceability_index.json                             │
│  experience_hash → [summary_ids]                     │
│  summary_id → memory_session_id → [obs_ids]          │
├──────────────────────────────────────────────────────┤
│                 MemPalace 存储层 (现有)                │
│  ChromaDB collection: mempalace_drawers              │
│  每条 Drawer 的 metadata:                            │
│    wing / room / layer / author / date               │
└──────────────────────────────────────────────────────┘
```

## 3. 数据模型

### 3.1 三层数据定义

| 层级 | 名称 | 数据来源 | 信息密度 | 数量级 |
|------|------|---------|---------|--------|
| **L0** | 蒸馏经验 | `dpar-*-experiences.json` | 最高（结论+根因+方案） | 636 条 |
| **L1** | Session Summary | `session_summaries` 表 | 中等（request+learned+completed） | 2,040 条 |
| **L2** | Observation | `observations` 表 | 最低（操作流水账，仅 bugfix/config 有价值） | 10,304 条（按需，可只存高价值类型） |

### 3.2 MemPalace Drawer metadata 规范

每条记录写入 MemPalace 时携带以下 metadata：

```json
{
  "wing": "dpar",
  "room": "NeverCook配置",
  "layer": "L0",
  "author": "hughesli",
  "date": "2026-03-15",
  "confidence": 0.92,
  "experience_hash": "a1b2c3d4e5f6",
  "source_session_id": "mem-1775009887541-o80irp",
  "experience_type": "configuration",
  "related_components": "NeverCook,DefaultGame.ini,Cook Pipeline"
}
```

字段说明：

| 字段 | 类型 | 用途 | L0 | L1 | L2 |
|------|------|------|:--:|:--:|:--:|
| `wing` | string | 项目隔离 | ✓ | ✓ | ✓ |
| `room` | string | 问题域聚合 | ✓ | ✓ | ✓ |
| `layer` | string | 层级标识（L0/L1/L2） | ✓ | ✓ | ✓ |
| `author` | string | 经验归属人 | ✓ | ✓ | ✓ |
| `date` | string | 原始记录时间（ISO date） | ✓ | ✓ | ✓ |
| `confidence` | float | 置信度 | ✓ | — | — |
| `experience_hash` | string | L1 经验的 hash，用于溯源 | ✓ | — | — |
| `source_session_id` | string | 来源 session ID | — | ✓ | ✓ |
| `experience_type` | string | 经验分类 | ✓ | — | — |
| `related_components` | string | 关联组件（逗号分隔） | ✓ | — | — |

### 3.3 溯源索引结构

蒸馏时生成 `traceability_index.json`，记录 L0→L1→L2 的映射：

```json
{
  "version": "1.0",
  "project": "dpar",
  "generated_at": "2026-04-12T16:00:00Z",
  "experiences": [
    {
      "hash": "a1b2c3d4e5f6",
      "issue_context_preview": "NeverCook优先级高于AlwaysCook...",
      "confidence": 0.92,
      "author": "hughesli",
      "room": "NeverCook配置",
      "source_summary_ids": [142, 178, 203],
      "source_session_ids": ["mem-1775009887541-o80irp"]
    }
  ],
  "sessions": {
    "mem-1775009887541-o80irp": {
      "summary_count": 153,
      "observation_count": 525,
      "author": "hughesli",
      "time_range": "2026-03-04 ~ 2026-03-20"
    }
  }
}
```

通过这个索引：
- L0 经验 → `source_summary_ids` → 直接定位 L1 的 summary 行
- L1 summary → `source_session_ids` → `memory_session_id` → 直接定位 L2 的 observation 行

### 3.4 Room 生成规则

Room 由 L0 经验的 `related_components` 自动聚类产生：

1. 提取所有 L0 经验的 `related_components`
2. 按共现频率聚类（同一经验中出现的 component 视为相关）
3. 每个聚类取频次最高的 component 作为 Room 名
4. 低频 component（仅出现 1 次）归入 `general` Room

预期 DPAR 项目的 Room 分布：

| Room 名 | 预估经验数 | 典型 component |
|---------|----------|---------------|
| `NeverCook配置` | ~45 | NeverCook, DefaultGame.ini, AlwaysCook |
| `Cook流水线` | ~60 | UE4 Cook, Cook.py, BuildTools |
| `Android打包` | ~40 | APK, OBB, Puffer CDN |
| `PowerShell兼容性` | ~25 | PowerShell, Windows, Unix工具 |
| `SVN与CI` | ~30 | SVN, 蓝盾CI, SYSTEM账户 |
| `UGC资源管理` | ~35 | UGC, Pak, Chunk, Blueprint |
| ... | ... | ... |

## 4. 源数据关联分析

### 4.1 现有数据的关联关系

```
session_summaries.memory_session_id ←→ observations.memory_session_id
```

| 人员 | distinct session IDs | summaries 总数 | observations 总数 | 平均 obs/session |
|------|---------------------|---------------|------------------|-----------------|
| hughesli | 8 | 599 | 1,624 | ~200 |
| ziyad | 138 | 1,441 | 8,680 | ~63 |

hughesli 只有 8 个 session（长会话），ziyad 有 138 个（短会话居多）。

### 4.2 蒸馏时需要补充的溯源信息

当前蒸馏管线（`pipeline.py`）在 L1 蒸馏时按 batch_size=50 分批，每批混合了多个 session 的 summary。蒸馏后的经验丢失了 batch 内的 summary 来源。

**改造要求**：蒸馏时记录每批输入的 summary IDs，蒸馏产出后建立 experience → summary_ids 的映射。

具体方案：
1. 每个 batch 传入 LLM 时，同时记录 `batch_summary_ids = [s['id'] for s in batch_summaries]`
2. LLM 返回的每条经验标记该 batch 的 summary_ids 作为候选来源
3. （可选精细化）用 TF-IDF 或 embedding 相似度从候选中筛选最相关的 2-3 条 summary

## 5. 检索调度层设计

### 5.1 两种检索模式

系统提供两种入口，分别对应"按事找知识"和"按人看经历"：

| 模式 | 入口参数 | 返回方式 | 典型场景 |
|------|---------|---------|---------|
| **search_by_issue** | `query` + 可选 wing/room/author | 按 confidence × similarity 排序，多人混排 | C 遇到 NeverCook 报错 |
| **search_by_author** | `author` + 可选 wing/time_range/room | 按时间倒序，按 Room 分组 | PM 看 hughesli 上周做了什么 |

两种模式共享底层的级联逻辑（L0→L1→L2），区别在于排序策略和分组方式。

### 5.2 按事搜索（search_by_issue）

```python
def search_by_issue(
    query: str,
    wing: str,
    room: str = None,
    author: str = None,       # 可选：限定某人的经验
    time_range: tuple = None,
    max_layer: str = "L2",
    n_results: int = 5,
) -> CascadeResult:
    """
    以问题为入口的级联检索。
    结果按 confidence × similarity 排序，多人经验并列。
    """
    results = CascadeResult(mode="by_issue")

    # L0: 向量搜索蒸馏经验
    l0_hits = search_layer(
        query, wing, room, author, time_range,
        layer="L0", n_results=n_results
    )
    results.l0 = l0_hits

    if max_layer == "L0" or l0_hits.sufficient:
        return results

    # L1: 溯源索引 → 关联 summary → 向量搜索补充
    related_summary_ids = trace_to_summaries(l0_hits)
    l1_hits = search_layer(
        query, wing, room, author, time_range,
        layer="L1", n_results=n_results,
        boost_ids=related_summary_ids
    )
    results.l1 = l1_hits

    if max_layer == "L1" or l1_hits.sufficient:
        return results

    # L2: session_id → 关联 observation
    related_session_ids = trace_to_sessions(l1_hits)
    l2_hits = search_layer(
        query, wing, room, author, time_range,
        layer="L2", n_results=n_results,
        restrict_sessions=related_session_ids
    )
    results.l2 = l2_hits
    return results
```

### 5.3 按人搜索（search_by_author）

```python
def search_by_author(
    author: str,
    wing: str,
    time_range: tuple = None,  # (start_date, end_date)
    room: str = None,          # 可选：限定某个问题域
    max_layer: str = "L1",     # 默认只到 L1，L2 按需展开
    n_results: int = 20,
) -> AuthorTimeline:
    """
    以人为入口的时间线检索。
    结果按时间倒序，按 Room 分组。
    """
    timeline = AuthorTimeline(author=author)

    # L0: 该作者的所有蒸馏经验，按时间倒序
    l0_hits = search_layer(
        query=None,  # 不做向量搜索，纯 metadata 过滤
        wing=wing, room=room, author=author,
        time_range=time_range,
        layer="L0", n_results=n_results,
        sort_by="date_desc"
    )

    # 按 Room 分组
    for hit in l0_hits:
        timeline.add_to_room(hit.room, "L0", hit)

    if max_layer == "L0":
        return timeline

    # L1: 该作者的 session summaries，按时间倒序
    l1_hits = search_layer(
        query=None,
        wing=wing, room=room, author=author,
        time_range=time_range,
        layer="L1", n_results=n_results,
        sort_by="date_desc"
    )
    for hit in l1_hits:
        timeline.add_to_room(hit.room, "L1", hit)

    return timeline
```

### 5.4 "sufficient" 判断标准

| 条件 | 判定 |
|------|------|
| L0 命中数 ≥ 3 且最高 similarity > 0.85 | L0 sufficient，不继续 |
| L0 命中数 > 0 但 similarity < 0.7 | 继续搜 L1 补充上下文 |
| L0 无命中 | 直接跳到 L1 |
| 按人搜索模式 | 始终返回 L0+L1（L2 按需展开） |

### 5.5 返回结构

```python
class CascadeResult:
    """按事搜索的返回结构"""
    mode: str                  # "by_issue"
    l0: list[L0Hit]            # 蒸馏经验，含 confidence + similarity + author
    l1: list[L1Hit]            # summary，含 request/learned/completed + author
    l2: list[L2Hit]            # observation，含 type/title/text
    trace: dict                # 溯源链

class AuthorTimeline:
    """按人搜索的返回结构"""
    author: str
    rooms: dict[str, RoomGroup]  # room_name → 该 Room 下按时间排列的记录

class RoomGroup:
    l0_entries: list[L0Hit]    # 该人在该 Room 的蒸馏经验
    l1_entries: list[L1Hit]    # 该人在该 Room 的 session summaries
    l2_expandable: bool        # 是否有可展开的 observation
```

### 5.6 查询维度汇总

| 查询方式 | 入口函数 | metadata filter | 排序方式 | 场景 |
|---------|---------|----------------|---------|------|
| 按问题搜索 | `search_by_issue` | `wing=dpar` + 向量搜索 | similarity × confidence | C 遇到新问题 |
| 按问题域浏览 | `search_by_issue` | `wing=dpar, room=NeverCook配置` | confidence 降序 | 系统了解某类问题 |
| 按人+问题 | `search_by_issue` | `wing=dpar, author=hughesli` + 向量搜索 | similarity × confidence | "hughesli 遇到过这个问题吗？" |
| 按人+时间 | `search_by_author` | `author=hughesli, date>=2026-03` | 时间倒序，按 Room 分组 | PM 回顾某人近期工作 |
| 跨人比较 | `search_by_issue` | `room=NeverCook配置` → 结果按 author 分组展示 | 每人按 confidence 排序 | "两人各自怎么处理的？" |
| 某人某域经历 | `search_by_author` | `author=ziyad, room=Cook流水线` | 时间倒序 | "ziyad 在 Cook 流水线上踩了哪些坑？" |

## 6. 数据灌入流程

### 6.1 整体流程

```
源数据 (.db)
    ↓
蒸馏管线 (pipeline.py, 已有)
    ↓ 产出: experiences.json + traceability_index.json (新增)
    ↓
灌入脚本 (ingest.py, 新建)
    ├→ L0: experiences → MemPalace Drawer (room 自动聚类)
    ├→ L1: summaries → MemPalace Drawer (room 继承自关联的 L0)
    └→ L2: observations → MemPalace Drawer (按需, 仅 bugfix/config)
```

### 6.2 灌入步骤

**Step 1: Room 聚类**
- 读取 L0 experiences.json
- 按 `related_components` 共现矩阵聚类
- 产出 `room_mapping.json`: `{component: room_name}`

**Step 2: 灌入 L0**
- 每条 L0 经验 → 1 个 MemPalace Drawer
- document = `issue_context + "\n" + root_cause + "\n" + solution`
- metadata = wing/room/layer/author/date/confidence/experience_hash/...

**Step 3: 灌入 L1**
- 每条 summary → 1 个 MemPalace Drawer
- document = `request + "\n" + learned + "\n" + completed`
- metadata 中 room 通过溯源索引从关联的 L0 经验继承
- 无关联 L0 的 summary 归入 `general` room

**Step 4: 灌入 L2（可选）**
- 仅灌入 `type in (bugfix, configuration)` 的 observation
- document = `title + "\n" + text`
- metadata 中 room 通过 `memory_session_id` 从关联的 L1 summary 继承

### 6.3 去重机制

- L0: 用 `experience_hash` 做 Drawer ID，天然幂等
- L1: 用 `summary_{db_id}` 做 Drawer ID
- L2: 用 `obs_{db_id}` 做 Drawer ID

## 7. 实施计划

### Phase 1: 溯源索引（改造蒸馏管线）

- 修改 `pipeline.py`：蒸馏时记录每批的 summary IDs
- 修改 `transform.py`：经验输出时附带 `_source_batch_summary_ids`
- 新增 `build_traceability_index()` 函数
- 产出 `traceability_index.json`

### Phase 2: 灌入脚本

- 新增 `ingest.py`：读取 experiences.json + traceability_index.json + 源 DB
- 实现 Room 自动聚类（基于 `related_components`）
- 按 L0→L1→L2 顺序灌入 MemPalace
- 支持增量灌入（检查已有 Drawer ID）

### Phase 3: 检索调度层

- 新增 `cascade_searcher.py`：实现 `cascade_search()` 函数
- 集成溯源索引，实现 L0→L1→L2 级联
- 支持 wing/room/author/time_range 多维度过滤

### Phase 4: MCP 集成（可选）

- 包装 `cascade_search` 为 MCP 工具
- AI 助手可直接调用分层检索

## 8. 预期效果

### 8.1 场景 A：按事搜索 — "Cook 流水线失败"

```
> search_by_issue("Cook 流水线失败", wing="dpar")

=== L0 蒸馏经验（3 条命中，多人混排） ===

[1] confidence=0.92, author=ziyad, similarity=0.91
    问题: FeaturePackConfig-NeverCook.csv 中包含了 UGC 路径，NeverCook 优先级
          高于 AlwaysCook，导致 UGC 资源被强制排除在 Cook 之外
    方案: 从 NeverCook CSV 中移除 UGC 路径条目
    预防: Cook 前自动校验 NeverCook/AlwaysCook 无冲突

[2] confidence=0.88, author=hughesli, similarity=0.87
    问题: INI 配置文件缺少正确的 section header，NeverCook 条目无法被引擎解析
    方案: 在 NeverCook 条目前补充 [/Script/UnrealEd.ProjectPackagingSettings]
    预防: INI 写入前校验 section header 存在

[3] confidence=0.85, author=hughesli, similarity=0.83
    ...

--- 需要更多上下文？输入 "expand" 查看原始排查记录 ---

=== L1 关联 Summary（展开后显示） ===

[Session mem-1775009887541] hughesli, 2026-03-15
    request: 用户希望将不重要的失败蓝图放入 nevercook 列表
    learned: 发现了需要放入 nevercook 列表的目录，确认热更新问题与服务器版本有关
    completed: 修复了 INI section header，加载失败条目数量显著减少

=== L2 关联 Observation（深度展开） ===

[bugfix] INI Section Header Fix
    修复完成，已在 NeverCook 条目之前添加了 section header...
```

### 8.2 场景 B：按人搜索 — "hughesli 最近两周做了什么"

```
> search_by_author("hughesli", wing="dpar", time_range=("2026-03-10", "2026-03-24"))

=== hughesli 的时间线（按 Room 分组） ===

── NeverCook配置 (12 条经验, 39 条 summary) ──

  L0 蒸馏经验:
    [0.88] 2026-03-18  INI section header 缺失导致 NeverCook 不生效
    [0.82] 2026-03-15  DirectoriesToNeverCook 在 Windows 构建下失效
    [0.78] 2026-03-12  AlwaysCook 与 NeverCook 优先级冲突
    ...

  L1 最近 Summary (展开):
    2026-03-18  request: 用户希望将失败蓝图放入 nevercook 列表
               learned: 发现 INI section header 缺失是根因
    2026-03-15  request: 用户询问 DirectoriesToNeverCook 是否因公共资产设置而存在
               learned: 发现 Windows 路径分隔符差异影响配置解析
    ...

── Android打包 (5 条经验, 14 条 summary) ──

  L0 蒸馏经验:
    [0.85] 2026-03-20  APK 中 UGC pak 文件挤占 OBB 空间导致崩溃
    [0.82] 2026-03-16  adb push Permission Denied (Android 14 Scoped Storage)
    ...

── PowerShell兼容性 (3 条经验, 8 条 summary) ──
    ...
```

### 8.3 场景 C：跨人比较 — "两人各自怎么处理 NeverCook 的"

```
> search_by_issue("NeverCook", wing="dpar", room="NeverCook配置")
> # 结果按 author 分组展示

=== hughesli 的经验 (34 条，取 Top 3) ===
  [0.88] INI section header 缺失 → 补充 section header
  [0.82] Windows 构建下 DirectoriesToNeverCook 失效 → DDC 缓存干扰
  [0.80] UGC 资产因 NeverCook 被跳过 → 移除 NeverCook 条目

=== ziyad 的经验 (11 条，取 Top 3) ===
  [0.92] NeverCook 优先级高于 AlwaysCook → 从 CSV 移除 UGC 路径
  [0.85] fixconfig 将 UGC 写入 NeverCook 列表 → 修复 fixconfig 脚本
  [0.78] CommunityMod 模式下 NeverCook 配置路径不同 → 区分构建模式
```

### 数据规模预估

| 层级 | DPAR 项目数据量 | MemPalace Drawer 数 |
|------|---------------|-------------------|
| L0 | 636 条经验 | 636 |
| L1 | 2,040 条 summary | 2,040 |
| L2 | ~174 条（仅 bugfix+config） | 174 |
| **总计** | | **~2,850 Drawers** |

ChromaDB 在万级别以内检索延迟 < 50ms，完全满足实时需求。
