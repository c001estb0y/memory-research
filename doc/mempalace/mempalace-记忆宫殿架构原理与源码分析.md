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

## 九、与其他记忆系统的设计对比


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

## 十、关键源码文件索引


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

## 十一、总结

MemPalace 的设计理念是「存储不等于记忆，存储 + 协议 = 记忆」。它不追求用 LLM 做复杂的记忆抽取，而是用极简的正则/启发式方法把原文 verbatim 存储到一个有空间结构的本地向量库中，配合四层按需加载 + 图遍历 + 时序图谱，在启动时只消耗数百 token 就能让 AI 代理拥有跨会话的持久记忆。

其核心取舍是：**用信息保真度换取零外部依赖**。所有 NLP 逻辑都是本地正则匹配，这意味着 Room 路由和 AAAK 压缩的精度不如 LLM-based 方案，但完全不需要 API Key、不需要网络、不需要付费——这在隐私敏感和离线场景下是显著优势。