# OpenClaw Agent 与 Subagent 上下文协作机制

> 基于 OpenClaw 源码分析，阐述 Agent / Subagent 的上下文管理、并行与等待关系、结果收集的完整协作流程。

---

## 1. 整体架构概览

OpenClaw 的 Agent 和 Subagent 运行在同一个 Gateway（Node.js 进程）中，通过 Command Queue 的多 Lane 机制实现并发控制。

**核心组件**：

- **Command Queue**：内置命令队列，分为 agent lane（默认并发 4）和 subagent lane（默认并发 8），各自独立调度
- **Main Agent（depth=0）**：主 Agent，session key 形如 `agent:<id>:main`
- **Subagent（depth=1）**：子代理，session key 形如 `agent:<id>:subagent:<uuid>`
- **Sub-Sub Agent（depth=2）**：子子代理，session key 形如 `agent:<id>:subagent:<uuid>:subagent:<uuid>`，永远是 leaf 节点

当 `maxSpawnDepth >= 2` 时，depth=1 的子代理充当 orchestrator 角色，可以继续 spawn depth=2 的 worker。

### 关键源码文件

- `src/agents/subagent-spawn.ts` — 子代理生成入口，校验深度/并发/权限，调用 Gateway 启动子运行
- `src/agents/subagent-announce.ts` — 子代理完成后的 announce 流程，包含等待、输出读取、向父投递
- `src/agents/subagent-announce-delivery.ts` — announce 投递策略（direct / queue / retry）
- `src/agents/subagent-announce-output.ts` — 从子代理会话历史中提取最终输出文本
- `src/agents/subagent-registry.ts` — 子代理运行注册表（内存 + 磁盘持久化）
- `src/agents/subagent-registry-lifecycle.ts` — 子代理生命周期事件：完成、清理、announce 触发
- `src/agents/subagent-capabilities.ts` — 按深度解析角色（main / orchestrator / leaf）和工具权限
- `src/agents/pi-tools.policy.ts` — 子代理工具策略（leaf 禁用会话工具，orchestrator 保留部分）
- `src/config/agent-limits.ts` — 默认并发、深度限制常量
- `src/process/command-queue.ts` — 多 lane 并发队列引擎
- `docs/tools/subagents.md` — 官方子代理机制文档

---

## 2. 上下文管理机制

### 2.1 会话隔离——每个 Agent/Subagent 拥有独立上下文

OpenClaw 的核心设计原则是 **会话隔离**。每个 Agent / Subagent 拥有独立的会话（Session），不共享对话历史。

**Session Key 命名规则**：

- depth=0：`agent:<agentId>:main` — 主 Agent
- depth=1：`agent:<agentId>:subagent:<uuid>` — 子代理（编排者或叶子）
- depth=2：`agent:<agentId>:subagent:<uuid>:subagent:<uuid>` — 子子代理（永远是叶子）

源码参考（subagent-spawn.ts:411）：

```typescript
const childSessionKey = `agent:${targetAgentId}:subagent:${crypto.randomUUID()}`;
```

### 2.2 主 Agent 与 Subagent 的上下文差异

**Bootstrap 注入**：

- 主 Agent — 完整注入 SOUL.md, IDENTITY.md, USER.md, HEARTBEAT.md, BOOTSTRAP.md, AGENTS.md, TOOLS.md
- Subagent — **仅注入 AGENTS.md + TOOLS.md**

**系统提示**：

- 主 Agent — 正常 agent system prompt
- Subagent — `buildSubagentSystemPrompt` 生成的精简版 + extraSystemPrompt

**任务消息**：

- 主 Agent — 用户输入
- Subagent — 格式化的 `[Subagent Context]` + `[Subagent Task]`

**对话历史**：

- 主 Agent — 完整用户对话
- Subagent — 隔离的独立 transcript

**Compaction**：两者都支持上下文压缩，各自独立运行。

**工具集**：

- 主 Agent — 全部工具
- Subagent — 全部工具 **减去** 会话管理工具（详见下节）

### 2.3 工具权限按深度分层

角色和控制范围在 spawn 时写入 session metadata，防止平级或恢复的 session 误获编排权限。

三种角色：main / orchestrator / leaf。两种控制范围：children / none。

**各深度的工具权限**：

- **depth=0（主 Agent）**，角色 main — 全部工具，始终可以 spawn 子代理
- **depth=1（maxSpawnDepth == 1 时）**，角色 leaf — 全部工具 **减去** sessions_spawn / subagents / sessions_list / sessions_history，不能 spawn
- **depth=1（maxSpawnDepth >= 2 时）**，角色 orchestrator — 全部工具 **减去** 系统工具，但 **保留** sessions_spawn / subagents / sessions_list / sessions_history，可以 spawn
- **depth=2**，角色 leaf — 全部工具 **减去** 所有会话工具，永远不能 spawn

### 2.4 上下文注入详情

子代理接收的任务消息结构：

```typescript
// subagent-spawn.ts:603-611
const childTaskMessage = [
  `[Subagent Context] You are running as a subagent (depth ${childDepth}/${maxSpawnDepth}). Results auto-announce to your requester; do not busy-poll for status.`,
  spawnMode === "session"
    ? "[Subagent Context] This subagent session is persistent and remains available for thread follow-up messages."
    : undefined,
  `[Subagent Task]: ${task}`,
].filter(Boolean).join("\n\n");
```

`buildSubagentSystemPrompt` 额外注入的系统提示包含：

- 子代理的角色声明（depth / maxDepth）
- 是否允许再 spawn
- ACP 路由指导（如启用）
- 请求方来源信息（channel, threadId 等）

---

## 3. 并行与等待关系

### 3.1 非阻塞 Spawn——立即返回

**核心设计：`sessions_spawn` 是非阻塞的。** 父代理调用后立即收到确认，不会等待子代理完成。

spawn 调用返回值：`{ status: "accepted", runId: "<uuid>", childSessionKey: "agent:...:subagent:<uuid>" }`

源码中明确禁止轮询（SUBAGENT_SPAWN_ACCEPTED_NOTE）。

**流程**：主 Agent 调用 sessions_spawn → 立即返回 accepted → 主 Agent 继续其他工作 → Subagent 在后台独立运行 → 完成后通过 announce 推送结果回主 Agent。

### 3.2 Command Queue 多 Lane 并发

OpenClaw 使用内置的命令队列引擎管理并发。Agent 和 Subagent 运行在 **不同的 lane** 上，各自有独立的并发限制。

```typescript
// agent-limits.ts:3-4
export const DEFAULT_AGENT_MAX_CONCURRENT = 4;   // agent lane 默认并发
export const DEFAULT_SUBAGENT_MAX_CONCURRENT = 8; // subagent lane 默认并发
```

**命令队列的并发泵机制**：

```typescript
// command-queue.ts:110-112 — 核心调度循环
const pump = () => {
  while (state.activeTaskIds.size < state.maxConcurrent && state.queue.length > 0) {
    const entry = state.queue.shift() as QueueEntry;
    // ... 执行任务 ...
  }
};
```

两个 Lane：

- `agent` lane — 主 Agent 运行，默认并发 4
- `subagent` lane — 子代理运行（sessions_spawn 产生），默认并发 8

### 3.3 并行安全阀——多层限制

```json
{
  "agents": {
    "defaults": {
      "subagents": {
        "maxConcurrent": 8,
        "maxSpawnDepth": 2,
        "maxChildrenPerAgent": 5,
        "runTimeoutSeconds": 900
      }
    }
  }
}
```

- `maxConcurrent` — 全局 subagent lane 并发上限
- `maxSpawnDepth` — 最大嵌套深度
- `maxChildrenPerAgent` — 每个 agent session 的活跃子代理上限
- `runTimeoutSeconds` — 单个子运行超时（秒）

源码中的校验逻辑（subagent-spawn.ts:371-388）：

```typescript
// ① 深度检查
const callerDepth = getSubagentDepthFromSessionStore(requesterInternalKey, { cfg });
if (callerDepth >= maxSpawnDepth) {
  return { status: "forbidden", error: "...depth exceeded..." };
}
// ② 每会话子代理数量检查
const activeChildren = countActiveRunsForSession(requesterInternalKey);
if (activeChildren >= maxChildren) {
  return { status: "forbidden", error: "...max children reached..." };
}
```

### 3.4 同步等待机制——Announce 阶段

虽然 spawn 是非阻塞的，但结果回传时存在同步等待。这发生在 **announce 阶段**，而非 spawn 阶段：

```typescript
// subagent-announce.ts:336-347
if (!reply && params.waitForCompletion !== false) {
  // 使用 Gateway 的 agent.wait 方法等待子运行结束
  const wait = await waitForSubagentRunOutcome(params.childRunId, settleTimeoutMs);
  const applied = applySubagentWaitOutcome({ wait, outcome, startedAt, endedAt });
  outcome = applied.outcome;
}
```

### 3.5 嵌套子代理的 Defer 机制

当子代理自身还有未完成的后代时，announce 会被 **延迟（defer）**，确保结果完整后再向上汇报：

```typescript
// subagent-announce.ts:372-379
const pendingChildDescendantRuns = Math.max(
  0,
  subagentRegistryRuntime.countPendingDescendantRuns(params.childSessionKey),
);
if (pendingChildDescendantRuns > 0 && announceType !== "cron job") {
  shouldDeleteChildSession = false;
  return false; // defer: 暂不 announce，等后代全部完成
}
```

**可选的 Wake 机制**：通过 `wakeOnDescendantSettle` 配置，待后代完成后可以再触发一轮 agent 调用，让编排者汇总所有子结果后再 announce。

流程：Orchestrator(depth=1) spawn Worker1 和 Worker2 → 自身完成 → [defer，因有 pending 后代] → Worker1 完成 announce 回 Orchestrator → Worker2 完成 announce 回 Orchestrator → [wake] 所有后代完成 → Orchestrator 汇总 → announce 回主 Agent。

---

## 4. 结果收集流程

### 4.1 完整的结果回传链路

**Step 1**：子代理 run 结束

**Step 2**：`completeSubagentRun()`（subagent-registry-lifecycle.ts）— 记录 outcome / endedAt，持久化

**Step 3**：`startSubagentAnnounceCleanupFlow()`

**Step 4**：`runSubagentAnnounceFlow()`（subagent-announce.ts）包含以下子步骤：

- `waitForSubagentRunOutcome()` — 等待子运行完全终止
- `countPendingDescendantRuns()` — 有后代未完成？→ defer 返回
- `readSubagentOutput()` — 从子会话 history 提取输出
- `buildChildCompletionFindings()` — 聚合直接子代理的完成摘要
- 构造 AgentInternalEvent（task_completion），包含 type / source / status / statusLabel / result / statsLine / replyInstruction

**Step 5**：`deliverSubagentAnnouncement()`（subagent-announce-delivery.ts）

- 请求方是顶层 agent？→ deliver: true（对外投递到用户频道）
- 请求方是子代理？→ deliver: false（内部注入到编排者会话）

### 4.2 completeSubagentRun——运行结束标记

```typescript
// subagent-registry-lifecycle.ts:397-489 (关键摘要)
const completeSubagentRun = async (completeParams) => {
  const entry = params.runs.get(completeParams.runId);
  entry.endedAt = completeParams.endedAt ?? Date.now();
  entry.outcome = completeParams.outcome; // { status: "ok" | "error" | "timeout" }
  entry.endedReason = completeParams.reason;
  await freezeRunResultAtCompletion(entry); // 冻结最终结果
  params.persist(); // 持久化到磁盘
  if (completeParams.triggerCleanup) {
    startSubagentAnnounceCleanupFlow(completeParams.runId, entry);
  }
};
```

### 4.3 输出文本提取

子代理的输出不是简单的"最后一条消息"，而是经过精心筛选的：

- `readSubagentOutput()` — 从 session history 读取最终 assistant/tool 输出
- `readLatestSubagentOutputWithRetry()` — 带重试的读取（应对延迟写入）
- `selectSubagentOutputText()` — 从 history 条目中选择最有用的文本
- `isAnnounceSkip()` — 子代理回复 "ANNOUNCE_SKIP" 则不发公告

### 4.4 投递策略——区分顶层与嵌套

- **顶层 Agent（depth=0）** — 外部投递，deliver: true，结果通过频道送达用户
- **子代理（depth>=1）** — 内部注入，deliver: false，结果注入编排者的会话上下文

```typescript
// subagent-announce.ts:560-601 (关键摘要)
const delivery = await deliverSubagentAnnouncement({
  requesterSessionKey: targetRequesterSessionKey,
  requesterIsSubagent,
  // ... 完整的投递参数
});
```

**投递容错**：

1. 先尝试 direct agent delivery（带稳定幂等键）
2. 失败则 fallback 到 queue routing
3. 仍失败则短指数退避重试后放弃

### 4.5 Announce 内容结构

每次 announce 构造一个标准化的 AgentInternalEvent：

```typescript
// subagent-announce.ts:543-557
const internalEvents: AgentInternalEvent[] = [
  {
    type: "task_completion",
    source: "subagent",                        // 来源标识
    childSessionKey: params.childSessionKey,    // 子会话 key
    childSessionId: announceSessionId,          // 子会话 ID
    announceType: "subagent task",              // 公告类型
    taskLabel: "研究用户认证方案",                // 任务标签
    status: "ok",                               // 运行状态
    statusLabel: "completed successfully",       // 状态描述
    result: "...子代理的输出内容...",              // 核心结果
    statsLine: "runtime 5m12s | tokens 1.2k/800", // 运行统计
    replyInstruction: "...",                    // 回复指导
  },
];
```

---

## 5. 完整协作示例

### 示例 1：简单的单层并行子代理

**场景**：主 Agent 需要同时研究 3 个独立主题。

**配置**：maxSpawnDepth: 1, maxConcurrent: 8, maxChildrenPerAgent: 5

**流程**：

1. 用户："帮我同时调研 Redis, Memcached 和 Caffeine 的缓存方案"
2. 主 Agent(depth=0) 调用 sessions_spawn(task="调研 Redis 缓存方案") → 返回 accepted, runId: "run-1"
3. 主 Agent 调用 sessions_spawn(task="调研 Memcached 缓存方案") → 返回 accepted, runId: "run-2"
4. 主 Agent 调用 sessions_spawn(task="调研 Caffeine 缓存方案") → 返回 accepted, runId: "run-3"
5. 主 Agent 继续其他工作（不阻塞）
6. 3 个 subagent 在 subagent lane 中并发执行
7. Subagent-1(Redis) 完成 → announce 回主 Agent
8. Subagent-3(Caffeine) 完成 → announce 回主 Agent
9. Subagent-2(Memcached) 完成 → announce 回主 Agent
10. 主 Agent 综合 3 个结果，回复用户

**上下文视角**：

- 主 Agent — 会话包含：用户消息 + 3 次 spawn 工具调用结果 + 3 个 announce 事件（task_completion）
- Subagent-1 — `[Subagent Context]` + `[Subagent Task]: 调研 Redis 缓存方案` → 独立完成
- Subagent-2 — `[Subagent Context]` + `[Subagent Task]: 调研 Memcached 缓存方案` → 独立完成
- Subagent-3 — `[Subagent Context]` + `[Subagent Task]: 调研 Caffeine 缓存方案` → 独立完成

### 示例 2：嵌套编排（Orchestrator 模式）

**场景**：主 Agent 委派一个编排子代理去协调多个 worker。

**配置**：maxSpawnDepth: 2, maxConcurrent: 8, maxChildrenPerAgent: 5

**流程**：

1. 主 Agent(depth=0) 调用 sessions_spawn(task="编排完整的安全审计") → 返回 accepted
2. **Orchestrator(depth=1, role=orchestrator)** 启动，接收任务。因 maxSpawnDepth >= 2 拥有 sessions_spawn 工具
3. Orchestrator 调用 sessions_spawn(task="扫描 SQL 注入漏洞") → **Worker-A(depth=2, role=leaf)** 启动，无 sessions_spawn 工具
4. Orchestrator 调用 sessions_spawn(task="检查 XSS 防护") → **Worker-B(depth=2, role=leaf)** 启动
5. Orchestrator 自身完成，但 Worker-A/B 仍在跑 → **[defer]**，countPendingDescendantRuns > 0，暂不 announce
6. Worker-A 完成 → announce 回 Orchestrator（deliver=false, 内部注入）
7. Worker-B 完成 → announce 回 Orchestrator（deliver=false, 内部注入）
8. **[wake]** 所有后代完成，wakeSubagentRunAfterDescendants 触发
9. Orchestrator 收到 childCompletionFindings："Worker-A: 发现 3 处 SQL 注入 | Worker-B: XSS 防护完备"
10. Orchestrator 汇总 → announce 回主 Agent（deliver=true, 对外投递）
11. 主 Agent 收到编排结果："安全审计完成：3 处 SQL 注入风险，XSS 防护良好" → 回复用户

**Announce 链（结果流向）**：

- Worker-A(depth=2) → announce(deliver=false) → Orchestrator(depth=1)
- Worker-B(depth=2) → announce(deliver=false) → Orchestrator(depth=1)
- Orchestrator 汇总后 → announce(deliver=true) → 主 Agent(depth=0) → 用户

### 示例 3：级联停止

用户在主聊天发送 `/stop`：

1. 主 Agent 收到 /stop 信号
2. 停止 Orchestrator(depth=1) → 级联停止其下的 Worker-A(depth=2) 和 Worker-B(depth=2)
3. 停止其他独立子代理

---

## 6. 关键设计决策总结

### 6.1 为什么 Spawn 是非阻塞的？

- **避免父 Agent 的 LLM 上下文窗口被长时间占据**：子代理可能运行数分钟到十几分钟，阻塞会浪费 token 和计算资源
- **允许并行扇出**：一次 turn 中可以 spawn 多个子代理
- **松耦合**：子代理通过 announce 事件异步报告，父代理在后续 turn 中处理

### 6.2 为什么子代理上下文是精简的？

- **成本控制**：每个子代理有独立的 token 用量，精简 bootstrap 减少基础消耗
- **关注聚焦**：子代理只需要知道可用工具（TOOLS.md）和 agent 规则（AGENTS.md），不需要完整的身份/灵魂设定
- **隔离性**：避免父级上下文中的无关信息干扰子任务执行

### 6.3 为什么用 Lane 而不是进程/线程隔离？

- **共享 Gateway 进程**：所有 agent 和 subagent 运行在同一个 Node.js 进程中
- **轻量调度**：命令队列 lane 提供了无需进程间通信的并发控制
- **maxConcurrent 作为安全阀**：防止资源耗尽

### 6.4 Announce vs 轮询

OpenClaw 选择 **推送（announce）** 而非 **轮询（poll）** 模式：

- **轮询子代理状态** — 明确禁止，原因：浪费父 Agent 的 token，增加延迟
- **Announce 推送** — 采用，原因：子代理完成后主动推送结果，零成本等待

---

## 7. 配置参考

```json
{
  "agents": {
    "defaults": {
      "maxConcurrent": 4,
      "subagents": {
        "maxConcurrent": 8,
        "maxSpawnDepth": 1,
        "maxChildrenPerAgent": 5,
        "runTimeoutSeconds": 900,
        "archiveAfterMinutes": 60,
        "model": "...",
        "thinking": "...",
        "allowAgents": ["*"]
      }
    }
  }
}
```

配置字段说明：

- `maxConcurrent`（agents.defaults 级）— agent lane 并发数
- `subagents.maxConcurrent` — subagent lane 并发数
- `subagents.maxSpawnDepth` — 嵌套深度（1=leaf, 2=orchestrator+leaf, 最大 5）
- `subagents.maxChildrenPerAgent` — 每 session 活跃子代理数（范围 1-20）
- `subagents.runTimeoutSeconds` — 子运行超时秒数（0=无超时）
- `subagents.archiveAfterMinutes` — 完成后自动归档时间
- `subagents.model` — 子代理默认模型（可比主 Agent 更便宜）
- `subagents.thinking` — 子代理默认 thinking 级别
- `subagents.allowAgents` — 允许 spawn 的 agentId 列表

---

## 8. 与 Cursor 的对比参考

**Spawn 方式**：

- OpenClaw — `sessions_spawn` 工具
- Cursor — `Task` tool 带 subagent_type

**阻塞性**：

- OpenClaw — 完全非阻塞，announce 推送
- Cursor — 子 agent 完成后返回结果（同步等待）

**上下文传递**：

- OpenClaw — 独立会话 + extraSystemPrompt + `[Subagent Task]`
- Cursor — prompt 参数描述任务，独立上下文

**结果回传**：

- OpenClaw — AgentInternalEvent → deliverSubagentAnnouncement
- Cursor — 直接返回 string 结果

**嵌套**：

- OpenClaw — 支持 depth 1-5，需配置 maxSpawnDepth
- Cursor — 不支持子 agent 再 spawn

**并发控制**：

- OpenClaw — 多 lane 命令队列，可配 maxConcurrent
- Cursor — 内置并发管理

**会话持久化**：

- OpenClaw — 独立 transcript，可 keep/delete/archive
- Cursor — 临时上下文，不持久

---

*文档基于 OpenClaw 源码 openclaw/src/agents/ 目录分析，最后更新：2026-03-30*
