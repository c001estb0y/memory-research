# Hermes + MemOS 远端记忆数据结构示例解析

> 数据来源：远端服务器上的 `~/.hermes/memos-plugin/data/memos.db`。  
> 说明：本文只使用关键字段和脱敏摘要，不展开认证文件、完整 prompt、完整回答和敏感配置。  
> 写作目标：用真实远端数据解释主脉络和主要数据结构，而不是只讲抽象流程。

## 1. 一句话总览

这套远端数据证明，Hermes（智能体运行时）接入 MemOS（记忆系统）后，不只是把聊天内容存下来，而是在做一条完整的经验演化链：

```text
Trace（执行痕迹）
  -> Episode（任务单元）
  -> Feedback（反馈）
  -> Reward（奖励评分）
  -> Policy（策略经验）
  -> World Model（环境认知）
  -> Skill（可调用技能）
```

当前远端库里已经有：

| 表 | 数量 | 说明 |
| --- | ---: | --- |
| `traces` | 72 | 每一步执行痕迹 |
| `episodes` | 17 | 多个 trace 组成的任务单元 |
| `feedback` | 10+ | 用户纠错、截图反馈等显式反馈 |
| `policies` | 10 | 从 trace / episode 中提炼出的策略经验 |
| `world_model` | 2 | 从 policy 中抽象出的环境认知 |
| `skills` | 3 | 从 policy 结晶出的候选技能 |
| `api_logs` | 1038 | 记忆系统内部工具调用日志 |

这意味着系统已经跑通了“记录、评分、提炼、结晶”的基本链路。但从数据质量看，它还处在早期：Skill 都是 `candidate`（候选状态），尚未被真实试用验证。

## 2. 主线例子：web-search skill 纠错链路

远端数据里最清楚的一条链路，是你围绕“联网搜索时是否应该使用 web-search skill”做过多次纠错。

对应的任务单元是：

```text
episode_id: ep_czpezhp81wkd
session_id: 20260520_104813_cac061f4
r_task: -0.165
trace_count: 10
```

这次任务里的关键事实是：

1. 你要求搜索 Anthropic 官方资料。
2. Agent（智能体）知道有本地搜索端点，但没有先加载 `web-search` skill。
3. Agent 直接通过 `curl` / `bash` 调了搜索端点。
4. 你追问“你刚刚使用 skill 去搜索的，还是直接调用 bash？”
5. 后续截图反馈继续指出：应该走 skill workflow（技能工作流），而不是绕过 skill。

远端 `traces` 表里保留了这些痕迹：

| trace | value | 摘要 | 含义 |
| --- | ---: | --- | --- |
| `tr_sjhgpvx0xxac` | -0.1056 | 你要求搜索王璐和 Anthropic 官方文章，并表达“以后不清楚优先网络搜索” | 用户偏好被记录 |
| `tr_jxw1d1sxz5kt` | -0.1587 | 用户截图指出 Agent 绕过 skill workflow | 负反馈证据 |
| `tr_j7hk6vb09fer` | -0.1650 | 用户继续用截图指出多次忘记联网搜索 skill | 更强的负反馈证据 |
| `tr_5ey2fvtbv0sw` | -0.1429 | Agent 总结 XML 标签与 Claude 训练关系 | 内容本身有用，但流程不合预期 |
| `tr_r4mce1cg0pkn` | -0.1174 | 用户运行 curl 搜索 Anthropic 文档 | 工具行为被记录 |

这条链路说明：MemOS 不只记“成功经验”，也会把“用户明确纠错”记录成反例证据。负向 trace 的 `priority` 可能是 0，不一定用于普通召回，但仍可用于 policy induction（策略归纳）和 decision repair（决策修复）。

## 3. Trace：最小执行证据

Trace（执行痕迹）是最底层的数据。它回答的是：

> 这一轮或这一步到底发生了什么？

远端 `traces` 表的核心字段包括：

| 字段 | 说明 |
| --- | --- |
| `id` | trace ID |
| `episode_id` | 属于哪个任务单元 |
| `session_id` | 属于哪个会话 |
| `user_text` | 用户输入 |
| `agent_text` | Agent 输出 |
| `summary` | 这一步的摘要 |
| `tool_calls_json` | 工具调用记录 |
| `reflection` | 系统对这一步的反思 |
| `value` | 这一步对任务成败的价值分 |
| `priority` | 后续召回优先级 |
| `tags_json` | 标签，比如 `skill`、`terminal`、`http` |

一个真实例子是 `tr_jxw1d1sxz5kt`：

```text
value: -0.1587
priority: 0
tags: ["http", "shell"]
summary: 用户截图指出 Agent 绕过了正确的 skill-loading workflow（技能加载流程）
reflection: Agent 意识到自己把“知道 skill 存在”误当成“已经加载并遵循 skill 内容”
```

这里的关键不是“搜索有没有结果”，而是“执行流程是否符合用户预期”。所以它虽然包含搜索相关工作，却被打成负值。

## 4. Episode：把多条 trace 合成任务单元

Episode（任务单元）比 trace 更高一层。它回答的是：

> 这一组 trace 合起来，是一个什么任务？整体做得怎么样？

远端 `episodes` 表的核心字段包括：

| 字段 | 说明 |
| --- | --- |
| `id` | episode ID |
| `session_id` | 所属会话 |
| `started_at` / `ended_at` | 开始和结束时间 |
| `trace_ids_json` | 包含哪些 trace |
| `r_task` | 任务级评分 |
| `status` | `open` 或 `closed` |
| `meta_json` | host、agent identity、namespace 等元信息 |

几个真实 episode 对比：

| episode | r_task | trace 数 | 说明 |
| --- | ---: | ---: | --- |
| `ep_czpezhp81wkd` | -0.165 | 10 | web-search skill 流程纠错，整体负向 |
| `ep_n8tsyrjsk9wf` | 0.225 | 7 | Obsidian 保存任务，整体正向 |
| `ep_3ncepa2czsys` | 0.645 | 14 | 模型网关文档修正与总结，整体高正向 |
| `ep_6g22gzgw7x53` | -0.03 | 15 | 写日记日期逻辑纠错，轻微负向 |

这说明 `r_task` 是任务级信号；它会继续影响 episode 内多条 trace 的 `value`。

## 5. Feedback：用户纠错如何进入系统

Feedback（反馈）是用户对任务的显式或隐式评价。它回答的是：

> 用户到底有没有明确表达满意、不满或纠正？

远端 `feedback` 表的核心字段包括：

| 字段 | 说明 |
| --- | --- |
| `id` | feedback ID |
| `episode_id` | 关联的任务单元 |
| `trace_id` | 可选，关联某条 trace |
| `channel` | `explicit` 或 `implicit` |
| `polarity` | `positive`、`negative`、`neutral` |
| `magnitude` | 强度 |
| `rationale` | 反馈理由 |
| `raw_json` | 原始反馈内容 |

真实例子：

```text
id: 6ef4733f-50a4-47d0-8ffa-1a4fad285f04
channel: explicit
polarity: negative
magnitude: 1.0
episode_id: ep_czpezhp81wkd
rationale: 用户通过截图指出 Agent 没有遵守 web-search skill 流程
```

再看另一个日记任务例子：

```text
polarity: negative
magnitude: 1.0
rationale: 你做的不对，你不应该写 5.21 的日记，每天写日记的时候都是要写前一天的日记
episode_id: ep_6g22gzgw7x53
```

这说明“显式反馈”不一定是点赞/点踩按钮。只要用户在聊天里明确纠错，Hermes verifier_feedback（校验反馈）就可能把它写入 `feedback` 表。

这里要区分 Hermes 和 MemOS 的职责边界：

```text
用户纠错消息
  -> Hermes 运行时捕获 / 判定 / 上报
  -> MemOS 写入 feedback 表
  -> MemOS reward 阶段计算 r_task
  -> MemOS 回传 trace.value
  -> MemOS 归纳 policy / world model / skill
```

也就是说，如果只看 `verifier_feedback` 这一步，它更偏 Hermes 功能：Hermes 在聊天运行时识别用户这句话像纠错、反问或评价，并把它包装成结构化反馈。  

但如果问“这条纠错如何影响记忆、评分和技能生成”，那就是 MemOS 的功能：MemOS 负责存储 feedback，关联 episode / trace，并在后续 reward、backprop、policy induction、decision repair 和 skill crystallize 中使用它。

## 6. Reward：把反馈回传到 trace

Reward（奖励评分）不是一张单独必须看的业务表，而是一段评分过程。它把 episode 级反馈传回 trace。

可以这样理解：

```text
用户纠错
  -> feedback.polarity = negative
  -> episode.r_task 变低
  -> episode 内相关 trace.value 变低
  -> 后续 policy induction 把这些 trace 当成反例
```

在 web-search 例子里：

```text
episode.r_task = -0.165
tr_jxw1d1sxz5kt.value = -0.1587
tr_j7hk6vb09fer.value = -0.1650
```

这里容易误解的一点是：负向反馈不会直接把每条 trace 的 `value` 设成 `-1`。它先进入 episode 级评分，再被回传到 trace。

第一步，用户纠错进入 `feedback` 表：

```text
feedback.polarity = negative
feedback.magnitude = 1.0
```

这表示“这是一次强负反馈”，但它还不是最终价值分。

第二步，Reward 阶段把反馈、任务结果和系统判断合成 episode 级任务分：

```text
ep_czpezhp81wkd.r_task = -0.165
```

也就是说，强负反馈经过 rubric（评分规则）或 heuristic（启发式规则）后，被映射成一个温和负分，而不是直接变成 `-1`。

直觉上，`r_task` 是 episode 级别的任务总评分。它回答的是：

```text
这一整个任务单元，最后对用户目标来说是成功、失败，还是混合？
```

可以按 `[-1, 1]` 理解：

| 区间 | 直觉含义 |
| --- | --- |
| 接近 `+1` | 任务很好地满足用户目标 |
| 接近 `0` | 中性、混合、信息不足 |
| 接近 `-1` | 明显失败或违背用户预期 |

因此 `feedback.magnitude=1.0` 和 `r_task=-0.165` 并不矛盾。前者表示用户反馈强度，后者表示系统综合评估后的任务结果。web-search 这次不是完全失败：搜索内容可能有价值，但关键流程违背了“应该使用 web-search skill”的预期，所以最终是温和负分。

第三步，系统把 episode 分数反向传播到 episode 内每条 trace。简化公式可以写成：

```text
V_T = R_human
V_t = alpha_t * R_human + (1 - alpha_t) * gamma * V_{t+1}
priority = max(V_t, 0) * time_decay
```

其中：

| 符号 | 含义 |
| --- | --- |
| `R_human` | episode 级任务满意度分，对应远端的 `r_task` |
| `alpha_t` | 当前 trace 的 reflection 质量权重 |
| `gamma` | 从后一条 trace 向前传播时的折扣 |
| `V_t` | 当前 trace 的最终 `value` |
| `priority` | 未来召回优先级，负值会被 `max(value, 0)` 截成 0 |

为什么要把 episode 的 reward 反向传播回 trace？核心是 credit assignment（归因分配）。

episode 是由多条 trace 组成的，reward 只是在任务级别说“整体好不好”。但系统后续真正要学习的是：

```text
哪一步值得复用？
哪一步应该避免？
哪个工具调用导致问题？
哪段回答其实没错，只是被后续流程问题牵连？
```

如果只给 episode 一个 `r_task`，系统只知道“这一整个任务不好”，但不知道坏在哪里。反向传播就是把整体结果分摊回每一步 trace：

```text
episode 结果不好
  -> 找到更接近失败原因的 trace
  -> 给这些 trace 更强的负 value
  -> 给更早、更间接的 trace 较弱负 value
```

所以同一个 episode 里，不同 trace 的负值会有梯度：

| trace | value | 解释 |
| --- | ---: | --- |
| `tr_j7hk6vb09fer` | -0.1650 | 最接近最终负反馈，几乎等于 episode 任务分 |
| `tr_jxw1d1sxz5kt` | -0.1587 | 明确指出绕过 skill workflow，强相关 |
| `tr_5ey2fvtbv0sw` | -0.1429 | 回答内容本身有用，但流程受负反馈影响 |
| `tr_r4mce1cg0pkn` | -0.1174 | 工具行为被记录，但离最终纠错更远 |
| `tr_sjhgpvx0xxac` | -0.1056 | 更早的用户偏好表达，负值较浅 |

越靠近最终负反馈、越能被归因为问题原因的 trace，`value` 越接近 `r_task=-0.165`；更早、更间接的 trace 会被折扣，负值会变浅。

所以系统学到的不是“不要搜索”，而是：

```text
不要绕过 web-search skill 直接 curl/bash。
```

这是负反馈转化为可复用经验的关键。

## 7. Policy：从多条证据中提炼做法

Policy（策略经验）是 L2 层。它回答的是：

> 下次遇到类似情况，应该怎么做？

远端 `policies` 表的核心字段包括：

| 字段 | 说明 |
| --- | --- |
| `id` | policy ID |
| `title` | 标题 |
| `trigger` | 触发条件 |
| `procedure` | 推荐做法 |
| `verification` | 如何验证做对了 |
| `boundary` | 适用边界 |
| `support` | 支持度 |
| `gain` | 收益估计 |
| `status` | `candidate`、`active`、`archived` |
| `experience_type` | 经验类型 |
| `evidence_polarity` | 证据倾向 |
| `source_trace_ids_json` | 来源 trace |
| `source_feedback_ids_json` | 来源反馈 |
| `decision_guidance_json` | 偏好和反模式 |
| `skill_eligible` | 是否可进一步结晶为 Skill |

web-search 这条来源 policy 是：

```text
id: po_06y9qwkqf7w3
support: 1
gain: 1.0
experience_type: success_pattern
evidence_polarity: positive
source_feedback_ids_json: ["6ef4733f-50a4-47d0-8ffa-1a4fad285f04"]
```

这个 policy 的标题和 trigger 有噪声：

```text
title: Success: 成功模式：[The user sent an image...]
trigger: 当用户要求格式化或转换数据时
```

但它的 decision guidance（决策指导）里保留了真正有价值的信息：

```text
preference:
Before performing any web search, always call skill_view('web-search') first
to load and follow the skill's defined procedure.

anti_pattern:
Agent repeatedly skips skill_view() and directly executes bare curl/bash commands
for web searches.
```

翻译成人话就是：

```text
联网搜索前先读 web-search skill；
不要凭记忆直接 curl 搜索端点。
```

这也是为什么只看 policy 标题会误判，必须看 `procedure`、`decision_guidance_json` 和来源 trace。

## 8. Support 和 gain：怎么看成熟度

Support（支持度）表示这条经验有多少证据支撑。Gain（收益）表示使用这条经验相对不用它，预期能带来多少改善。

在 policy 层：

```text
support: 证据强度
gain: 使用该策略后的价值提升估计
```

在 skill 层，当前远端数据里 `skills.support` 基本来自来源 policy：

| skill | 来源 policy | policy.support | skill.support |
| --- | --- | ---: | ---: |
| `ack_self_correction_summarize` | `po_hd2pnjy1y219` | 28 | 28 |
| `rebase_on_push_rejection` | `po_epa5yvcjb3zt` | 6 | 6 |
| `web_search_via_skill_workflow` | `po_06y9qwkqf7w3` | 1 | 1 |

所以不能把 `evidence_anchors_json` 的条目数直接当 support。

比如 `web_search_via_skill_workflow` 有 6 个 evidence anchors：

```text
tr_sjhgpvx0xxac
tr_jxw1d1sxz5kt
tr_r4mce1cg0pkn
tr_5ey2fvtbv0sw
tr_2k0xdpt2q41p
tr_j7hk6vb09fer
```

但它的 support 仍然是 1。原因是：anchors 更像“可展示的证据样本”，support 是 policy 归纳阶段计算出的支持度。

## 9. World Model：抽象环境认知

World Model（世界模型/环境认知）是 L3 层。它回答的是：

> 这个运行环境有什么稳定规律、约束和背景？

远端 `world_model` 表的核心字段包括：

| 字段 | 说明 |
| --- | --- |
| `id` | world model ID |
| `title` | 标题 |
| `body` | 文字说明 |
| `policy_ids_json` | 来源 policy |
| `structure_json` | 结构化环境、推理、约束 |
| `domain_tags_json` | 领域标签 |
| `confidence` | 置信度 |
| `status` | 状态 |

当前有 2 条：

| world model | confidence | 来源 policy | 说明 |
| --- | ---: | --- | --- |
| `wm_j069n86s6m9p` | 0.38 | `po_06y9qwkqf7w3` | 被标成 Docker chat interface，但证据不足 |
| `wm_r9dw4q45f5n5` | 0.25 | `po_6ze0zv6594gt` | 低信号领域 stub |

第一个 world model 的正文大意是：

```text
没有足够证据恢复 Docker 环境结构；
证据主要来自聊天截图和格式化/转换工作流；
没有容器拓扑、文件系统布局、网络命名空间等事实。
```

这说明 L3 不是一定正确。它依赖上游 policy 的标签和证据。如果 L2 已经把 web-search 纠错误标成 `docker`，L3 也会跟着抽象出低质量环境认知。

## 10. Skill：从 policy 结晶成候选能力

Skill（可调用技能）是比 policy 更接近执行的一层。它回答的是：

> 下次能不能把这个经验当成一个可调用流程来用？

远端 `skills` 表的核心字段包括：

| 字段 | 说明 |
| --- | --- |
| `id` | skill ID |
| `name` | 技能名 |
| `status` | `candidate`、`active`、`archived` |
| `invocation_guide` | 调用说明 |
| `procedure_json` | 结构化步骤、参数、例子 |
| `eta` | 可靠度估计 |
| `support` | 支持度 |
| `gain` | 收益 |
| `trials_attempted` | 试用次数 |
| `trials_passed` | 试用通过次数 |
| `source_policies_json` | 来源 policy |
| `evidence_anchors_json` | 证据锚点 |
| `usage_count` | 实际使用次数 |

当前 3 条 Skill 都是候选状态：

| skill | eta | support | gain | trials | usage | 解释 |
| --- | ---: | ---: | ---: | --- | ---: | --- |
| `ack_self_correction_summarize` | 0.2193 | 28 | 0.2193 | 0/0 | 0 | 用户说“不用改/我已修正”时，简短确认并总结学到的差异 |
| `rebase_on_push_rejection` | 0.1 | 6 | 0.0258 | 0/0 | 0 | `git push` 被远端拒绝时，先 rebase 再 push |
| `web_search_via_skill_workflow` | 1.0 | 1 | 1.0 | 0/0 | 0 | 联网搜索前先加载 `web-search` skill，再按其流程搜索 |

这里有两个重点。

第一，`candidate` 不等于“已经成熟可用”。这 3 条 Skill 的 `trials_attempted`、`trials_passed`、`usage_count` 都是 0，说明它们还没有经过真实试用闭环。

第二，`web_search_via_skill_workflow` 有元技能倾向。它不是“搜索能力本身”，而是“要求 Hermes 使用 `web-search` skill 的流程”。更自然的设计可能是：

```text
web-search 本身作为 Skill；
MemOS 记录一条 preference（偏好）/ policy：
需要实时信息时优先调用 web-search；
Agent Loop（智能体循环）直接召回 web-search，而不是召回一个“使用 web-search 的 Skill”。
```

所以这条候选 Skill 有解释价值，但不一定是最好的产品形态。

## 11. API Logs：系统内部真的在跑哪些步骤

API Logs（接口日志）记录 MemOS 内部工具调用。它回答的是：

> 系统有没有真的执行搜索、写入、归纳、生成 Skill？

远端 `api_logs` 表的核心字段包括：

| 字段 | 说明 |
| --- | --- |
| `tool_name` | 内部工具名 |
| `input_json` | 输入 |
| `output_json` | 输出 |
| `duration_ms` | 耗时 |
| `success` | 是否成功 |
| `called_at` | 调用时间 |

当前调用统计：

| tool_name | count | ok | fail | 说明 |
| --- | ---: | ---: | ---: | --- |
| `system_model_status` | 742 | 625 | 117 | 模型状态检查 |
| `system_error` | 136 | 0 | 136 | 错误记录 |
| `memory_add` | 33 | 32 | 1 | 写入记忆 |
| `memos_search` | 20 | 20 | 0 | 记忆检索 |
| `session_relation_classify` | 19 | 19 | 0 | 会话关系分类 |
| `skill_generate` | 19 | 7 | 12 | 技能生成尝试 |
| `policy_evolve` | 13 | 13 | 0 | policy 演化 |
| `memory_search` | 12 | 12 | 0 | 记忆搜索 |
| `world_model_evolve` | 12 | 12 | 0 | world model 演化 |
| `world_model_generate` | 12 | 2 | 10 | world model 生成 |
| `policy_generate` | 6 | 6 | 0 | policy 生成 |
| `skill_evolve` | 5 | 5 | 0 | skill 演化 |
| `task_done` | 5 | 5 | 0 | 任务完成记录 |
| `task_failed` | 4 | 0 | 4 | 失败任务记录 |

这组日志能证明：远端不是只展示静态表，而是持续运行 `memory_add`、`policy_evolve`、`world_model_generate`、`skill_generate` 这类后台步骤。

## 12. 当前数据质量判断

从这批真实数据看，系统已经具备四层演化能力，但质量控制还需要加强。

已经跑通的部分：

```text
聊天与工具行为能写成 trace；
多条 trace 能聚合成 episode；
用户纠错能进入 feedback；
reward 能把反馈变成 value；
policy 能从 trace / feedback 中归纳出来；
world model 和 skill 都已经开始生成。
```

主要问题：

1. Policy 标题和 trigger 有噪声。`web_search_via_skill_workflow` 的来源 policy 把触发条件写成“格式化或转换数据”，明显偏题。
2. World model 置信度偏低。两个 world model 的 confidence 分别是 0.38 和 0.25，说明系统自己也不太确信。
3. Skill 还没有通过试用。3 条 Skill 都是 candidate，`usage_count=0`。
4. `web_search_via_skill_workflow` 可能是过度包装。它更适合成为“调用 web-search 的偏好规则”，不一定要独立成 Skill。
5. 负反馈能被利用，但也容易污染上游标签。如果截图识别、标题归纳或标签不准，后续 L3 / Skill 会继续放大噪声。

## 13. 最终理解

这套数据可以用一句话概括：

```text
Hermes 把对话和工具执行交给 MemOS；
MemOS 先记成 trace，再按 episode 汇总；
用户纠错进入 feedback，reward 把反馈回传成 value；
高价值或高信号模式被归纳成 policy；
policy 再尝试抽象 world model 或结晶 skill；
但当前远端生成的 skill 仍是候选，需要真实试用和人工/用户反馈继续筛选。
```

如果只看 `skills` 页面，会以为系统已经“学会了三个技能”。更准确的说法是：

```text
远端已经从记忆中生成了 3 个候选技能；
它们能解释系统学到了什么；
但还不能说明这些技能已经稳定、正确、可自动复用。
```
