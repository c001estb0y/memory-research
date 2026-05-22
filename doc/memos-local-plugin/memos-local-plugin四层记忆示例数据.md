# MemOS Local Plugin 四层记忆示例数据

> 说明：以下示例是根据 `MemOS/apps/memos-local-plugin` 源码字段与流程“捏造”的数据，不是真实数据库内容。目的不是还原某次真实任务，而是帮助理解 L1 / L2 / L3 / Skill 四层分别记录什么。

## 1. 源码里的四层对应关系

`memos-local-plugin` 的四层不是四份普通摘要，而是一条从执行事实到可调用能力的演化链。

| 层级 | 源码落点 | 核心记录 |
| --- | --- | --- |
| L1 Trace | `traces` 表、`TraceRow`、`core/capture/capture.ts`、`core/retrieval/tier2-trace.ts` | 单步或单轮执行事实：用户输入、助手输出、工具调用、反思、标签、错误签名、价值分 |
| L2 Policy | `policies` 表、`PolicyRow`、`core/memory/l2` | 从多个高价值 L1 trace 中归纳出的过程策略：触发条件、做法、验证方式、边界、收益 |
| L3 World Model | `world_model` 表、`WorldModelRow`、`core/memory/l3` | 从多个 L2 policy 抽象出的环境认知：环境拓扑、推理规则、约束/禁忌 |
| Skill | `skills` 表、`SkillRow`、`core/skill`、`core/retrieval/tier1-skill.ts` | 从成熟 L2 policy 结晶出的可调用能力：调用说明、参数、步骤、证据、试用表现 |

简化流程是：

```text
一次任务执行
  -> capture 写入 L1 traces
  -> reward/backprop 更新 value、alpha、priority
  -> L2 从相似高价值 traces 归纳 policy
  -> L3 从多个兼容 policy 抽象 world model
  -> Skill 从高 support、高 gain 的 active policy 打包成可调用过程
```

## 2. L1 Trace：记录一次具体执行事实

L1 是最细的事实层。源码里 `core/memory/l1/README.md` 明确说 L1 不单独建模块，真正的读写面在 `traces` 表、`TraceRow`、capture、reward 和 tier2 retrieval 里。

一个伪造的 L1 trace 可以长这样：

```json
{
  "id": "tr_20260519_001",
  "episodeId": "ep_memos_doc_0519",
  "sessionId": "se_cursor_0519",
  "ts": 1779163920123,
  "turnId": 1779163919000,
  "userText": "看一下 memos-local-plugin 的源码，说明 L1 L2 L3 Skill 分别记录什么。",
  "agentText": "定位到 core/memory/l1、l2、l3、skill 和 storage schema，准备按源码字段造示例。",
  "summary": "阅读 memos-local-plugin 源码，确认四层记忆的存储字段和演化链路。",
  "toolCalls": [
    {
      "name": "ReadFile",
      "input": {
        "path": "MemOS/apps/memos-local-plugin/core/memory/l2/types.ts"
      },
      "output": "L2Config、PatternSignature、InductionDraft、L2Event 等类型定义"
    },
    {
      "name": "ReadFile",
      "input": {
        "path": "MemOS/apps/memos-local-plugin/core/types.ts"
      },
      "output": "TraceRow、PolicyRow、WorldModelRow、SkillRow 字段定义"
    }
  ],
  "agentThinking": null,
  "reflection": "这一步先确认源码中的事实字段，而不是直接沿用 README 的概念表，因此后续示例可以贴近真实 schema。",
  "value": 0.72,
  "alpha": 0.81,
  "rHuman": 0.9,
  "priority": 0.66,
  "tags": ["memos-local-plugin", "source-reading", "memory-layer"],
  "errorSignatures": [],
  "share": {
    "scope": "private"
  },
  "schemaVersion": 1
}
```

这类记录回答的是：“当时到底发生了什么？”它保留的是 grounded 证据，包括用户说了什么、Agent 做了什么、调用了哪些工具、结果是什么、后来反思评分如何。后续 L2/L3/Skill 都要能回指到这些 L1 trace。

如果任务失败，L1 也会保存失败细节，例如：

```json
{
  "id": "tr_20260519_002",
  "summary": "第一次用宽泛关键词搜索源码没有命中核心类型，改为直接读取 core/memory/l2 与 core/types。",
  "toolCalls": [
    {
      "name": "rg",
      "input": {
        "pattern": "WorldModel|Policy|Skill|Trace"
      },
      "output": "No files with matches found"
    }
  ],
  "reflection": "搜索路径或大小写可能导致漏检，应该改用已知目录和 Glob 结果直接读文件。",
  "value": 0.34,
  "alpha": 0.67,
  "priority": 0.28,
  "tags": ["search", "source-reading"],
  "errorSignatures": ["No files with matches found"]
}
```

这说明 L1 不只记录成功路径，也记录低价值或失败路径。后续的结构化匹配可以用 `errorSignatures` 找回类似问题。

## 3. L2 Policy：记录跨任务可复用策略

L2 不再关心某一次工具调用的完整流水，而是把多个相似 L1 trace 归纳成“遇到什么情况，应该怎么做”。源码里的 `PolicyRow` 主要字段包括 `title`、`trigger`、`procedure`、`verification`、`boundary`、`support`、`gain`、`status`、`sourceEpisodeIds`、`sourceTraceIds` 等。

一个伪造的 L2 policy 可以长这样：

```json
{
  "id": "po_read_source_before_layer_doc",
  "title": "写四层记忆说明前先锁定源码字段",
  "trigger": "用户要求解释 L1/L2/L3/Skill 等架构层，并要求基于某个具体源码目录输出文档。",
  "procedure": "先读取核心类型文件和 schema，再读取对应的 pipeline/README，最后用字段级示例解释每一层。不要只复述产品文档里的概念表。",
  "verification": "文档中的每层示例都能对应到源码字段：L1 对应 TraceRow/traces，L2 对应 PolicyRow/policies，L3 对应 WorldModelRow/world_model，Skill 对应 SkillRow/skills。",
  "boundary": "适用于源码中已经有明确类型或 schema 的系统说明；不适用于只有产品概念、没有实现代码的早期方案。",
  "support": 3,
  "gain": 0.27,
  "status": "active",
  "experienceType": "success_pattern",
  "evidencePolarity": "positive",
  "salience": 0.82,
  "confidence": 0.76,
  "sourceEpisodeIds": [
    "ep_memos_doc_0519",
    "ep_memos_flowchart_0518",
    "ep_memory_schema_review_0517"
  ],
  "sourceTraceIds": [
    "tr_20260519_001",
    "tr_20260518_014",
    "tr_20260517_006"
  ],
  "preference": [
    "Prefer: 先读 `core/types.ts` 和 migration schema，再写概念解释。",
    "Prefer: 示例字段名尽量贴近 DTO/Row，而不是另造一套抽象字段。"
  ],
  "antiPattern": [
    "Avoid: 只根据 README 的四层表格直接发挥。",
    "Avoid: 把 L2 policy 写成聊天摘要；它应该是可复用过程策略。"
  ],
  "inducedBy": "l2.induction@v1"
}
```

这类记录回答的是：“多次任务里重复有效的做法是什么？”它不是单次事实，而是跨任务归纳。源码里 L2 还会用 `PatternSignature` 给 trace 分桶，形如：

```text
<primaryTag>|<secondaryTag>|<tool>|<errCode>
```

例如这次任务可能形成的候选签名是：

```text
memos-local-plugin|source-reading|ReadFile|_
```

如果不同 episode 里多个高价值 trace 落入同类签名，且满足相似度、support、gain 等条件，就可能诱导出上面的 policy。

## 4. L3 World Model：记录环境认知

L3 是环境模型层。源码里的 `WorldModelRow` 包含 `title`、`body`、`structure`、`domainTags`、`confidence`、`policyIds`、`sourceEpisodeIds`、`version`、`status` 等。`structure` 被拆成三类：

- `environment`：环境拓扑，什么东西在哪里。
- `inference`：推理规则，环境通常如何响应某类动作。
- `constraints`：约束和禁忌，什么不能做或要特别注意。

一个伪造的 L3 world model 可以长这样：

```json
{
  "id": "wm_memos_local_plugin_memory_arch",
  "title": "memos-local-plugin 的本地记忆分层实现模型",
  "domainTags": ["memos-local-plugin", "memory-layer", "reflect2evolve"],
  "confidence": 0.84,
  "version": 2,
  "status": "active",
  "policyIds": [
    "po_read_source_before_layer_doc",
    "po_trace_policy_world_skill_mapping",
    "po_check_retrieval_tiers_before_explaining"
  ],
  "sourceEpisodeIds": [
    "ep_memos_doc_0519",
    "ep_memos_flowchart_0518"
  ],
  "structure": {
    "environment": [
      {
        "label": "L1 存储不在 core/memory/l1 里",
        "description": "L1 的目录只有 README，实际 schema 在 `core/storage/migrations/001-initial.sql` 的 `traces` 表，类型在 `core/types.ts` 的 `TraceRow`。",
        "evidenceIds": ["po_read_source_before_layer_doc", "tr_20260519_001"]
      },
      {
        "label": "L2/L3 是算法模块",
        "description": "`core/memory/l2` 有 signature、candidate pool、induce、gain 等流程；`core/memory/l3` 有 cluster、abstract、merge 和 confidence 调整。",
        "evidenceIds": ["po_trace_policy_world_skill_mapping"]
      },
      {
        "label": "Skill 是独立 lifecycle",
        "description": "`core/skill` 会检查 policy 的 status、support、gain 和成功锚点，再通过 crystallize prompt 生成可调用的 invocation guide。",
        "evidenceIds": ["po_check_retrieval_tiers_before_explaining"]
      }
    ],
    "inference": [
      {
        "label": "字段优先于概念命名",
        "description": "当问题要求解释层级时，如果能对应到 Row/DTO/schema 字段，解释会比只讲抽象概念更稳定。",
        "evidenceIds": ["po_read_source_before_layer_doc"]
      },
      {
        "label": "高层记忆必须能回指低层证据",
        "description": "L2 policy 带 `sourceTraceIds`，L3 world model 带 `policyIds` 和 `sourceEpisodeIds`，Skill 带 `evidenceAnchors`，都用于审计来源。",
        "evidenceIds": ["po_trace_policy_world_skill_mapping"]
      }
    ],
    "constraints": [
      {
        "label": "不要把 L3 写成项目摘要",
        "description": "L3 应该描述环境结构、行为规则和约束，而不是复述某一次任务过程。",
        "evidenceIds": ["po_trace_policy_world_skill_mapping"]
      },
      {
        "label": "不要把 Skill 当作普通经验条目",
        "description": "Skill 必须有调用说明、步骤、参数、证据锚点和试用/采用指标，才符合源码里的 SkillRow 语义。",
        "evidenceIds": ["po_check_retrieval_tiers_before_explaining"]
      }
    ]
  },
  "body": "memos-local-plugin 的记忆系统从 L1 trace 的事实证据开始，经由 L2 policy 归纳过程策略，再由 L3 world_model 压缩环境结构、规则和约束。Skill 不是第四种摘要，而是从成熟 policy 打包出的可调用能力。"
}
```

这类记录回答的是：“这个环境通常是什么样，它有什么规律和禁忌？”它比 L2 更抽象，不直接说某个任务怎么做，而是告诉 Agent 这个项目里的结构性事实。

## 5. Skill：记录可调用能力包

Skill 是最接近“行动组件”的一层。源码里 `SkillRow` 包含 `name`、`status`、`invocationGuide`、`procedureJson`、`eta`、`support`、`gain`、`trialsAttempted`、`trialsPassed`、`sourcePolicyIds`、`sourceWorldModelIds`、`evidenceAnchors`、`usageCount` 等。

一个伪造的 Skill 可以长这样：

```json
{
  "id": "sk_explain_memory_layers_from_source",
  "name": "explain_memory_layers_from_source",
  "status": "active",
  "eta": 0.78,
  "support": 5,
  "gain": 0.31,
  "trialsAttempted": 4,
  "trialsPassed": 3,
  "version": 1,
  "usageCount": 9,
  "lastUsedAt": 1779165000000,
  "sourcePolicyIds": [
    "po_read_source_before_layer_doc"
  ],
  "sourceWorldModelIds": [
    "wm_memos_local_plugin_memory_arch"
  ],
  "evidenceAnchors": [
    "tr_20260519_001",
    "tr_20260518_014",
    "tr_20260517_006"
  ],
  "procedureJson": {
    "summary": "当用户要求解释某个系统的分层记忆设计时，先从源码类型、schema、pipeline 和 retrieval 入口建立字段映射，再用贴近源码的伪造数据说明每层记录什么。",
    "parameters": [
      {
        "name": "sourceDir",
        "type": "string",
        "required": true,
        "description": "要阅读的源码目录，例如 MemOS/apps/memos-local-plugin"
      },
      {
        "name": "outputPath",
        "type": "string",
        "required": false,
        "description": "生成说明文档的 Markdown 路径"
      }
    ],
    "preconditions": [
      "源码目录存在，并且能读取核心类型或 schema 文件。",
      "用户需要的是解释性文档，而不是修改运行时代码。"
    ],
    "steps": [
      {
        "title": "锁定存储和类型",
        "body": "读取 `core/types.ts`、storage migration 和 repos，确认每层真实字段。"
      },
      {
        "title": "读取演化流程",
        "body": "读取 capture、L2、L3、skill、retrieval 相关入口，确认每层如何产生和召回。"
      },
      {
        "title": "构造字段级样例",
        "body": "分别为 TraceRow、PolicyRow、WorldModelRow、SkillRow 造一条合理数据，并标明它回答的问题。"
      },
      {
        "title": "写成文档",
        "body": "把源码依据、示例 JSON 和层级差异写入 Markdown，避免只输出抽象定义。"
      }
    ],
    "examples": [
      {
        "input": "sourceDir=MemOS/apps/memos-local-plugin, outputPath=doc/memos-local-plugin/四层记忆示例.md",
        "expected": "生成一份包含 L1/L2/L3/Skill 示例数据和源码依据的说明文档。"
      }
    ],
    "decisionGuidance": {
      "preference": [
        "Prefer: 示例对象字段尽量对应源码中的 Row/DTO 字段。",
        "Prefer: 先解释该层回答的问题，再展示数据。"
      ],
      "antiPattern": [
        "Avoid: 把 L2/L3/Skill 都写成同一种摘要。",
        "Avoid: 没读源码就直接套概念解释。"
      ]
    },
    "tags": ["source-reading", "memory-layer", "documentation"],
    "tools": ["ReadFile", "Glob", "rg", "ApplyPatch"]
  },
  "invocationGuide": "# Explain Memory Layers From Source\n\nUse this when the user asks to explain a layered memory system from source code. First map implementation fields, then produce field-level examples for each layer."
}
```

这类记录回答的是：“以后再遇到类似任务，Agent 能不能直接调用一个成熟流程？”它比 L2 policy 更可执行：有调用条件、参数、步骤、示例、证据锚点、试用通过率和采用率。

## 6. 四层之间的关键差别

可以用同一个任务主题串起来理解：

| 层级 | 它保存的不是 | 它真正保存的是 | 示例中的核心字段 |
| --- | --- | --- | --- |
| L1 | 不是总结文章 | 一次具体执行证据 | `userText`、`agentText`、`toolCalls`、`reflection`、`value`、`tags`、`errorSignatures` |
| L2 | 不是某次执行流水 | 多次任务归纳出的做法 | `trigger`、`procedure`、`verification`、`boundary`、`support`、`gain`、`sourceTraceIds` |
| L3 | 不是操作步骤 | 环境结构、规律和约束 | `structure.environment`、`structure.inference`、`structure.constraints`、`confidence`、`policyIds` |
| Skill | 不是经验描述 | 可召回、可试用、可演化的能力包 | `invocationGuide`、`procedureJson`、`eta`、`trialsAttempted`、`trialsPassed`、`evidenceAnchors` |

一句话压缩：

```text
L1 记“这次发生了什么”；
L2 记“多次之后学到怎么做”；
L3 记“这个环境有什么结构和规律”；
Skill 记“把稳定有效做法封装成下次可直接调用的能力”。
```
