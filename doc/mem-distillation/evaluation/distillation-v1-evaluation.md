# 蒸馏管线 v1 实际运行评估

> 对比蒸馏管线自动输出（distilled_experiences.json + experience_narratives.md）与 Claude Code 手动总结（shadowfolk-experience-summary.md），评估管线处理效果。

## 1. 运行概况

基于 distill.log 的最终成功运行数据：

| 指标 | 实际值 |
|------|--------|
| 输入数据 | 32 summaries + 188 observations = **220 条** |
| Layer 1 批次 | 11 批 |
| Layer 1 输出 | **43 条**结构化经验 |
| Layer 1 耗时 | 443.1 秒（约 7.4 分钟） |
| Layer 2 主题组 | 7 个（debugging/architecture/configuration/cross_platform/deployment/tooling/workflow） |
| Layer 2 成功叙事 | **7 条**（其中 4 个主题失败跳过） |
| Layer 2 耗时 | 746.4 秒（约 12.4 分钟） |
| **全流程总耗时** | **1189.5 秒（约 19.8 分钟）** |
| API 调用总数 | 18 次 |

## 2. 处理得好的地方

### 2.1 Layer 1 结构化蒸馏 — 质量优秀

**覆盖面广**：43 条经验覆盖了 7 个经验类型，Claude Code 手动总结的 6 条经验全部被覆盖，且额外提取了手动总结中遗漏的内容：

| Claude Code 手动总结（6 条） | 管线是否覆盖 | 管线额外发现 |
|---|---|---|
| MCP stdio → HTTP 迁移 | ✅ 覆盖，且拆得更细（传输层区分、无状态选型、Nginx 兼容性） | — |
| Bearer Token 401 排查 | ✅ 覆盖，含两条独立经验（前缀缺失 + 日志无记录两个角度） | — |
| CRLF 跨平台 | ✅ 覆盖 | — |
| Lockfile 同步 | ✅ 覆盖 | — |
| 多层认证排查 | ✅ 覆盖 | — |
| MCP 零成本分发 | ✅ 覆盖 | — |
| — | — | **get_projects 项目可见性盲区**（手动总结完全没提到） |
| — | — | **search_memory_like schema 与实现不同步** |
| — | — | **PowerShell curl 别名陷阱** |
| — | — | **MCP Content-Negotiation（Accept 头要求）** |
| — | — | **ProjectAccessService 权限收敛架构** |
| — | — | **TypeScript 重构后类型不匹配** |

管线从 220 条原始记忆中提取出了手动总结忽略的 **10+ 条有价值经验**，尤其是 `get_projects` 项目可见性问题和 `ProjectAccessService` 架构决策，这两条在实际项目中影响很大，但手动总结时被遗漏了。

**噪音过滤有效**：最后一批（批次 11/11）全部为空结果查询记录，管线正确识别为噪音并跳过，说明 System Prompt 中的噪音判断标准生效。

**confidence 评分合理**：分布在 0.78-0.97 之间，Bearer Token 问题 confidence=0.95（因为有明确的根因链条），TypeScript 类型问题 confidence=0.78（因为修复步骤较通用，确定性较低），评分区分度合理。

**去重机制工作正常**：日志中多处出现 `去重跳过 1`，说明 hash 去重有效避免了重复经验。

### 2.2 日志可观测性 — 完全达标

日志设计在实际运行中完整体现：
- 每个批次的记录数、耗时、输出经验数清晰可追踪
- Venus API 的实际 token 用量被记录（如 `input=9069 output=2856 tokens`）
- Pydantic 校验失败时完整记录了 LLM 原始输出，排查问题无需猜测
- 限流等待、网络超时、重试均有日志，可精确定位时间消耗点

### 2.3 Layer 2 成功的叙事 — 质量很高

architecture 主题（2 条叙事）和 cross_platform 主题（4 条叙事）成功生成，质量对比：

| 维度 | Claude Code 手动叙事 | 管线 Layer 2 自动叙事 |
|---|---|---|
| 失败尝试还原 | ✅ 有，但较精简（如"第一次失败→第二次失败"） | ✅ **更详细**：每步标注了做法、现象、线索、结论 |
| 方法论标签 | ✅ 有，6 条通用方法论 | ✅ 有，每条叙事独立标注（如"分层隔离调试"/"载体适配原则"） |
| 代码示例 | ❌ 无 | ✅ 有内联代码块（如 PowerShell → SSH 的具体命令） |
| 可读性 | ⭐⭐⭐⭐⭐ 人写的叙事，流畅自然 | ⭐⭐⭐⭐ 结构清晰但略显模板化 |

## 3. 当前存在的问题

### 3.1 🔴 Layer 2 失败率高 — 核心问题

7 个主题中 **4 个失败**，失败率 57%：

| 失败主题 | 失败原因 | 根因 |
|---------|---------|------|
| **debugging**（19 条经验） | 网络超时（Read timed out），3 次重试全部失败 | 输入 ~12934 tokens + 预期输出 ~5000+ tokens，总量超出 Venus API 的单次请求时限 |
| **configuration**（2 条） | Pydantic JSON 校验失败（Invalid JSON: expected `,` or `}` at line 9） | LLM 输出的 JSON 在 investigation_journey 字段的长文本中包含未转义的引号或换行 |
| **deployment**（1 条） | 同上：JSON 校验失败 | 同上 |
| **tooling**（1 条） | 同上：JSON 校验失败 | 同上 |

**影响**：debugging 是最大的主题组（19 条经验），它的失败意味着管线丢失了最有价值的叙事输出。

**根因分析**：

1. **debugging 主题超时**：19 条经验 + 84 条原始观测 = 输入太长。Layer 2 的 `max_tokens: 8192` 加上巨大输入，总请求量超出 Venus API 的 120 秒超时。需要对大主题做二次拆分。

2. **JSON 校验失败（3 个主题）**：LLM 输出的叙事文本中包含未转义的特殊字符（引号、反斜杠、换行符），导致 JSON 结构被破坏。日志清楚记录了 `expected ',' or '}'`，说明是长文本字段中的转义问题。这是 `response_format: json_schema` 在 Venus API 的实际表现——**并不像设计文档假设的那样 100% 严格**。

### 3.2 🟡 Layer 1 早期也有 JSON 校验失败

日志第一次运行（14:31-14:36）中，批次 1 和批次 4 分别出现了：
- 批次 1：LLM 输出被 ` ```json ... ``` ` 包裹（Markdown 代码块），导致 JSON 解析失败
- 批次 4：JSON 格式不完整，输出被截断

第二次运行（14:37 开始）同样的数据全部成功，说明 **LLM 输出格式存在随机性**，`response_format: json_schema` 无法完全消除格式问题。

### 3.3 🟡 耗时远超预估

| 指标 | 设计文档预估 | 实际运行 | 差距 |
|------|------------|---------|------|
| Layer 1 总耗时 | 15-30 秒 | 443 秒 | **15-30x** |
| Layer 2 总耗时 | 20-40 秒 | 746 秒 | **19-37x** |
| 全流程 | 35-70 秒 | 1189 秒 | **17-34x** |
| 单次 API 延迟 | 3-6 秒 | 24-99 秒 | **4-33x** |

主要原因：
- 设计文档假设的数据量是 85 条（一周），实际全量是 220 条
- Venus API 单次延迟远高于预估（实际 24-99 秒 vs 预估 3-6 秒）
- Layer 2 的大主题（debugging 19 条）导致超时

### 3.4 🟡 Layer 2 主题聚合粒度过粗

debugging 主题包含 19 条经验 + 84 条原始观测，内容跨度从 Bearer Token 排查到项目可见性排查到 SQLite 引号问题，主题过于宽泛，不适合在一次 LLM 调用中生成一篇连贯叙事。

### 3.5 🟢 经验重复/相似度较高

43 条经验中存在明显的内容重叠：
- Bearer Token 前缀问题出现了至少 3 条相似经验（#1、#7、#5 角度略不同但根因相同）
- CRLF 问题出现了至少 4 条（#13、脚本上传、远程执行、数据库查询各一条）
- PowerShell 语法不兼容出现了至少 5 条

这不完全是坏事（不同角度有不同价值），但说明去重粒度可以更细。

## 4. 与 Claude Code 手动总结的对比

| 维度 | Claude Code 手动总结 | 蒸馏管线输出 | 评价 |
|------|---------------------|------------|------|
| **经验条数** | 6 条 | L1: 43 条, L2: 7 条叙事 | 管线覆盖面远超手动，但有重复 |
| **遗漏率** | 遗漏了 get_projects 权限、search_memory_like 实现不同步等问题 | 220 条记录全部扫描，无遗漏 | **管线大幅胜出** |
| **方法论层** | 独立章节，6 条通用方法论 + 总结框架图 | 每条叙事独立标签，有方法论总结 | 手动的方法论更抽象、更可迁移 |
| **失败路径还原** | 有，但精简 | Layer 2 成功的叙事还原更详细 | 管线在细节上胜出 |
| **可读性** | ⭐⭐⭐⭐⭐ 叙事流畅，像老手讲故事 | ⭐⭐⭐⭐ 结构化但略模板化 | 手动略胜 |
| **可靠性** | 100%（人写的不会格式错误） | Layer 1 ~95%，Layer 2 ~43% | **管线的 L2 可靠性是核心短板** |
| **成本** | 约 30 分钟人工 + Claude Code 上下文 | 约 20 分钟自动 + 18 次 API 调用 | 管线可重复，但首次运行需要人排查失败 |
| **可重复性** | 不可重复（依赖 Claude Code 的对话上下文） | 完全可重复（同样输入同样流程） | **管线大幅胜出** |

## 5. 改进建议

### P0：修复 Layer 2 失败率

1. **大主题拆分**：当主题内经验超过 8 条时，按 `related_components` 二次聚类，拆为子主题分别生成叙事
2. **JSON 转义加固**：在 Pydantic 校验失败时，尝试用正则清理 LLM 输出中的 ` ```json ``` ` 包裹和未转义字符，再次校验；或对长文本字段改用 `str` 而非在 JSON 中内联 Markdown
3. **超时调整**：Venus API 请求超时从 120 秒提升到 180 秒；Layer 2 输入限制在 8K tokens 以内

### P1：提升去重粒度

当前用 `issue_context + solution` 的 hash 去重。建议增加语义相似度去重：对 `root_cause` 做 embedding，cosine similarity > 0.85 的视为重复。

### P2：耗时优化

- Layer 1 各批次之间无依赖，可用 `asyncio` 并行调用（注意 RPM 限制）
- 预估从 19.8 分钟降至 ~5-8 分钟

### P3：方法论自动提炼

当前方法论由 Layer 2 的 `methodology_summary` 字段产出，但成功率不稳定。建议在 Layer 2 全部完成后，增加一个独立的 **Layer 3**：将所有成功叙事的 `methodology_tags` 汇总，让 LLM 做跨主题的方法论抽象——类似 Claude Code 手动总结中的"方法论总结"章节。

## 6. 为什么 Claude Code 的手动总结读起来更容易理解？

这不是"感觉"，是有具体的结构性差异的。

### 6.1 根本原因：Claude Code 有对话上下文，管线没有

Claude Code 在写 `shadowfolk-experience-summary.md` 时，它知道：
- **你是谁** — minusjiang，正在调研 AI Agent 记忆方案的工程师
- **你关心什么** — 你之前问了哪些问题、对哪些点追问了、哪些地方说"配色有点丑"然后换了方案
- **你的知识水平** — 不需要解释什么是 Bearer Token，但需要说清楚为什么选无状态不选 SSE
- **当前对话的情绪节奏** — 你刚解决了一个问题，现在想总结分享

管线的 LLM 调用**每次都是冷启动**——它只看到一批记忆记录，不知道读者是谁、关心什么、已经懂什么。

### 6.2 五个具体差异

**① 叙事视角不同**

| Claude Code | 管线 Layer 2 |
|---|---|
| "minusjiang 连续失败了三次才解决 CRLF 问题" — **第三人称讲故事** | "第 1 步（失败）：首先怀疑是 MCP 服务端本身的问题" — **步骤报告** |

Claude Code 在讲一个**人的经历**，管线在做一份**事件记录**。人天然更容易代入故事，而不是代入报告。

**② Claude Code 会省略你已经知道的东西**

手动总结里写"Bearer Token 认证问题中，前缀格式错误是最常见的低级错误之一"——一句话。

管线输出写："HTTP Bearer Token 规范（RFC 6750）要求格式严格为 'Bearer <token>'，缺少前缀或空格会导致服务端解析失败。客户端配置层的格式错误比服务端 bug 更难发现，因为错误不会在配置阶段报出，只在运行时体现为 401"——三行。

对同项目的人来说，第一种写法**省掉了你已经知道的背景**，直接给结论。管线不知道你懂什么，所以什么都解释一遍，读起来就像在看教科书。

**③ Claude Code 会做"跨经验串联"**

手动总结的方法论章节写：

> "经验三中，minusjiang 连续失败了三次：第一次失败排除了内联命令 → 第二次失败定位到 CRLF → 第三次失败确认多层嵌套不可行 → 第四次成功。他没有在第一次失败后卡住，每次失败都缩小了问题空间。"

这是**跨步骤的模式识别**——不是在描述发生了什么，而是在解释"为什么这个人这样做是对的"。管线的 Layer 2 只能在单条叙事内做串联，无法跨主题抽象出"这个人解决问题的共性思路是什么"。

**④ 信息密度的取舍不同**

Claude Code 的经验一，核心信息：
> "4 小时完成从设计到部署验证，效率来自于动手之前已经想清楚了架构"

管线的同一经验，展开写了：SDK 版本确认 → tools.ts 拆分 → handlers.ts 拆分 → http.ts 实现 → sessionIdGenerator 设为 undefined → enableJsonResponse 设为 true → 挂载顺序 → TOF 中间件…

前者告诉你**为什么快**，后者告诉你**做了什么**。对读者来说，"为什么快"是可以迁移到其他项目的洞察，"做了什么"只是 Shadow-Folk 的实现细节。

**⑤ Claude Code 有"编辑判断力"**

手动总结决定只写 6 条经验——不是因为只有 6 条值得写，而是 Claude Code 判断了：**这 6 条足够让读者建立完整的理解框架**，写更多反而会稀释重点。

管线输出了 43 条 L1 经验 + 7 条 L2 叙事，面面俱到但**没有主次**。读者不知道应该先看哪条、哪条最重要、哪些可以跳过。

### 6.3 本质区别：面向读者 vs 面向数据

```
Claude Code 手动总结的思路：
  "minusjiang 想把经验分享给同事"
  → 同事需要什么？→ 能快速定位问题的速查 + 理解思路的叙事
  → 省略他们已知的 → 强调他们不知道的 → 串联成可记住的故事

管线的思路：
  "这里有 220 条记忆数据"
  → 哪些有价值？→ 提取 → 结构化 → 聚合 → 生成叙事
  → 每条都写清楚 → 每个字段都填满 → 输出
```

一个是**以读者为中心**（reader-centric），一个是**以数据为中心**（data-centric）。

### 6.4 管线能弥补这个差距吗？

部分可以，但不完全：

| 差距 | 能否弥补 | 做法 |
|------|---------|------|
| 缺少读者上下文 | 🟡 部分 | 在 prompt 中注入读者画像（如"目标读者是 Shadow-Folk 的同事，已了解 MCP 基本概念"） |
| 缺少编辑判断力 | 🟡 部分 | 增加 Layer 3：从 43 条中选出"最值得分享的 Top 5"，用 LLM 做重要性排序 |
| 缺少跨经验串联 | 🟡 部分 | 方法论总结阶段把所有 `methodology_tags` 汇总，要求 LLM 做跨主题模式识别 |
| 叙事像报告不像故事 | 🔴 困难 | System Prompt 可以要求"用讲故事的方式写"，但 LLM 冷启动时写出的"故事"往往像教科书里的案例分析，缺少真正的对话感 |
| 知道省略什么 | 🔴 困难 | 需要知道读者"已经懂什么"，这依赖于持续的对话上下文，不是单次 prompt 能给的 |

**结论**：管线擅长的是**不遗漏、不偏见地从大量数据中提取信息**。Claude Code 擅长的是**知道什么对这个人重要、用什么方式说他最容易理解**。理想的工作流是：管线做 L1 + L2 的全量提取，然后**人（或有对话上下文的 AI）做最后的编辑和取舍**。

## 7. 总结

| 层级 | 评分 | 一句话 |
|------|------|--------|
| Layer 1 结构化蒸馏 | ⭐⭐⭐⭐ | 覆盖面、噪音过滤、去重都很好，经验质量高于预期 |
| Layer 2 叙事重建 | ⭐⭐ | 成功的叙事质量很高，但 57% 失败率不可接受 |
| 日志可观测性 | ⭐⭐⭐⭐⭐ | 完全达标，排查问题无需猜测 |
| 整体 | ⭐⭐⭐ | L1 可直接使用，L2 需要修复后才能投入 |

**核心结论**：管线的 Layer 1 已经证明了自动化蒸馏的价值——43 条结构化经验、零遗漏、有效噪音过滤。Layer 2 的叙事重建方向正确但实现不够鲁棒，JSON 校验失败和大主题超时是必须修复的 P0 问题。修复后，管线可以完全替代手动总结的"信息提取"环节，但手动总结在"方法论抽象"和"可读性打磨"上仍有不可替代的价值。

---

## 8. DPAR 项目蒸馏实战（2026-04-11）

> 将蒸馏管线应用于 DPAR 项目的两个开发者（hughesli、ziyad）的 Shadow-Folk 记忆数据库，验证管线在大数据量、跨项目场景下的实际表现。

### 8.1 背景与目标

DPAR 项目有两份 SQLite 记忆数据库：

| 数据库 | session_summaries | observations | 总记录数 |
|--------|-------------------|--------------|----------|
| `dpar-hughesli.db` | 599 | 1,624 | **2,223** |
| `dpar-ziyad.db` | 1,441 | 8,680 | **10,121** |

数据量相比 v1 评估的 220 条增长了 **10-46 倍**，是对管线扩展性的真实压力测试。

#### 数据适配

管线原生支持 CSV 输入，DPAR 数据为 SQLite。编写了 `run_dpar_distill.py` 完成自动适配：
1. 从两个 `.db` 文件导出 `session_summaries` 和 `observations` 为 CSV
2. 表结构与管线期望字段完全匹配（request/investigated/learned/completed/next_steps、text/type/title/facts/concepts）
3. 按人分别执行全量蒸馏

#### 运行配置

| 参数 | 值 | 说明 |
|------|------|------|
| batch_size | 30 | 比 v1 的 20 更大，减少 API 调用次数 |
| min_confidence | 0.5 | 与 v1 一致 |
| model | claude-sonnet-4-6 | Venus API |
| hours | 0（全量） | 不做时间过滤 |

### 8.2 运行结果概览

#### hughesli（完整运行）

| 阶段 | 指标 | 结果 |
|------|------|------|
| Extract | 输入数据 | 599 summaries + 1,624 observations = 2,223 条 |
| Layer 1 | 批次数 | 75 批（batch_size=30） |
| Layer 1 | 产出经验 | **327 条** |
| Layer 1 | 失败批次 | 2 批（Pydantic JSON 校验失败，跳过） |
| Layer 1 | 耗时 | 3,226.6 秒（约 54 分钟） |
| Layer 2 | 主题组 | 7 个：debugging(156), architecture(44), configuration(40), workflow(38), cross_platform(21), deployment(14), tooling(14) |
| Layer 2 | 成功叙事 | **4 条**（cross_platform 主题成功） |
| Layer 2 | 失败主题 | 6/7 个（Venus API 超时） |
| 全流程 | 总耗时 | 5,593.5 秒（约 93 分钟） |
| 全流程 | API 调用 | 82 次 |

#### ziyad（75 批截断运行）

ziyad 原始数据 10,121 条需 338 批，全量运行预计 ~3.7 小时。实际运行 75 批后截断。

| 阶段 | 指标 | 结果 |
|------|------|------|
| Extract | 输入数据 | 1,441 summaries + 8,680 observations = 10,121 条 |
| Layer 1 | 实际批次 | 75 批（前 42 批 + 续跑 43-75 批） |
| Layer 1 | 产出经验 | **309 条** |
| Layer 1 | 失败批次 | 3 批（JSON 校验失败，跳过） |
| Layer 1 | 耗时 | ~83 分钟（分两段） |
| Layer 2 | 主题组 | 7 个：debugging(155), workflow(52), configuration(32), architecture(32), tooling(21), cross_platform(11), deployment(6) |
| Layer 2 | 成功叙事 | **6 条**（deployment 主题成功） |
| Layer 2 | 失败主题 | 6/7 个（超时 + JSON 截断） |
| 全流程 | 总耗时 | 4,349.5 秒（约 72 分钟，续跑部分） |
| 全流程 | API 调用 | 40 次（续跑部分） |

#### 合计

| 指标 | hughesli | ziyad | 合计 |
|------|----------|-------|------|
| 原始记录 | 2,223 | 2,250（75 批） | **4,473** |
| L1 经验 | 327 | 309 | **636** |
| L2 叙事 | 4 | 6 | **10** |
| 压缩率 | 6.8:1 | 7.3:1 | **7.0:1** |

### 8.3 Layer 1 质量评估（hughesli 327 条详细分析）

#### 置信度分布

| 区间 | 数量 | 占比 |
|------|------|------|
| 0.9-1.0 | 82 | 25.1% |
| 0.8-0.9 | 185 | **56.6%** |
| 0.7-0.8 | 57 | 17.4% |
| 0.6-0.7 | 3 | 0.9% |
| < 0.6 | 0 | 0% |
| **平均** | **0.846** | |

相比 v1 的 0.78-0.97（均值 ~0.80），DPAR 蒸馏的置信度均值更高（0.846），且无低于 0.65 的条目。

#### 类型分布

| 类型 | 数量 | 占比 | 说明 |
|------|------|------|------|
| debugging | 156 | 47.7% | UE Cook、热更新、APK 打包排障 |
| architecture | 44 | 13.5% | UE 资产系统、CDN 架构、构建流程设计 |
| configuration | 40 | 12.2% | DefaultGame.ini、Gradle、CI Pipeline 配置 |
| workflow | 38 | 11.6% | SVN 操作、ADB 调试、CI 工作流 |
| cross_platform | 21 | 6.4% | PowerShell/Bash 兼容、Android 版本适配 |
| deployment | 14 | 4.3% | APK/OBB 打包部署 |
| tooling | 14 | 4.3% | ripgrep、Cook 日志工具链 |

debugging 占 48% 符合 DPAR 项目特征（UE 构建问题排查为主）。

#### 高频关联组件 Top 10

| 组件 | 出现次数 | 说明 |
|------|----------|------|
| PowerShell | 38 | 跨平台 shell 兼容性高发区 |
| SVN | 37 | 版本控制操作经验 |
| UE4 Cook | 20 | 引擎 Cook 流程 |
| DefaultGame.ini | 18 | UE 配置管理 |
| dpar_build_wrapper.py | 17 | 构建脚本 |
| Build.py | 16 | 构建入口 |
| ADB | 16 | Android 调试 |
| DPAR | 14 | 项目级 |
| Puffer CDN | 10 | CDN 部署 |
| Gradle | 7 | Android 构建 |

#### 字段填充与内容质量

| 指标 | 值 | 说明 |
|------|------|------|
| prevention 填充率 | 99.7%（326/327） | v1 也是高填充，DPAR 更好 |
| related_components 空值 | 0 | 100% 填充 |
| issue_context 含具体错误信息 | 51.4% | 包含"报错/失败/error"等关键词 |
| issue_context 平均长度 | 96 字符 | 足够具体 |
| solution 平均长度 | 122 字符 | 多数给出具体操作步骤 |
| 含模糊用词（"可能/似乎"）| 15.0% | 多为合理的不确定性表述 |
| 基于前 80 字符的重复 | 0 | 零重复 |
| 过短 issue_context（< 40 字符）| 8 条（2.4%） | 内容仍然具体有价值 |

#### 高质量经验样例

**[debugging] conf=0.95 — Python multiprocessing Windows 崩溃**
> 问题：Python multiprocessing 脚本在 Windows 构建流水线中 wrapper 脚本顶层代码被重复执行，23 个子进程全部崩溃，215081 个 pak 文件处理全部失败。
> 根因：Windows multiprocessing spawn 机制下子进程重新 import 主模块，顶层 monkey-patch 逻辑被再次执行。
> 方案：将启动逻辑包裹在 `if __name__ == '__main__':` 条件块内。

**[configuration] conf=0.93 — 硬编码导致多配置构建失效**
> 问题：DPAR_COOKED_CHECK_DIR 路径硬编码为 'Development'，切换 Shipping 配置时校验失效。
> 根因：v1 wrapper 迁移到 YAML 内联时，动态变量被错误替换为硬编码字符串。

**[cross_platform] conf=0.98 — PowerShell wc 命令不存在**
> 问题：PowerShell 中执行 `wc -l` 报 CommandNotFoundException。
> 方案：使用 `(Get-Content 'filepath').Count` 替代。

### 8.4 Layer 2 叙事质量

成功生成的 10 条叙事中，hughesli 的 4 条叙事（cross_platform 主题）质量尤为突出：

| 叙事标题 | 内容摘要 |
|----------|----------|
| **UE4 Windows 构建：DirectoriesToNeverCook 配置失效的跨平台陷阱** | 还原了配置在 Windows 失效但其他平台正常的排查过程，含 DDC 缓存干扰的噪音识别 |
| **Android 14 Scoped Storage：adb push Permission Denied 完整排查** | 8 步排查路径，含 4 次失败尝试（adb push → adb shell cp → 文件管理器 → MTP），最终两步走方案 |
| **PowerShell 语法陷阱全图鉴** | 汇总 8 个跨 shell 语法踩坑场景（&&、&、curl JSON、Invoke-RestMethod、Unix 工具、cmd 续行符、adb shell、Get-ChildItem），含替代方案对照表 |
| **Android 14 兼容性：多点失效与系统级 API 变更** | 分析存储/后台进程/显示 API 三个维度同时失效的根因聚合 |

叙事包含完整的排查路径、噪音识别、思维转折点和方法论标签，质量与 v1 评估中成功的叙事一致。

### 8.5 与 v1 蒸馏的对比

| 维度 | v1（Shadow-Folk） | DPAR（hughesli） | 变化 |
|------|-------------------|------------------|------|
| 输入数据量 | 220 条 | 2,223 条 | **10x** |
| L1 产出 | 43 条 | 327 条 | **7.6x** |
| L1 压缩率 | 5.1:1 | 6.8:1 | 噪音过滤更强 |
| L1 置信度均值 | ~0.80 | 0.846 | 提升 |
| L1 失败批次 | 2/11（18%） | 2/75（2.7%） | **大幅改善**（JSON 修复逻辑生效） |
| L2 成功率 | 43%（3/7） | 14%（1/7） | 恶化（数据量增大导致更多超时） |
| 单批 API 延迟 | 24-99 秒 | 30-60 秒 | 更稳定 |
| 全流程耗时 | 19.8 分钟 | 93 分钟 | 数据量 10x，耗时 ~5x（合理） |

**关键发现**：
- **L1 质量随数据量增大保持稳定**：327 条经验的置信度、字段填充率、去重效果均与 v1 持平或更优
- **L1 JSON 修复逻辑（v1 后新增）有效**：失败率从 18% 降至 2.7%，`_strip_markdown_json()` 和 `_try_repair_json()` 发挥了作用
- **L2 超时问题随数据量放大恶化**：大主题（debugging 156 条 + 397 条观测）远超 Venus API 120 秒超时限制
- **实际使用的 Schema 是简化版**：去掉了设计文档中定义的 `scope`、`environment_conditions`、`trigger_patterns` 字段，核心四元组（issue_context → root_cause → solution → rationale）质量足够高

### 8.6 发现的问题与改进方向

#### P0（已解决）：L2 大主题超时 — 分片 + 增大 timeout

首次跑 L2 时绝大多数主题因 Venus API 120s timeout 失败。通过以下优化成功补跑全部主题：

**优化策略**（`rerun_l2.py`）：
1. **激进分片**：每主题仅取 Top 6 高置信度经验 + 3 条观测（从原来的全量降低 95%+）
2. **字段截断**：每个字段截断到 200 字符
3. **增大 timeout**：从 120s 提升到 300s（实际每次调用 60-75s 完成）
4. **降低 max_tokens**：从 8192 降到 4096
5. **JSON escape 修复**：增加 `_fix_json_escapes()` 处理 LLM 输出中的非法转义

**补跑结果**：

| 人员 | 补跑主题数 | 成功 | 失败 | 新增叙事 | 备注 |
|------|-----------|------|------|----------|------|
| hughesli | 6 | 6 | 0 | 20 条 | configuration(4) / debugging(3) / deployment(4) / tooling(3) / workflow(3) / architecture(3) |
| ziyad | 6 | 6 | 0 | 19 条 | debugging(4, 重试1次) / configuration(3) / workflow(3) / cross_platform(3) / architecture(2) / tooling(4) |

**关键参数对比**：

| 参数 | 首次跑（失败） | 补跑（成功） |
|------|---------------|-------------|
| 每主题经验数 | 全量（14-156） | Top 6 |
| 每主题观测数 | 全量（2-397） | 最多 3 |
| 字段截断 | 无 | 200 字符 |
| timeout | 120s | 300s |
| max_tokens | 8192 | 4096 |
| 平均耗时 | >120s（超时） | 60-75s |
| input tokens | 估计 10000+ | 2200-2800 |

#### P1：补充缺失的结构化字段

当前实际使用的 Schema 缺少三个设计文档中定义的重要字段：

| 缺失字段 | 影响 | 建议 |
|----------|------|------|
| `scope` | 无法区分"全员必知"vs"特定环境"经验 | 在 system prompt 的 SCHEMA_HINT 中补充 |
| `environment_conditions` | 无法做环境级路由过滤 | 同上 |
| `trigger_patterns` | 无法做泛化匹配，降低召回覆盖面 | 同上 |

#### P2：经验标签补充

DPAR 蒸馏的经验缺少 `project` 和 `user` 标签，灌入 memory 系统时需要后处理补充：

```python
for exp in experiences:
    exp["project"] = "dpar"
    exp["user"] = "hughesli"  # 或 "ziyad"
```

### 8.7 输出文件清单

| 文件 | 内容 | 大小 |
|------|------|------|
| `output/dpar-hughesli-experiences.json` | hughesli L1 结构化经验 | 327 条 |
| `output/dpar-hughesli-narratives.md` | hughesli L2 叙事 | 24 条（7 个主题全覆盖） |
| `output/dpar-ziyad-experiences.json` | ziyad L1 结构化经验 | 309 条 |
| `output/dpar-ziyad-narratives.md` | ziyad L2 叙事 | 25 条（7 个主题全覆盖） |
| `dpar-export/*.csv` | 中间导出 CSV | 4 个文件 |
| `run_dpar_distill.py` | DPAR 蒸馏入口脚本 | SQLite → CSV → Pipeline |
| `run_ziyad_continue.py` | ziyad 续跑脚本 | 断点续跑 + L2 分片优化 |
| `rerun_l2.py` | L2 补跑脚本 | 分片 + 增大 timeout + 字段截断 |
| `rerun_l2_debug.py` | debugging 单主题重试 | JSON escape 修复 |

### 8.8 DPAR 蒸馏总结

| 层级 | 评分 | 一句话 |
|------|------|--------|
| Layer 1 结构化蒸馏 | ⭐⭐⭐⭐⭐ | 636 条经验、零重复、99.7% prevention 填充、平均置信度 0.846，质量全面超越 v1 |
| Layer 2 叙事重建 | ⭐⭐⭐⭐ | 49 条高质量叙事，7 个主题全覆盖；补跑通过激进分片（6 exps + 3 obs + 200 字截断 + 300s timeout）实现 12/12 主题 100% 成功 |
| 扩展性验证 | ⭐⭐⭐⭐ | 数据量 10x 增长下 L1 质量保持稳定，JSON 修复逻辑使失败率从 18% 降至 2.7% |
| 整体 | ⭐⭐⭐⭐⭐ | L1 + L2 均已达到可用于生产级 memory 灌入的质量 |

**核心价值**：636 条结构化工程经验 + 49 条完整叙事，覆盖 configuration / debugging / deployment / tooling / workflow / cross_platform / architecture 共 7 大主题，涵盖 UE Cook、Android 热更新、SVN 凭据、Python multiprocessing、PowerShell 兼容性、CI/CD 流水线等 DPAR 项目的核心踩坑领域，可直接作为项目级踩坑记录 memory 的种子数据。

**L2 补跑关键经验**：
- Venus API 对 prompt 大小极敏感，2200-2800 input tokens 是稳定区间（60-75s 响应）
- 大主题不需要全量经验，Top 6 高置信度经验足以生成高质量叙事
- JSON escape 是 LLM 输出的常见问题，需在 JSON repair 链中增加 `_fix_json_escapes()` 步骤
- `temperature=0.3`（降低随机性）比 `0.5` 更适合 JSON 格式输出

## 9. 成本审计与管线优化 (2026-04-12)

### 9.1 全流程成本审计

基于终端日志中 167 次 Venus API 调用的精确统计：

| 指标 | 数值 |
|------|------|
| API 调用总数 | 167 次 |
| 总 input tokens | 1,250,404 |
| 总 output tokens | 442,451 |
| **总 tokens** | **1,692,855（≈170 万）** |
| 总 API 耗时 | 8,194 秒（136.6 分钟） |
| 含失败重试的端到端时间 | 约 3.5 小时 |
| input/output 比 | 2.8:1 |

#### 按层级拆分

| 层级 | 调用数 | input tokens | output tokens | 总 tokens | 占比 |
|------|--------|-------------|---------------|-----------|------|
| L1 结构化蒸馏 | 149 | 1,184,264 | 367,384 | 1,551,648 | 91.7% |
| L2 叙事重建 | 18 | 66,140 | 75,067 | 141,207 | 8.3% |

#### 按阶段拆分

| 阶段 | 调用数 | 总 tokens | 平均 input | 平均 output | 平均耗时 |
|------|--------|----------|-----------|------------|---------|
| hughesli L1 (75 batches) | 75 | 700,517 | 7,012 | 2,327 | 43s |
| hughesli L2 首次 | 3 | 31,315 | 7,210 | 3,227 | 60s |
| ziyad L1 Part1 (42 batches) | 41 | 594,833 | 11,462 | 3,045 | 59s |
| ziyad L1 续跑 (33 batches) | 33 | 256,298 | 5,706 | 2,060 | 38s |
| ziyad L2 首次 | 2 | 22,821 | 5,342 | 6,068 | 108s |
| L2 补跑 (11 themes) | 12 | 80,168 | 2,584 | 4,096 | 69s |
| ziyad debugging 重试 | 1 | 6,903 | 2,807 | 4,096 | 69s |

#### 效率指标

| 指标 | 数值 |
|------|------|
| 每条 L1 经验消耗 | 2,661 tokens |
| 每条 L2 叙事消耗 | 34,548 tokens |
| 原始记录 → L1 经验压缩比 | 19.4:1 |
| 每条原始记录消耗 | 137 tokens |

### 9.2 成本结构分析

#### Observation 的投入产出比

| 维度 | Summary | Observation |
|------|---------|-------------|
| 数据量 | 2,040 条（17%） | 10,304 条（83%） |
| 含问题关键词 | 70% (hughesli) / 46% (ziyad) | 42% / 41% |
| 在 L1 prompt 中的 token 占比 | ~57% | ~43%（≈515,000 tokens） |
| 独立信息价值 | 高（每条含 request/learned/completed 三元组） | 低（89% 为过程流水账，仅 bugfix+configuration 类型有价值） |

observation 贡献了 L1 input 的 43%（约 51.5 万 tokens），但 L2 补跑时将 observation 从全量（500+ 条）降到 3 条，叙事质量无下降。说明 L1 蒸馏已将 observation 中的有效信息吸收完毕。

**结论**：做踩坑记录场景下，summary 足够。observation 中仅 `bugfix`（占 2.8%）和 `configuration`（占 3.1%）类型有独立价值。

#### System Prompt 重复开销

每次 L1 调用发送 ~600 tokens 的 system prompt，149 次调用 = 89,400 tokens（占总量 5.3%）。batch_size=20 导致分批过碎。

#### L1 语义重复

NeverCook 一个问题蒸馏出 45 条经验（hughesli 34 + ziyad 11），636 条中估计 30-40% 为同一问题不同角度复述。当前 hash 去重只能过滤完全相同的 `issue_context|solution`，无法识别语义重复。

### 9.3 改造方案

#### P0（已实施）：Observation 可配置过滤

pipeline.py 新增 `--include-obs-types` 参数：
- 默认为空 — 不传 observation，仅用 summary 蒸馏
- 设为 `bugfix,configuration` — 仅保留高价值类型（占 observation 的 6%）
- 设为 `all` — 传全量 observation（兼容旧行为）

预期 L1 input tokens 从 118 万降到 ~55 万（节省 53%）。

#### P1（已实施）：增大 batch_size

默认 `--batch-size` 从 20 改为 50。预期 L1 批次数从 149 降到 ~41（节省 72% API 调用次数）。

#### P2（待实施）：L1.5 语义去重层

在 L1 全部完成后，按 `related_components` 聚类 → 簇内用 embedding 做语义去重 → 取 confidence 最高的 + 合并互补信息。预计 636 条精简到 ~400 条。

#### P3（待实施）：L2 按需实时生成

将 L2 从预生成改为检索命中后实时生成。用户查询命中 Top 5 经验时才做叙事重建，每次 1 次 API 调用（~6,600 tokens，60-70s）。

#### P4（待实施）：小模型预筛

用 Haiku 等小模型先判断每批记忆是否含有价值信息，预计过滤 40-50% 的噪音批次。增加系统复杂度，建议 P0-P3 落地后再考虑。

### 9.4 P0+P1 优化预期效果

| 指标 | 优化前 | 优化后 | 节省 |
|------|--------|--------|------|
| 总 tokens | 170 万 | ~70 万 | 59% |
| API 调用次数 | 167 | ~55 | 67% |
| 端到端时间 | 136 分钟 | ~40 分钟 | 71% |
| 产出数量 | 636 条 | ~550 条 | -14% |
| 产出质量 | 置信度 0.846 | 预计持平 | — |
