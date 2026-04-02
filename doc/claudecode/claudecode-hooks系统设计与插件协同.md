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

## 八、与 claude-mem 插件协同（基于 v10.6.3 源码）

> 以下内容基于 `claude-mem` 开源仓库（`github.com/thedotmack/claude-mem` v10.6.3）的实际源码分析，而非推测。

### 8.1 claude-mem 真实架构

`claude-mem` 不是简单的"向量数据库索引"插件，而是一个**带 Observer AI 的持久化记忆系统**：

| 组件 | 实现 | 职责 |
|------|------|------|
| **Worker Service** | Express HTTP 服务，默认 `127.0.0.1:37777` | 核心守护进程，管理会话、存储、搜索、Agent |
| **Observer AI（SDK Agent）** | Claude Agent SDK，独立于主 Claude Code 的 LLM 调用 | "旁观者 AI"，接收工具观察和 transcript 片段，生成结构化 XML 记忆 |
| **SQLite 存储** | `bun:sqlite` + FTS5 虚拟表 | 主存储：`observations`、`session_summaries`、`user_prompts` 表 |
| **Chroma 向量** | 通过 `uvx chroma-mcp` 子进程 | 语义搜索：将 SQLite 中的观测/摘要同步为向量 |
| **MCP Server** | `@modelcontextprotocol/sdk` + stdio | 对 Claude Code 暴露 `search`/`timeline`/`get_observations`/`smart_search` 等工具 |
| **Web Viewer** | React SPA + SSE | `http://localhost:37777` 实时展示观测和摘要 |

关键区别：claude-mem **不是**直接对文件做向量索引——而是由一个独立的 Observer AI 将每次工具调用**蒸馏**为结构化的 `<observation>` XML（包含 type、title、facts、narrative、concepts、files 等字段），然后存入 SQLite 并异步同步到 Chroma。

### 8.2 真实的 Hooks 配置

claude-mem 通过 `plugin/hooks/hooks.json` 注册 hooks，**不在用户的 `.claude/settings.json` 中配置**，而是由插件系统自动加载。实际配置如下（简化展示核心逻辑）：

```json
{
  "hooks": {
    "Setup": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "$PLUGIN_ROOT/scripts/setup.sh", "timeout": 300 }
        ]
      }
    ],

    "SessionStart": [
      {
        "matcher": "startup|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "node $PLUGIN_ROOT/scripts/bun-runner.js $PLUGIN_ROOT/scripts/worker-service.cjs start",
            "timeout": 60
          },
          {
            "type": "command",
            "command": "node $PLUGIN_ROOT/scripts/bun-runner.js $PLUGIN_ROOT/scripts/worker-service.cjs hook claude-code context",
            "timeout": 60
          }
        ]
      }
    ],

    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "... worker-service.cjs hook claude-code session-init",
            "timeout": 60
          }
        ]
      }
    ],

    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "... worker-service.cjs hook claude-code observation",
            "timeout": 120
          }
        ]
      }
    ],

    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "... worker-service.cjs hook claude-code summarize",
            "timeout": 120
          }
        ]
      }
    ],

    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "node -e \"...POST http://127.0.0.1:37777/api/sessions/complete...\"",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

注意与原文档的**重大差异**：
- **没有 PreToolUse hook**——不存在"知识库冲突检查"功能
- **没有 PreCompact hook**——不存在"压缩前 flush"功能
- **没有独立的 CLI 命令**——所有命令都通过 `worker-service.cjs hook claude-code <event>` 调用
- **只有 6 个事件**（Setup、SessionStart、UserPromptSubmit、PostToolUse、Stop、SessionEnd），不是原文描述的 7 个
- 所有 hook 都是同步 `command` 类型，不是异步的

### 8.3 真实工作流全景

```
插件安装后首次启动
  │
  ├── [Setup] setup.sh
  │   → 检查依赖（Bun 等），安装必要运行时
  │
  ▼
会话开始
  │
  ├── [SessionStart] worker-service.cjs start
  │   → 启动 Worker 守护进程（Express HTTP，端口 37777）
  │   → Worker 初始化：SQLite 连接、Chroma MCP 子进程、MCP 客户端
  │   → 以 detached 进程运行，避免被 Claude Code 沙箱杀掉
  │
  ├── [SessionStart] hook claude-code context
  │   → contextHandler 调用 GET /api/context/inject
  │   → ContextBuilder 从 SQLite 查询本项目的历史观测和摘要
  │   → 按时间线组织，计算 token 预算，渲染为 Markdown
  │   → 通过 hook 的 additionalContext 输出注入到 Claude Code 的上下文中
  │   → 模型在会话开始时就能"看到"之前的记忆
  │
  ▼
用户输入 "修复购物车并发问题"
  │
  ├── [UserPromptSubmit] hook claude-code session-init
  │   → sessionInitHandler 调用 POST /api/sessions/init
  │   → 在 SQLite 中创建/更新会话记录，存储用户提示语
  │   → 如果是第一个 prompt（promptNumber=1），启动 SDK Agent（Observer AI）
  │   → Observer AI 收到 buildInitPrompt，进入观察待命状态
  │
  ▼
模型调用 FileRead 读取 src/cart/service.ts
  │
  ├── [PostToolUse] hook claude-code observation
  │   → observationHandler 调用 POST /api/sessions/observations
  │   → 将 {tool_name: "FileRead", tool_input, tool_response} 发给 Worker
  │   → Worker 将工具观察包装为 XML 发给 Observer AI：
  │     <observed_from_primary_session>
  │       <what_happened>FileRead</what_happened>
  │       <parameters>{"path": "src/cart/service.ts"}</parameters>
  │       <outcome>文件内容...</outcome>
  │     </observed_from_primary_session>
  │   → Observer AI 判断是否值得记录，如果值得则输出：
  │     <observation>
  │       <type>code_analysis</type>
  │       <title>Cart service uses optimistic locking</title>
  │       <facts><fact>getCart() uses version field for optimistic lock</fact></facts>
  │       <files_read><file>src/cart/service.ts</file></files_read>
  │     </observation>
  │   → ResponseProcessor 解析 XML → 事务写入 SQLite → 异步同步到 Chroma
  │   → SSE 广播到 Web Viewer（如果打开了 localhost:37777）
  │
  ▼
模型修改文件、运行测试（每次 PostToolUse 都重复上述流程）
  │
  ├── [PostToolUse] 每次工具调用后都触发 observation
  │   → Observer AI 持续观察，选择性记录有价值的工具操作
  │   → 不是所有操作都会产生 observation（AI 判断是否值得记录）
  │
  ▼
回合结束（模型不再调用工具）
  │
  ├── [Stop] hook claude-code summarize
  │   → summarizeHandler 从 transcript 文件提取最后一条 assistant 消息
  │   → 调用 POST /api/sessions/summarize
  │   → Observer AI 收到 buildSummaryPrompt，生成进度摘要：
  │     <summary>
  │       <request>修复购物车并发问题</request>
  │       <investigated>分析了 service.ts 中的锁机制</investigated>
  │       <learned>乐观锁在高并发下导致 CPU 飙升</learned>
  │       <completed>改用悲观锁（SELECT FOR UPDATE）</completed>
  │       <next_steps>需要压力测试验证</next_steps>
  │     </summary>
  │   → 解析并存入 SQLite + Chroma
  │
  ▼
会话结束
  │
  └── [SessionEnd] POST /api/sessions/complete
      → 标记会话为已完成
      → 下次新会话的 SessionStart context hook 能检索到本次的记忆
```

### 8.4 真实协同模式解析

**模式一：会话启动时的记忆注入（SessionStart → context）**

这是 claude-mem 的核心价值——在会话开始时，通过 `additionalContext` 将历史记忆注入上下文。`ContextBuilder` 从 SQLite 查询本项目最近的观测和摘要，按时间线组织，控制 token 预算后渲染为 Markdown。模型在第一轮对话时就已经"记得"之前做过什么。

**模式二：Observer AI 蒸馏（PostToolUse → observation）**

这是 claude-mem 与简单"文件索引"方案的核心区别。不是程序化地把 tool_result 存入向量库——而是由一个**独立的 Observer AI** 判断每次工具调用是否值得记录，并将其蒸馏为结构化的 observation（包含 type、title、facts、narrative、concepts、files 等字段）。这相当于一个"旁观者"在旁边做笔记，只记有价值的内容。

**模式三：回合摘要生成（Stop → summarize）**

每个回合结束时，从 transcript 提取最后一条 assistant 消息，发给 Observer AI 生成结构化摘要（request → investigated → learned → completed → next_steps → notes）。这些摘要在下次 SessionStart 时被检索出来，形成跨回合的连续记忆。

**模式四：三层渐进式检索（MCP → search → timeline → get_observations）**

claude-mem 注册的 MCP Server 暴露 7 个工具给 Claude Code。核心检索遵循三层工作流：
1. `search(query)` → 返回索引级结果（ID + 标题，每条约 50-100 token）
2. `timeline(anchor=ID)` → 围绕某条结果展示时间上下文
3. `get_observations(ids=[...])` → 按需获取完整详情（每条约 500-1000 token）

这种分层设计使 token 消耗减少约 10 倍。此外还有 `smart_search`/`smart_unfold`/`smart_outline` 三个基于 tree-sitter AST 解析的本地代码搜索工具。

**模式五：Web Viewer 实时监控**

Worker 在 `localhost:37777` 提供 React Web UI，通过 SSE（`/stream`）实时推送新的 observation 和 summary。开发者可以在浏览器中实时查看 Observer AI 在"记"什么。

### 8.5 claude-mem 与文档其他章节描述的差异

| 原文描述 | 实际情况 |
|---------|---------|
| `claude-mem init --session` | 不存在。通过 `worker-service.cjs start` 启动 Worker |
| `claude-mem retrieve --query` | 不存在。检索通过 MCP Server 的 `search` 工具完成 |
| `claude-mem index-file --stdin` | 不存在。PostToolUse 将工具调用发给 Observer AI 蒸馏 |
| `claude-mem capture-output` | 不存在。所有工具输出统一走 observation 流程 |
| `claude-mem extract-learnings` | 不存在。Stop hook 调用 `/api/sessions/summarize` |
| `claude-mem flush` | 不存在。无 PreCompact hook |
| `claude-mem sync` | 不存在。SessionEnd 调用 `/api/sessions/complete` |
| PreToolUse 知识库冲突检查 | 不存在。无 PreToolUse hook |
| PreCompact 压缩前 flush | 不存在。无 PreCompact hook |
| 文件变更异步索引到向量库 | 不是简单索引，而是 Observer AI 蒸馏为结构化 XML |
| UserPromptSubmit 检索注入 | 上下文注入在 SessionStart，不在 UserPromptSubmit |

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

> 本文档第八章基于 claude-mem v10.6.3 源码详细展示了真实的 hooks 驱动记忆系统。

| 能力 | 依赖的 Hook | Cursor | Claude Code |
|------|------------|--------|-------------|
| 会话开始时注入历史记忆 | sessionStart / SessionStart | ✅ | ✅（claude-mem 实际使用此模式） |
| 工具调用后 Observer AI 蒸馏 | PostToolUse | ❌ | ✅（claude-mem 实际使用此模式） |
| 回合结束生成进度摘要 | stop / Stop | ✅ | ✅（claude-mem 实际使用此模式） |
| 会话结束标记完成 | sessionEnd / SessionEnd | ✅ | ✅（claude-mem 实际使用此模式） |
| 通过 MCP 工具检索记忆 | MCP Server 注册 | ✅ | ✅（claude-mem 的主要检索方式） |
| 将事件推送到外部知识服务 | http 类型 hook | ❌ | ✅（hooks 支持但 claude-mem 未使用） |

> **注意**：claude-mem 实际上没有使用 PreCompact（压缩前 flush）、PreToolUse（冲突检查）或 async hook。它的所有 hook 都是同步 command 类型，通过 Worker HTTP API 与守护进程通信。记忆检索主要通过 MCP Server 暴露的 `search`/`timeline`/`get_observations` 工具完成，而非 hook stdout 注入。

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
