# Claude Code 支持 Symphony 式编排器的扩展分析

分析 OpenAI Symphony 的编排模型，对照 Claude Code 现有架构，梳理需要扩展的能力。

---

## 一、Symphony 是什么

Symphony 是 OpenAI 开源的**自主编码代理编排服务**——把 Issue Tracker（如 Linear）中的 ticket 自动转化为 coding agent（Codex）的执行任务，最终产出 PR。

**核心定位**：不是让 agent 更强，而是让 agent 的**调度和管理**自动化。工程师管理的是「工作」（issue），不是「agent」。

```
Linear Issue                    Symphony                        Codex Agent
 "修复登录 bug"  ──轮询──→  调度器 ──JSON-RPC──→  创建工作区
                           过滤/分发              读代码/改代码/跑测试
                           并发控制               提交 PR
                           重试/恢复              更新 issue 状态
```

---

## 二、Symphony 与 Codex 的通信协议

### 2.1 JSON-RPC 2.0 over stdio

Symphony 与 Codex 之间使用 **JSON-RPC 2.0**（省略 `"jsonrpc":"2.0"` 头）通过 **stdio**（JSONL 格式）通信。每行一条 JSON 消息。

**三种消息类型：**

```json
// 请求（有 id，期待响应）
{ "method": "thread/start", "id": 10, "params": { "model": "gpt-5.4" } }

// 响应（echo id）
{ "id": 10, "result": { "thread": { "id": "thr_123" } } }

// 通知（无 id，单向推送）
{ "method": "turn/started", "params": { "turn": { "id": "turn_456" } } }
```

### 2.2 核心原语

| 原语 | 说明 |
|------|------|
| **Thread** | 一次对话（相当于 Claude Code 的 session） |
| **Turn** | 一次用户请求 + agent 工作（相当于 Claude Code 的一个 agent loop 回合） |
| **Item** | 输入/输出单元（用户消息、agent 消息、命令执行、文件变更、工具调用等） |

### 2.3 生命周期

```
initialize → initialized（握手）
    ↓
thread/start（创建对话）
    ↓
turn/start（发送任务 + 用户输入）
    ↓
item/started → item/agentMessage/delta → item/completed（流式事件）
    ↓
turn/completed（回合结束，status: completed/interrupted）
    ↓
thread/archive（归档）
```

### 2.4 关键 API

| 方法 | 作用 |
|------|------|
| `thread/start` | 创建新对话（指定 model、cwd、sandbox 策略等） |
| `thread/resume` | 恢复已有对话 |
| `thread/fork` | 从已有对话分叉出新对话 |
| `turn/start` | 发送用户输入，开始 agent 工作 |
| `turn/steer` | 向正在运行的 turn 追加指令（不创建新 turn） |
| `turn/interrupt` | 取消正在运行的 turn |
| `thread/compact/start` | 触发上下文压缩 |
| `thread/rollback` | 回退最后 N 个 turn |
| `model/list` | 列出可用模型 |
| `command/exec` | 在 sandbox 中执行命令 |
| `review/start` | 启动代码审查 |

---

## 三、Symphony 的六大组件

| 组件 | 职责 | Claude Code 对应物 |
|------|------|-------------------|
| **Workflow Loader** | 读取仓库中的 `WORKFLOW.md`（YAML frontmatter + prompt） | `CLAUDE.md` + Agent frontmatter |
| **Config Layer** | 管理工作流配置、默认值、环境变量 | settings.json 多层合并 |
| **Issue Tracker Client** | 从 Linear 拉取和过滤 issue | 无（Claude Code 不对接 issue tracker） |
| **Orchestrator** | 轮询循环、分发决策、并发控制、重试 | 部分由 Coordinator 模式覆盖 |
| **Workspace Manager** | 为每个 issue 创建隔离工作目录 | Worktree 隔离（`isolation: "worktree"`） |
| **Agent Runner** | 构建 prompt、启动 coding agent | runAgent.ts + query loop |

---

## 四、Claude Code 现有能力对照

### 4.1 已具备的能力

| Symphony 需要的能力 | Claude Code 现有实现 | 差距 |
|-------------------|---------------------|------|
| 创建隔离工作区 | Worktree 隔离 + Remote 隔离 | 基本满足 |
| 并发执行多个 agent | 异步 agent（`run_in_background`）+ Coordinator 模式 | 基本满足 |
| Agent 内部的工具调用 | 完整工具系统（Read/Write/Bash/Grep 等） | 完全满足 |
| 上下文压缩 | 五层压缩策略 | 完全满足 |
| 生命周期 hooks | 27 种 Hook 事件 + 4 种执行方式 | 完全满足 |
| Agent 记忆 | Auto Memory + Session Memory | 完全满足 |
| 工作流配置 | CLAUDE.md + settings.json | 部分满足 |

### 4.2 缺失的能力

| Symphony 能力 | Claude Code 现状 | 需要扩展 |
|--------------|-----------------|---------|
| **JSON-RPC 服务器** | 无。Claude Code 是 CLI 工具，没有长驻服务模式 | 核心缺失 |
| **Issue Tracker 对接** | 无。不主动拉取外部任务 | 核心缺失 |
| **外部编排器接口** | 仅 SDK（TypeScript/Python）和 CLI，非标准化 RPC | 需要标准化 |
| **Thread 管理 API** | 有 session 概念，但无外部可调用的 CRUD API | 需要暴露 |
| **Turn 粒度控制** | Agent loop 内部管理，外部只能整体启动/中断 | 需要细化 |
| **Steer（运行中追加指令）** | 无。turn 一旦开始，外部无法注入新指令 | 需要新增 |
| **长驻轮询服务** | 无。每次执行是一次性 CLI 进程 | 需要新增 |
| **WORKFLOW.md 规范** | CLAUDE.md 是自由格式文本，不是结构化工作流定义 | 需要结构化 |

---

## 五、需要扩展的功能——分层分析

### 5.1 第一层：JSON-RPC 服务层（核心基础设施）

Claude Code 目前是一个 CLI 工具（`claude` 命令），每次调用是一个独立进程。要支持 Symphony 式编排，首先需要一个**长驻服务模式**。

**需要实现的：**

```
codex app-server（Codex 的做法）
  ↕ JSON-RPC over stdio/WebSocket
Symphony / 任意编排器

类比 Claude Code 需要的：
claude --serve（假设的服务模式）
  ↕ JSON-RPC over stdio/WebSocket
外部编排器
```

**具体 API 设计参考（对照 Codex app-server）：**

| Codex 方法 | Claude Code 需要的等价物 | 映射到现有概念 |
|-----------|------------------------|--------------|
| `initialize` | `initialize` | 建立连接，交换 client 信息 |
| `thread/start` | `session/start` | 创建新 session（对应现有 session 概念） |
| `thread/resume` | `session/resume` | 恢复已有 session（对应 `--resume`） |
| `thread/fork` | `session/fork` | 从已有 session 分叉（对应 Fork 机制） |
| `turn/start` | `turn/start` | 发送 user message，启动 agent loop |
| `turn/steer` | `turn/steer` | 运行中追加指令 |
| `turn/interrupt` | `turn/interrupt` | 对应 AbortController |
| `thread/compact/start` | `session/compact` | 触发上下文压缩 |
| `model/list` | `model/list` | 列出可用模型 |

**实现要点：**
- 用 stdio JSONL 作为默认传输（最简单，Unix 哲学）
- 可选 WebSocket 传输（支持远程连接）
- 通知流（item/started、item/completed、agentMessage/delta）映射到现有的 StreamEvent

### 5.2 第二层：Thread/Session 管理 API

Claude Code 现有的 session 概念是内部的（transcript 文件 + session ID），没有暴露给外部的管理接口。

**需要暴露的操作：**

| 操作 | 说明 | 现有基础 |
|------|------|---------|
| `session/list` | 列出所有 session（含状态、创建时间等） | transcript 文件已存在，需要加索引 |
| `session/read` | 读取 session 详情（不恢复） | recordSidechainTranscript 已有读取逻辑 |
| `session/archive` | 归档 session | 无，需要新增 |
| `session/rollback` | 回退最后 N 轮 | 无，需要新增（需改消息列表管理） |
| `session/status` | 获取 session 运行状态 | 内部有 AppState，需要暴露 |

### 5.3 第三层：Turn 粒度控制

当前 Claude Code 的 agent loop 对外是"黑盒"——启动后只能等待完成或中断。Symphony 模式需要更细粒度的控制。

**需要新增的能力：**

- **turn/steer**：向正在执行的 turn 追加用户指令，不中断当前工作。这在 Claude Code 中完全没有对应物——agent loop 里的 `messages` 数组只有 loop 内部可以追加。
- **turn/pause + turn/resume**：暂停/恢复（可选，Codex 也没有）
- **细粒度事件流**：item 级别的开始/进度/完成通知

```json
// 编排器向 Claude Code 发送 steer 指令
{ "method": "turn/steer", "id": 5, "params": {
  "sessionId": "sess_abc",
  "input": [{ "type": "text", "text": "优先检查 auth 模块" }]
}}

// Claude Code 内部需要：
// 1. 安全地向正在运行的 agent loop 注入新 user message
// 2. 不破坏 tool_use/tool_result 配对
// 3. 在下一个安全点（工具执行完毕后）插入
```

### 5.4 第四层：Issue Tracker 集成

Claude Code 目前是"被动"的——等用户输入。Symphony 模式需要"主动"轮询外部任务源。

**两种实现路径：**

**路径 A：Hook 机制扩展（最小改动）**

利用现有 hooks 系统，在 `SessionStart` 时注入 issue 信息：

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "linear-cli fetch-next-issue --team ENG --format json"
      }]
    }]
  }
}
```

限制：这只是在 session 开始时拉一次，不是持续轮询。

**路径 B：新增 Orchestrator 服务（Symphony 的做法）**

在 Claude Code 之上构建一个独立的轮询服务（可以用任何语言），通过 JSON-RPC 接口调度 Claude Code：

```
┌──────────────────────────────────────────┐
│  Orchestrator 服务（新增）                 │
│  ├── 轮询 Linear/Jira/GitHub Issues      │
│  ├── 过滤/排优先级                        │
│  ├── 并发控制（max_sessions）             │
│  └── 重试/恢复                           │
└───────────────┬──────────────────────────┘
                │ JSON-RPC (stdio/WebSocket)
                ↓
┌──────────────────────────────────────────┐
│  Claude Code（服务模式）                   │
│  ├── session/start → 创建工作区           │
│  ├── turn/start → 执行任务               │
│  ├── 事件流 → 返回进度                    │
│  └── turn/completed → 返回结果            │
└──────────────────────────────────────────┘
```

这是更彻底的方案，也是 Symphony + Codex 的实际架构。

### 5.5 第五层：WORKFLOW.md 结构化工作流

Symphony 的 `WORKFLOW.md` 是 YAML frontmatter + prompt 模板，定义了整个自动化工作流的策略：

```yaml
---
tracker:
  kind: linear
  team_key: ENG
  active_states: ["In Progress", "Todo"]
  terminal_states: ["Done", "Cancelled"]
workspace:
  root: ./workspaces
concurrency:
  max_sessions: 3
agent:
  executable: codex
  timeout_minutes: 60
---

You are working on issue {{identifier}}: {{title}}
{{description}}
Follow the project's AGENTS.md for coding conventions...
```

**Claude Code 的 CLAUDE.md 对比：**

| 维度 | WORKFLOW.md | CLAUDE.md |
|------|------------|-----------|
| 格式 | YAML frontmatter + Markdown prompt 模板 | 纯 Markdown 自由文本 |
| 结构化配置 | 有（tracker、workspace、concurrency、agent） | 无（纯指令文本） |
| 模板变量 | 有（`{{identifier}}`、`{{title}}`、`{{description}}`） | 无 |
| 作用域 | 定义整个自动化工作流 | 定义项目级编码规范 |
| 编排策略 | 包含（并发数、超时、状态机） | 不包含 |

**需要扩展**：如果 Claude Code 要支持类似的结构化工作流，可以扩展 CLAUDE.md 支持 YAML frontmatter，或者新增独立的 `WORKFLOW.md` 文件。

---

## 六、扩展优先级建议

按实现价值和难度排序：

### P0：JSON-RPC 服务层

这是一切的基础。没有标准化的进程间通信协议，任何外部编排器都无法与 Claude Code 集成。

| 子项 | 难度 | 价值 |
|------|------|------|
| stdio JSONL 传输 | 低 | 高——最简路径 |
| initialize/initialized 握手 | 低 | 高——建立连接 |
| session/start + turn/start + turn/completed | 中 | 高——最小可用 |
| 事件流通知（item/started 等） | 中 | 高——编排器需要知道进度 |

**Claude Code 已有的可复用基础**：
- SDK（`@anthropic-ai/claude-code`）已经支持 programmatic 调用
- `StreamEvent` 类型已经定义了细粒度事件
- `query()` 函数已经是 AsyncGenerator，天然支持流式输出

### P1：Session 管理 API

| 子项 | 难度 | 价值 |
|------|------|------|
| session/list（基于 transcript 文件） | 低 | 中 |
| session/resume（对应现有 `--resume`） | 低 | 高——恢复中断的工作 |
| session/fork（对应现有 Fork 机制） | 中 | 中 |
| session/status 通知 | 中 | 高——编排器监控 |

### P2：Turn 中途控制

| 子项 | 难度 | 价值 |
|------|------|------|
| turn/interrupt（对应 AbortController） | 低 | 高 |
| turn/steer（运行中注入指令） | 高 | 中——需要改 agent loop 核心 |

### P3：结构化工作流

| 子项 | 难度 | 价值 |
|------|------|------|
| WORKFLOW.md 支持 | 中 | 中 |
| 模板变量（issue 信息注入） | 低 | 高——编排器自动化的关键 |

### P4：长驻轮询服务

| 子项 | 难度 | 价值 |
|------|------|------|
| Issue tracker 集成 | 高 | 高——但可由外部编排器实现 |
| 并发管理 + 重试 | 高 | 高——但可由外部编排器实现 |

> 注：P4 实际上不需要 Claude Code 自身实现。Symphony 的模式是让编排器（Symphony 本身）处理轮询/并发/重试，coding agent（Codex）只需暴露 JSON-RPC 接口。因此 P0 是真正的阻塞项。

---

## 七、最小可行方案：只改 Claude Code 的 P0

如果目标是让 Claude Code 可以被 Symphony 或类似编排器调用，最小改动方案是：

```
只需要实现一个 "claude --serve" 模式：

1. 启动一个 stdio JSON-RPC 服务
2. 接受 initialize 握手
3. 接受 session/start（内部创建 session + worktree）
4. 接受 turn/start（内部执行 agent loop）
5. 流式推送事件（tool 执行、文件变更、agent 消息）
6. 推送 turn/completed（返回最终结果）
```

**这相当于把 Claude Code 的 SDK 封装成 JSON-RPC 协议。** 现有的 SDK 已经暴露了大部分能力：

```typescript
// 现有 SDK 调用方式
import { query } from '@anthropic-ai/claude-code'

const result = await query({
  prompt: "修复登录 bug",
  options: { model: "claude-sonnet-4-20250514" }
})
```

**只需要在外面包一层 JSON-RPC 路由：**

```typescript
// 伪代码：JSON-RPC 服务包装
readline.on('line', async (line) => {
  const msg = JSON.parse(line)
  
  switch (msg.method) {
    case 'initialize':
      respond(msg.id, { serverInfo: { name: 'claude-code' } })
      break
    case 'session/start':
      const session = await createSession(msg.params)
      respond(msg.id, { session })
      break
    case 'turn/start':
      for await (const event of query({ prompt: msg.params.input })) {
        notify('item/delta', event)
      }
      notify('turn/completed', { status: 'completed' })
      break
  }
})
```

---

## 八、对比总结

```
┌──────────────────────────────────────────────────────────────────┐
│                    Symphony + Codex 架构                          │
│                                                                  │
│  Linear ──→ Symphony（轮询/分发/并发/重试）                       │
│                │                                                 │
│                │ JSON-RPC over stdio                              │
│                ↓                                                 │
│            Codex app-server（长驻 JSON-RPC 服务）                  │
│                │                                                 │
│                ↓                                                 │
│            Codex agent loop（执行编码任务）                        │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│              Claude Code 要达到等价能力需要的改动                   │
│                                                                  │
│  Issue Tracker ──→ 外部编排器（如 Symphony 或自研）                │
│                        │                                         │
│  现有能力               │  ← 需要新增的 JSON-RPC 接口（P0）       │
│  ┌──────────────┐      ↓                                        │
│  │ Claude Code  │  claude --serve                                │
│  │ ├─ SDK       │  （JSON-RPC 服务模式）                          │
│  │ ├─ CLI       │      │                                        │
│  │ ├─ Hooks     │      ↓                                        │
│  │ ├─ Agent Loop│  session/turn 管理 ← 需要暴露的 API（P1）      │
│  │ ├─ Worktree  │      │                                        │
│  │ ├─ Memory    │      ↓                                        │
│  │ └─ Compress  │  现有 agent loop（完全复用）                    │
│  └──────────────┘                                                │
│                                                                  │
│  结论：核心 agent 能力已经具备，缺的是"被编排"的接口层              │
└──────────────────────────────────────────────────────────────────┘
```

**一句话总结**：Claude Code 的 agent 执行能力（工具调用、上下文管理、记忆、压缩、subagent）已经完全具备，缺少的是一个标准化的 JSON-RPC 服务层，让外部编排器能够通过进程间通信来创建 session、发送任务、接收事件流。这是一个接口层的工作，不需要改动 agent 核心逻辑。
