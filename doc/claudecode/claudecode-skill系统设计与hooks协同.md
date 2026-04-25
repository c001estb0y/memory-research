# Claude Code Skill 系统设计与 Hooks 协同

基于 Claude Code 开源快照源码的深度分析，整合 `claudecode-hooks系统设计与插件协同.md`、`claudecode-subagent-配置与编排机制.md`、`claudecode-cache缓存体系深度分析.md` 三篇文档中与 Skill 相关的内容，并补充 Skill 与 Hooks 在 frontmatter 层的协同机制。

---

## 一、Skill 是什么

Skill 是 Claude Code 提供给**主模型自主调用**的"技能手册"——磁盘上一份带 YAML frontmatter 的 Markdown（`SKILL.md`），运行时按**渐进披露（Progressive Disclosure）**三段式喂给 LLM：

- **第 1 段（目录常驻）**：Skill 的 `name + description` 注入 system prompt，模型知道"有哪些技能可用"。
- **第 2 段（body 按需装载）**：模型决定用某个 skill 时调用 `SkillTool`，runtime 才读入 `SKILL.md` body 作为 user message 注入会话。
- **第 3 段（资源按需读取）**：Skill body 里 @引用的脚本 / 模板文件，由模型在执行中用 `Read` 工具按需加载。

这种设计让"技能数量"不再受上下文窗口限制——目录只占几十 token/条，body 只有被选中时才进入上下文。

> Skill 与 CLAUDE.md（记忆）、Subagent（子会话）、Hook（事件回调）是**正交**的四种扩展能力，分别对应"模型可见的指令"、"子会话中的能力包"、"runtime 旁路回调"等不同维度。

---

## 二、磁盘布局与来源分层

Skill 按多级作用域扫描、同名覆盖（优先级从低到高）：

| 来源 | 路径 | 信任级别 |
|---|---|---|
| 内置 | Claude Code 包内 | builtin |
| 用户级 | `~/.claude/skills/<name>/SKILL.md` | user |
| 项目级 | `<project>/.claude/skills/<name>/SKILL.md` | project |
| 插件级 | `<plugin>/skills/<name>/SKILL.md` | plugin（受 `isRestrictedToPluginOnly` 约束） |

`SKILL.md` 的文件结构与 Subagent 几乎同构：

```markdown
---
name: <skill-name>
description: <何时使用此 skill 的一句话说明>
allowed-tools: <可选，此 skill 被激活期间可用的工具白名单>
hooks:
  <事件名>:
    - matcher: "<工具/模式>"
      command: "<shell 命令>"
---

# <skill 正文，给模型看的执行手册>
```

启动时 runtime 做两件事：

1. **目录遍历**：按作用域扫描所有 `SKILL.md`，解析 frontmatter → 构建 Skill Registry（内存索引），**只保留 name/description 等 metadata，body 延迟加载**。
2. **Frontmatter hooks 注册**：若 frontmatter 里声明了 `hooks`，交给 `src/utils/hooks/registerSkillHooks.ts` 注册进 `HooksConfig`（见第五章）。

---

## 三、LLM 可见性：三段式注入

### 3.1 第 1 段——Skill 目录注入 system prompt

组装 system prompt 时拼入一段"技能目录"，形如：

```text
<available_skills>
  <skill name="daily-journal">Read today's summaries from codebuddy-mem.db ... Use when ...</skill>
  <skill name="westock-data">查询A股、港股、美股个股 ...</skill>
</available_skills>
```

缓存层用 `Sent skill names` 键记录"当前会话已注入过哪些 skill 目录版本"，避免 compact 后重发造成 prompt caching miss：

```555:569:doc/claudecode/claudecode-cache缓存体系深度分析.md
| System prompt injection | 缓存打破器 |
| Post-compact cleanup | 系统提示区段、microcompact 跟踪等 |
| Sent skill names | Skill 列表重发标记 |
```

### 3.2 动态发现——Skill Discovery Prefetch

每轮迭代并行启动轻量 LLM（Haiku）做"和本轮用户输入相关的 skill top-N"推荐：

```429:430:doc/claudecode/claudecode-agentloop-工具调用与提示词设计.md
- **内存预取**（`startRelevantMemoryPrefetch`）—— 在循环入口启动，工具执行后消费
- **技能发现预取**（`startSkillDiscoveryPrefetch`）—— 每轮迭代启动
```

结果在下一轮作为 reminder 注入，解决"skill 目录太长、模型忽略"的问题。

### 3.3 第 2 段——SkillTool 按需装载 body

主模型可见一个通用工具 `SkillTool`：

```192:192:doc/claudecode/claudecode-agentloop-工具调用与提示词设计.md
- `SkillTool` — 执行技能
```

模型调用 `Skill(skill_name="daily-journal")` 时 runtime 行为：

1. 从 Skill Registry 按 name 查对象（找不到报错）。
2. `fs.readFileSync(skill.filePath)` 读完整 `SKILL.md`，去掉 frontmatter。
3. body 以 **user message 形式**注入当前会话（角色像"一段新到达的任务指令"，而不是 tool_result）。
4. 记录到 `Invoked skills` 缓存，同会话同 skill 不重复装载。
5. 命中 `SkillTool prompt cache`（Anthropic prompt caching 的 cache breakpoint），后续轮次不重算 KV。
6. 若 frontmatter 声明了 `allowed-tools`，临时收敛当前会话工具白名单。

```567:584:doc/claudecode/claudecode-cache缓存体系深度分析.md
| Invoked skills | 技能内容缓存 |
| Dynamic skills | 动态技能 |
...
| SkillTool prompt cache | 技能提示缓存 |
...
- `clearInvokedSkills` — 选择性清理（只清除不在保留列表中的）
```

### 3.4 Subagent 的强制预加载

当 Subagent frontmatter 里声明 `skills: [...]`，runtime 不等模型决策，直接为子会话预加载对应 skill 的 body：

```118:121:doc/claudecode/claudecode-subagent-配置与编排机制.md
  tools?: string[]            // 允许使用的工具列表（"*" = 全部）
  disallowedTools?: string[]  // 禁用的工具列表
  skills?: string[]           // 可用技能
  mcpServers?: McpServerSpec[]// 可用的 MCP 服务器
```

```779:779:doc/claudecode/claudecode-subagent-配置与编排机制.md
| 技能预加载 | `runAgent.ts` → skillsToPreload 循环 | 代码强制将 frontmatter 中声明的 skills 以 user message 注入 |
```

这是 Subagent"专精化"的核心手段——定向绑定一组 SKILL.md 作为子会话的任务手册。

---

## 四、Skill 与 Subagent、Hook 的边界

| 维度 | Skill | Subagent | Hook |
|---|---|---|---|
| **本质** | 给主模型看的指令手册 | 独立子会话的能力包 | 不进上下文的确定性代码/HTTP/agent |
| **进不进 LLM 上下文** | 进（user message） | 进（子会话） | **不进** |
| **触发者** | 主模型调 `SkillTool` / subagent 预加载 | 主模型调 `Task` 工具 | runtime 在事件点触发 |
| **目的** | 扩展**认知与行为** | 隔离上下文 + 定向专精 | 扩展**工程侧能力**（审计、校验、格式化、可观测性） |
| **失效表现** | 模型没选它 | 主模型没 fork | 代码路径没走到 |

三者正交、可任意组合。**Skill 和 Hook 不是同一个概念**，只是都可以通过 frontmatter 声明（详见第五章）。

---

## 五、Skill 与 Hooks 的真实协同

### 5.1 双向协同

Skill 与 Hooks 的关系是**双向的**：

- **Skill 作为 Hook 提供方**：`SKILL.md` frontmatter 可声明 hooks，随 skill 装载注册、随 skill 卸载清理（`src/utils/hooks/registerSkillHooks.ts`）。
- **Skill 作为 Hook 观测对象**：内部 postSampling hook `skillImprovement` 在每次采样后异步分析 skill 使用质量（`src/utils/hooks/skillImprovement.ts`）。

源码层的证据：

```799:802:doc/claudecode/claudecode-hooks系统设计与插件协同.md
| `src/utils/hooks/registerSkillHooks.ts` | 技能 frontmatter hooks 注册 |
| `src/utils/hooks/registerFrontmatterHooks.ts` | Agent frontmatter hooks 注册 |
| `src/utils/hooks/sessionHooks.ts` | 会话级 hooks |
| `src/utils/hooks/skillImprovement.ts` | Skill 改进分析（postSampling） |
```

注意 skill 与 agent 是**两个独立的注册器**——说明它们生命周期、权限边界、清理策略各自独立，并非概念合一。

### 5.2 启动时合并路径

```317:323:doc/claudecode/claudecode-hooks系统设计与插件协同.md
启动时 captureHooksConfigSnapshot()
  ├── 加载 settings.json 中的 hooks
  ├── 加载插件注册的 hooks（registerHookCallbacks）
  ├── 加载技能/agent 的 frontmatter hooks
  └── 加载 session 级回调 hooks
  ↓
合并为统一的 HooksConfig
  ↓
按策略过滤（allowManagedHooksOnly / disableAllHooks）
```

### 5.3 生命周期绑定

Skill 的 frontmatter hooks 是**会话作用域**：

| 阶段 | 动作 |
|---|---|
| 首次装载（SkillTool 调用 / subagent 预加载） | `registerSkillHooks` 把该 skill 的 hooks 注入 `HooksConfig` |
| skill 被使用中 | 正常匹配事件（PreToolUse/PostToolUse/Stop/...） |
| 会话结束 / compact 选择性清理 | 连同 `invokedSkills` 一起摘除 |
| 非信任来源（插件） | `hooksAllowedForThisAgent` 同构判定，**阻止注册** |

> 对应的安全规则参见 subagent 文档：

```707:707:doc/claudecode/claudecode-subagent-配置与编排机制.md
| Hook 权限 | `runAgent.ts` → `hooksAllowedForThisAgent` | 非信任来源 agent 的 frontmatter hooks 被代码阻止注册 |
```

### 5.4 内部观测：`skillImprovement`

`skillImprovement` 通过 `registerPostSamplingHook()` 注册，**不暴露给用户配置**：

```616:621:doc/claudecode/claudecode-hooks系统设计与插件协同.md
通过 `registerPostSamplingHook()` 注册，在模型采样完成后执行：

- **SessionMemory 提取**：`extractSessionMemory`
- **Skill 改进分析**：`skillImprovement`
- **MagicDocs**：文档分析
```

每次主模型采样完成后异步跑 Haiku，分析"本轮 skill 使用是否命中、SKILL.md 是否有改进空间"，是 Anthropic 埋的 self-improvement 信号。

---

## 六、层次模型：Plugin 才是"分发容器"

**Plugin 不是"Skill + Hook 的合体"**，它是**能力打包单元**，同一个插件仓库可以同时装：skill、subagent、slash command、hooks、MCP server、tool 扩展。

```text
┌──────────────────────────────────────────────────────────────┐
│                     Plugin（分发容器）                        │
│   - 信任域 / 生命周期 / $PLUGIN_ROOT / 缓存命名空间            │
├──────────────────────────────────────────────────────────────┤
│  能力维度（正交）：                                            │
│                                                               │
│   Skill   │   Subagent │   Hook    │  Slash   │  MCP  │ Tool │
│  (给模型) │  (子会话)  │ (旁路)    │ Command  │ Server│      │
│                                                               │
│  ───────────────────────────────────────────────────────     │
│  Skill / Subagent 的 frontmatter 可以「顺带」声明 Hook        │
│  └─ 这是语法糖（生命周期绑定），不是概念合并                   │
└──────────────────────────────────────────────────────────────┘
```

插件级 hooks 走 `plugin/hooks/hooks.json`，而非 frontmatter。以 `claude-mem` 为例，它只用 hooks，没用 skill：

```383:383:doc/claudecode/claudecode-hooks系统设计与插件协同.md
claude-mem 通过 `plugin/hooks/hooks.json` 注册 hooks，**不在用户的 `.claude/settings.json` 中配置**，而是由插件系统自动加载。
```

这直接证明了 Plugin 不是"Skill + Hook 的合体"，而是更高层的**能力组合容器**。

---

## 七、实战案例：给 `daily-journal` 加上 Hooks

以用户本机 `~/.claude/skills/daily-journal/SKILL.md`（CodeBuddy 记忆日志生成器）为原型，**展示一个同时使用 Skill body、frontmatter hooks、多事件协同的真实 skill**。

### 7.1 目标

这是一个"读数据库 → 写 Markdown → git commit/push"的工作流型 skill。加入 hooks 解决三个现实问题：

| 问题 | Hook 对策 |
|---|---|
| `git push` 容易因代理/网络失败 → 想自动重试 | `PostToolUseFailure` 匹配 `Bash(git push*)`，异步重试 |
| 生成日志容易忘记加入 Overview/Reflections 段落 → 想发版前校验 | `PostToolUse` 匹配 `Write`，跑 schema 校验脚本 |
| 日志推完后想同步通知企业微信 | `Stop` 事件，HTTP POST 到 webhook |
| 首次装载时检查本地 journal 仓库是否克隆好 | `Setup`（会话首次装载该 skill 时触发） |

### 7.2 带 hooks 的 `SKILL.md`（完整示例）

```markdown
---
name: daily-journal
description: Read today's summaries from codebuddy-mem.db across all projects, generate a daily work journal in Chinese, and push it to the journal GitHub repo. Use when the user wants to write a daily journal, review today's work, or generate a work summary.
allowed-tools:
  - Read
  - Write
  - Bash(sqlite3:*)
  - Bash(git:*)
  - Bash(ls:*)
  - Bash(mkdir:*)
hooks:
  # —— 装载时：确保 journal 仓库已克隆 ——
  Setup:
    - type: command
      command: "node ${CLAUDE_PROJECT_DIR}/.claude/hooks/journal/ensure-repo.js"
      timeout: 60
      statusMessage: "Preparing journal repo..."

  # —— 写文件后：校验 Markdown schema ——
  PostToolUse:
    - matcher: "Write"
      if: "Write(/d/github/journal/**/*.md)"
      hooks:
        - type: command
          command: "node ${CLAUDE_PROJECT_DIR}/.claude/hooks/journal/validate-schema.js"
          timeout: 10

  # —— git push 失败时：指数退避重试，最多 3 次 ——
  PostToolUseFailure:
    - matcher: "Bash"
      if: "Bash(git push*)"
      hooks:
        - type: command
          command: "node ${CLAUDE_PROJECT_DIR}/.claude/hooks/journal/retry-push.js"
          timeout: 90
          async: true
          asyncRewake: true

  # —— 回合结束：推送完成后通知企业微信 ——
  Stop:
    - type: http
      url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
      headers:
        Authorization: "Bearer $WEWORK_BOT_TOKEN"
      allowedEnvVars: ["WEWORK_BOT_TOKEN"]
      timeout: 5
---

# Daily Journal Generator

Generate a daily work journal from CodeBuddy memory database and push to GitHub.

## Database Info

- **Path**: `/c/Users/minusjiang/.codebuddy-mem/codebuddy-mem.db` (SQLite)
- **Table**: `session_summaries`
- **Key columns**: `project`, `request`, `completed`, `learned`, `investigated`,
  `next_steps`, `created_at` (UTC), `created_at_epoch`

## Workflow

### Step 1: Query today's summaries

（注：Beijing UTC+8，Beijing 日 = UTC 前一日 16:00 ~ 当日 16:00）

```bash
sqlite3 /c/Users/minusjiang/.codebuddy-mem/codebuddy-mem.db \
  "SELECT project, request, completed, next_steps, created_at \
   FROM session_summaries \
   WHERE created_at >= '2026-04-19T16:00:00' \
     AND created_at < '2026-04-20T16:00:00' \
   ORDER BY created_at_epoch ASC;"
```

### Step 2: Group by project, generate markdown

（省略：和原 SKILL 一致）

### Step 3: Write file to `/d/github/journal/YYYY/MM/YYYY-MM-DD.md`

### Step 4: git add / commit / push

> **注意**：Step 1 之前，`Setup` hook 已自动确保 `/d/github/journal` 存在；
> Step 3 写入后，`PostToolUse` hook 会自动跑 schema 校验；
> Step 4 如果 `git push` 失败，`PostToolUseFailure` hook 会自动重试；
> 整个回合结束后，`Stop` hook 会向企业微信推送"日志已生成"通知。
>
> **因此你（模型）不需要在正文里写重试逻辑、校验逻辑、通知逻辑，专注主流程即可。**

## Incremental Update（同日重复触发）

…（同原 SKILL）…
```

### 7.3 Hook 脚本实现（示意）

对应的三个 hook 脚本放在项目的 `.claude/hooks/journal/` 目录下：

```javascript
// .claude/hooks/journal/ensure-repo.js
// Setup hook：装载 skill 时确保 journal 仓库已克隆
const { execSync } = require('child_process');
const fs = require('fs');
const REPO = '/d/github/journal';
try {
  if (!fs.existsSync(REPO)) {
    execSync(`git clone https://github.com/c001estb0y/journal.git ${REPO}`,
             { stdio: 'inherit' });
  } else {
    execSync(`git -C ${REPO} pull --ff-only`, { stdio: 'inherit' });
  }
  process.stdout.write(JSON.stringify({ ok: true }));
} catch (e) {
  process.stdout.write(JSON.stringify({
    blockingError: `journal repo prepare failed: ${e.message}`,
    preventContinuation: true,
  }));
  process.exit(2);
}
```

```javascript
// .claude/hooks/journal/validate-schema.js
// PostToolUse hook：校验 Markdown 必须包含 Overview / Projects / Reflections 三个段落
let stdin = '';
process.stdin.on('data', c => stdin += c);
process.stdin.on('end', () => {
  const input = JSON.parse(stdin);
  const filePath = input.tool_input?.file_path;
  const content = require('fs').readFileSync(filePath, 'utf8');
  const required = ['## Overview', '## Projects', '## Reflections'];
  const missing = required.filter(h => !content.includes(h));
  if (missing.length) {
    process.stdout.write(JSON.stringify({
      decision: 'block',
      reason: `Journal missing sections: ${missing.join(', ')}`,
    }));
    process.exit(2);
  }
  process.stdout.write(JSON.stringify({
    additionalContext: 'Journal schema validated.',
  }));
});
```

```javascript
// .claude/hooks/journal/retry-push.js
// PostToolUseFailure hook：git push 失败时异步重试（指数退避）
let stdin = '';
process.stdin.on('data', c => stdin += c);
process.stdin.on('end', async () => {
  const input = JSON.parse(stdin);
  const cmd = input.tool_input?.command || '';
  if (!cmd.startsWith('git push')) return;

  const { exec } = require('child_process');
  for (const delay of [2000, 5000, 15000]) {
    await new Promise(r => setTimeout(r, delay));
    try {
      await new Promise((resolve, reject) =>
        exec(cmd, { cwd: '/d/github/journal' },
             (err, out) => err ? reject(err) : resolve(out)));
      process.stdout.write(JSON.stringify({
        additionalContext: `git push retried successfully after ${delay}ms`,
      }));
      return;
    } catch (_) { /* keep retrying */ }
  }
  process.stdout.write(JSON.stringify({
    blockingError: 'git push failed after 3 retries',
  }));
  process.exit(2);
});
```

### 7.4 完整执行时序

```text
用户: "帮我生成今天的日报"
  │
  ▼
主模型判断相关 → 调用 Skill(skill_name="daily-journal")
  │
  ├── [runtime] 读 SKILL.md → body 注入 user message
  ├── [runtime] registerSkillHooks → 4 个 hooks 注册到 HooksConfig
  │
  ├── [Setup hook] ensure-repo.js
  │     → git clone / git pull 确保 /d/github/journal 就绪
  │
  ▼
模型按 SKILL body 执行：
  │
  ├─ Tool: Bash(sqlite3 ...)           查询当天 summaries
  │
  ├─ Tool: Write(/d/.../2026-04-20.md) 写入日志
  │     └─ [PostToolUse hook] validate-schema.js
  │          → 发现缺少 ## Reflections 段落 → decision: block
  │          → 模型收到反馈，补全段落后重写
  │
  ├─ Tool: Bash("git add . && git commit -m ...")
  │
  ├─ Tool: Bash("git push")
  │     └─ ❌ 网络超时 → tool failure
  │          [PostToolUseFailure hook] retry-push.js 异步启动
  │          → 2s/5s/15s 指数退避 → 第 2 次重试成功
  │          → asyncRewake 唤醒模型："git push retried successfully"
  │
  ▼
模型回答："已生成日报 2026-04-20.md 并推送到 GitHub"（回合结束）
  │
  └── [Stop hook] HTTP POST → 企业微信群通知 "日报已生成"
```

### 7.5 为什么把这些逻辑放 Hook 而不是写进 SKILL body

| 维度 | 放 SKILL body（给模型执行） | 放 Hook（runtime 旁路） |
|---|---|---|
| 是否确定性执行 | ❌ 模型可能忘 | ✅ 100% 执行 |
| 是否占用上下文 token | ❌ 每次都要装载 | ✅ 不进上下文 |
| 是否能并发/异步 | ❌ 串行 | ✅ `async: true` |
| 调试可观测 | 看模型 thinking | 看 hook 脚本日志/exit code |
| 可复用到别的 skill | ❌ 需要复制 body | ✅ hook 脚本被 frontmatter 引用 |

**设计原则**：**主流程（语义性、需要模型判断）放 body；工程性流程（校验、重试、通知、清理）放 hook。** 这符合"模型做认知、代码做确定性"的分工。

---

## 八、从这张图看整体

```text
┌─────────── 磁盘 ───────────┐
│ SKILL.md                    │
│   ├─ frontmatter            │
│   │    ├─ name/description  │→ (A) 注入 system prompt 目录
│   │    ├─ allowed-tools     │→ (C) 收敛工具白名单
│   │    └─ hooks             │→ (D) registerSkillHooks
│   └─ body                   │→ (B) SkillTool 按需注入 user msg
└─────────────────────────────┘
                │
    ┌───────────┼────────────┬───────────────┐
    ▼           ▼            ▼               ▼
  system     skill       HooksConfig     Invoked skills cache
  prompt     body        合并           (防重复装载)
  目录       (user msg)
    │           │            │
    │           │            ▼
    │           │         事件触发（PreToolUse/PostToolUse/Stop/...）
    │           │            │
    │           │            ▼
    │           │          command/prompt/http/agent 执行
    │           │            │
    ▼           ▼            ▼
    ┌───────────────────────────────────────────┐
    │        主模型（Claude Sonnet/Opus）        │
    │   看到：目录 + body + hook 返回的 context  │
    └───────────────────────────────────────────┘
                │
                ▼ 每次采样后
         [postSampling] skillImprovement 分析 skill 使用质量
                │
                ▼
            写回改进建议（Anthropic 内部信号）
```

---

## 九、关键文件索引

| 文件 | 职责 |
|---|---|
| `src/skills/*` | Skill Registry 与按需装载（body 加载、cache） |
| `src/tools/SkillTool.ts` | `SkillTool` 工具实现 |
| `src/utils/hooks/registerSkillHooks.ts` | Skill frontmatter hooks 注册 |
| `src/utils/hooks/registerFrontmatterHooks.ts` | Agent frontmatter hooks 注册（对比） |
| `src/utils/hooks/hooksConfigSnapshot.ts` | 启动时统一合并 4 路 hooks |
| `src/utils/hooks/postSamplingHooks.ts` | postSampling 内部 hook 注册表 |
| `src/utils/hooks/skillImprovement.ts` | Skill 改进分析（postSampling） |
| `src/agentLoop/prefetch.ts` | `startSkillDiscoveryPrefetch` |
| `src/runAgent.ts` | Subagent 的 `skillsToPreload` 预加载循环 |

---

## 十、对 OpenClaw / CodeBuddy 适配的启示

1. **抽象层次要分清**：不要把 Skill/Hook/Plugin 混在一个 interface 里。合理的分层是：
   - `Skill`：`{ name, description, body, allowedTools?, hooks? }`
   - `Hook`：`{ event, matcher?, type: command|prompt|http|agent, ... }`
   - `Plugin`：`{ skills: Skill[], hooks: Hook[], agents: Agent[], mcpServers: ... }`

2. **SKILL.md 的 frontmatter hooks 是"生命周期绑定"而非"概念合并"**：实现时要显式处理 hook 的注册/注销时机跟 skill 装载/卸载同步，防止泄漏。

3. **复用 `.cursor/hooks/` 现有脚本**：你仓库里的 `codebuddy-handler.js` / `hook-handler.js` / `langfuse-client.js` 本质上就是一套 "cursor-plugin 级别" 的 hook，加一层 frontmatter 注册器就能和 Claude Code 的 skill hooks 打通。

4. **`skillImprovement` 是自进化的天然入口**：可以和 `doc/mem-distillation/evaluation/layered-memory-design.md` 里的记忆分层沉淀打通——每个 skill 的使用轨迹反哺 SKILL.md 的持续改写。

---

## 参考文档

- [Claude Code Hooks 系统设计与插件协同](./claudecode-hooks系统设计与插件协同.md)
- [Claude Code Subagent 配置与编排机制](./claudecode-subagent-配置与编排机制.md)
- [Claude Code Cache 缓存体系深度分析](./claudecode-cache缓存体系深度分析.md)
- [Claude Code Agent Loop 工具调用与提示词设计](./claudecode-agentloop-工具调用与提示词设计.md)
- [Claude Code Memory 系统设计](./claudecode-memory系统设计.md)
