# OpenClaw Compaction 算法详解

> 基于 OpenClaw 源码 `compaction.ts`、`compaction-safeguard.ts`、`compaction-safeguard-quality.ts`、`compaction-instructions.ts` 的完整分析。

---

## 一、Compaction 是什么

Compaction（压缩）是 OpenClaw 在对话上下文窗口即将溢出时，将**旧对话历史用 LLM 摘要替换**的机制。

它的本质是：**用一段结构化摘要取代原始消息，腾出上下文空间给新对话**。

压缩后，模型看到的上下文变为：

```
[System Prompt] + [Compaction 摘要] + [最近几轮原始消息]
```

原始消息**不可逆地**从上下文中消失（但仍保存在 JSONL 日志中）。

---

## 二、触发时机

### 2.1 自动触发（默认开启）

当会话的 token 数接近或超过模型的 `contextWindow` 时，自动触发。

```
假设 Claude Sonnet 的 contextWindow = 200K tokens

当前上下文使用 ≈ 195K tokens → 触发 auto_compaction_start
```

### 2.2 手动触发

用户发送 `/compact` 命令，可附带指令：

```
/compact 重点保留架构决策和待办事项
```

---

## 三、一个完整的实际例子（精简版）

### 场景设定

- 模型上下文窗口：**20K tokens**（为了演示方便，用一个小窗口）
- 自动压缩阈值：80%（即 16K tokens 时触发）
- recentTurnsPreserve = **2**（保留最近 2 轮原文）

---

### 第一步：对话积累

你和 OpenClaw 进行了 6 轮对话，讨论一个 Todo App 的开发。共产生 **24 条消息，约 17K tokens**——即将触发自动压缩。

| 轮次 | user | assistant | tokens |
|------|------|-----------|--------|
| 1 | "帮我做一个 Todo App，用 React + Express" | "好的，我建议前端 Vite+React，后端 Express+Prisma+SQLite..." | 2K |
| 2 | "先帮我写后端的增删改查 API" | read_file app.ts → write_file routes/todo.ts (CRUD 4个接口, 60行) → shell tsc 通过 | 5K |
| 3 | "再写前端页面" | write_file src/App.tsx (TodoList组件, 80行) → write_file src/api.ts (fetch封装) | 4K |
| 4 | "加一个完成状态的切换功能" | read_file App.tsx → write_file App.tsx (添加 toggle) → read_file todo.ts → write_file todo.ts (添加 PATCH /toggle) | 3K |
| 5 | "样式太丑了，用 Tailwind 美化一下" | shell npm install tailwindcss → write_file tailwind.config.js → write_file App.tsx (加 className) | 2K |
| 6 | "加一个按截止日期排序的功能" | read_file todo.ts → write_file todo.ts (添加 ?sort=deadline 参数) → "排序功能已加好" | 1K |

> 此时上下文：System Prompt (1K) + 24 条消息 (17K) = **18K tokens**，超过 16K 阈值，触发 compaction。

---

### 第二步：Memory Flush（记忆刷写）

压缩前，OpenClaw 先静默写一份记忆文件：

> 写入 `memory/2026-03-27.md`：Todo App, React+Express+Prisma+SQLite, CRUD 已完成, Tailwind 样式, 按 deadline 排序

---

### 第三步：消息分类

`splitPreservedRecentTurns()` 将 24 条消息分为两组：

- **保留区**（最近 2 轮原文，不压缩）：轮次 5-6，共 8 条消息，约 3K tokens
- **摘要区**（需要 LLM 压缩）：轮次 1-4，共 16 条消息，约 14K tokens

| 分组 | 消息范围 | 条数 | tokens |
|------|---------|------|--------|
| 摘要区 | msg[0] ~ msg[15]（轮次 1-4） | 16 | 约 14K |
| 保留区 | msg[16] ~ msg[23]（轮次 5-6） | 8 | 约 3K |

---

### 第四步：自适应分块

```python
effective_max = floor(20000 * 0.4 - 4096) / 1.2 = 3920 tokens
```

每块上限约 **3.9K tokens**。

---

### 第五步：分块（贪心遍历）

从 msg[0] 开始一条一条放，装满了就换下一个桶：

| Chunk | 消息范围 | 对应轮次 | tokens | 内容概要 |
|-------|---------|---------|--------|---------|
| **A** | msg[0]~msg[3] | 轮次 1-2 前半 | 约 3.5K | 需求讨论 + 读取 app.ts |
| **B** | msg[4]~msg[11] | 轮次 2 后半-3 | 约 3.8K | 写 CRUD API (60行) + 写前端 App.tsx (80行) |
| **C** | msg[12]~msg[15] | 轮次 4 | 约 3K | 添加 toggle 功能，修改前后端 |

> msg[4] (write_file todo.ts 60行代码) 单条就有 ~2K tokens，所以 Chunk A 只装了 4 条就满了。

---

### 第六步：滚动摘要（核心）

这是关键——**不是一次性把 14K tokens 丢给 LLM，而是一块一块喂，每次带上上一轮的笔记**。

**第 1 轮：LLM 只看 Chunk A**

| 输入 | 内容 |
|------|------|
| 系统指令 | "请按 5 个 section 格式总结..." |
| previousSummary | 无（第一轮） |
| 消息 | Chunk A 的 4 条消息 |

> 输出 **Summary_A：** Todo App 项目启动, React+Express+Prisma+SQLite, 读取了 app.ts 结构

**第 2 轮：LLM 看 Summary_A + Chunk B**

| 输入 | 内容 |
|------|------|
| 系统指令 | 同上 |
| previousSummary | **Summary_A**（上一轮输出） |
| 消息 | Chunk B 的 8 条消息 |

> 输出 **Summary_AB：** Todo App, CRUD API 已实现 (routes/todo.ts), 前端 TodoList 组件已完成 (App.tsx + api.ts)

**第 3 轮：LLM 看 Summary_AB + Chunk C**

| 输入 | 内容 |
|------|------|
| 系统指令 | 同上 |
| previousSummary | **Summary_AB**（上一轮输出） |
| 消息 | Chunk C 的 4 条消息 |

> 输出 **Summary_ABC（最终摘要）：**
>
> **Decisions:** Todo App, React+Express+Prisma+SQLite; CRUD API 完成; 前端 TodoList 完成; toggle 完成状态功能已加
>
> **Exact identifiers:** routes/todo.ts, src/App.tsx, src/api.ts, PATCH /api/todos/:id/toggle

**注意滚动的关键：每一轮的输出 = 下一轮的 previousSummary。Chunk A 的 60 行 CRUD 代码经过 3 轮摘要后，只剩"CRUD API 完成"6 个字。**

---

### 第七步：质量审计 + 组装 + 持久化

审计通过后，最终摘要 + 保留区原文 + 文件清单拼成完整 compaction entry，写入 JSONL。

---

### 压缩前后对比

**压缩前（18K tokens）：**

| 区域 | tokens |
|------|--------|
| System Prompt | 1K |
| 轮次 1-6 的 24 条完整消息 | 17K |
| **总计** | **18K** |

**压缩后（约 5K tokens）：**

| 区域 | tokens |
|------|--------|
| System Prompt | 1K |
| Compaction 摘要（轮次 1-4 被蒸馏为一段文字） | 约 1K |
| 轮次 5 原文保留（Tailwind 安装和配置） | 约 2K |
| 轮次 6 原文保留（排序功能） | 约 1K |
| **总计** | **约 5K** |

**释放了 13K tokens（72%）给后续对话。**

#### 丢失了什么？

| 信息 | 压缩前 | 压缩后 |
|------|--------|--------|
| todo.ts 的 60 行 CRUD 完整代码 | 在 msg[4] 中 | **丢失**，只剩"CRUD API 完成" |
| App.tsx 的 80 行 React 组件代码 | 在 msg[8] 中 | **丢失**，只剩"TodoList 完成" |
| toggle 功能的前后端修改细节 | 在 msg[12-15] 中 | **丢失**，只剩"toggle 已加" |
| "好的，我建议..."的完整架构讨论 | 在 msg[1] 中 | **丢失** |
| --- | --- | --- |
| Tailwind 配置和样式修改 | 轮次 5 | **保留**（最近 2 轮原文） |
| 排序功能的实现 | 轮次 6 | **保留**（最近 2 轮原文） |
| 文件路径 routes/todo.ts, src/App.tsx | 摘要中 | **保留**（Exact identifiers） |

---

## 四、关键算法参数一览

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `BASE_CHUNK_RATIO` | 0.4 | 每块占 contextWindow 的比例 |
| `MIN_CHUNK_RATIO` | 0.15 | 大消息时的最小块比例 |
| `SAFETY_MARGIN` | 1.2 | token 估算的 20% 安全余量 |
| `SUMMARIZATION_OVERHEAD_TOKENS` | 4096 | 摘要 prompt 本身的预留 |
| `DEFAULT_RECENT_TURNS_PRESERVE` | 3 | 保留最近 N 轮原文 |
| `MAX_RECENT_TURNS_PRESERVE` | 12 | 最多保留轮次上限 |
| `MAX_RECENT_TURN_TEXT_CHARS` | 600 | 每轮原文截断字符数 |
| `MAX_COMPACTION_SUMMARY_CHARS` | 16000 | 摘要总字符数上限 |
| `MAX_TOOL_FAILURES` | 8 | 记录的工具失败上限 |
| `maxHistoryShare` | 0.5 | 历史在窗口中的最大占比 |
| `qualityGuardMaxRetries` | 1 | 质量不达标时的重试次数 |

---

## 五、信息丢失分析

以上面的例子为例，压缩后**丢失**了什么：

### 5.1 一定会丢失

| 丢失内容 | 举例 |
|----------|------|
| 工具调用的详细输出 | `npm test` 的完整 stdout（可能几千行） |
| 代码的逐行修改细节 | 哪一行从什么改成了什么 |
| 中间的讨论过程 | "我试了方案 A 不行，换了方案 B" 的具体细节 |
| 非文本内容 | 图片、代码截图 |

### 5.2 可能会丢失（取决于 LLM 摘要质量）

| 可能丢失 | 原因 |
|----------|------|
| 早期的约束和规则 | 后续对话冲淡了 LLM 的注意力 |
| 中间轮次的 TODO | LLM 可能认为已完成而省略 |
| 具体的数值/配置 | 非标识符的数值可能被概括 |
| 对话的语气和意图 | 摘要只保留事实，丢失语境 |

### 5.3 一定会保留

| 保留内容 | 机制 |
|----------|------|
| 最近 3 轮的完整对话 | `recentTurnsPreserve` |
| 关键标识符（UUID、路径、端口） | `identifierPolicy: "strict"` + 质量审计 |
| 最后一个用户请求 | `hasAskOverlap` 质量检查 |
| 文件读写清单 | `fileOps` 附加到摘要 |
| 工具失败记录 | `collectToolFailures` |
| AGENTS.md 中的关键规则 | `readWorkspaceContextForSummary` |

---

## 六、多次 Compaction 的级联退化

真实场景中，一天的长对话可能触发**多次** compaction，信息会级联衰减：

| 阶段 | 上下文内容 | 说明 |
|------|-----------|------|
| 原始对话 | 200K tokens | 完整对话 |
| **第 1 次 compaction** | 摘要 1 (约2K) + 最近 3 轮 (约15K) = **17K** | 继续对话... |
| 上下文再次增长 | 200K tokens | 新对话填满上下文窗口 |
| **第 2 次 compaction** | 摘要 2 (约2K) + 最近 3 轮 (约15K) = **17K** | 摘要 2 是对 [摘要1 + 大量新对话] 的再摘要，摘要1中的信息被二次压缩 |
| 上下文再次增长 | 200K tokens | - |
| **第 3 次 compaction** | 摘要 3 (约2K) + 最近 3 轮 (约15K) = **17K** | 最早期的对话内容几乎完全丢失 |

**信息衰减曲线：**

| 阶段 | C0(原始) | C1(第1次) | C2(第2次) | C3(第3次) | C4(第4次) | C5(第5次) | C6(第6次) | C7(第7次) |
|------|---------|----------|----------|----------|----------|----------|----------|----------|
| 信息保留率 | 100% | 80% | 60% | 40% | 20% | 10% | 5% | 约2% |

C0 = 原始信息保留率，C7 = 经过 7 次 compaction 后的信息保留率

---

## 七、与其他方案的对比

| 维度 | OpenClaw Compaction | claude-mem / codebuddy-mem | mem0 |
|------|--------------------|-----------------------------|------|
| 核心思路 | 会话级有损摘要 | 事件级结构化蒸馏 | 对话级事实提取 |
| 触发时机 | 上下文窗口快满时 | 每次工具调用后 | 每次 add() 调用 |
| 信息存储 | 摘要替换原始消息 (JSONL 中) | observation 永久存储 (SQLite+向量) | fact 永久存储 (向量+图数据库) |
| 可逆性 | 不可逆 | 原始数据可回溯 | 事实可追溯 |
| 跨会话检索 | 不支持，只限当前会话 | 支持，语义检索 | 支持，语义+图检索 |
| 级联衰减 | 严重（多次压缩信息逐渐消失） | 无（独立存储） | 无（独立存储） |
| 适用场景 | 单次长对话的上下文管理 | 开发过程的持续记忆 | 用户画像和知识库 |

### 根本差异

**OpenClaw Compaction：**
对话 → 压缩 → 摘要 → 替换上下文。比喻：*"把 20 页笔记压缩成 1 页摘要，然后把原稿扔掉"*

**claude-mem / codebuddy-mem：**
对话 → 观察 → observations → 永久数据库 → 按需检索到上下文。比喻：*"每次讨论后提取知识卡片，放进卡片盒，需要时翻出来"*

**mem0：**
对话 → 提取 → facts → 查重合并 → 知识图谱 → 按需检索到上下文。比喻：*"每次对话提炼出事实，和已有知识合并，形成百科全书"*

---

## 八、配置优化建议

### 8.1 延长信息保留时间

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "recentTurnsPreserve": 6,
        "qualityGuardEnabled": true,
        "qualityGuardMaxRetries": 2,
        "identifierPolicy": "strict"
      }
    }
  }
}
```

### 8.2 使用更强的模型做摘要

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "model": "openrouter/anthropic/claude-sonnet-4-6"
      }
    }
  }
}
```

### 8.3 配合 Memory 使用

```json
{
  "memory": {
    "enabled": true,
    "flush": {
      "preCompaction": true
    }
  }
}
```

### 8.4 调整会话重置策略减少不必要的上下文丢失

```json
{
  "session": {
    "reset": {
      "mode": "idle",
      "idleMinutes": 480
    }
  }
}
```

---

## 九、核心机制图解：分块与滚动摘要

### 9.1 Chunk 是怎么分块的？

分块的本质非常简单——**就是一个贪心遍历**。

想象你有一摞纸牌（消息），你要把它们分成几叠，每叠不能超过一个重量上限。你从第一张开始一张一张放，放不下了就开新的一叠。

用上面 Todo App 的例子：

| 步骤 | 拿起的消息 | 当前块累计 | 动作 |
|------|-----------|-----------|------|
| 1 | msg[0] "帮我做 Todo App" (200 tokens) | 200 | 放入当前块 |
| 2 | msg[1] "好的，建议 Vite+React..." (800 tokens) | 1000 | 放入当前块 |
| 3 | msg[2] "先写后端 API" (100 tokens) | 1100 | 放入当前块 |
| 4 | msg[3] read_file app.ts (400 tokens) | 1500 | 放入当前块 |
| 5 | msg[4] write_file todo.ts **60行代码** (2000 tokens) | 3500 | 放入当前块 |
| 6 | msg[5] toolResult "文件已写入" (50 tokens) | 3550 | 放入当前块 |
| 7 | msg[6] shell tsc (100 tokens) | 3650 | 放入当前块 |
| 8 | msg[7] "CRUD API 已完成" (200 tokens) | 3850 | 放入当前块 |
| 9 | msg[8] write_file App.tsx **80行** (2500 tokens) | **6350 → 超过 3.9K！** | **封存 Chunk A**，msg[8] 放入新块 |
| ... | 继续 | ... | ... |

**关键规则：**

- **消息不会被切断** — 一条消息永远完整地属于一个 chunk
- **顺序不变** — msg[0] 永远在 msg[1] 前面
- **块大小自适应** — 如果某条消息很大（如 80 行代码），这个 chunk 的消息条数就少

用代码表达就一句话：

```python
if current_tokens + msg_tokens > 上限 and 当前块不为空:
    封存当前块, 开新块
```

**从前往后一条一条放，装满了就换下一个桶。**

### 9.2 滚动摘要是怎么"滚动"的？

"滚动"的意思是：**不是把所有 chunk 一次性给 LLM，而是一个一个喂，每次把上一轮的摘要传给下一轮**。

用 Todo App 的 3 个 Chunk 演示：

| 轮次 | LLM 看到什么 | LLM 输出什么 |
|------|-------------|-------------|
| 第 1 轮 | [系统指令] + Chunk A (4条消息) | Summary_A: "Todo App, React+Express, 读取了 app.ts" |
| 第 2 轮 | [系统指令] + **Summary_A** + Chunk B (8条消息) | Summary_AB: "Todo App, CRUD 完成, TodoList 完成" |
| 第 3 轮 | [系统指令] + **Summary_AB** + Chunk C (4条消息) | Summary_ABC: "Todo App, CRUD+前端+toggle 全部完成" |

**"滚动"的核心就是这一行代码：**

```python
previousSummary = 上一轮的输出
```

每一轮的输出，变成下一轮的输入前缀。像雪球一样滚下去。

**打个比方：** 你要读一本 300 页的书写笔记，但一次只能看 100 页：

| 步骤 | 你看的内容 | 你写出来的笔记 |
|------|-----------|--------------|
| 第 1 轮 | 第 1-100 页 | 笔记 A（1 页） |
| 第 2 轮 | **笔记 A** + 第 101-200 页 | 笔记 AB（1 页） |
| 第 3 轮 | **笔记 AB** + 第 201-300 页 | 笔记 ABC（1 页）— 最终笔记 |

### 9.3 为什么信息必然丢失？

用 Todo App 的例子：

| 阶段 | Chunk A 的信息量 | 发生了什么 |
|------|-----------------|-----------|
| 原始 | 3.5K tokens（含 60 行 CRUD 代码） | 完整保留 |
| Summary_A | 约 100 tokens | 60 行代码变成"读取了 app.ts" |
| Summary_AB | 约 150 tokens | 进一步压成"CRUD 完成" |
| Summary_ABC | 约 200 tokens | 只剩"CRUD+前端+toggle 全部完成" |

**60 行完整的 CRUD 代码最终变成了 4 个字："CRUD 完成"。** 越早的对话，信息丢失越严重。

---

## 十、Compaction 是压缩还是蒸馏？

### 10.1 两种"压缩"的根本区别

一个常见的误解是：Compaction 是像 gzip 那样的可逆压缩。**实际上它是纯粹的 LLM 蒸馏总结，不可逆，不能解压。**

| 维度 | 算法压缩（如 gzip/zstd） | OpenClaw Compaction |
|------|------------------------|---------------------|
| 原理 | 数学编码，利用数据中的重复模式 | LLM 读完原文，用自然语言重写一份摘要 |
| 可逆性 | **完全可逆**，解压后一个字节都不差 | **不可逆**，原始对话内容永久丢失 |
| 压缩比 | 通常 2:1 ~ 10:1 | **约 29:1 甚至更高**（58K → 2K） |
| 信息保真度 | 100%，无任何信息丢失 | LLM 自行判断什么重要什么不重要 |
| 类比 | 把文件装进 zip 包，随时解压还原 | 让人读完一本书后凭记忆写 2 页笔记，然后把书扔了 |

Compaction 之所以能做到 29:1 这样夸张的"压缩比"，正是因为它**丢掉了大量信息**。真正的无损压缩不可能达到这个比例（自然语言的信息熵决定了无损压缩比通常只有 2~3 倍）。

### 10.2 原始消息去哪了？

原始消息并没有彻底消失，它们还保存在磁盘上的 **JSONL 日志文件**中。但关键是：

- LLM 的上下文窗口里，原始消息被**摘要替换**了
- 没有任何机制能把 JSONL 中的原始消息**自动恢复**回上下文
- JSONL 日志只是用于审计和调试，模型**看不到**它们

从模型的视角来说，compaction 之后，原始内容就是"扔掉了"。

**一句话：Compaction 叫"压缩"其实是个误导性的名字。它不是 zip，不是 gzip，不是任何可逆编码。它的本质是：让 LLM 读完旧对话写一份笔记，然后用笔记替换掉原文。**

---

## 十一、被压缩的信息需要时怎么办？

### 11.1 一个真实场景

沿用 Todo App 的例子。在轮次 2，你和 OpenClaw 详细讨论了 CRUD API 的实现，todo.ts 里有 60 行代码，包括参数校验、错误处理、分页逻辑等。

**压缩后**，摘要里只剩下：

> Decisions: CRUD API 完成, routes/todo.ts

具体的参数校验逻辑？分页怎么实现的？**全没了。**

### 11.2 当你再次需要这些信息

你问了一句：

> "帮我给 todo.ts 的列表接口加一个按 priority 筛选的参数，跟之前的分页参数格式保持一致"

OpenClaw **不记得**之前分页参数的格式是 `?page=1&limit=20` 还是 `?offset=0&count=20`。

此时只有 3 条路：

| 方案 | 怎么做 | 效果 |
|------|-------|------|
| **重新读文件** | `read_file routes/todo.ts` | 能看到当前代码，但**之前讨论的设计理由**找不回来 |
| **靠 Memory 文件** | `read_file memory/2026-03-27.md` | 如果 Memory Flush 恰好记了这个细节能找回，但大概率**没记** |
| **让你重新说一遍** | "请问分页参数的格式是什么？" | 最常见的结果——**你得重复自己说过的话** |

### 11.3 对比：如果有外置记忆系统

| 时间点 | OpenClaw（compaction） | mem0 / claude-mem |
|--------|----------------------|-------------------|
| 讨论 CRUD 时 | 信息在上下文中 | 信息在上下文中，**同时**自动提取为持久事实 |
| compaction 之后 | 信息变成"CRUD API 完成" | 上下文也会缩短，但持久存储里有完整记录 |
| 再次问分页格式 | 不记得了 | 自动从向量数据库检索到相关记忆，注入上下文 |

核心区别：Compaction 的信息只存在于上下文窗口中，压缩就丢了（白板擦字）；外置记忆把信息抄了一份到笔记本里，白板擦了没关系。

### 11.4 现实中的应对策略

如果你只能用 OpenClaw（没有外置记忆），有以下方法减少损失：

**策略 1：关键信息主动写到文件里**

在讨论重要设计决策时，主动要求记录：

> "把刚才讨论的 API 设计写到 docs/api-design.md 里"

文件是永久的，不会被 compaction 吞掉。下次需要时可以重新读取。

**策略 2：把 recentTurnsPreserve 调大**

默认保留最近 3 轮原文。如果你的工作经常需要回顾近期内容，可以调到 6 甚至 10。代价是压缩后的上下文更大，留给新对话的空间更少。

**策略 3：及时分会话**

一个话题聊完了，开个新会话再开始下一个话题。避免一个超长会话触发多次 compaction 导致级联衰减。

**一句话总结：Compaction 丢掉的信息，要么重新从文件里读，要么让用户重新说一遍，没有第三条路。这就是为什么真正的记忆系统（mem0、claude-mem）会把信息存到上下文窗口之外。**

---

## 十二、总结

OpenClaw 的 Compaction 是一个**精心设计但本质有损的**上下文管理算法：

1. **算法核心**：分块 → 滚动 LLM 摘要 → 质量审计 → 结构化输出
2. **优点**：自适应分块、标识符保留、近期轮次原文保留、质量审计重试
3. **根本局限**：信息不可逆丢失、多次压缩级联衰减、完全依赖 LLM 摘要质量
4. **适用场景**：单次会话的上下文窗口管理，不适合作为长期记忆方案

如果你需要**跨会话的持久记忆**，Compaction 解决不了问题——你需要 claude-mem、codebuddy-mem 或 mem0 这样的**外置记忆系统**。
