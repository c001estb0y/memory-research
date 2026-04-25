# claude-mem 新版架构与图片语义修复分析

> 基于 [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) 最新主分支（2026-04-20 pull）源码走读，对照旧版 cloudboyguo 分支的 summary-image-missing-analysis 进行差异分析。

---

## 一、背景

旧版 claude-mem（cloudboyguo 分支）存在一个核心问题：**Claude Code / CodeBuddy IDE 产生的 Summary 完全看不到图片语义**。根因是这些适配器未注册 `afterAgentResponse` 事件，导致助手对图片的文字分析从未被录入记忆系统。

本文分析新版是否修复了该问题，以及架构上发生了哪些变化。

---

## 二、核心结论

**原始问题已通过架构重写彻底解决，但走的不是旧分析文档中的任何一个方案，而是一条全新路径——从 transcript 文件直接读取助手回复。**

---

## 三、架构重写：适配器与事件系统

### 3.1 旧版架构（cloudboyguo 分支）

每个适配器包含三层事件定义：

- `SUPPORTED_EVENTS` — 声明支持哪些事件
- `EVENT_MAP` — IDE 事件名 → 内部事件名映射
- `HOOKS_EVENTS` — 实际注册到 IDE 的事件列表

五套适配器：Cursor、CodeBuddy IDE、Claude Code、Claude Internal、CodeBuddy 插件版。

### 3.2 新版架构

适配器精简为纯粹的数据转换层，只做两件事：

```typescript
interface PlatformAdapter {
  normalizeInput(raw: unknown): NormalizedHookInput;   // IDE 格式 → 统一格式
  formatOutput(result: HookResult): unknown;           // 统一格式 → IDE 格式
}
```

适配器列表变更：

| 旧版适配器 | 新版适配器 |
|-----------|-----------|
| `cursor.ts`（含 EVENT_MAP + HOOKS_EVENTS） | `cursor.ts`（仅 normalize/format） |
| `claude-code.ts` + `claude-internal` | `claude-code.ts`（仅 normalize/format） |
| `codebuddy-ide.ts` | **已移除** |
| `codebuddy.ts` | **已移除** |
| — | 新增 `gemini-cli.ts` |
| — | 新增 `windsurf.ts` |
| — | 新增 `raw.ts`（通用兜底） |

事件处理器从旧版的"适配器内嵌事件列表 + 单一 hooks-cli.ts 大函数"重构为独立的 Handler 模块：

| Handler | 对应事件 | 职责 |
|---------|---------|------|
| `context` | SessionStart | 注入跨会话记忆上下文 |
| `session-init` | UserPromptSubmit | 初始化会话 |
| `observation` | PostToolUse | 保存工具调用观察 |
| `summarize` | Stop（phase 1） | 生成 summary |
| `session-complete` | Stop（phase 2） | 清理会话 |
| `user-message` | SessionStart（并行） | 向用户展示信息 |
| `file-edit` | afterFileEdit | 文件编辑记录（Cursor） |
| `file-context` | PreToolUse | 注入文件观察历史 |

**注意：`afterAgentResponse` 事件在新版中被彻底移除**——不再需要。

---

## 四、图片语义修复：从 Transcript 读取助手回复

### 4.1 新方案：transcript-based（= 旧分析方案 B）

新版在 `summarize` handler（Stop hook）中实现了全新策略：

```
Stop hook 触发
    │
    ├─ 接收 input.transcriptPath（Claude Code 传入 transcript 文件路径）
    │
    ├─ extractLastMessage(transcriptPath, 'assistant', true)
    │   └─ 解析 JSONL transcript → 提取最后一条 assistant 消息的完整文本
    │
    ├─ POST /api/sessions/summarize { contentSessionId, last_assistant_message }
    │   └─ Worker 将 last_assistant_message 持久化到 pending_messages 表
    │
    ├─ SDKAgent / OpenRouterAgent / GeminiAgent 消费消息
    │   └─ buildSummaryPrompt({ last_assistant_message })
    │       └─ Summary LLM 能看到助手的完整回复（含图片分析内容） ✅
    │
    └─ Poll 等待 summary 完成 → session-complete 清理
```

`extractLastMessage` 支持两种 transcript 格式：
- **JSONL**（Claude Code）：逐行解析 `type: "assistant"` 的消息
- **JSON document**（Gemini CLI 0.37.0+）：解析 `{ messages: [{ type: "gemini", content: "..." }] }`

### 4.2 `last_assistant_message` 不再是死代码

旧版中 `last_assistant_message` 被标记为"死代码"——接口声明了但从未使用。新版全链路打通：

| 环节 | 旧版 | 新版 |
|------|------|------|
| 数据来源 | 无（依赖 afterAgentResponse 事件） | transcript 文件直接读取 |
| 传递路径 | 未传入 buildSummaryPrompt | summarize handler → Worker → SDKAgent |
| Prompt 使用 | buildSummaryPrompt 中未引用 | 作为 `summary_context_label` 内容注入 prompt |
| 持久化 | 无 | pending_messages 表（SQLite） |

### 4.3 五个原始问题的逐项对照

| 原始问题 | 旧版状态 | 新版状态 |
|---------|---------|---------|
| Claude Code 缺少 `afterAgentResponse` | ❌ 缺失 | ✅ **不再需要**——改为 transcript 读取 |
| `attachments` 字段未处理 | ❌ 声明但未读取 | ⚠️ **仍未直接处理**（影响降低，见下文） |
| `last_assistant_message` 死代码 | ❌ 死代码 | ✅ **已激活**——全链路打通 |
| `beforeSubmitPrompt` 补偿只录 user_prompt | ❌ 只录用户文本 | ✅ **补偿机制已移除** |
| `recordResponse` 不支持多模态 | ❌ 纯文本 | ✅ **管道已废弃**——transcript 包含完整回复 |

关于 `attachments`：图片像素数据仍不进入 claude-mem，但模型对图片的文字分析通过 transcript 被完整捕获。对于原始场景（Claude Code 对报错截图的分析），新版能正确反映图片语义。

---

## 五、IDE 支持范围扩展

### 5.1 从 5 个平台到 13 个平台

新版大幅扩展了 IDE 支持：

| IDE | 集成模式 | 能捕获对话 | 能注入上下文 | 能搜索记忆 |
|-----|---------|-----------|------------|-----------|
| **Claude Code** | Hooks | ✅ | ✅ | ✅ |
| **Cursor** | Hooks + MCP | ✅ | ✅ | ✅ |
| **Windsurf** | Hooks | ✅ | ✅ | ✅ |
| **Gemini CLI** | Hooks | ✅ | ✅ | ✅ |
| **OpenClaw** | Plugin | ✅ | ✅ | ✅ |
| **OpenCode** | Plugin | ✅ | ✅ | ✅ |
| **Codex CLI** | Transcript | ✅ | ✅ | ✅ |
| **Copilot CLI** | MCP-only | ❌ | ✅ | ✅ |
| **Goose** | MCP-only | ❌ | ✅ | ✅ |
| **Crush** | MCP-only | ❌ | ✅ | ✅ |
| **Roo Code** | MCP-only | ❌ | ✅ | ✅ |
| **Warp** | MCP-only | ❌ | ✅ | ✅ |
| **Antigravity** | MCP-only | ❌ | ✅ | ✅ |

### 5.2 三种集成模式解析

#### Hooks-based（最深度集成）

IDE 在 Agent 生命周期关键节点（提交 prompt、工具调用前后、Stop 等）主动调用外部脚本，通过 stdin 传入 JSON 数据。

```
IDE 内部事件 → 调用 hook 脚本 → claude-mem 处理 → 返回 JSON 响应
```

能力：完整的对话捕获、上下文注入、记忆检索。

代表：Claude Code、Cursor、Windsurf、Gemini CLI。

#### Plugin-based（进程内集成）

IDE 有自己的插件系统，允许第三方代码作为插件运行在 Agent 进程内部，注册事件回调。

OpenClaw 的插件 API 暴露了丰富的生命周期事件：

- `before_agent_start` — Agent 启动前
- `before_prompt_build` — Prompt 构建前（可注入上下文）
- `tool_result_persist` — 工具结果持久化
- `agent_end` — Agent 结束
- `session_start` / `session_end` — 会话生命周期
- `after_compaction` — 上下文压缩后
- `message_received` — 消息接收

与 hooks-based 的能力等价甚至更强（同进程调用，无 IPC 开销），只是安装方式不同——把预编译插件复制到扩展目录并注册，而非写 hooks 配置文件。

代表：OpenClaw、OpenCode。

#### MCP-only（最低成本接入）

IDE 支持 MCP 协议，可连接外部 MCP Server 获取工具和资源，但不支持 Hook 或 Plugin。

```
IDE → 通过 MCP 调用 claude-mem 的搜索工具
IDE → 通过 MCP 读取 claude-mem 的记忆上下文
```

安装时 claude-mem 在 IDE 的 MCP 配置文件中注册自己的 MCP Server（`plugin/scripts/mcp-server.cjs`），提供搜索和上下文工具。

**关键限制**：只能读（检索已有记忆），不能写（不捕获当前对话）。安装完成后会明确提示：

> Note: This is an MCP-only integration providing search tools and context.
> Transcript capture is not available for {IDE}.

代表：Copilot CLI、Goose、Crush、Roo Code、Warp、Antigravity。

### 5.3 为什么 MCP-only 不能捕获对话？

MCP 协议的设计是 **Agent → Server**（Agent 主动调用 Server 的工具），不是 **Server → Agent**（Server 主动推送事件给 Agent）。claude-mem 作为 MCP Server 只能被动等待 Agent 调用搜索工具，无法像 Hook 那样在每个生命周期节点收到事件通知。

要捕获对话，需要 IDE 在关键时刻**主动通知** claude-mem（Hook 或 Plugin 模式），MCP 协议不提供这种机制。

---

## 六、其他重要架构变更

### 6.1 Subagent 感知

新版新增了 subagent 识别能力。`NormalizedHookInput` 包含 `agentId` 和 `agentType` 字段：

```typescript
interface NormalizedHookInput {
  // ...
  agentId?: string;      // Claude Code subagent agent_id
  agentType?: string;    // Claude Code subagent agent_type
}
```

`summarize` handler 会跳过 subagent 的 summary 生成——subagent 不拥有会话 summary 的所有权。

### 6.2 错误分级

`hook-command.ts` 实现了精细的错误分级策略：

| 错误类型 | 退出码 | 行为 |
|---------|--------|------|
| Worker 不可用（ECONNREFUSED 等） | 0（优雅降级） | 不阻塞用户 |
| HTTP 429（限流） | 0（优雅降级） | 视为临时不可用 |
| HTTP 4xx（客户端错误） | 2（阻塞错误） | 开发者需要修复 |
| TypeError / ReferenceError | 2（阻塞错误） | 代码 bug |
| 未知事件类型 | 0（no-op） | 返回空结果 |

### 6.3 WorktreeAdoption

新增 `WorktreeAdoption.ts`，支持 Git worktree 场景下的会话隔离。

### 6.4 多 AI 引擎支持

新版 Summary 生成支持三种 AI 引擎并行：

- `SDKAgent`（默认，OpenAI 兼容）
- `OpenRouterAgent`（OpenRouter）
- `GeminiAgent`（Gemini）

三者都正确传递 `last_assistant_message`。

---

## 七、关键源码文件索引（新版）

| 文件 | 作用 |
|------|------|
| `src/cli/hook-command.ts` | Hook 调用总入口，错误分级 |
| `src/cli/adapters/index.ts` | 适配器工厂（5 种平台适配器） |
| `src/cli/adapters/claude-code.ts` | Claude Code 输入归一化 |
| `src/cli/adapters/cursor.ts` | Cursor 输入归一化 |
| `src/cli/handlers/index.ts` | 事件处理器工厂（8 种事件） |
| `src/cli/handlers/summarize.ts` | Stop hook → transcript 读取 → 排队 summary |
| `src/cli/handlers/session-complete.ts` | Stop hook phase 2 → 会话清理 |
| `src/cli/handlers/observation.ts` | PostToolUse → 保存工具观察 |
| `src/shared/transcript-parser.ts` | JSONL / Gemini JSON transcript 解析 |
| `src/sdk/prompts.ts` | buildSummaryPrompt（使用 last_assistant_message） |
| `src/services/worker/SDKAgent.ts` | AI 引擎：summary 生成 |
| `src/services/worker/SessionManager.ts` | 会话管理与消息队列 |
| `src/services/integrations/McpIntegrations.ts` | MCP-only IDE 集成 |
| `src/services/integrations/CursorHooksInstaller.ts` | Cursor hooks + MCP 集成 |
| `src/services/integrations/OpenClawInstaller.ts` | OpenClaw 插件安装 |
| `src/npx-cli/commands/ide-detection.ts` | 13 种 IDE 自动检测 |
| `openclaw/src/index.ts` | OpenClaw 插件实现（1200+ 行） |

---

*文档生成时间：2026-04-20 · 基于 claude-mem 最新主分支 commit 49ab404c*
