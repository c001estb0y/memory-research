# MemPalace — 记忆宫殿架构原理与源码分析

> **依据**：`milla-jovovich/mempalace` v3.0.0 源码（Python）  
> **核心技术栈**：ChromaDB（本地向量数据库）、SQLite（知识图谱）、纯正则/启发式 NLP（无 LLM 依赖）  
> **核心理念**：借鉴古希腊「记忆术」（Method of Loci），将 AI 记忆组织为 Wing → Room → Drawer 的空间隐喻结构，全部本地运行，零 API 调用。

---

## 一、总体架构：记忆宫殿的空间隐喻

MemPalace 的核心设计是将「记忆」映射到一座虚拟宫殿的空间结构中：

```
┌──────────────────── Memory Palace ────────────────────┐
│                                                        │
│  ┌─── Wing: shadow-folk ───┐  ┌── Wing: tank-game ──┐ │
│  │                          │  │                      │ │
│  │  Room: technical         │  │  Room: architecture  │ │
│  │  ┌────────────────┐     │  │  ┌────────────────┐  │ │
│  │  │ Drawer (chunk1) │     │  │  │ Drawer (chunk1) │  │ │
│  │  │ Drawer (chunk2) │     │  │  │ Drawer (chunk2) │  │ │
│  │  └────────────────┘     │  │  └────────────────┘  │ │
│  │                          │  │                      │ │
│  │  Room: decisions         │  │  Room: problems      │ │
│  │  ┌────────────────┐     │  │  ┌────────────────┐  │ │
│  │  │ Drawer (chunk1) │     │  │  │ Drawer (chunk1) │  │ │
│  │  └────────────────┘     │  │  └────────────────┘  │ │
│  └──────────────────────────┘  └──────────────────────┘ │
│                                                        │
│  ════════ Tunnel: "mcp" 跨翼连接 ════════              │
│  (同一 room 出现在多个 wing → 形成「隧道」)             │
└────────────────────────────────────────────────────────┘
```

### 1.1 概念映射表


| 宫殿隐喻           | 代码实现                                    | 存储位置                              | 说明                |
| -------------- | --------------------------------------- | --------------------------------- | ----------------- |
| **Wing（翼）**    | ChromaDB 元数据 `wing` 字段                  | `mempalace_drawers` collection    | 对应一个人、项目或专题域      |
| **Room（房间）**   | ChromaDB 元数据 `room` 字段                  | 同上                                | 对应一个具体主题/想法       |
| **Drawer（抽屉）** | ChromaDB 中的一条 `document`                | 同上                                | 原始文本块，verbatim 存储 |
| **Hall（大厅）**   | 元数据 `hall` 字段（实际挖掘流程中较少写入）              | 同上                                | 记忆类型分类走廊          |
| **Tunnel（隧道）** | `palace_graph.py` 中同名 room 跨多 wing 的聚合边 | 运行时计算                             | 跨域知识桥梁            |
| **Closet（壁橱）** | AAAK 压缩摘要                               | `mempalace_compressed` collection | Drawer 的有损摘要层     |


Wing 的命名来自项目配置或对话目录名：

```python
# miner.py: 从 mempalace.yaml 获取 wing
wing = wing_override or config["wing"]

# convo_miner.py: 从目录名自动推导
wing = convo_path.name.lower().replace(" ", "_").replace("-", "_")
```

---

## 二、四层记忆栈（4-Layer Memory Stack）

MemPalace 的检索架构核心是一个「按需加载」的四层记忆栈，定义在 `layers.py` 中：

```
启动加载（~600-900 tokens）           按需加载
┌──────────────────────┐  ┌──────────────────────────────┐
│  L0: Identity        │  │  L2: On-Demand               │
│  (~100 tokens)       │  │  (~200-500 tokens/次)        │
│  "我是谁"             │  │  Wing/Room 过滤检索           │
│  identity.txt        │  │                              │
├──────────────────────┤  ├──────────────────────────────┤
│  L1: Essential Story │  │  L3: Deep Search             │
│  (~500-800 tokens)   │  │  (无限深度)                   │
│  Top 15 Drawers 摘要  │  │  ChromaDB 语义搜索           │
└──────────────────────┘  └──────────────────────────────┘
```

### 2.1 L0 — Identity（身份层）

读取 `~/.mempalace/identity.txt`，用户手写的纯文本身份描述。token 估算用 `len(text) // 4`。

```python
# layers.py Layer0
def render(self) -> str:
    if os.path.exists(self.path):
        with open(self.path, "r") as f:
            self._text = f.read().strip()
    else:
        self._text = "## L0 — IDENTITY\nNo identity configured."
    return self._text
```

### 2.2 L1 — Essential Story（核心故事层）

从 ChromaDB 全量拉取 Drawer，按 `importance` / `emotional_weight` 排序取 **Top 15 条**，按 room 分组，每条截断到 **200 字符**，总长硬顶 **3200 字符**（约 800 tokens）：

```python
# layers.py Layer1
MAX_DRAWERS = 15
MAX_CHARS = 3200

# 打分逻辑：优先高权重记忆
for key in ("importance", "emotional_weight", "weight"):
    val = meta.get(key)
    if val is not None:
        importance = float(val)
        break

# 截断保持紧凑
if len(snippet) > 200:
    snippet = snippet[:197] + "..."
```

### 2.3 L2 — On-Demand（按需检索层）

当对话提到某个具体话题时触发，通过 Wing/Room 过滤条件从 ChromaDB `get()` 检索，不做语义匹配：

```python
# layers.py Layer2
def retrieve(self, wing=None, room=None, n_results=10):
    where = {}
    if wing and room:
        where = {"$and": [{"wing": wing}, {"room": room}]}
    elif wing:
        where = {"wing": wing}
    results = col.get(include=["documents", "metadatas"], limit=n_results, where=where)
```

### 2.4 L3 — Deep Search（深度语义搜索层）

完全的 ChromaDB 语义搜索，支持 Wing/Room 过滤，返回相似度分数：

```python
# layers.py Layer3 / searcher.py
results = col.query(
    query_texts=[query],
    n_results=n_results,
    include=["documents", "metadatas", "distances"],
)
# 距离转相似度
similarity = round(1 - dist, 3)
```

### 2.5 MemoryStack 统一入口

```python
class MemoryStack:
    def wake_up(self, wing=None):    # L0 + L1 → 注入 system prompt
    def recall(self, wing, room):     # L2 按需
    def search(self, query):          # L3 深搜
```

> **关于「170 token」的说明**：README 宣称的 ~170 token 启动加载是文档层面的理想值（L0≈50 + L1 AAAK≈120）。实际 `wake_up()` 实现并未对 L1 做 AAAK 压缩，直接输出的是截断原文摘录，实际约 **600-900 tokens**。两者存在实现差距。

---

## 三、数据摄入：Mine 流水线

### 3.1 项目挖掘（Project Mining）

`miner.py` 负责将代码/文档目录挖掘入宫殿：

```
mempalace.yaml → 读取 wing/rooms 配置
      ↓
scan_project() → 递归扫描，跳过 SKIP_DIRS，尊重 .gitignore
      ↓
chunk_text() → 滑动窗口分块（800 字符/块，100 重叠）
      ↓
detect_room() → 路径匹配 → 文件名匹配 → 关键词评分 → fallback "general"
      ↓
add_drawer() → 写入 ChromaDB，ID = MD5(source_file + chunk_index)[:16]
```

分块策略在段落边界优先断点：

```python
# miner.py
CHUNK_SIZE = 800   # 每块约 800 字符
CHUNK_OVERLAP = 100

# 优先在段落边界断开
newline_pos = content.rfind("\n\n", start, end)
if newline_pos > start + CHUNK_SIZE // 2:
    end = newline_pos
```

Room 路由的三级优先策略：

```python
# miner.py detect_room()
# 1. 路径中的目录名匹配 room name/keywords
# 2. 文件名匹配 room name
# 3. 内容前 2000 字符的关键词频率评分
# 4. Fallback → "general"
```

### 3.2 对话挖掘（Conversation Mining）

`convo_miner.py` 处理聊天导出（Claude Code、ChatGPT、Slack 等），有两种提取模式：

**Exchange 模式（默认）**：按问答对分块

```python
# convo_miner.py chunk_exchanges()
# 检测 > 标记的用户轮次
if quote_lines >= 3:
    # 用户一行 (>) + AI 响应最多 8 行 = 一个 chunk
    return _chunk_by_exchange(lines)
else:
    # Fallback: 按段落或 25 行组分块
    return _chunk_by_paragraph(content)
```

**General 模式**：使用 `general_extractor.py` 提取 5 类结构化记忆

```python
# general_extractor.py — 纯正则/关键词，无 LLM
# 1. DECISIONS  — "we went with X because Y"
# 2. PREFERENCES — "always use X", "never do Y"
# 3. MILESTONES — "it works!", "got it working"
# 4. PROBLEMS  — "what broke", "root cause"
# 5. EMOTIONAL — feelings, vulnerability
```

每种类型用正则 Marker 集合匹配，例如决策类：

```python
DECISION_MARKERS = [
    r"\blet'?s (use|go with|try|pick|choose|switch to)\b",
    r"\bwe (should|decided|chose|went with|picked|settled on)\b",
    r"\binstead of\b",
    r"\btrade-?off\b",
    # ...
]
```

### 3.3 每条 Drawer 的元数据结构

写入 ChromaDB 的每条记录包含：

```python
{
    "documents": [content],       # verbatim 原文块
    "ids": [drawer_id],           # MD5 哈希 ID
    "metadatas": [{
        "wing": "shadow-folk",    # 项目/人物翼
        "room": "technical",      # 主题房间
        "source_file": "...",     # 来源文件路径
        "chunk_index": 0,         # 块序号
        "added_by": "mempalace",  # 写入代理
        "filed_at": "2026-04-03T04:22:41", # 归档时间
    }]
}
```

---

## 四、AAAK 压缩格式（有损摘要层）

AAAK（Adaptive Abbreviated Associative Keynotes）定义在 `dialect.py`，是一种**有损结构化摘要格式**——不是可逆压缩，而是用极少 token 捕获原文的关键信号。

### 4.1 AAAK 编码格式

```
头部:   WING|ROOM|DATE|SOURCE_STEM
正文:   0:ENTITIES|topic_keywords|"key_quote"|emotions|flags
隧道:   T:ZID<->ZID|label
情弧:   ARC:emotion->emotion->emotion
```

### 4.2 压缩流水线

`Dialect.compress()` 的五步提取：

```
原文 → ① 实体检测 → ② 主题提取 → ③ 关键句选取 → ④ 情绪检测 → ⑤ 标记检测
         ↓              ↓              ↓              ↓            ↓
       3字母码       频率+专名加权    句子评分截断    关键词匹配    关键词匹配
       ALC+BOB      mcp_http_auth   "key sent..."  determ+frust  DECISION+TECHNICAL
```

**实体编码**：已知实体用注册表映射（如 `Alice→ALC`），未知实体取首三字母大写：

```python
# dialect.py
def encode_entity(self, name):
    if name in self.entity_codes:
        return self.entity_codes[name]
    return name[:3].upper()  # Auto-code
```

**主题提取**：词频统计 + 专有名词/技术词加权：

```python
# dialect.py _extract_topics()
for w in words:
    if w[0].isupper():       # 首字母大写 +2 分
        freq[w_lower] += 2
    if "_" in w or "-" in w:  # 含下划线/连字符 +2 分
        freq[w_lower] += 2
ranked = sorted(freq.items(), key=lambda x: -x[1])
return [w for w, _ in ranked[:3]]
```

**关键句选取**：句子评分，偏好短句和含决策词的句子：

```python
# dialect.py _extract_key_sentence()
decision_words = {"decided", "because", "instead", "prefer", "switched",
                  "chose", "realized", "important", "key", "critical", ...}
# 短句加分
if len(s) < 80: score += 1
if len(s) < 40: score += 1
# 长句扣分
if len(s) > 150: score -= 2
# 截断到 55 字符
if len(best) > 55: best = best[:52] + "..."
```

**情绪和标记检测**：两组关键词表扫描，各取前 3 个：

```python
# 情绪信号: "decided"→determ, "worried"→anx, "excited"→excite
_EMOTION_SIGNALS = {"decided": "determ", "frustrated": "frust", ...}

# 标记信号: "decided"→DECISION, "api"→TECHNICAL, "breakthrough"→PIVOT
_FLAG_SIGNALS = {"decided": "DECISION", "api": "TECHNICAL", ...}
```

### 4.3 Token 估算

```python
# dialect.py
def count_tokens(text):
    words = text.split()
    return max(1, int(len(words) * 1.3))  # ~1.3 tokens/word
```

README 曾用 `len(text)//3` 导致压缩率虚高，已修正为基于词数的保守估计。

---

## 五、知识图谱（Temporal Knowledge Graph）

`knowledge_graph.py` 实现了一个基于 SQLite 的时序三元组图谱，与 ChromaDB 向量库并行运作：

```sql
-- 实体表
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'unknown',
    properties TEXT DEFAULT '{}'
);

-- 三元组表（带时间有效性）
CREATE TABLE triples (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT,      -- 事实生效时间
    valid_to TEXT,        -- 事实失效时间（NULL=仍然有效）
    confidence REAL DEFAULT 1.0,
    source_closet TEXT    -- 关联回 verbatim 原文
);
```

这让 MemPalace 能回答「某个事实在什么时间段为真」，例如：

```python
kg.add_triple("MCP", "uses", "stdio", valid_from="2026-03-01")
kg.invalidate("MCP", "uses", "stdio", ended="2026-03-30")
kg.add_triple("MCP", "uses", "HTTP", valid_from="2026-03-30")
# 查询：2026 年 4 月，MCP 用什么？→ HTTP
```

---

## 六、Palace Graph — 跨翼图遍历

`palace_graph.py` 在运行时从 ChromaDB 元数据构建内存图，不依赖外部图数据库：

```python
# palace_graph.py build_graph()
# 节点 = room, 属性 = {wings: set, halls: set, count: int}
# 边 = 同一 room 出现在多个 wing 时产生

for room, data in room_data.items():
    wings = sorted(data["wings"])
    if len(wings) >= 2:  # 跨翼 → 隧道
        for i, wa in enumerate(wings):
            for wb in wings[i+1:]:
                edges.append({"room": room, "wing_a": wa, "wing_b": wb})
```

**BFS 遍历**：从起始 room 出发，沿「共享 wing」边扩散：

```python
# palace_graph.py traverse()
# 邻居定义：与当前 room 共享至少一个 wing 的其他 room
frontier = [(start_room, 0)]
while frontier:
    current_room, depth = frontier.pop(0)
    for room, data in nodes.items():
        shared_wings = current_wings & set(data["wings"])
        if shared_wings:
            results.append({...})
```

**隧道发现**：`find_tunnels()` 找出连接两个指定 Wing 的共享 Room，实现跨域知识桥接。

---

## 七、MCP Server — AI 代理接入协议

`mcp_server.py` 暴露了一套 MCP 工具集，让 Claude Code 等 AI 代理能读写宫殿：

**读工具**：`mempalace_status`、`mempalace_search`、`mempalace_list_wings`、`mempalace_list_rooms`、`mempalace_get_taxonomy`

**写工具**：`mempalace_add_drawer`、`mempalace_delete_drawer`、`mempalace_diary_write`

**图谱工具**：`mempalace_kg_add`、`mempalace_kg_query`、`mempalace_kg_invalidate`

启动时注入的 `PALACE_PROTOCOL` 约束 AI 行为：

```
1. ON WAKE-UP: 调用 mempalace_status 加载宫殿概览
2. BEFORE RESPONDING: 先查宫殿，不猜测
3. IF UNSURE: 说"让我查一下"
4. AFTER SESSION: 写日记记录学到的东西
5. WHEN FACTS CHANGE: 失效旧事实 + 添加新事实
```

---

## 八、具体示例：用 ShadowFolk 数据走一遍完整流程

以下使用 `doc/SourceMem/` 中的真实 ShadowFolk 会话数据，演示一条记忆从产生到存储到检索的完整路径。

### 8.1 原始数据：一条 ShadowFolk Summary 记录

取 `shadowfolk_summaries_week.csv` 中 `id=1429` 的记录（已脱敏简化）：

```
request:    用户想排查为什么接入 ShadowFolk 的 HTTP MCP 认证仍然失败
learned:    关键发现是 401 的直接原因可能是 Authorization 缺少 Bearer 前缀；
            服务端 HTTP MCP 接口在开发环境已验证可正常工作
completed:  完成了 HTTP MCP 端点开发、部署与联调验证；
            将 MCP 接入方式从 stdio 切到 HTTP
next_steps: 建议直接抓取客户端实际发出的 Authorization 头确认是否带有 Bearer
meta_intent: 用户的深层目的是让 Cursor 通过 HTTP 方式稳定接入远端 MCP 服务
```

以及对应的 Observation 记录（`id=12248`）：

```
type:  debugging
title: 定位到 401 原因是 Authorization 缺少 Bearer 前缀
facts:
  - 截图中的 Authorization 配置未包含 Bearer 前缀
  - 服务端 validateApiToken 会先检查是否以 'Bearer ' 开头
  - 若缺少该前缀，validateApiToken 返回 null，导致持续 401
  - 修复方法是补上 Bearer 前缀
concepts: authorization-header, bearer-token, 401-unauthorized, validateApiToken
```

### 8.2 Step 1 — Mine：将原始记忆切块入宫

假设将 ShadowFolk 的对话记录放在一个目录中，执行：

```bash
mempalace mine ~/convos/shadow-folk --mode convos
```

`convo_miner.py` 会：

1. **确定 Wing**：目录名 `shadow-folk` → `wing = "shadow_folk"`
2. **Normalize**：`normalize.py` 将聊天格式统一
3. **Chunk**：`chunk_exchanges()` 按问答对分块
4. **Detect Room**：`detect_convo_room()` 对前 3000 字符扫描关键词

以上面的 Summary 文本为例，关键词评分结果：

```python
# convo_miner.py TOPIC_KEYWORDS 扫描
"technical": ["code","api","server","deploy","debug","test"] → 命中 api,server,deploy → 3分
"problems":  ["problem","issue","failed","fix","solved"]     → 命中 failed → 1分
"decisions": ["decided","switched","migrated","approach"]    → 命中 switched → 1分
# 最高分 → room = "technical"
```

1. **写入 ChromaDB**：

```python
collection.add(
    documents=["用户想排查为什么接入 ShadowFolk 的 HTTP MCP 认证仍然失败..."],
    ids=["drawer_shadow_folk_technical_a3f8e2b1c4d57069"],
    metadatas=[{
        "wing": "shadow_folk",
        "room": "technical",
        "source_file": "session_1429.txt",
        "chunk_index": 0,
        "added_by": "mempalace",
        "filed_at": "2026-04-03T04:22:41",
        "ingest_mode": "convos",
        "extract_mode": "exchange",
    }]
)
```

如果使用 General 模式（`--extract general`），`general_extractor.py` 会从中提取出：


| 类型        | 匹配的 Marker                  | 提取内容                                    |
| --------- | --------------------------- | --------------------------------------- |
| DECISION  | `switched to`, `instead of` | "将 MCP 接入方式从 stdio 切到 HTTP"             |
| PROBLEM   | `failed`, `fix`             | "401 的直接原因是 Authorization 缺少 Bearer 前缀" |
| MILESTONE | `完成了...验证`                  | "完成了 HTTP MCP 端点开发、部署与联调验证"             |


### 8.3 Step 2 — AAAK Compress：有损摘要

对该 Drawer 执行 `mempalace compress`，`Dialect.compress()` 会：

```python
dialect = Dialect()
text = """用户想排查为什么接入 ShadowFolk 的 HTTP MCP 认证仍然失败，
即使已经给请求头加了 Bearer。关键发现是此前 401 的直接原因确实可能是
Authorization 缺少 Bearer 前缀；服务端 HTTP MCP 接口在开发环境已验证
可正常工作。完成了 HTTP MCP 端点开发、部署与联调验证；将 MCP 接入方式
从 stdio 切到 HTTP。"""

compressed = dialect.compress(text, metadata={
    "wing": "shadow_folk",
    "room": "technical",
    "date": "2026-04-03",
    "source_file": "session_1429.txt"
})
```

五步提取结果：


| 步骤    | 提取方法                  | 结果                                          |
| ----- | --------------------- | ------------------------------------------- |
| ① 实体  | 大写词检测 → 首三字母          | `SHA+HTT+MCP`（ShadowFolk, HTTP, MCP）        |
| ② 主题  | 词频+专名加权               | `mcp_http_bearer`                           |
| ③ 关键句 | 句子评分，偏好短决策句           | `"Authorization 缺少 Bearer 前缀"`              |
| ④ 情绪  | `_EMOTION_SIGNALS` 匹配 | `determ`（"decided/switched"→ determination） |
| ⑤ 标记  | `_FLAG_SIGNALS` 匹配    | `DECISION+TECHNICAL`                        |


**AAAK 输出**：

```
shadow_folk|technical|2026-04-03|session_1429
0:SHA+HTT+MCP|mcp_http_bearer|"Authorization 缺少 Bearer 前缀"|determ|DECISION+TECHNICAL
```

**原文约 120 词 → AAAK 约 15 词，压缩比约 8x（有损）。**

### 8.4 Step 3 — Wake-up：启动加载

当 AI 代理开始新会话时，`MemoryStack.wake_up()` 执行：

```
=== L0: Identity ===
我是 ShadowFolk AI 助手，帮助 minusjiang 管理开发项目记忆。
追踪项目包括: shadow-folk, tank-game, memory-research

=== L1: Essential Story ===
## L1 — ESSENTIAL STORY

[technical]
  - 用户想排查为什么接入 ShadowFolk 的 HTTP MCP 认证仍然失败，
    即使已经给请求头加了 Bearer。关键发现是 401 的直接原因...  (session_1429.txt)
  - 远端 CVM 上的 stdio MCP 方案已被 HTTP MCP 直连模式替代...  (session_1376.txt)

[decisions]
  - 采用无状态 /api/mcp JSON 模式更合理，避免额外 SSE 改动...  (session_1331.txt)

[problems]
  - 日志中大量 GET /api/mcp 是 Cursor SSE 轮询探测噪音...      (observation.txt)
```

### 8.5 Step 4 — Search：语义检索

用户问「之前 MCP 鉴权失败是什么原因？」时，触发 L3 深搜：

```python
stack.search("MCP 鉴权失败原因", wing="shadow_folk")
```

ChromaDB 语义匹配返回：

```
[1] shadow_folk / technical (sim=0.892)
    用户想排查为什么接入 ShadowFolk 的 HTTP MCP 认证仍然失败...
    src: session_1429.txt

[2] shadow_folk / technical (sim=0.856)
    定位到 401 原因是 Authorization 缺少 Bearer 前缀...
    src: observation_12248.txt

[3] shadow_folk / decisions (sim=0.731)
    采用无状态 HTTP JSON 模式更合理...
    src: session_1331.txt
```

### 8.6 Step 5 — Graph Traverse：跨翼发现

假设 `tank-game` 翼下也有 `room="technical"` 的 Drawer（关于 UE 开发环境配置），则 `palace_graph.py` 会发现隧道：

```python
find_tunnels(wing_a="shadow_folk", wing_b="tank_game")
# → [{"room": "technical", "wings": ["shadow_folk", "tank_game"], "count": 42}]
```

这意味着「技术调试」这个主题在两个项目间有知识桥梁——MCP 联调的经验可能对 tank 项目的工具链配置也有参考价值。

### 8.7 Step 6 — Knowledge Graph：时序事实

用 MCP 工具将关键事实写入知识图谱：

```python
kg.add_triple("MCP", "uses", "stdio", valid_from="2026-03-01")
kg.add_triple("MCP", "uses", "HTTP_JSON", valid_from="2026-03-30")
kg.invalidate("MCP", "uses", "stdio", ended="2026-03-30")

kg.add_triple("401_error", "caused_by", "missing_Bearer_prefix",
              valid_from="2026-04-03")
kg.add_triple("401_error", "resolved_by", "adding_Bearer_prefix",
              valid_from="2026-04-03")
```

后续查询 `kg.query_entity("MCP", as_of="2026-04-05")` → 只返回 `uses → HTTP_JSON`，旧的 stdio 事实已失效。

---

## 九、协作项目示例：两位开发者的 DPAR 构建记忆

以下使用 DPAR 项目的真实 Session Summary 数据，展示 MemPalace 如何处理**多人协作**场景——两位开发者（ziyadyao 和 hughesli）在同一个 UE4 构建项目上的工作记录，如何被分别入宫、跨翼关联、最终形成可检索的团队记忆。

### 9.1 原始数据：两个人的 Session Summary DB

数据来源是两个 SQLite 数据库文件，每个文件记录一位开发者的会话摘要：

```
dpar-mem/
├── dpar-hughesli.db    ← hughesli 的 3 条 session summary
└── dpar-ziyad.db       ← ziyadyao 的 5 条 session summary
```

每个 DB 的 `memories` 表结构（`content_type = 'session_summary'`）：

```sql
id | user_id   | content (TEXT)                    | created_at
---+-----------+-----------------------------------+------------------------
 1 | hughesli  | "我完成了DPAR流水线从QuickGenAPP到..."  | 2026-03-25T07:08:57.222Z
 2 | hughesli  | "我在DPAR集成项目中完成了以下关键工作..."    | 2026-03-27T07:54:58.672Z
...
```

**8 条 summary 的时间线**（3/23 ~ 4/03）：

```
时间轴  ├── 3/23 ──┤── 3/25 ──┤── 3/26 ──┤── 3/27 ──┤── 3/28 ──┤── 4/02 ──┤── 4/03 ──┤
ziyadyao:  ■ SVN修复   │          ■ 资产迁移   │          ■ 关卡搭建   ■■ 构建重构  │
           │ +AI配置    │          │ +Python   │          │          │ +打包排查  │
hughesli:  │          ■ 流水线     │          ■ 11项      │          │          ■ 全流程
           │          │ 转换      │          │ 集成修复    │          │          │ 理解
```

> **核心问题**：这 8 条记忆，MemPalace 怎么处理？

---

### 9.2 数据准备：从 DB 导出到文件目录

MemPalace 不直接读 SQLite。需要先将 session summary 导出为文本文件，按人组织目录：

```python
import sqlite3
from pathlib import Path

for db_name, user in [("dpar-ziyad.db", "ziyad"), ("dpar-hughesli.db", "hughesli")]:
    conn = sqlite3.connect(f"dpar-mem/{db_name}")
    rows = conn.execute(
        "SELECT content, created_at FROM memories WHERE content_type='session_summary' ORDER BY created_at"
    ).fetchall()
    out_dir = Path(f"dpar-convos/{user}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (content, ts) in enumerate(rows):
        date = ts[:10]
        (out_dir / f"session_{date}_{i}.txt").write_text(content, encoding="utf-8")
    conn.close()
```

导出后的目录结构：

```
dpar-convos/
├── ziyad/
│   ├── session_2026-03-23_0.txt    (SVN修复+AI配置)
│   ├── session_2026-03-26_1.txt    (资产迁移+Python脚本)
│   ├── session_2026-03-28_2.txt    (关卡场景搭建)
│   ├── session_2026-04-02_3.txt    (构建流程重构)
│   └── session_2026-04-02_4.txt    (打包排查+工具调研)
└── hughesli/
    ├── session_2026-03-25_0.txt    (流水线转换)
    ├── session_2026-03-27_1.txt    (11项集成修复)
    └── session_2026-04-03_2.txt    (全流程理解)
```

---

### 9.3 路径一：convo_miner 默认处理与中文关键词限制

最直观的做法是对每个人的目录执行 `mine_convos`：

```bash
mempalace mine-convos dpar-convos/ziyad     --palace ~/.mempalace/palace
mempalace mine-convos dpar-convos/hughesli  --palace ~/.mempalace/palace
```

**Step 1 — Wing 分配**：

```python
# convo_miner.py L273-274
wing = convo_path.name.lower().replace(" ", "_").replace("-", "_")
# "ziyad" → wing = "ziyad"
# "hughesli" → wing = "hughesli"
```

**Step 2 — 分块**：

Session summary 是单段纯文本（无 `>` 标记），`chunk_exchanges()` 走 `_chunk_by_paragraph` fallback。每条 summary 长度约 150~500 字符，均 > `MIN_CHUNK_SIZE(30)` 且无 `\n\n` 分隔，所以每条 summary = 一个 Chunk：

```python
# convo_miner.py L104-122
# paragraphs = [整条summary]，len(paragraphs)=1，content.count("\n") < 20
# → 不走 25 行分组，直接作为单个段落 chunk
# 8 条 summary → 8 个 Drawer
```

**Step 3 — Room 检测（关键问题）**：

`detect_convo_room()` 用英文关键词桶扫描文本前 3000 字符。对中文为主的 session summary，匹配情况如下：

| Summary | 文本语言 | 英文关键词命中 | 得分 | Room |
|---------|---------|-------------|------|------|
| ziyad 3/23 | 中文+少量英文 | `"api"` (API平台), `"function"` (MaterialFunction) | technical=2 | **technical** |
| ziyad 3/26 | 中文+少量英文 | `"python"` (Unreal Python), `"git"` (Git忽略规则) | technical=2 | **technical** |
| ziyad 3/28 | 中文+极少英文 | 无匹配 (Lua, json 均不在关键词表中) | 全0 | **general** |
| ziyad 4/02AM | 中文+少量英文 | `"python"` (Python化流程) | technical=1 | **technical** |
| ziyad 4/02PM | 中文+少量英文 | `"code"` (Claude Code) | technical=1 | **technical** |
| hughes 3/25 | 中文+UE术语 | 无匹配 (Cook, SVN, Blueprint 均不在关键词表) | 全0 | **general** |
| hughes 3/27 | 中文+UE术语 | 无匹配 (PsExec, INI, YAML 均不在关键词表) | 全0 | **general** |
| hughes 4/03 | 中文+UE术语 | 无匹配 (Cook, Pak, Puffer 均不在关键词表) | 全0 | **general** |

**结果**：ziyadyao 的 4 条进了 `technical`，1 条进了 `general`；hughesli 的 3 条全部落入 `general`。

**问题分析**：`TOPIC_KEYWORDS` 完全是英文关键词（`"code"`, `"python"`, `"fix"`, `"deploy"` 等），对中文为主的文本几乎无法触发有效匹配。hughesli 的文本虽然技术含量极高（流水线修复、Cook 机制、蓝图编译），但关键术语如"修复"、"问题"、"构建"均为中文，不匹配 `"fix"`、`"problem"`、`"deploy"` 等英文关键词。

> **核心发现**：MemPalace 的 Room 路由对**非英文内容**有先天盲区——这是纯正则/关键词方案的固有局限。

---

### 9.4 路径二：project miner + 自定义 Room（推荐方案）

解决中文关键词问题的正确方式是用 `miner.py` 的**项目挖掘**模式——通过 `mempalace.yaml` 自定义中文 Room 和关键词：

```yaml
# dpar-convos/mempalace.yaml
wing: dpar
rooms:
  - name: build_pipeline
    description: "构建流水线、Cook流程、打包出包"
    keywords: [构建, 流水线, Cook, pak, 打包, 出包, 编译, pipeline, Build]
  - name: asset_management
    description: "资产迁移、SVN管理、引用修复"
    keywords: [资产, 迁移, SVN, 引用, uasset, 修复脚本, PublicAssets]
  - name: debugging
    description: "问题排查、Bug修复、崩溃分析"
    keywords: [排查, 问题, 修复, 崩溃, 异常, 失败, 报错, 修复]
  - name: level_design
    description: "关卡设计、场景搭建、地图配置"
    keywords: [关卡, 场景, 地图, 搭建, 布局, 坦克, 竞技场]
  - name: toolchain
    description: "开发工具、CI/CD、脚本自动化"
    keywords: [脚本, Python, 自动化, YAML, wrapper, 流程重构]
```

但此时两个人的数据在同一个 Wing `dpar` 下，无法利用跨翼隧道。更好的方式是**每人一个目录 + 各自的 yaml**：

```
dpar-convos/
├── ziyad/
│   ├── mempalace.yaml        ← wing: dpar_ziyad, rooms: [同上]
│   └── *.txt
└── hughesli/
    ├── mempalace.yaml        ← wing: dpar_hughesli, rooms: [同上]
    └── *.txt
```

执行：

```bash
mempalace mine dpar-convos/ziyad     --palace ~/.mempalace/palace
mempalace mine dpar-convos/hughesli  --palace ~/.mempalace/palace
```

**Room 检测过程**（`miner.py` 的 `detect_room()`，走内容关键词评分）：

以 hughesli 3/25 为例，对前 2000 字符扫描自定义关键词命中：

```python
# miner.py detect_room() — Priority 3: keyword scoring
content_lower = "我完成了dpar流水线从quickgenapp到cookpak模式的转换，解决了多个关键构建问题..."

scores = {
    "build_pipeline": 0,  # 待计算
    "asset_management": 0,
    "debugging": 0,
    ...
}

# build_pipeline keywords: [构建, 流水线, Cook, pak, 打包, 出包, 编译, pipeline, Build]
# "构建" → count("构建")=1, "流水线" → count=1, "cook" → count=3(Cook×3),
# "pak" → count=1(cookpak), "编译" → count=1
# → build_pipeline = 7

# asset_management keywords: [资产, 迁移, SVN, 引用, ...]
# "svn" → count=1, "资产" → count=0(不在此文中)... 
# → asset_management = 1

# debugging keywords: [排查, 问题, 修复, ...]
# "问题" → count=2, "修复" → count=3
# → debugging = 5

# 最高分 build_pipeline=7 → room = "build_pipeline"
```

所有 8 条 summary 的 Room 分配结果：

| Summary | build_pipeline | asset_mgmt | debugging | level_design | toolchain | **→ Room** |
|---------|:-:|:-:|:-:|:-:|:-:|---|
| ziyad 3/23 (SVN修复+AI配置) | 0 | 3 (SVN,引用,修复脚本) | 2 (排查,问题) | 0 | 1 (脚本) | **asset_management** |
| ziyad 3/26 (资产迁移) | 0 | 4 (资产,迁移,SVN×2) | 0 | 0 | 1 (脚本) | **asset_management** |
| ziyad 3/28 (关卡搭建) | 0 | 0 | 2 (排查,问题) | 5 (关卡,场景,地图,搭建,竞技场) | 0 | **level_design** |
| ziyad 4/02AM (构建重构) | 3 (构建,流水线,打包) | 0 | 1 (排查) | 0 | 3 (脚本,Python,流程重构) | **build_pipeline** ★ |
| ziyad 4/02PM (打包排查) | 2 (打包,pak) | 1 (资产) | 2 (排查,问题) | 0 | 0 | **build_pipeline** ★ |
| hughes 3/25 (流水线转换) | 7 (构建,流水线,Cook×3,pak,编译) | 1 (SVN) | 5 (问题,修复×3,报错) | 0 | 0 | **build_pipeline** ★ |
| hughes 3/27 (11项修复) | 4 (构建,Cook,pak,编译) | 2 (资产,SVN) | 8 (问题×3,修复×4,崩溃) | 0 | 1 (脚本) | **debugging** |
| hughes 4/03 (全流程理解) | 6 (构建,Cook×4,pak) | 2 (资产,PublicAssets) | 2 (问题,失败) | 0 | 0 | **build_pipeline** ★ |

> ★ 标记表示在 `build_pipeline` 这个 Room 中，**两个 Wing 都有 Drawer**——这正是隧道形成的条件。

---

### 9.5 分块与入库：ChromaDB Drawer 写入

`miner.py` 的 `chunk_text()` 使用 800 字符滑动窗口。大部分 session summary 长度 < 800 字符，直接作为单个 Drawer。hughesli 3/27 的内容约 650 字符，也是单块。

8 个 Drawer 写入 ChromaDB `mempalace_drawers` 集合：

```python
# 以 hughesli 3/25 为例
collection.add(
    documents=["我完成了DPAR流水线从QuickGenAPP到cookpak模式的转换，解决了多个关键构建问题..."],
    ids=["drawer_dpar_hughesli_build_pipeline_7a2e3f4b8c1d9056"],
    metadatas=[{
        "wing": "dpar_hughesli",
        "room": "build_pipeline",
        "source_file": "dpar-convos/hughesli/session_2026-03-25_0.txt",
        "chunk_index": 0,
        "added_by": "mempalace",
        "filed_at": "2026-04-08T10:00:00",
    }]
)

# 以 ziyad 4/02AM 为例
collection.add(
    documents=["我对比了流水线YAML构建逻辑与本地已跑通的构建脚本..."],
    ids=["drawer_dpar_ziyad_build_pipeline_e5f1a2b3c4d67890"],
    metadatas=[{
        "wing": "dpar_ziyad",
        "room": "build_pipeline",
        "source_file": "dpar-convos/ziyad/session_2026-04-02_3.txt",
        "chunk_index": 0,
        "added_by": "mempalace",
        "filed_at": "2026-04-08T10:00:01",
    }]
)
```

完成后宫殿状态：

```
  MemPalace Status — 8 drawers

  WING: dpar_hughesli
    ROOM: build_pipeline          2 drawers   (3/25 流水线转换, 4/03 全流程理解)
    ROOM: debugging               1 drawers   (3/27 11项集成修复)

  WING: dpar_ziyad
    ROOM: asset_management        2 drawers   (3/23 SVN修复, 3/26 资产迁移)
    ROOM: build_pipeline          2 drawers   (4/02AM 构建重构, 4/02PM 打包排查)
    ROOM: level_design            1 drawers   (3/28 关卡搭建)
```

---

### 9.6 跨翼隧道：两位开发者的知识桥接

`palace_graph.py` 的 `build_graph()` 扫描所有 Drawer 的元数据，发现：

```python
room_data = {
    "build_pipeline": {
        "wings": {"dpar_hughesli", "dpar_ziyad"},  # ← 两个 wing！
        "count": 4,
    },
    "asset_management": {
        "wings": {"dpar_ziyad"},  # 只有一个 wing
        "count": 2,
    },
    "debugging": {
        "wings": {"dpar_hughesli"},
        "count": 1,
    },
    "level_design": {
        "wings": {"dpar_ziyad"},
        "count": 1,
    },
}
```

`build_pipeline` 同时出现在 `dpar_hughesli` 和 `dpar_ziyad` 两个 Wing 中 → **形成隧道**：

```python
# palace_graph.py L68-84
edges = [
    {
        "room": "build_pipeline",
        "wing_a": "dpar_hughesli",
        "wing_b": "dpar_ziyad",
        "hall": "",
        "count": 4,
    }
]
```

宫殿图的可视化结构：

```
┌───── Wing: dpar_ziyad ──────┐     ┌──── Wing: dpar_hughesli ───┐
│                              │     │                             │
│  Room: asset_management (2)  │     │                             │
│  ┌────────────────────────┐  │     │                             │
│  │ 3/23 SVN修复+AI配置     │  │     │                             │
│  │ 3/26 资产迁移+Python    │  │     │                             │
│  └────────────────────────┘  │     │                             │
│                              │     │                             │
│  Room: build_pipeline (2)  ══╪═════╪══ Room: build_pipeline (2)  │
│  ┌────────────────────────┐  │ 隧道 │  ┌─────────────────────────┐│
│  │ 4/02 构建流程重构        │  │     │  │ 3/25 流水线转换           ││
│  │ 4/02 打包排查           │  │     │  │ 4/03 全流程理解           ││
│  └────────────────────────┘  │     │  └─────────────────────────┘│
│                              │     │                             │
│  Room: level_design (1)      │     │  Room: debugging (1)        │
│  ┌────────────────────────┐  │     │  ┌─────────────────────────┐│
│  │ 3/28 关卡场景搭建       │  │     │  │ 3/27 11项集成修复        ││
│  └────────────────────────┘  │     │  └─────────────────────────┘│
└──────────────────────────────┘     └─────────────────────────────┘
```

> **隧道的含义**：`build_pipeline` 是两位开发者的**共同知识域**——hughesli 理解了 Cook/Pak 的完整机制，ziyadyao 重构了 Python 化构建流程。当有人问「DPAR 构建流程怎么走」时，隧道确保**两个人的经验都能被检索到**。

通过 BFS 遍历（`traverse("build_pipeline")`），从 `build_pipeline` 出发可发现：

```python
traverse("build_pipeline", max_hops=2)
# [
#   {"room": "build_pipeline", "wings": ["dpar_hughesli","dpar_ziyad"], "hop": 0},
#   {"room": "asset_management", "wings": ["dpar_ziyad"], "hop": 1, "connected_via": ["dpar_ziyad"]},
#   {"room": "level_design", "wings": ["dpar_ziyad"], "hop": 1, "connected_via": ["dpar_ziyad"]},
#   {"room": "debugging", "wings": ["dpar_hughesli"], "hop": 1, "connected_via": ["dpar_hughesli"]},
# ]
```

从 `build_pipeline` 一跳就能到达所有其他 Room——因为 4 个 Room 分别只属于两个 Wing 之一，而 `build_pipeline` 横跨两个 Wing，充当了整个图的枢纽。

---

### 9.7 四层检索：在协作记忆中搜索

**L1 Wake-up**（启动加载）：

```python
stack = MemoryStack(palace_path="~/.mempalace/palace")
print(stack.wake_up())
```

输出（取 Top 15 Drawer，按 room 分组，每条截断 200 字符）：

```
## L1 — ESSENTIAL STORY

[asset_management]
  - 我完成了 SVN 资产引用修复脚本的增强（v3），新增了 MaterialFunction 类型的专门处理逻辑；
    分析了 dry run 报告中 607 个 uasset 的断裂引用分布；配置了 Continue AI 代码助手并对接第三方
    代理 API...  (session_2026-03-23_0.txt)
  - 我在资产迁移领域进行了大量工作：使用 Unreal Python 开发依赖校验和抽查脚本，验证了公共资产的
    完整性；完成了 607 个迁移资产及其依赖路径修复的 SVN 提交...  (session_2026-03-26_1.txt)

[build_pipeline]
  - 我完成了DPAR流水线从QuickGenAPP到cookpak模式的转换，解决了多个关键构建问题：修复了SVN认证和
    路径不匹配问题；通过dpar_build_wrapper.py容错Blueprint编译错误...  (session_2026-03-25_0.txt)
  - 我对比了流水线YAML构建逻辑与本地已跑通的构建脚本，发现现有流水线DPAR更像内联脚本硬编码方案...
    (session_2026-04-02_3.txt)
  ... (more in L3 search)
```

**L3 Deep Search**（语义搜索）：

```python
stack.search("DPAR Cook失败是什么原因", wing="dpar_hughesli")
```

```
## L3 — SEARCH RESULTS for "DPAR Cook失败是什么原因"

[1] dpar_hughesli/build_pipeline (sim=0.887)
    我深入理解了DPAR项目的完整构建流程：主Cook处理/Game/路径资源（NeverCook排除PublicAssets），
    DPAR Cook单独处理PublicAssets并打成dpar_*.pak通过Puffer分发。发现了PublicAssets挂载时序
    导致的核心问题——3/30全量Cook时挂载点未注册...
    src: session_2026-04-03_2.txt

[2] dpar_hughesli/build_pipeline (sim=0.841)
    我完成了DPAR流水线从QuickGenAPP到cookpak模式的转换，解决了多个关键构建问题：修复了SVN认证
    和路径不匹配问题...发现并解决了Cook过程覆写PublicAssets源文件导致HotShaderDivide报
    "Package is too old"的问题...
    src: session_2026-03-25_0.txt

[3] dpar_hughesli/debugging (sim=0.762)
    我在DPAR集成项目中完成了以下关键工作...(7)重构资产扫描和批量Cook过程，
    解决命令行参数超长限制...
    src: session_2026-03-27_1.txt
```

**跨 Wing 搜索**（不指定 wing，搜索所有人的记忆）：

```python
stack.search("DPAR 打包流程优化")
```

```
[1] dpar_ziyad/build_pipeline (sim=0.901)
    我对比了流水线YAML构建逻辑与本地已跑通的构建脚本，发现现有流水线DPAR更像内联脚本硬编码方案，
    确定了用本地Python化流程替换的优化方向...
    src: session_2026-04-02_3.txt

[2] dpar_hughesli/build_pipeline (sim=0.873)
    我深入理解了DPAR项目的完整构建流程：主Cook处理/Game/路径资源...
    src: session_2026-04-03_2.txt

[3] dpar_ziyad/build_pipeline (sim=0.845)
    我排查了DPAR打包流程中资产未被打进pak的问题...
    src: session_2026-04-02_4.txt
```

> 不限定 Wing 时，两位开发者关于构建优化的经验会**交叉出现**，语义检索自动将最相关的内容排在前面。

---

### 9.8 知识图谱：时序事实链

从两位开发者的记忆中，可提取出关于 DPAR 构建流程演变的时序事实：

```python
kg = KnowledgeGraph()

# 构建模式演变（来自 hughesli 3/25 + ziyad 4/02）
kg.add_triple("DPAR流水线", "uses", "QuickGenAPP",
              valid_from="2026-03-01", source_closet="session_2026-03-25_0")
kg.invalidate("DPAR流水线", "uses", "QuickGenAPP", ended="2026-03-25")
kg.add_triple("DPAR流水线", "uses", "cookpak模式",
              valid_from="2026-03-25", source_closet="session_2026-03-25_0")
kg.add_triple("DPAR流水线", "uses", "Python化本地流程",
              valid_from="2026-04-02", source_closet="session_2026-04-02_3")

# Cook 故障链（来自 hughesli 4/03）
kg.add_triple("PublicAssets挂载点", "status", "未注册",
              valid_from="2026-03-30", valid_to="2026-04-03",
              source_closet="session_2026-04-03_2")
kg.add_triple("增量Cook缓存", "contains", "失败BP记录",
              valid_from="2026-03-30", valid_to="2026-04-03",
              source_closet="session_2026-04-03_2")
kg.add_triple("DevelopmentAssetRegistry.bin", "deleted_to_fix", "Cook缓存",
              valid_from="2026-04-03",
              source_closet="session_2026-04-03_2")

# 资产迁移里程碑（来自 ziyad 3/26）
kg.add_triple("607个迁移资产", "status", "引用修复完成+SVN提交",
              valid_from="2026-03-26",
              source_closet="session_2026-03-26_1")
```

查询示例：

```python
# 2026-04-05 时，DPAR流水线用什么构建模式？
kg.query_entity("DPAR流水线", as_of="2026-04-05")
# → [("uses", "cookpak模式", valid_from="3/25"),
#    ("uses", "Python化本地流程", valid_from="4/02")]
# QuickGenAPP 已失效，不再返回

# 公共资产挂载问题修复了吗？
kg.query_entity("PublicAssets挂载点", as_of="2026-04-05")
# → [] (valid_to="2026-04-03"，已超过有效期，说明问题已解决)
```

---

### 9.9 小结：多人协作场景的关键要点

1. **Wing = 人**：每位开发者一个 Wing（`dpar_ziyad`, `dpar_hughesli`），这是最自然的多人协作建模方式
2. **自定义 Room 不可少**：对非英文内容，必须通过 `mempalace.yaml` 定义中文关键词，否则 `convo_miner` 的内置英文关键词表会导致大部分记忆落入 `general`
3. **隧道 = 共同知识域**：`build_pipeline` 成为连接两位开发者的隧道，代表了他们的共享专业领域
4. **语义搜索天然跨人**：不指定 Wing 时，ChromaDB 的向量检索自动将两人最相关的记忆混合排序
5. **知识图谱追踪演变**：将两人的发现串成时序链，可回答「某个时间点，构建流程处于什么状态」

---

## 十、与其他记忆系统的设计对比


| 维度         | MemPalace                      | Mem0       | Graphiti / CodeBuddy-Mem |
| ---------- | ------------------------------ | ---------- | ------------------------ |
| **存储**     | ChromaDB + SQLite（纯本地）         | 云端向量库      | Neo4j 图数据库（云/本地）         |
| **LLM 依赖** | 无（纯正则/启发式）                     | 需要 LLM 抽取  | 需要 LLM 抽取实体与关系           |
| **压缩**     | AAAK 有损符号摘要                    | LLM 摘要     | 图节点 + Episode 结构         |
| **时序**     | SQLite triples 的 valid_from/to | 无原生时序      | Episode 时序 + 边权重衰减       |
| **检索**     | 四层栈（身份→摘要→过滤→语义搜索）             | 向量相似度      | 图遍历 + 向量搜索混合             |
| **空间隐喻**   | Wing/Room/Drawer/Tunnel        | 无          | 无                        |
| **成本**     | 零                              | 按 API 调用计费 | Neo4j 云版按量计费             |


MemPalace 的核心差异化在于：

1. **完全本地，零 API 调用** — 所有 NLP 逻辑靠正则和启发式实现
2. **空间隐喻组织** — 不只是「存储 + 检索」，还有「宫殿 + 图遍历」的导航维度
3. **四层按需加载** — 启动只消耗数百 token，95%+ 上下文留给对话
4. **AAAK 格式** — 专为 LLM 设计的符号摘要，无需解码器

---

## 十一、关键源码文件索引


| 文件                     | 核心职责                         | 关键行                                                       |
| ---------------------- | ---------------------------- | --------------------------------------------------------- |
| `config.py`            | 配置管理、默认 Wing/Hall/Collection | L11-12: 默认路径与集合名                                          |
| `miner.py`             | 项目文件挖掘、分块、Room 路由            | L301-340: `detect_room`; L348-388: `chunk_text`           |
| `convo_miner.py`       | 对话挖掘、问答对分块                   | L54-122: `chunk_exchanges`; L196-206: `detect_convo_room` |
| `general_extractor.py` | 5 类记忆提取（纯正则）                 | L30-52: `DECISION_MARKERS`                                |
| `layers.py`            | 四层记忆栈、wake-up                | L83-84: L1 硬限; L389-408: `wake_up`                        |
| `dialect.py`           | AAAK 压缩编码                    | L545-607: `compress`; L436-461: `_extract_topics`         |
| `searcher.py`          | ChromaDB 语义搜索封装              | L21-52: `search`; L93-152: `search_memories`              |
| `palace_graph.py`      | 宫殿图构建、BFS 遍历、隧道发现            | L33-96: `build_graph`; L99-158: `traverse`                |
| `knowledge_graph.py`   | SQLite 时序三元组                 | L55-85: 建表; L99+: CRUD                                    |
| `mcp_server.py`        | MCP 工具接口、协议定义                | L93-119: `PALACE_PROTOCOL` + `AAAK_SPEC`                  |
| `normalize.py`         | 多源聊天格式统一                     | —                                                         |


---

## 十二、总结

MemPalace 的设计理念是「存储不等于记忆，存储 + 协议 = 记忆」。它不追求用 LLM 做复杂的记忆抽取，而是用极简的正则/启发式方法把原文 verbatim 存储到一个有空间结构的本地向量库中，配合四层按需加载 + 图遍历 + 时序图谱，在启动时只消耗数百 token 就能让 AI 代理拥有跨会话的持久记忆。

其核心取舍是：**用信息保真度换取零外部依赖**。所有 NLP 逻辑都是本地正则匹配，这意味着 Room 路由和 AAAK 压缩的精度不如 LLM-based 方案，但完全不需要 API Key、不需要网络、不需要付费——这在隐私敏感和离线场景下是显著优势。