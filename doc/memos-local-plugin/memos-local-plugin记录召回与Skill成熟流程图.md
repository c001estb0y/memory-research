# MemOS Local Plugin：记录、召回与 Skill 成熟流程图

> 独立说明文件，不与原来的使用流程文档混在一起。  
> 对应 HTML：`memos-local-plugin记录召回与Skill成熟流程图.html`  
> 源码位置：`D:\GitHub\memory-research\MemOS\apps\memos-local-plugin`

本文专门回答三个问题：

1. `memos-local-plugin` 如何从 OpenClaw / Hermes 记录执行数据？
2. 这些记录如何被标记为“有用”或“没用”？
3. 共性经验如何进一步变成成熟 Skill，并在下一次任务中被召回？

---

## 1. 总览流程图

```text
Agent 执行
  ├─ 用户输入
  ├─ assistant 输出
  ├─ thinking
  ├─ tool calls
  └─ tool results
        ↓
Adapter 标准化
  ├─ OpenClaw: TypeScript in-process hooks
  └─ Hermes: Python MemoryProvider + JSON-RPC bridge
        ↓
MemoryCore
        ↓
L1 Trace
  ├─ userText
  ├─ agentText
  ├─ agentThinking
  ├─ toolCalls
  ├─ reflection
  └─ summary
        ↓
评分回传
  ├─ alpha: step reflection 质量分
  ├─ R_human: episode 任务满意度分
  ├─ value: 每条 trace 的价值
  └─ priority: 未来召回优先级
        ↓
经验提炼
  ├─ L2 Policy: 跨任务过程策略
  ├─ L3 World Model: 环境认知
  └─ Skill: 可调用能力
        ↓
下一轮 Turn Start
        ↓
三层召回
  ├─ Tier 1: Skill
  ├─ Tier 2: Trace / Episode
  └─ Tier 3: World Model
        ↓
InjectionPacket 注入 Agent prompt
```

一句话概括：

```text
记录阶段负责把执行变成证据；
评分阶段负责判断哪些证据有价值；
提炼阶段负责把重复高价值证据变成策略和 Skill；
召回阶段负责在下一次任务开始前把最有用的经验注入 prompt。
```

---

## 2. 记录链路：插件如何拿到执行数据

### 2.1 OpenClaw 路径

OpenClaw adapter 是 TypeScript in-process 插件，直接运行在 OpenClaw 进程里。

```text
OpenClaw Host
  ↓
adapters/openclaw/index.ts
  ↓
createOpenClawBridge()
  ↓
OpenClaw hooks
  ├─ before_prompt_build
  ├─ before_tool_call
  ├─ after_tool_call
  ├─ agent_end
  ├─ session_start
  └─ session_end
  ↓
MemoryCore
```

关键 hook：

| Hook | 时机 | 送给 MemOS 的数据 |
|---|---|---|
| `before_prompt_build` | prompt 构造前 | 当前用户输入、上下文、可用于召回的查询 |
| `before_tool_call` | 工具调用前 | 工具名、调用 ID、开始时间 |
| `after_tool_call` | 工具调用后 | 工具输出、错误码、耗时、成功/失败 |
| `agent_end` | Agent 本轮结束 | assistant 回复、thinking、工具调用序列、最终结果 |
| `session_start/session_end` | 会话开始/结束 | session 生命周期 |

OpenClaw 这条路径的特点是：没有跨语言 RPC，adapter 可以直接 import core 并调用 `core.onTurnStart()`、`core.onTurnEnd()`、`recordToolOutcome()`。

### 2.2 Hermes 路径

Hermes 是 Python adapter，不能直接调用 TypeScript core，因此通过 JSON-RPC bridge。

```text
Hermes Agent
  ↓
Python MemoryProvider
  ↓
post_tool_call / post_llm_call
  ↓
bridge_client.py
  ↓
JSON-RPC over stdio
  ↓
bridge.cts
  ↓
TypeScript MemoryCore
```

Hermes adapter 主要负责：

- 实现 Hermes 的 `MemoryProvider` 接口；
- 注册 `post_tool_call`、`post_llm_call` 等 hook；
- 在 Python 侧收集工具结果和模型输出；
- 通过 JSON-RPC 调用 TypeScript core 的 `turn.start`、`turn.end` 等方法。

### 2.3 进入 core 后统一成 L1 Trace

无论来自 OpenClaw 还是 Hermes，进入 core 后都会统一为类似结构：

| 字段 | 含义 | 后续用途 |
|---|---|---|
| `userText` | 用户请求、环境状态、观察 | 作为 state evidence |
| `agentText` | Agent 输出或回答 | 作为 action evidence |
| `agentThinking` | Host 能提供时的模型 thinking | 用于 reflection 评分 |
| `toolCalls` | 工具名、输入、输出、错误码 | 判断动作是否有效 |
| `reflection` | 当前 step 的反思 | 用于 alpha 评分 |
| `summary` | 短摘要 | viewer 展示和检索 embedding |

这一步只是“记录事实”，还没有证明这些事实有用。

---

## 3. 评分机制：哪些数据被标记为有用

插件不是把所有历史记录平等保存、平等召回。它会给不同层级打分。

### 3.1 Step 级：`alpha`

`alpha` 是每个 step 的 reflection 质量分。

```text
alpha ∈ [0, 1]
```

它回答的问题是：

> 这条 step 的反思是否可信、具体、有因果解释、能迁移到未来任务？

源码中的 `REFLECTION_SCORE_PROMPT` / `BATCH_REFLECTION_PROMPT` 会看：

| 评分轴 | 含义 |
|---|---|
| faithfulness | 是否忠实描述真实 thinking、action、tool call、outcome |
| causal insight | 是否说明为什么这样做、为什么成功或失败 |
| transferability | 是否能迁移到相似任务 |
| concreteness | 是否包含具体命令、错误、路径、文件、决策 |

如果 reflection 是空话，例如“我应该更仔细”，`alpha` 会很低甚至为 0。

注意：`alpha` 不是用户满意度，它只是 step 级证据质量分。

### 3.2 Episode 级：`R_human`

`R_human` 是整个任务的结果分。

```text
R_human ∈ [-1, 1]
```

它回答的问题是：

> 这个任务最终是否满足人的目标？

`core/reward` 会构造 task summary，再结合用户反馈。默认 LLM rubric 会从三轴打分：

| 轴 | 含义 |
|---|---|
| `goal_achievement` | 用户真实目标是否完成 |
| `process_quality` | 过程是否合理、高效、少折腾 |
| `user_satisfaction` | 用户是否满意，或后续语气是否接受 |

组合公式：

```text
R_human = 0.45 * goal_achievement
        + 0.30 * process_quality
        + 0.25 * user_satisfaction
```

这里的 “human” 不等于用户每次手动打分。它代表“人的目标是否被满足”。来源可以是：

| 来源 | 是否需要用户主动打分 | 说明 |
|---|---|---|
| 显式用户反馈 | 可选 | 例如“很好”“不对”“以后别这样” |
| 隐式反馈 | 不需要 | 是否自然收尾、是否继续纠错、语气是否满意 |
| LLM rubric | 自动 | 根据 task summary + feedback 打三轴分 |
| heuristic fallback | 自动 | LLM 不可用时保守映射 |

### 3.3 Trace 级：`value` 和 `priority`

有了 episode 级 `R_human` 后，系统会把这个任务结果反向传播到每条 trace。

```text
V_T = R_human
V_t = alpha_t * R_human + (1 - alpha_t) * gamma * V_{t+1}
priority = max(V_t, 0) * time_decay
```

这一步会产生两个关键值：

| 字段 | 含义 | 影响 |
|---|---|---|
| `value` | 这条 trace 对任务成败的贡献 | 用于 L2 policy gain |
| `priority` | 这条 trace 未来召回的优先级 | 用于 Tier 2 retrieval |

直觉上：

- 高 `value`：这条路径对成功有贡献；
- 高 `priority`：这条经验有价值、还比较新，值得优先召回；
- 低或负 `value`：不适合作为正向经验，但可以作为失败反例保留。

---

## 4. 从高价值 Trace 到 L2 Policy

L2 policy 是跨任务归纳出来的“过程策略”。

它不是记录“某次任务发生了什么”，而是归纳：

```text
当看到状态 X 时，
优先采取动作 Y，
用 Z 验证，
注意 W 边界。
```

### 4.1 L2 生成流程图

```text
高价值 L1 traces
  ↓
按相似 state/action 聚类
  ↓
candidate pool
  ↓
LLM induction
  ↓
L2 policy candidate
  ↓
support / gain 计算
  ↓
candidate / active / archived
```

### 4.2 `support`

`support` 回答：

> 这个策略有多少证据支持？

它来自源 trace / episode 的数量。

但要注意，到了 Skill 层，`support` 不一定等于 `evidenceAnchors` 的条目数。真实远端数据里，`skills.support` 是落在 `skills` 表中的字段，每个 Skill 通过 `source_policies_json` 指向来源 policy；当前看到的候选 Skill 基本是从来源 policy 继承或转写 support。

例如：

```text
ack_self_correction_summarize <- po_hd2pnjy1y219: policy.support=28, skill.support=28
rebase_on_push_rejection      <- po_epa5yvcjb3zt: policy.support=6,  skill.support=6
web_search_via_skill_workflow <- po_06y9qwkqf7w3: policy.support=1,  skill.support=1
```

因此 `evidenceAnchors` 更像溯源样本和解释材料；`support` 是归纳阶段计算出的支持度，不能简单按 anchors 数量相加。

### 4.3 `gain`

`gain` 回答：

> 使用这个策略是否真的比没用它更好？

近似公式：

```text
gain = mean(V_with_policy) - blended_mean(V_without_policy)
```

其中：

- `V_with_policy` 是使用该策略的 trace 价值；
- `V_without_policy` 是没有使用该策略的对照 trace 价值；
- `blended_mean` 会向中性基线 `0.5` 做 shrinkage，避免早期没有失败对照组时所有 gain 都接近 0。

直觉判断：

| 情况 | gain 表现 | 结果 |
|---|---|---|
| 策略经常出现在成功路径中 | 正 | 更可能 active |
| 策略效果一般 | 接近 0 | 留在 candidate |
| 策略导致失败或低质量路径 | 负 | 可能 archived |

### 4.4 Policy 状态迁移

```text
candidate
  ├─ support >= minSupport 且 gain >= minGain
  │     ↓
  │   active
  │
  └─ 否则继续 candidate

active
  ├─ gain < archiveGain 或 support <= 0
  │     ↓
  │   archived
  │
  └─ 否则保持 active
```

只有 active policy 才有资格进一步变成 Skill。

---

## 5. 从 Active Policy 到成熟 Skill

Skill 是比 policy 更进一步的形态：它是可调用、可验证、可生命周期管理的能力包。

### 5.1 Skill 结晶流程图

```text
Active L2 Policy
  ↓
Eligibility Check
  ├─ policy.status === active
  ├─ policy.gain >= skill.minGain
  ├─ policy.support >= skill.minSupport
  ├─ has success anchor
  └─ not already covered by newer skill
        ↓
LLM Skill Crystallization
  ↓
Skill Draft
  ↓
Deterministic Verifier
  ├─ tool coverage
  └─ evidence resonance
        ↓
Candidate Skill
  ↓
Trial / Feedback / Reward Drift
  ↓
Active Skill 或 Archived Skill
```

### 5.2 Eligibility：先判断 policy 是否够格

源码中的 `eligibility.ts` 要求：

1. `policy.status === "active"`；
2. `policy.gain >= skill.minGain`；
3. `policy.support >= skill.minSupport`；
4. 有正向 success anchor；
5. 没有被更新的非归档 Skill 覆盖。

不满足这些条件，就不会进入 Skill 结晶。

### 5.3 LLM crystallize：把策略写成 Skill

满足条件后，`SKILL_CRYSTALLIZE_PROMPT` 会让 LLM 根据以下输入生成 Skill draft：

- L2 policy；
- 正向 evidence traces；
- 负向 counter examples；
- repair hints；
- 已有 skill 命名空间；
- evidence 中真实出现过的工具列表。

输出包括：

| 字段 | 说明 |
|---|---|
| `name` | snake_case 技能名 |
| `display_title` | 面向用户的标题 |
| `summary` | 什么时候用、做什么 |
| `parameters` | 参数 schema |
| `preconditions` | 前置条件 |
| `steps` | 执行步骤 |
| `examples` | 示例 |
| `tools` | 允许使用的工具 |
| `decision_guidance` | prefer / avoid |
| `tags` | 标签 |

### 5.4 Verifier：防止 LLM 编造 Skill

Skill draft 生成后，不会直接启用。源码里还有确定性 verifier：

| 校验 | 目的 |
|---|---|
| tool coverage | Skill 声称使用的工具必须出现在 evidence 中 |
| evidence resonance | Skill 的 summary / steps 要和证据 trace 有足够重叠 |

这一步很关键：Skill 不是 LLM 自己“想出来”的能力，而必须能回到历史证据。

### 5.5 Candidate Skill：先试用，再成熟

通过 verifier 后，新 Skill 默认仍是 `candidate`。

它接下来靠生命周期信号成熟：

| 信号 | 来源 | 对 Skill 的影响 |
|---|---|---|
| `trial.pass` | Skill 被调用后成功 | 增加通过次数，提高可靠度 |
| `trial.fail` | Skill 被调用后失败 | 增加失败证据，降低可靠度 |
| `reward.updated` | 源 policy 收益变化 | 重新影响 Skill 的 `eta` |
| `user.positive` | 用户明确正反馈 | 提高 `eta` |
| `user.negative` | 用户明确负反馈 | 降低 `eta` |

### 5.6 Skill 状态迁移

```text
candidate
  ├─ trialsAttempted >= candidateTrials
  │  且 eta >= minEtaForRetrieval
  │     ↓
  │   active
  │
  └─ eta 过低
        ↓
      archived

active
  ├─ eta < archiveEta
  │     ↓
  │   archived
  │
  └─ eta 足够
        ↓
      保持 active
```

`eta` 可以理解为 Skill 的可靠度。

---

## 6. 召回链路：有用经验如何回到下一次 Prompt

记录和评分只是前半段。真正有价值的是：下一次任务开始前，插件能把最相关的经验召回。

### 6.1 召回总流程

```text
Turn Start
  ↓
adapter 调用 core.onTurnStart()
  ↓
构造查询
  ├─ 当前用户请求
  ├─ workspace
  ├─ namespace
  ├─ tool context
  └─ 历史状态
        ↓
三层检索
  ├─ Tier 1: Skill
  ├─ Tier 2: Trace / Episode
  └─ Tier 3: World Model
        ↓
排序 / 过滤 / 渲染
        ↓
InjectionPacket
        ↓
注入 Agent prompt
```

### 6.2 Tier 1：Skill

Skill 优先级最高，因为它已经通过多轮筛选：

- 源自 active policy；
- 有 evidence；
- 通过 verifier；
- 有 trial / eta 生命周期；
- 可能已经被用户正反馈强化。

Skill 注入给 Agent 的通常不是历史聊天，而是过程能力：

- 何时使用；
- 前置条件；
- 参数；
- 步骤；
- 示例；
- preference；
- anti-pattern；
- 适用边界。

### 6.3 Tier 2：Trace / Episode

Trace / Episode 用来补具体历史细节。

适合情况：

- 没有命中成熟 Skill；
- 当前任务是 edge case；
- 需要真实错误信息、命令、文件路径；
- 需要回看某个历史任务 timeline。

Trace 排名会结合：

- embedding 相似度；
- 关键词匹配；
- `priority`；
- 时间衰减；
- tag / namespace 等过滤条件。

### 6.4 Tier 3：World Model

World Model 是环境认知，不直接告诉 Agent 做什么。

它回答：

- 这个项目目录结构如何？
- 哪些约束长期存在？
- 哪些平台/环境差异会影响执行？
- 哪些事实会导致哪些结果？

它能减少重复探索。

---

## 7. 到底是 human 打分，还是 agent 自动打分？

准确答案：**混合评分**。

不是纯 human scoring，也不是 agent 自己说了算。

| 分数/信号 | 来源 | 人工还是自动 | 用途 |
|---|---|---|---|
| `alpha` | LLM reflection judge | 自动 | 判断 step reflection 是否可信、具体、可迁移 |
| `R_human` | 用户反馈 + LLM rubric + heuristic | 混合 | 判断整个任务是否满足人的目标 |
| `value` | backprop 公式 | 自动 | 判断 trace 对任务成败的贡献 |
| `priority` | value + 时间衰减 | 自动 | 决定 trace 召回优先级 |
| `support` | 证据计数 | 自动 | 判断 policy 是否有足够样本 |
| `gain` | with/without 对照价值计算 | 自动 | 判断 policy 是否真的带来收益 |
| `eta` | policy gain + trial + 用户反馈 | 混合 | 判断 Skill 是否可靠 |
| `user.positive/negative` | 用户显式反馈 | 人工 | 直接影响 Skill 生命周期 |

### 7.1 用户不必每轮手动打分

系统默认会自动估计：

```text
task summary + feedback
  ↓
LLM rubric / heuristic
  ↓
R_human
  ↓
trace value
  ↓
policy gain
  ↓
skill eta
```

### 7.2 但用户反馈是强信号

如果用户明确说：

```text
很好，以后就按这个方式做。
```

那么这类反馈会强化相关 trace / policy / skill。

如果用户说：

```text
这个做法不对，以后别这样。
```

那么系统会把相关路径作为负向信号，影响：

- `R_human`；
- trace value；
- policy gain；
- Skill eta；
- decision guidance 中的 anti-pattern。

### 7.3 最重要的理解

```text
Skill 不是由 LLM 一次总结出来就成熟。

它必须先有高价值 trace，
再有 active policy，
再通过 LLM crystallize，
再通过 verifier，
再经历 trial 和 eta 生命周期，
最后才成为 active Skill。
```

---

## 8. 最短记忆版

如果只记一条：

```text
memos-local-plugin 的学习机制是：

记录执行事实 → 给 step 和 episode 打分 → 回传 trace 价值 →
归纳 policy → 验证并结晶 Skill → 通过 trial/用户反馈成熟 →
下一轮按 Skill / Trace / World Model 三层召回。
```

其中：

- `alpha` 评的是 step 反思质量；
- `R_human` 评的是任务是否满足人的目标；
- `value/priority` 决定 trace 是否值得召回；
- `support/gain` 决定 policy 是否有用；
- `eta/trials/user feedback` 决定 Skill 是否成熟。
