# MemOS Local Plugin 使用流程说明

> 说明对象：`MemOS/apps/memos-local-plugin`  
> 代码位置：`D:\GitHub\memory-research\MemOS\apps\memos-local-plugin`  
> 参考材料：公众号《产品更新｜MemOS Local Plugin 2.0：Hermes Agent 和 OpenClaw 双 Agent 同步支持》、本地源码与 README  
> 适用读者：想理解 MemOS Local Plugin 2.0 为什么存在、如何安装、如何接入 Agent、如何把执行过程变成可学习资产的研究者或开发者。

---

## 0. 基于公众号介绍的重新梳理

公众号文章给 MemOS Local Plugin 2.0 的定位，不是“又一个保存聊天记录的 memory 插件”，而是：

> 在 Agent 执行层架设一套持续更新的外部学习系统，让大模型负责通用推理，让 MemOS 负责学习用户的本地世界。

这条主线可以压缩成六句话：

1. **要补的是大模型和真实用户之间的“最后一公里”。** 预训练模型知道公开知识，但不知道你的代码库、目录结构、命名风格、历史踩坑和个人偏好。
2. **执行链本身开始变成学习资产。** Agent 每一步的动作、观察、反思、结果，不再只是临时上下文，而会进入长期记忆流水线。
3. **反馈被拆成两层。** 环境反馈告诉系统“跑没跑通”，用户反馈告诉系统“是不是符合期待”。
4. **记忆从细到粗演化成四层认知资产。** L1 trace 记录原始步骤，L2 policy 归纳跨任务策略，L3 world model 形成场域认知，Skill 把稳定高价值做法结晶成可调用能力。
5. **检索不是一次性灌满上下文，而是三层按需召回。** Skill 给任务骨架，Trace 补历史细节，World Model 给背景认知。
6. **OpenClaw 和 Hermes 共享同一份核心。** 新 Agent 通过轻薄 adapter 接入契约层，底层复用同一个进化记忆引擎。

因此，理解这个插件的最好方式不是从“它存了什么”开始，而是从“它如何把执行过程加工成未来可复用经验”开始。

```text
执行任务
  ↓
捕获每一步动作 / 观察 / 反思
  ↓
引入环境反馈和用户反馈
  ↓
生成 L1 trace
  ↓
跨任务归纳 L2 policy
  ↓
多策略抽象 L3 world model
  ↓
高价值策略结晶成 Skill
  ↓
下一次任务按需检索并注入
```

这个版本的文档会按这条产品叙事重排，再映射回源码中的目录、安装方式和运行流程。

## 1. 它是什么

`memos-local-plugin` 是 MemOS 仓库中的本地 Agent 记忆插件包，包名是 `@memtensor/memos-local-plugin`。它不是一个独立聊天客户端，也不是直接给 Claude Code / Cursor 使用的 Hook 插件，而是面向 Agent Host 的记忆插件。

它的 README 将其定位为：

> Reflect2Evolve memory plugin for AI agents. One algorithm core, multiple agent adapters.

也就是说，它把 MemOS / Reflect2Evolve 的记忆算法封装成一个可以被不同 Agent Host 接入的本地插件。

从公众号视角看，它的关键词是 **“执行即学习”**：Agent 不是任务结束后才被总结，而是在做任务的过程中，把可审计、可归因、可复用的学习信号沉淀下来。源码中的 `core/`、`pipeline/`、`capture/`、`reward/`、`memory/l1`、`memory/l2`、`memory/l3`、`skill/`，对应的就是这条加工链。

当前源码中主要支持两个 Agent Host：

| Host | 适配方式 | 运行形态 | 说明 |
|---|---|---|---|
| OpenClaw | TypeScript in-process adapter | 插件直接运行在 OpenClaw 进程内 | 延迟低，直接 import `core/` |
| Hermes | Python adapter + JSON-RPC bridge | Python 侧通过 `bridge.cts` 调 Node/TS core | Python 适配层无状态，核心逻辑仍在 TS |

---

## 2. 它和 MemOS 的关系

可以把关系分成三层理解：

```text
MemOS 仓库
└── apps/memos-local-plugin
    ├── core/              # 记忆算法核心
    ├── agent-contract/    # Host 与 core 之间的稳定协议
    ├── adapters/          # OpenClaw / Hermes 等宿主适配器
    ├── server/            # 本地 HTTP + SSE viewer 服务
    ├── web/               # viewer 前端
    └── templates/         # 安装时生成的 config.yaml 模板
```

### 2.1 MemOS 是体系，插件是落地形态

MemOS 更像一个记忆系统总工程，包含多种记忆能力、应用和实验实现。`memos-local-plugin` 是其中面向本地 Agent 的一个应用：把 L1/L2/L3/Skill 这套记忆层级接到真实 Agent 运行时里。

### 2.2 插件核心不是 UI，而是 MemoryCore

`core/` 是整个插件的中心。它不关心外部 Host 是 OpenClaw 还是 Hermes，只暴露稳定的 `MemoryCore` 能力，例如：

- turn start 时检索记忆；
- turn end 时捕获当前轮交互；
- 记录工具执行结果；
- 接收用户反馈；
- 维护 L1 trace、L2 policy、L3 world model、Skill。

Host 差异都被隔离在 `adapters/` 里。

### 2.3 Viewer 只是观察窗口

`server/` 和 `web/` 提供本地可视化面板，用来查看 traces、policies、world models、skills、retrieval preview、logs、settings 等。记忆能力不依赖浏览器打开，但 viewer 是调试和理解插件行为的主要入口。

---

## 3. 安装后文件在哪里

源码 README 明确区分了“插件代码”和“运行数据”：

| Agent | 插件代码安装到 | 运行数据和配置 |
|---|---|---|
| OpenClaw | `~/.openclaw/plugins/memos-local-plugin/` | `~/.openclaw/memos-plugin/` |
| Hermes | `~/.hermes/plugins/memos-local-plugin/` | `~/.hermes/memos-plugin/` |

运行数据目录结构：

```text
~/.openclaw/memos-plugin/
├── config.yaml       # 唯一配置文件，包含 provider、key、viewer、hub 等设置
├── data/
│   └── memos.db      # SQLite，本地记忆数据库
├── skills/           # 结晶出来的 skill 包
├── logs/             # memos.log / error.log / audit.log / llm.jsonl / perf.jsonl / events.jsonl
└── daemon/           # bridge pid / port 等运行状态
```

重要设计点：升级或卸载插件代码不会删除 `data/`、`skills/`、`logs/`、`config.yaml`。这保证了本地记忆和配置不会因为插件升级丢失。

---

## 4. Windows 上如何安装

仓库中有 Windows 安装脚本：

```powershell
cd D:\GitHub\memory-research\MemOS\apps\memos-local-plugin
npm install
npm run build:package
.\install.ps1 openclaw
```

如果安装 Hermes：

```powershell
.\install.ps1 hermes
```

公众号面向普通用户给出的 macOS / Linux 一行安装命令是：

```bash
curl -fsSL https://raw.githubusercontent.com/MemTensor/MemOS/main/apps/memos-local-plugin/install.sh | bash
```

这条命令会走 `install.sh`，自动检测 Hermes Agent / OpenClaw，并安装发布包。它适合普通用户“上车体验”。但你当前是在 Windows + 本地源码仓库里研究，因此更适合用 `install.ps1` 或直接阅读源码。

`install.ps1` 做的事情很直接：

1. 把当前插件源码部署到 `%USERPROFILE%\.openclaw\plugins\memos-local-plugin\` 或 `%USERPROFILE%\.hermes\plugins\memos-local-plugin\`。
2. 创建运行数据目录 `%USERPROFILE%\.openclaw\memos-plugin\` 或 `%USERPROFILE%\.hermes\memos-plugin\`。
3. 从 `templates/config.<agent>.yaml` 生成 `config.yaml`，已有配置默认不覆盖。
4. 复制用户说明 README。
5. 如果存在 adapter-specific installer，再交给适配器自己的安装脚本。

注意：Linux/macOS 的 `install.sh` 更完整，支持从 npm 版本安装、自动检测 OpenClaw / Hermes、重建原生依赖、等待 viewer ready 等。Windows 的 `install.ps1` 更像本地源码部署脚本。

---

## 5. OpenClaw 接入流程

OpenClaw 是这个插件目前最直接的运行方式。它以 TypeScript 插件形式运行在 OpenClaw Host 进程内。

### 5.1 启动时

OpenClaw 加载插件 manifest 后，会进入 `adapters/openclaw/index.ts`：

```text
OpenClaw Host
└── load memos-local-plugin
    └── adapters/openclaw/index.ts
        ├── bootstrapMemoryCoreFull({ agent: "openclaw" })
        ├── core.init()
        ├── createOpenClawBridge()
        ├── registerOpenClawTools()
        └── startHttpServer(:18799)
```

启动后：

- `MemoryCore` 初始化 SQLite、迁移、embedding、LLM provider、pipeline。
- OpenClaw adapter 注册记忆工具。
- Viewer 默认监听 `http://127.0.0.1:18799`。
- 配置从 `~/.openclaw/memos-plugin/config.yaml` 读取。

### 5.2 注册的工具

`openclaw.plugin.json` 中声明了这些工具：

| 工具 | 用途 |
|---|---|
| `memory_search` | 按查询检索相关记忆 |
| `memory_get` | 根据 ID 获取详细记忆 |
| `memory_timeline` | 查看时间线 |
| `memory_environment` | 查看环境/运行状态 |
| `skill_list` | 列出已结晶技能 |
| `skill_get` | 查看某个技能 |

这些工具是 Agent 可主动调用的“显式记忆接口”。除此之外，插件也会在 turn start 自动做检索注入。

---

## 6. 每一轮对话发生了什么

插件的核心价值在于每一轮 Agent 交互都被拆成“检索 → 执行 → 沉淀 → 评分 → 提炼”的流水线。

公众号里用了一个 Alpine 容器安装 Python 包失败的例子来说明为什么“只保存最终总结”不够。普通记忆可能只会留下：

> Alpine 下安装失败，后来修好了。

MemOS Local Plugin 2.0 更关心中间链路：

```text
Step 1：尝试直接 pip install
观察：编译失败，缺少系统依赖
反思：这不是 Python 包版本问题，更像 Alpine musl 环境缺少构建工具

Step 2：安装 build-base / libxml2-dev
观察：依赖错误减少，但仍有 C 扩展编译问题
反思：需要进一步确认包对应的原生依赖
```

这就是“执行即学习”的实际含义：系统要保存的不只是结论，而是能解释结论如何产生的路径。

```text
用户发起一轮任务
    │
    ▼
before_prompt_build
    │
    ├── adapter 提取当前 turn
    ├── core.onTurnStart()
    ├── Tier 1 检索 Skill
    ├── Tier 2 检索 Trace / Episode
    ├── Tier 3 检索 World Model
    └── 生成 InjectionPacket 注入 prompt
    │
    ▼
Agent 执行任务
    │
    ├── 可能调用 memory_search / memory_get / skill_get
    ├── 可能调用普通工具
    └── 工具结果被 adapter 记录成 outcome
    │
    ▼
agent_end
    │
    ├── adapter 收集 assistant 回复和工具调用
    ├── core.onTurnEnd()
    ├── finalize episode
    ├── capture L1 trace
    ├── reward 评分和价值回传
    ├── L2 policy induction
    ├── L3 world model abstraction
    └── Skill crystallization
```

### 6.1 turn start：检索和注入

在 OpenClaw 中，对应 hook 是 `before_prompt_build`。adapter 会把当前对话历史压平成统一格式，找出最近的 user / assistant turn，然后调用：

```ts
core.pipeline.orchestrator.onTurnStart(...)
```

核心检索分三层：

| 层级 | 检索内容 | 默认角色 |
|---|---|---|
| Tier 1 | Skill | 最高价值、可直接复用的过程能力 |
| Tier 2 | Trace / Episode | 具体历史任务、代码片段、失败和修复经验 |
| Tier 3 | World Model | 对项目环境、约束、结构的压缩认知 |

这些结果会被渲染成 `memos_context` 一类上下文块，插入到当前 prompt 中。

### 6.2 执行中：工具结果只记录，不中途注入

架构文档强调一个原则：

> 不在每次 `onToolCall` / `onToolResult` 时静默注入上下文。

工具调用期间，插件主要记录：

- 工具是否成功；
- 失败次数；
- 延迟；
- 输出摘要；
- 是否触发 decision-repair 信号。

如果某类工具连续失败达到阈值，插件会生成 repair context，但不会插入当前已经进行中的 LLM step，而是缓存到下一轮或下一个可注入时机。

### 6.3 turn end：捕获和沉淀

在 OpenClaw 中，对应 hook 是 `agent_end`。adapter 会收集：

- 用户输入；
- assistant 最终回复；
- 工具调用列表；
- 工具结果；
- session / episode 信息。

然后交给 `core.onTurnEnd()`，触发：

1. episode finalize；
2. capture subscriber 抽取 L1 trace；
3. reward subscriber 对任务打分；
4. 价值分数回传到 trace；
5. 后续 L2/L3/Skill 异步提炼。

---

## 6.4 双层反馈如何进入记忆

公众号把反馈分成两类：

| 反馈层级 | 来源 | 告诉系统什么 | 对记忆的影响 |
|---|---|---|---|
| 步级反馈 | 模型 ↔ 环境 | 每一步是否执行成功、输出是什么、错误在哪里 | 帮助区分关键线索、无效试探、失败路径 |
| 任务级反馈 | 人类 ↔ 模型 | 最终结果是否满意、哪些做法应该保留或避免 | 沿执行链回流，调整每一步的价值权重 |

这和源码中的 `reward/`、`feedback/`、`recordToolOutcome`、`reward.updated` 等模块对应。

一个重要区别是：用户反馈不会只被保存成一句偏好，而是会被映射回具体执行链。例如用户说：

> 整体不错，但中间那种改法别再用了。

系统理想上要学到的是：

```text
prefer：优先复用 A 类路径
avoid：避免 B 类改法
scope：只在同类项目结构下触发
```

这也是它比普通 RAG 更进一步的地方：RAG 解决“搜到什么”，双层反馈解决“哪些经验值得信任、哪些路径以后应该避免”。

---

## 7. 四层记忆分别是什么

README 里给出的四层是：

| 层 | 名称 | 粒度 | 作用 |
|---|---|---|---|
| L1 | trace | 单步/工具/局部交互 | 记录 grounded 事实和执行轨迹 |
| L2 | policy | 跨多个相似任务归纳出的策略 | 把重复有效做法提炼成经验 |
| L3 | world model | 环境认知 | 压缩项目结构、约束、领域规律 |
| Skill | crystallized capability | 可调用能力包 | 把高价值 policy 变成可复用过程 |

这个设计比“只存对话摘要”更像成长型记忆系统：它不仅能 recall 过去内容，还试图从多次任务里归纳行为策略，并在未来任务中改变 Agent 的执行方式。

公众号特别强调：四层不是“分别存四份”，而是同一份记忆从细到粗的逐级演化：

```text
L1：本次任务中的原始轨迹
  ↓ 跨任务出现共性
L2：可复用策略
  ↓ 多个策略抽象出环境规律
L3：场域认知 / 世界模型
  ↓ 高频稳定且收益明确
Skill：可调用、可治理、可退役的能力包
```

Skill 不是静态模板。它应该带有：

- 可靠度；
- 调用次数；
- 适用边界；
- 生命周期状态；
- 证据来源；
- 失败后归档或降权机制。

这正是“可审计、可归因、可复用”的落点。

---

## 7.1 三层按需检索

公众号对检索的表达很清楚：上下文窗口很贵，不能每次把所有记忆倒进去。

MemOS Local Plugin 2.0 的检索按三层组织：

| Tier | 注入内容 | 适合时机 | 作用 |
|---|---|---|---|
| Tier 1 | Skill | 新任务刚开始，命中稳定技能 | 给 procedure、scope、避坑规则，直接形成任务骨架 |
| Tier 2 | Trace / Episode | 没有命中 Skill，或执行遇到 edge case | 补“上次具体怎么解决”的历史步骤 |
| Tier 3 | World Model | 同主题、同项目、同环境任务 | 给项目背景、目录约定、领域画像 |

这比 session start 一次性注入最近历史更精细：它不是“记忆越多越好”，而是“在对的时刻注入对的粒度”。

---

## 7.2 从“记录”角度看插件如何运转

记录链路的核心不是把聊天转存成历史，而是把一次执行拆成可评分、可归因、可演化的证据。

```text
Host hook / adapter
  ↓
TurnInputDTO / TurnResultDTO / ToolCallDTO
  ↓
Episode + L1 Trace
  ↓
Reflection + α 质量分
  ↓
R_human 任务分
  ↓
V_t / priority 回传到每个 trace
  ↓
L2 policy / L3 world model / Skill
```

### 7.2.1 L1 先保存“发生过什么”

OpenClaw 通过 `before_prompt_build`、`agent_end`、`before_tool_call`、`after_tool_call` 把执行数据送给 core；Hermes 通过 Python `MemoryProvider` 和 JSON-RPC bridge 把 `post_tool_call`、`post_llm_call` 等事件送到同一套 core。进入 core 后，数据会被标准化为：

| 数据 | 作用 |
|---|---|
| `userText` | 用户/环境给出的状态 |
| `agentText` | Agent 当步输出 |
| `agentThinking` | Host 能提供时记录模型 thinking |
| `toolCalls` | 工具名、输入、输出、错误码 |
| `reflection` | 当前步骤的反思，可能来自模型原生输出，也可能由 LLM 后补 |
| `summary` | viewer 和检索使用的短摘要 |

这一步形成的是 L1 trace：它是事实地基，不直接等于“有用经验”。

### 7.2.2 Capture 会先给每步一个 α

Capture 阶段有两种运行形态：

1. **lite capture**：每轮结束后先把 trace 写入库，`reflection=null`、`alpha=0`，确保 viewer 能尽快看到原始记录。
2. **reflect capture**：episode 结束后批量调用 reflection scorer，让 LLM 看完整因果链，为每个 step 生成或校验 reflection，并打 `α ∈ [0,1]`。

`α` 不是用户满意度，而是“这条 step 反思是否可信、具体、可迁移”的质量权重。源码里的 `REFLECTION_SCORE_PROMPT` / `BATCH_REFLECTION_PROMPT` 会按四个轴打分：

| 轴 | 含义 |
|---|---|
| faithfulness | 是否忠实描述真实 thinking、action、tool call、outcome |
| causal insight | 是否解释了为什么这样做、为什么成功或失败 |
| transferability | 是否能迁移到未来相似任务 |
| concreteness | 是否包含真实命令、错误、路径、决策，而不是泛泛而谈 |

如果 reflection 空洞或不可信，`alpha` 会被压到 0，后续价值回传就不会太依赖这一步的自我解释。

### 7.2.3 Reward 给整个任务一个 R_human

任务级评分由 `core/reward` 完成。它会把 episode 组织成 task summary，再结合显式或隐式用户反馈，得到一个 `R_human ∈ [-1,1]`。

这里的“human”不是说一定要用户手动点分，而是指它代表**人类目标是否被满足**：

| 来源 | 是否需要用户主动打分 | 说明 |
|---|---|---|
| 显式用户反馈 | 可选 | 用户说“很好”“不对，重做”“以后别这样”等，会进入 feedback repo |
| 隐式反馈 | 不需要 | 最后一轮是否自然收尾、是否继续纠错、语气是否满意等，会被保守解释 |
| LLM rubric | 默认使用 | LLM 根据 task summary + feedback 打三轴分 |
| heuristic fallback | LLM 不可用时使用 | 没反馈通常趋近 0，有明确正负反馈时做保守映射 |

`REWARD_R_HUMAN_PROMPT` 的三轴是：

| 轴 | 含义 |
|---|---|
| `goal_achievement` | 用户真实目标是否完成 |
| `process_quality` | 执行路径是否合理、高效、少折腾 |
| `user_satisfaction` | 用户反馈或后续语气是否满意 |

最终源码按权重组合：

```text
R_human = 0.45 * goal_achievement
        + 0.30 * process_quality
        + 0.25 * user_satisfaction
```

### 7.2.4 Backprop 把任务分回传成每步价值 V_t

有了 episode 级别的 `R_human`，系统会从最后一步往前回传，给每条 L1 trace 写入 `value` 和 `priority`：

```text
V_T = R_human
V_t = α_t * R_human + (1 - α_t) * γ * V_{t+1}
priority = max(V_t, 0) * time_decay
```

这回答了“哪些数据有用”的第一层判断：

- `value` 高：这条 trace 所在路径对成功任务有贡献。
- `priority` 高：这条 trace 不仅有价值，而且还比较新，适合被召回。
- `value` 低或负：不适合作为普通正向经验，但仍可用于 decision repair，告诉系统以后避免类似路径。

---

## 7.3 从“召回”角度看插件如何运转

召回链路的目标不是“把所有记忆塞进 prompt”，而是按当前任务意图，从不同层级选少量最有用的证据。

```text
Turn start
  ↓
query / context hints / namespace
  ↓
Tier 1: Skill
Tier 2: Trace / Episode
Tier 3: World Model
  ↓
rank + filter + render
  ↓
InjectionPacket
  ↓
Agent prompt
```

### 7.3.1 Skill 优先，因为它是成熟经验

Tier 1 先找 Skill。Skill 是已经通过支持度、收益、验证和生命周期筛选的高价值过程能力。命中时，它给 Agent 的不是一段历史聊天，而是：

- 何时使用；
- 前置条件；
- 参数；
- 步骤；
- 示例；
- preference / anti-pattern；
- 可用工具范围。

所以 Skill 对 prompt 的价值最高：它直接改变 Agent “怎么做”。

### 7.3.2 Trace 兜底具体细节

Tier 2 找 trace / episode。它更像“上次具体怎么处理”的证据库，适合以下情况：

- 没有成熟 Skill；
- 当前问题是 edge case；
- Agent 需要真实路径、错误消息、命令、文件名；
- 需要回看某次历史任务的完整 timeline。

Trace 排名会结合语义相似度、关键词匹配、`priority`、时间衰减等信号。也就是说，不是所有历史都平等，成功路径中高价值、近似场景的 trace 会更容易浮上来。

### 7.3.3 World Model 给背景认知

Tier 3 找 L3 world model。它不告诉 Agent “做什么”，而告诉 Agent “这个环境是什么样”：

- 项目目录结构；
- 运行约束；
- 平台差异；
- 领域惯例；
- 哪些事实会导致哪些结果。

它适合帮助 Agent 少走探索弯路，例如还没打开项目文件前就知道“这个 repo 通常把组件放在 `src/components`，配置改完要重启服务才生效”。

---

## 7.4 共性经验如何被标记为有用，并进一步变成 Skill

你的理解是对的：这里确实有一套打分机制，而且不是单一分数，而是一串“从 trace 到 policy 到 skill”的门槛。

```text
L1 trace
  ├── alpha：反思质量 / 可归因程度
  ├── value：这步对任务成败的贡献
  └── priority：召回优先级
        ↓
L2 policy
  ├── support：有多少 episode 支持
  ├── gain：使用这类策略后是否带来正向收益
  └── status：candidate / active / archived
        ↓
Skill
  ├── eta：技能可靠度
  ├── trialsAttempted / trialsPassed：真实调用试验
  └── status：candidate / active / archived
```

### 7.4.1 L2 policy 的有用性：support + gain

当多条高价值 trace 命中相似 state/action 模式时，L2 会先生成 candidate policy。之后系统会计算：

| 指标 | 说明 |
|---|---|
| `support` | 有多少条/多少个 episode 的证据支持这个策略 |
| `gain` | 使用这个策略的 trace 价值，是否高于“没使用该策略”的对照基线 |
| `status` | `candidate`、`active`、`archived` 生命周期状态 |

源码里的 gain 不是单纯平均分，而是近似：

```text
gain = mean(V_with_policy) - blended_mean(V_without_policy)
```

其中 without 集合会向中性基线 `0.5` 做 shrinkage，避免真实使用早期“没有足够失败对照组”时所有策略 gain 都接近 0。直觉上：

- 使用该策略的路径经常成功，`gain` 会变正；
- 该策略效果一般，`gain` 接近 0；
- 该策略带来失败或低质量路径，`gain` 会变负。

policy 从 candidate 变 active 的条件是：

```text
support >= minSupport
gain >= minGain
```

如果 active policy 的 `gain < archiveGain` 或 support 归零，则会归档。

### 7.4.2 Skill 的有用性：先结晶，再试用，再晋升

Skill crystallization 不是“LLM 觉得像技能就立刻启用”。源码的 `eligibility.ts` 先要求 policy 满足：

1. `policy.status === "active"`；
2. `policy.gain >= skill.minGain`；
3. `policy.support >= skill.minSupport`；
4. 有正向 success anchor；
5. 还没有被一个更新的非归档 Skill 覆盖。

满足后，`SKILL_CRYSTALLIZE_PROMPT` 才会让 LLM 把 policy + evidence + counter examples 整理成结构化 Skill draft。随后还有确定性 verifier：

| 校验 | 目的 |
|---|---|
| tool coverage | Skill 声称要用的工具必须来自 evidence，不允许凭空发明 |
| evidence resonance | Skill 的 summary / steps 要和证据 trace 有足够重叠，避免跑题 |

通过校验后，新 Skill 默认仍是 `candidate`。它还要经历 lifecycle：

| 信号 | 对 Skill 的影响 |
|---|---|
| `trial.pass` / `trial.fail` | 增加试用次数和通过次数，更新 `eta` |
| `reward.updated` | 源 policy 收益变化会重新影响 `eta` |
| `user.positive` / `user.negative` | 用户明确点赞/踩会直接调高或调低 `eta` |

当 candidate 的 `trialsAttempted >= candidateTrials` 且 `eta >= minEtaForRetrieval`，才会晋升为 `active`。如果 `eta < archiveEta`，会被归档。

### 7.4.3 到底是 human 打分还是 agent 自动打分？

准确说是**混合评分**：

| 分数/信号 | 谁给 | 自动还是人工 | 用途 |
|---|---|---|---|
| `alpha` | LLM judge | 自动 | 判断 step reflection 是否可信、可迁移 |
| `R_human` | LLM rubric + 用户反馈 | 混合 | 判断整个任务是否满足人的目标 |
| `V_t` | 公式 backprop | 自动 | 把任务成败回传到每个 trace |
| `priority` | 公式 + 时间衰减 | 自动 | 决定 trace 召回优先级 |
| `support` | 证据计数 | 自动 | 判断 policy 是否有足够样本 |
| `gain` | 对照价值计算 | 自动 | 判断 policy 是否真的带来收益 |
| `eta` | policy gain + trial/user feedback | 混合 | 判断 Skill 是否可靠、是否可召回 |
| `user.positive/negative` | 用户 | 人工 | 直接修正 Skill 生命周期 |

所以不是纯 human scoring，也不是纯 agent 自嗨。用户反馈是最高价值信号之一，但系统不会要求用户每次手动打分；默认会用 LLM rubric 和启发式逻辑把任务结果转成 `R_human`，再通过公式传播到 trace、policy 和 skill。

可以这样理解：

```text
用户不必每轮打分；
但用户一旦给出明确反馈，这个反馈会压过普通历史摘要，
进入 reward / feedback / lifecycle 链路，
影响哪些 trace 被优先召回、哪些 policy 被激活、哪些 skill 被晋升或归档。
```

---

## 7.5 Viewer 的角色：让学习过程可审计

公众号还强调 Memory Viewer 的升级：用户不应该只能相信“系统说它学到了”，而应该能看见学习过程。

文章中提到的视图包括：

| 视图 | 关注点 |
|---|---|
| 原始记忆视图 | 每步评分、关键步骤、无效试错 |
| 任务视图 | 单任务动作 / 观察 / 反思完整记录 |
| 经验视图 | 跨任务策略库、来源任务、增益分、状态 |
| 场域认知视图 | 主题领域画像、原始证据、手动修正 |
| 技能库视图 | 可靠度、调用次数、生命周期、归档 |
| 系统监控 | 实时进化事件流和全链路审计日志 |

源码架构文档中还列出了更细的 viewer 页面，如 Overview、Traces、Policies、WorldModel、Episodes、Skills、Retrieval、Hub、Logs、Settings。两者并不冲突：公众号讲的是用户可理解的产品视图，源码讲的是工程模块拆分。

---

## 8. 配置重点

OpenClaw 默认配置模板是 `templates/config.openclaw.yaml`。关键部分：

```yaml
viewer:
  port: 18799

embedding:
  provider: local
  apiKey: ""

llm:
  provider: host
  apiKey: ""
  model: ""

hub:
  enabled: false

telemetry:
  enabled: true

logging:
  level: info
```

### 8.1 embedding provider

Embedding 决定检索效果。如果只用 local provider，开箱成本低；如果要更强语义检索，可以换成 openai compatible、gemini、cohere、voyage、mistral 等 provider，并配置 key。

### 8.2 llm provider

LLM 用于总结、评分、归纳、Skill 结晶等。如果设置为 `host`，插件尝试复用 Agent Host 的 LLM 能力。如果设置为云 provider，需要在 `config.yaml` 中配置 API key 和 model。

### 8.3 viewer

OpenClaw viewer 固定使用 `18799`。源码中即使读取 config，也明确固定为：

```text
OpenClaw -> http://127.0.0.1:18799
Hermes   -> http://127.0.0.1:18800
```

这是为了避免多个 Agent 共用一个 viewer port 后出现所有权和写入冲突。

---

## 9. 和 claude-mem 的关键差异

`claude-mem` 和 `memos-local-plugin` 都是 Agent 记忆系统，但设计重心不同。

| 维度 | claude-mem | memos-local-plugin |
|---|---|---|
| 接入点 | Claude Code / Cursor / Gemini 等 Hook + Worker | OpenClaw / Hermes Agent Host adapter |
| 主要存储 | SQLite + Chroma | SQLite + 本地 vector blob / core storage |
| 写入方式 | 观察者 AI 蒸馏工具调用和 summary | turn end 捕获 episode、trace、reward、policy、skill |
| 注入方式 | SessionStart 注入最近 observation timeline | turn start 三层检索注入 InjectionPacket |
| 记忆层级 | observation + session summary 为主 | L1 trace + L2 policy + L3 world model + Skill |
| 强项 | Claude Code 生态接入直接、hook 完整 | 成长型经验提炼和 skill 结晶更重 |
| 使用对象 | Claude Code 这类编码助手 | OpenClaw/Hermes 这类可扩展 Agent runtime |

简单说：

- `claude-mem` 更像“给 Claude Code 加长期上下文和可搜索历史”。
- `memos-local-plugin` 更像“给 Agent runtime 加一套会演化的本地认知系统”。

---

## 10. 如何判断插件是否工作

OpenClaw 场景下，可以按以下顺序检查：

### 10.1 目录是否生成

```powershell
Test-Path "$HOME\.openclaw\plugins\memos-local-plugin"
Test-Path "$HOME\.openclaw\memos-plugin\config.yaml"
Test-Path "$HOME\.openclaw\memos-plugin\data"
```

### 10.2 viewer 是否启动

浏览器打开：

```text
http://127.0.0.1:18799
```

或用命令检查：

```powershell
Invoke-WebRequest http://127.0.0.1:18799/api/system
```

### 10.3 日志是否写入

```powershell
Get-ChildItem "$HOME\.openclaw\memos-plugin\logs"
```

重点看：

- `memos.log`
- `error.log`
- `events.jsonl`
- `llm.jsonl`
- `perf.jsonl`

### 10.4 数据库是否增长

完成几轮 Agent 任务后，检查：

```powershell
Test-Path "$HOME\.openclaw\memos-plugin\data\memos.db"
```

如果 viewer 中开始出现 Traces / Episodes / Retrieval 结果，说明链路基本跑通。

---

## 11. 推荐的真实试用路径

如果只是想验证插件是否真的有用，不建议一上来跑复杂项目。推荐用一个小项目做连续任务：

1. 在 OpenClaw workspace 中创建一个空项目，例如 `task-cli`。
2. 让 Agent 连续完成 6 到 10 个相似但不完全相同的开发任务。
3. 每轮任务围绕同一项目展开，例如：
   - 添加 JSON 存储；
   - 添加 YAML 存储；
   - 添加 SQLite 存储；
   - 为每种存储补测试；
   - 添加 CLI add/list 命令；
   - 最后让 Agent “按已有风格添加一种新存储”。
4. 观察 viewer：
   - 是否生成 L1 traces；
   - 是否开始归纳 L2 policies；
   - 是否形成 L3 world model；
   - 是否有 skills；
   - 最后一轮是否能召回前面任务的结构和风格。

这个测试比单轮问答更能体现它的价值，因为 L2/L3/Skill 都依赖跨任务重复模式。

---

## 12. 一句话总结

`memos-local-plugin` 是 MemOS 在本地 Agent 场景下的插件化落地：它通过 OpenClaw/Hermes adapter 接入对话生命周期，在 turn start 注入三层检索结果，在 turn end 沉淀 trace 和 episode，再通过 reward、policy induction、world model abstraction、skill crystallization 把“历史记录”升级成“可迁移经验”。
