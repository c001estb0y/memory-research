# Claude Code Hooks 系统设计与插件协同

基于 Claude Code 开源快照源码的深度分析。

---

## 一、Hooks 是什么

Hooks 是 Claude Code 的**事件驱动扩展机制**——在特定时刻（工具调用前/后、会话开始/结束、上下文压缩等），自动执行用户配置的外部命令、LLM 检查、HTTP 请求或 agent 验证。

类比 Git hooks（pre-commit / post-commit），Claude Code hooks 让你在 AI 工作流的关键节点插入自定义逻辑。

---

## 二、27 种 Hook 事件

Claude Code 定义了 27 种事件，覆盖了完整的工作流生命周期：

### 工具相关（最常用）

| 事件 | 触发时机 | 典型用途 |
|------|----------|----------|
| **PreToolUse** | 工具执行前 | 拦截危险操作、修改工具输入 |
| **PostToolUse** | 工具成功执行后 | 记录文件变更、触发格式化 |
| **PostToolUseFailure** | 工具执行失败后 | 错误通知、自动重试逻辑 |

### 会话生命周期

| 事件 | 触发时机 |
|------|----------|
| **SessionStart** | 会话启动 |
| **SessionEnd** | 会话结束 |
| **Setup** | 初始化完成 |
| **UserPromptSubmit** | 用户提交输入 |
| **Stop** | 回合结束（模型不再调用工具） |
| **StopFailure** | 回合结束处理失败 |

### Subagent 相关

| 事件 | 触发时机 |
|------|----------|
| **SubagentStart** | 子代理启动 |
| **SubagentStop** | 子代理停止 |
| **TaskCreated** | 异步任务创建 |
| **TaskCompleted** | 异步任务完成 |
| **TeammateIdle** | Teammate 空闲 |

### 上下文压缩

| 事件 | 触发时机 |
|------|----------|
| **PreCompact** | 上下文压缩前 |
| **PostCompact** | 上下文压缩后 |

### 权限相关

| 事件 | 触发时机 |
|------|----------|
| **PermissionRequest** | 请求权限 |
| **PermissionDenied** | 权限被拒绝 |

### 通知与交互

| 事件 | 触发时机 |
|------|----------|
| **Notification** | 发送通知 |
| **Elicitation** | 请求用户输入 |
| **ElicitationResult** | 用户输入结果 |

### 配置与文件

| 事件 | 触发时机 |
|------|----------|
| **ConfigChange** | 配置变更 |
| **InstructionsLoaded** | 指令文件加载 |
| **CwdChanged** | 工作目录变更 |
| **FileChanged** | 文件变更 |
| **WorktreeCreate** | Git worktree 创建 |
| **WorktreeRemove** | Git worktree 移除 |

---

## 三、四种 Hook 执行方式

### 3.1 command — Shell 命令

最基础的 hook 类型，执行外部 shell 命令：

```json
{
  "type": "command",
  "command": "npm run lint -- --fix ${tool_input.file_path}",
  "shell": "bash",
  "timeout": 30,
  "statusMessage": "Running linter...",
  "async": false
}
```

**特殊字段**：
- `shell`：`bash`（默认，用 $SHELL）或 `powershell`（用 pwsh）
- `async`：后台执行，不阻塞
- `asyncRewake`：后台执行，但 exit code 2 时唤醒模型处理 blocking error
- `once`：只执行一次，之后自动移除

**输入**：hook 输入 JSON 通过 stdin 传给命令。
**输出**：命令的 stdout 可以输出 JSON 来影响后续行为（如阻止操作、修改工具输入等）。

### 3.2 prompt — LLM 单次判断

用一个轻量 LLM 调用做快速判断：

```json
{
  "type": "prompt",
  "prompt": "检查以下工具调用是否可能修改敏感文件（如 .env、credentials）。如果不安全，返回 {\"ok\": false, \"reason\": \"原因\"}。输入：$ARGUMENTS",
  "model": "claude-sonnet-4-6",
  "timeout": 15
}
```

**特点**：
- `$ARGUMENTS` 占位符会被替换为 hook 输入 JSON
- 返回 `{ ok: true/false, reason?: string }`
- `ok: false` 时可阻止操作继续
- `preventContinuation: true` 可停止整个回合

### 3.3 http — HTTP Webhook

将 hook 输入 POST 到外部服务：

```json
{
  "type": "http",
  "url": "https://my-service.com/hooks/post-tool",
  "headers": {
    "Authorization": "Bearer $MY_API_TOKEN"
  },
  "allowedEnvVars": ["MY_API_TOKEN"],
  "timeout": 10
}
```

**特点**：
- 将 hook 输入 JSON 作为 POST body 发送
- header 值支持 `$VAR_NAME` 环境变量插值（仅 `allowedEnvVars` 中列出的变量）
- 响应 JSON 与 command hook 格式相同

### 3.4 agent — 多轮验证 Agent

最强大的 hook 类型——spawn 一个完整的 agent 做多轮验证：

```json
{
  "type": "agent",
  "prompt": "验证刚写入的文件是否通过 TypeScript 类型检查。运行 tsc --noEmit 并报告结果。",
  "model": "claude-sonnet-4-6",
  "timeout": 60
}
```

**特点**：
- 可使用工具（Bash、Read 等），做多轮交互
- 最终通过 StructuredOutput 工具返回 `{ ok, reason? }`
- 最多 50 轮
- 适合需要「运行命令 → 分析结果 → 再运行」的复杂检查

---

## 四、配置格式

### 4.1 在 settings.json 中配置

```json
{
  "hooks": {
    "<事件名>": [
      {
        "matcher": "工具名模式（可选）",
        "hooks": [
          { "type": "command", "command": "..." },
          { "type": "prompt", "prompt": "..." }
        ]
      }
    ]
  }
}
```

**配置层级**（高→低优先级）：
- `.claude/settings.json`（项目级，提交到仓库）
- `~/.claude/settings.json`（用户级）
- 管理策略（policySettings）

### 4.2 Matcher 匹配规则

`matcher` 字段用于过滤 hook 触发范围：

- **工具名匹配**：`"Write"` — 只在 FileWrite 工具时触发
- **多工具匹配**：`"Edit|Write"` — 在 FileEdit 或 FileWrite 时触发
- **省略/空**：匹配所有（该事件的任何触发都执行 hook）

### 4.3 if 条件过滤

每个 hook 还可以设 `if` 字段做更精细的过滤：

```json
{
  "type": "command",
  "command": "echo 'sensitive file modified'",
  "if": "Write(*.env)"
}
```

使用权限规则语法（如 `Bash(git *)`、`Read(*.ts)`），在 spawn hook 之前评估，避免不必要的进程创建。

---

## 五、Hook 输入数据结构

每个 hook 收到的输入 JSON 包含基础字段和事件特有字段。

### 5.1 基础字段（所有事件共享）

```json
{
  "session_id": "abc-123",
  "transcript_path": "/path/to/transcript.jsonl",
  "cwd": "/project/root",
  "permission_mode": "default",
  "agent_id": "可选，子代理中才有",
  "agent_type": "可选，如 general-purpose"
}
```

### 5.2 PreToolUse 事件

```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "rm -rf /tmp/test" },
  "tool_use_id": "toolu_xxx"
}
```

### 5.3 PostToolUse 事件

```json
{
  "hook_event_name": "PostToolUse",
  "tool_name": "Write",
  "tool_input": { "file_path": "src/app.ts", "content": "..." },
  "tool_response": "File written successfully",
  "tool_use_id": "toolu_xxx"
}
```

### 5.4 Stop 事件

```json
{
  "hook_event_name": "Stop",
  "stop_hook_active": true
}
```

---

## 六、Hook 输出与控制

Hook 可以通过 stdout（command 类型）或返回值影响 Claude Code 的行为。

### 6.1 PreToolUse 输出

```json
{
  "decision": "block",
  "reason": "This command would delete production data"
}
```

可选 decision 值：
- `"approve"` — 自动批准（跳过权限确认）
- `"block"` — 阻止操作
- `"modify"` — 修改工具输入后继续
- 省略 — 不干预

### 6.2 PostToolUse 输出

```json
{
  "additionalContext": "Note: this file is part of the public API surface"
}
```

向模型注入额外上下文信息。

### 6.3 通用阻断

```json
{
  "blockingError": "Hook check failed: ...",
  "preventContinuation": true
}
```

`preventContinuation: true` 会停止整个回合。

---

## 七、Hook 执行机制

### 7.1 注册与合并

```
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

### 7.2 触发流程（以 PreToolUse 为例）

```
模型决定调用 Write 工具
  ↓
toolExecution.ts → runPreToolUseHooks()
  ↓
toolHooks.ts → executePreToolHooks()
  ↓
utils/hooks.ts → executeHooks()
  ├── 信任检查
  ├── getMatchingHooks(event="PreToolUse", matcher="Write")
  ├── 并行执行所有匹配的 hooks
  │   ├── command → spawn shell 进程
  │   ├── prompt → queryModelWithoutStreaming
  │   ├── http → POST fetch
  │   └── agent → 多轮 query loop
  └── 聚合结果（AggregatedHookResult）
       ├── success → 继续执行工具
       ├── blocking → 阻止执行，返回错误给模型
       └── cancel → 取消操作
```

### 7.3 异步 Hook 的轮询

`async: true` 的 command hook 在后台执行，通过 `AsyncHookRegistry` 管理：

1. spawn 进程后立即返回（不阻塞主流程）
2. 后台轮询 stdout，逐行尝试解析 JSON
3. 解析到有效 JSON 后通过 `emitHookResponse` 通知
4. `asyncRewake: true` 时，exit code 2 会唤醒模型处理 blocking error

---

## 八、与 claude-mem 插件协同示例

假设有一个 `claude-mem` 插件，提供了知识库管理功能（向量存储、自动索引、上下文增强）。以下展示 hooks 如何与这类记忆插件深度协同。

### 8.1 场景描述

`claude-mem` 插件的核心能力：
- 将代码变更自动索引到本地向量数据库
- 在用户提问时检索相关历史上下文
- 在会话结束时提取关键知识点

### 8.2 配置文件

在 `.claude/settings.json` 中配置 hooks：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "claude-mem init --session $SESSION_ID --cwd $CWD",
            "timeout": 10,
            "statusMessage": "Initializing memory index..."
          }
        ]
      }
    ],

    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "claude-mem retrieve --query \"$(cat)\" --top-k 5 --format json",
            "timeout": 5,
            "statusMessage": "Searching knowledge base..."
          }
        ]
      }
    ],

    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "claude-mem index-file --stdin",
            "async": true
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "claude-mem capture-output --stdin",
            "if": "Bash(npm test *)",
            "async": true
          }
        ]
      }
    ],

    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "prompt",
            "prompt": "检查以下文件修改是否与知识库中的已知约定冲突。文件操作：$ARGUMENTS。如果发现潜在冲突，返回 {\"ok\": false, \"reason\": \"冲突描述\"}，否则返回 {\"ok\": true}。",
            "model": "claude-sonnet-4-6",
            "timeout": 10,
            "statusMessage": "Checking against knowledge base..."
          }
        ]
      }
    ],

    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "claude-mem extract-learnings --session $SESSION_ID --transcript $TRANSCRIPT_PATH",
            "timeout": 30,
            "async": true,
            "statusMessage": "Extracting session learnings..."
          }
        ]
      }
    ],

    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "claude-mem flush --session $SESSION_ID --reason pre-compact",
            "timeout": 15,
            "statusMessage": "Flushing memory before compaction..."
          }
        ]
      }
    ],

    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "claude-mem sync --session $SESSION_ID",
            "timeout": 20,
            "statusMessage": "Syncing memory to persistent store..."
          }
        ]
      }
    ]
  }
}
```

### 8.3 工作流全景

```
会话开始
  │
  ├── [SessionStart] claude-mem init
  │   → 初始化向量索引，加载项目知识库
  │
  ▼
用户输入 "修复购物车并发问题"
  │
  ├── [UserPromptSubmit] claude-mem retrieve
  │   → 搜索知识库："购物车并发"相关知识
  │   → 返回上下文：之前的并发修复方案、相关文件路径等
  │   → 模型获得增强上下文，做出更好决策
  │
  ▼
模型决定修改 src/cart/service.ts
  │
  ├── [PreToolUse: Write] prompt hook
  │   → LLM 检查：这个修改是否与知识库中已知的 "购物车模块不能用 
  │     乐观锁" 这条约定冲突？
  │   → 返回 { ok: false, reason: "知识库记录：购物车模块必须用 
  │     悲观锁，因为高并发下乐观锁重试导致 CPU 飙升" }
  │   → 模型收到反馈，改用悲观锁方案
  │
  ▼
模型写入文件（悲观锁版本）
  │
  ├── [PostToolUse: Write] claude-mem index-file (async)
  │   → 后台异步将修改后的文件索引到向量数据库
  │   → 不阻塞模型继续工作
  │
  ▼
模型运行测试
  │
  ├── [PostToolUse: Bash] claude-mem capture-output (async, if: npm test)
  │   → 后台捕获测试输出，索引到知识库
  │   → 记录 "购物车并发测试通过" 这一事实
  │
  ▼
回合结束
  │
  ├── [Stop] claude-mem extract-learnings (async)
  │   → 从 transcript 中提取关键学习点：
  │     - "购物车模块的并发问题用悲观锁解决"
  │     - "service.ts 中 getCart() 需要 SELECT FOR UPDATE"
  │   → 写入知识库供未来会话使用
  │
  ▼
上下文快要满了
  │
  ├── [PreCompact] claude-mem flush
  │   → 在压缩前将当前会话的重要信息 flush 到持久存储
  │   → 确保压缩不会丢失关键知识
  │
  ▼
会话结束
  │
  └── [SessionEnd] claude-mem sync
      → 将本地索引同步到持久存储
      → 下次会话可以检索到本次的知识
```

### 8.4 关键协同模式解析

**模式一：上下文增强（UserPromptSubmit → retrieve）**

用户提问时，hook 从知识库检索相关上下文，通过 stdout JSON 注入给模型。模型看到的不仅是用户的问题，还有历史积累的相关知识。

**模式二：预防性检查（PreToolUse → prompt hook）**

在修改文件前，用 LLM 检查是否与知识库中的已知约定冲突。这实现了「组织记忆的自动执行」——团队曾经踩过的坑不会再踩。

**模式三：异步索引（PostToolUse → async command）**

文件修改后异步索引到向量数据库。`async: true` 确保不阻塞主流程，但知识在后续查询中可用。

**模式四：压缩前 flush（PreCompact → flush）**

上下文压缩前将重要信息写入外部存储。这解决了纯 in-context memory 的核心问题——压缩可能丢失关键信息。

**模式五：会话结束提取（Stop → extract-learnings）**

回合结束时从 transcript 中提取结构化知识点。这是 Claude Code 自身 `extractMemories` 的外部扩展——插件可以用自己的格式和存储。

---

## 九、内部 Hook（非用户配置）

除了用户可配置的 hooks，Claude Code 内部还有两类不暴露给用户的 hook 机制：

### 9.1 Callback Hooks

通过 `registerHookCallbacks()` 注册的内部回调，`type: 'callback'`，标记 `internal: true`：

- **sessionFileAccessHooks**：追踪文件访问（memory 文件、transcript 等的读写遥测）
- 其他内部生命周期回调

### 9.2 PostSampling Hooks

通过 `registerPostSamplingHook()` 注册，在模型采样完成后执行：

- **SessionMemory 提取**：`extractSessionMemory`
- **Skill 改进分析**：`skillImprovement`
- **MagicDocs**：文档分析

这些 hook 不通过 settings.json 配置，是 Claude Code 核心功能的内部扩展点。

---

## 十、安全机制

### 10.1 策略控制

| 设置 | 作用 |
|------|------|
| `disableAllHooks` | 全局禁用所有 hooks |
| `allowManagedHooksOnly` | 只允许管理策略配置的 hooks |
| `allowedHttpHookUrls` | HTTP hook URL 白名单 |
| `httpHookAllowedEnvVars` | HTTP header 可用的环境变量白名单 |

### 10.2 信任检查

每个 hook 执行前都会经过信任检查，确保来源可信、配置合法。

### 10.3 超时保护

所有 hook 类型都有 `timeout` 字段（秒），超时后强制终止。agent 类型默认 60 秒。

### 10.4 SSRF 防护

`ssrfGuard.ts` 对 HTTP hook 的 URL 做安全检查，防止请求内网地址。

---

## 十一、Cursor vs Claude Code Hooks 对比

### 11.1 一句话概括

- **Cursor hooks**：GUI IDE 里的安全门控，侧重"拦截危险操作 + 合规审计"
- **Claude Code hooks**：CLI 工具的插件体系，侧重"工作流自动化 + 外部系统集成"

两者**核心事件已趋于对齐**，且 Cursor 主动兼容 Claude Code 的 hooks 格式（exit code 2 = deny、`CLAUDE_PROJECT_DIR` 环境变量别名、可加载 `.claude/settings.json` 中的 hooks）。

### 11.2 关键差异速览

| 维度 | Cursor | Claude Code |
|------|--------|-------------|
| 产品形态 | GUI IDE（VS Code 分支） | CLI 终端工具 |
| 事件数 | 20（含 2 个 Tab 专属） | 27 |
| 执行类型 | 2 种：command、prompt | 4 种：command、prompt、**http**、**agent** |
| 配置文件 | `.cursor/hooks.json` | `.claude/settings.json` 的 hooks 字段 |
| 异步执行 | ❌ | ✅ `async` / `asyncRewake` / `once` |
| 条件过滤 | matcher 匹配工具名/命令文本 | matcher + `if` 权限规则语法 |
| 企业分发 | ✅ Cloud Dashboard + MDM | 仅文件级策略 |
| Tab 补全 | ✅ 专属 hooks | ❌（CLI 无 Tab） |
| Partner 生态 | ✅ Semgrep、1Password 等 | 社区为主 |

### 11.3 基于 Hooks 可支撑的特性

这是两者 hooks 系统的真正价值所在——通过组合事件 + 执行类型，可以实现以下六大类特性：

#### 特性一：安全门控与合规

> 在危险操作发生前拦截，确保 Agent 不越界。

| 能力 | 依赖的 Hook | Cursor | Claude Code |
|------|------------|--------|-------------|
| 阻止危险 Shell 命令（`rm -rf`） | PreToolUse / beforeShellExecution | ✅ | ✅ |
| 阻止修改敏感文件（`.env`） | PreToolUse + matcher | ✅ | ✅ |
| 阻止敏感文件内容发送给 LLM | beforeReadFile | ✅ | ❌（无对应事件） |
| MCP 工具调用门控 | beforeMCPExecution | ✅ | ❌（通过 PreToolUse 统一处理） |
| 子代理创建审批 | subagentStart / SubagentStart | ✅ | ✅ |
| 用 LLM 做模糊安全判断 | prompt 类型 hook | ✅ | ✅ |
| 用多轮 Agent 做深度安全验证 | agent 类型 hook | ❌ | ✅ |
| 失败时强制阻断（failClosed） | failClosed 配置 | ✅ | ❌ |

**典型场景**：Semgrep 扫描 AI 生成的代码漏洞（Cursor Partner），1Password 验证环境变量挂载。

#### 特性二：自动格式化与质量保障

> 文件修改后自动运行 lint/format，保持代码质量。

| 能力 | 依赖的 Hook | Cursor | Claude Code |
|------|------------|--------|-------------|
| 文件编辑后自动格式化 | afterFileEdit / PostToolUse | ✅ | ✅ |
| Tab 补全后自动格式化 | afterTabFileEdit | ✅ | ❌ |
| 异步 lint（不阻塞主流程） | PostToolUse + `async: true` | ❌ | ✅ |
| 类型检查（多步：运行 tsc → 分析错误） | agent 类型 hook | ❌ | ✅ |

**典型配置**（Claude Code）：
```json
{ "PostToolUse": [{ "matcher": "Write", "hooks": [
  { "type": "command", "command": "prettier --write ${tool_input.file_path}", "async": true }
]}]}
```

#### 特性三：记忆与知识库

> 本文档第八章详细展示的核心场景——hooks 驱动外部记忆系统。

| 能力 | 依赖的 Hook | Cursor | Claude Code |
|------|------------|--------|-------------|
| 会话开始时加载知识库 | sessionStart / SessionStart | ✅ | ✅ |
| 用户提问时检索相关上下文 | beforeSubmitPrompt / UserPromptSubmit | ✅ | ✅ |
| 文件变更后异步索引 | PostToolUse + `async: true` | ❌ | ✅ |
| 压缩前 flush 关键信息 | preCompact / PreCompact | ✅ | ✅ |
| 回合结束提取学习点 | stop / Stop | ✅ | ✅ |
| 会话结束同步持久化 | sessionEnd / SessionEnd | ✅ | ✅ |
| 将事件推送到外部知识服务 | http 类型 hook | ❌ | ✅ |

> Claude Code 的 `async` + `http` 组合使其在记忆插件场景上有明显优势：异步索引不阻塞，HTTP webhook 可直接对接远程向量数据库。

#### 特性四：审计与可观测性

> 记录 Agent 的所有行为，供合规审查或分析优化。

| 能力 | 依赖的 Hook | Cursor | Claude Code |
|------|------------|--------|-------------|
| 全量操作审计日志 | 所有 pre/post 事件 | ✅ | ✅ |
| Shell 命令 + 输出完整记录 | afterShellExecution | ✅ | ❌（PostToolUse 不含终端原始输出） |
| MCP 调用审计 | afterMCPExecution | ✅ | ❌ |
| Agent 思维过程观测 | afterAgentThought | ✅ | ❌ |
| Agent 回复文本记录 | afterAgentResponse | ✅ | ❌ |
| 将审计事件推送到远程服务 | http 类型 hook | ❌ | ✅ |
| 权限请求/拒绝记录 | PermissionRequest/Denied | ❌ | ✅ |
| 配置/文件/目录变更追踪 | ConfigChange/FileChanged/CwdChanged | ❌ | ✅ |

> Cursor 在 IDE 侧观测更细（能看到思维链、每条回复），Claude Code 在系统侧覆盖更广（权限、配置、文件变更）。

#### 特性五：工作流自动化与循环

> Agent 完成任务后自动触发下一步，实现无人值守的迭代循环。

| 能力 | 依赖的 Hook | Cursor | Claude Code |
|------|------------|--------|-------------|
| 回合结束自动提交跟进消息 | stop `followup_message` | ✅ | ❌ |
| 子代理完成后自动迭代 | subagentStop `followup_message` | ✅ | ❌ |
| 循环次数限制 | `loop_limit` 配置 | ✅（默认 5） | ❌ |
| 异步任务完成后回调 | TaskCompleted | ❌ | ✅ |
| 多代理协作调度 | TeammateIdle / TaskCreated | ❌ | ✅ |
| Git worktree 生命周期管理 | WorktreeCreate/Remove | ❌ | ✅ |

> Cursor 的 `followup_message` + `loop_limit` 适合"测试→修复→再测试"的简单循环。Claude Code 的 Teammate/Task 事件适合多代理并行协作。

#### 特性六：外部系统集成

> 将 Agent 事件与企业基础设施打通。

| 能力 | 依赖的 Hook | Cursor | Claude Code |
|------|------------|--------|-------------|
| Webhook 推送到 Slack/飞书 | http 类型 hook | ❌ | ✅ |
| 推送到遥测系统（Datadog 等） | http 类型 + header 环境变量 | ❌ | ✅ |
| CI/CD 触发 | command / http hook | ✅（command） | ✅（command + http） |
| 版本控制集成（GitButler 等） | afterFileEdit + stop | ✅ | ✅ |
| 企业统一策略分发 | Cloud Dashboard / MDM | ✅ | ❌ |
| 第三方安全扫描集成 | Partner 生态 | ✅（内置） | ❌ |

### 11.4 一句话总结

**Cursor** 更适合团队/企业场景的**安全管控与合规审计**——细粒度的门控事件、Tab 补全防护、企业 Cloud 分发、Partner 安全生态。

**Claude Code** 更适合开发者个人的**工作流自动化与插件扩展**——4 种执行类型、异步后台处理、HTTP 外部集成、Agent 多轮验证、更丰富的生命周期事件。

两者可以共存：同一个 hook 脚本通过兼容协议在 Cursor 和 Claude Code 中复用。

---

## 十二、核心源码文件索引

| 文件 | 职责 |
|------|------|
| `src/schemas/hooks.ts` | Hook 配置 Zod schema（HookCommand、HookMatcher、HooksSchema） |
| `src/entrypoints/sdk/coreSchemas.ts` | 27 种 HOOK_EVENTS 定义 + 各事件输入 schema |
| `src/utils/hooks.ts` | Hook 核心执行引擎（executeHooks、匹配、聚合） |
| `src/utils/hooks/execAgentHook.ts` | agent 类型 hook 执行（多轮 query loop） |
| `src/utils/hooks/execPromptHook.ts` | prompt 类型 hook 执行（单次 LLM） |
| `src/utils/hooks/execHttpHook.ts` | http 类型 hook 执行（POST fetch） |
| `src/utils/hooks/hookEvents.ts` | Hook 事件广播系统（started/progress/response） |
| `src/utils/hooks/AsyncHookRegistry.ts` | 异步 hook 注册与轮询 |
| `src/utils/hooks/hooksConfigManager.ts` | Hook 配置管理（合并、元数据） |
| `src/utils/hooks/hooksConfigSnapshot.ts` | 启动时配置快照 |
| `src/utils/hooks/postSamplingHooks.ts` | 采样后内部 hook 注册 |
| `src/utils/hooks/registerSkillHooks.ts` | 技能 frontmatter hooks 注册 |
| `src/utils/hooks/registerFrontmatterHooks.ts` | Agent frontmatter hooks 注册 |
| `src/utils/hooks/sessionHooks.ts` | 会话级 hooks |
| `src/utils/hooks/skillImprovement.ts` | Skill 改进分析（postSampling） |
| `src/utils/hooks/ssrfGuard.ts` | HTTP hook SSRF 防护 |
| `src/services/tools/toolHooks.ts` | 工具级 hook 封装（runPre/PostToolUseHooks） |
| `src/query/stopHooks.ts` | 回合结束 hook 处理 |
| `src/utils/sessionFileAccessHooks.ts` | 内部文件访问追踪 hook |
| `src/utils/settings/types.ts` | Settings 中 hooks 字段定义 |
