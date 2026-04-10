# Cursor Hooks Langfuse 可观测性实践与 Trace 分析

基于 `memory-research` 项目中 `.cursor/hooks/` 实现的深度分析，涵盖架构设计、子代理 Trace 追踪、与 Claude Code hooks 的对比，以及已知盲区。

---

## 一、系统总览

### 1.1 做了什么

在 Cursor IDE 的 Agent 工作流中，通过 hooks 机制拦截每一个关键生命周期事件，将其上报到 Langfuse 平台，实现：

- **全量行为追踪**：Prompt 提交、Agent 思考、文件读写、Shell 执行、MCP 调用
- **会话级聚合**：同一 `conversation_id` 的所有事件归入一条 Langfuse Trace
- **多 IDE 适配**：通过 Adapter 层同时支持 Cursor / CodeBuddy / Claude Code（预留）
- **效率评分**：Agent 结束时自动计算 `completion_status` 和 `efficiency` 分数

### 1.2 架构

```
Cursor IDE (每个 hook 事件)
  │
  ▼ stdin JSON
.cursor/hooks/hook-handler.js        ← 统一入口
  │
  ├── lib/adapters/cursor.js          ← Cursor 字段标准化
  ├── lib/adapters/codebuddy.js       ← CodeBuddy 字段标准化
  ├── lib/adapters/claude.js          ← Claude Code（预留）
  │       ↓
  │   normalize(rawInput) → 标准格式
  │
  ├── lib/langfuse-client.js          ← Langfuse SDK 封装
  │       ↓
  │   getOrCreateTrace(input) → Langfuse Trace
  │
  ├── lib/handlers.js                 ← 12 种事件处理器
  │       ↓
  │   routeHookHandler() → span/generation/event/score
  │
  └── lib/utils.js                    ← 工具函数
```

---

## 二、12 种 Hook 事件映射

Cursor 的 IDE 事件名经过 Adapter 层标准化为 canonical 名称：


| Cursor 事件名             | Canonical 名称       | Langfuse 类型   | 说明                           |
| ---------------------- | ------------------ | ------------- | ---------------------------- |
| `beforeSubmitPrompt`   | `before_prompt`    | generation    | 用户提交 Prompt                  |
| `afterAgentResponse`   | `after_response`   | generation    | Agent 回复文本                   |
| `afterAgentThought`    | `after_thought`    | span          | Agent 思维链（extended thinking） |
| `beforeShellExecution` | `before_shell`     | span          | Shell 命令执行前                  |
| `afterShellExecution`  | `after_shell`      | span          | Shell 命令执行后（含输出）             |
| `beforeMCPExecution`   | `before_mcp`       | span          | MCP 工具调用前                    |
| `afterMCPExecution`    | `after_mcp`        | span          | MCP 工具返回后                    |
| `beforeReadFile`       | `before_file_read` | span          | 文件读取前                        |
| `afterFileEdit`        | `after_file_edit`  | span          | 文件编辑后（含 diff 统计）             |
| `stop`                 | `stop`             | event + score | Agent 回合结束                   |
| `beforeTabFileRead`    | `before_tab_read`  | span          | Tab 补全读取文件                   |
| `afterTabFileEdit`     | `after_tab_edit`   | span          | Tab 补全编辑文件                   |


---

## 三、子代理 (Subagent) Trace 的行为特征

### 3.1 发现过程

在 Langfuse 中观测到一条异常 Trace：

- **名称**：`Cursor composer-2-fast`
- **Input/Output**：均为 `undefined`
- **Observations**：只有 10 个 `Read: xxx.py` span + 1 个 `Agent Thinking` span
- **无** `beforeSubmitPrompt`、`afterAgentResponse`、`stop` 事件

### 3.2 根因

该 Trace 来自 Cursor Agent 的 **Task 工具**派生的子代理（`subagent_type: "explore"`）：

```
父会话 eaa42113-... (2026-04-08 20:23 创建)
  │
  ├── 用户提问 → 分析数据 → 读文件 → ...
  │
  ├── [Line 52] 调用 Task 工具:
  │     description: "Study mempalace source code pipeline"
  │     subagent_type: "explore"
  │     readonly: true
  │         │
  │         ▼
  │     子代理 1021f39b-... (2026-04-09 23:45 执行)
  │         ├── Read: convo_miner.py
  │         ├── Read: miner.py
  │         ├── ... (共 10 个文件)
  │         ├── Agent Thinking
  │         └── Read: normalize.py
  │         → 结果返回父会话
  │
  └── 继续后续处理 ...
```

**证据链**：

- agent-transcripts 中 `eaa42113-*/subagents/1021f39b-*.jsonl` 确认了父子关系
- 子代理 transcript 第 1 行的 prompt 正是父会话 Task 工具的完整指令
- 父会话 transcript 第 52 行的 Task 调用生成了该子代理

### 3.3 子代理 Trace 的 3 个盲区


| 盲区            | 原因                                                      | 影响                   |
| ------------- | ------------------------------------------------------- | -------------------- |
| **Input 为空**  | 子代理的 prompt 由 Task 工具内部注入，不经过 `beforeSubmitPrompt` hook | Trace 没有 `input` 字段  |
| **Output 为空** | 子代理的结果直接返回给父 Agent，不经过 `afterAgentResponse` hook        | Trace 没有 `output` 字段 |
| **无 stop 事件** | 子代理的生命周期结束不触发 `stop` hook                               | 无完成状态和效率评分           |


只有**中间过程**的 hook（`beforeReadFile`、`afterAgentThought` 等）能正常触发。

### 3.4 父子 Trace 跨天问题

父会话可能跨天运行。本案例中：

- 父 Trace 时间戳：**04-08** 20:23
- 子 Trace 时间戳：**04-09** 23:45

在 Langfuse 按日期筛选时容易漏掉父 Trace。

---

## 四、Agent Thinking 的输出内容

### 4.1 常见误解

看到 `Agent Thinking` span 的输出是一段自然语言的规划文本，而非结构化的 `{ text, tool_calls }` JSON。

### 4.2 原因

Cursor 将一次 LLM 调用的完整响应**拆解**为多个独立的 hook 事件：

```
一次 LLM API 调用的完整响应
│
├── thinking 部分  →  afterAgentThought   ← 纯文本思维链
├── text 部分      →  afterAgentResponse   ← 回复文本
└── tool_calls     →  每个工具各自的 before/after hook
        ├── beforeReadFile
        ├── beforeShellExecution
        └── beforeMCPExecution
```

无法通过任何单一 hook 获取完整的结构化响应。`afterAgentThought` 捕获的就是 Claude 的 extended thinking 内容——模型在决定做什么之前的内部推理。

### 4.3 Metadata 中的有效信息

```json
{
  "generation_id": "8284124d-170b-4aba-ab79-d3dc0cc33e24",
  "duration_ms": 10077,
  "duration_formatted": "10.1s",
  "thinking_length": 1107
}
```

可用于分析：思考时长、思维链长度、每轮思考的效率。

---

## 五、与 Claude Code Hooks 的对比

### 5.1 设计哲学差异


| 维度              | Cursor Hooks                                             | Claude Code Hooks                                                          |
| --------------- | -------------------------------------------------------- | -------------------------------------------------------------------------- |
| **核心抽象**        | **模型输出生命周期**（prompt → thinking → response → tool → stop） | **工具执行生命周期**（PreToolUse → PostToolUse → Stop）                              |
| **Thinking 捕获** | `afterAgentThought` 直接拿到思维链文本                            | **无对应事件**（thinking 对 hooks 不可见）                                            |
| **Response 捕获** | `afterAgentResponse` 拿到回复文本                              | **无对应事件**（通过 PostSampling 内部 hook 处理）                                      |
| **文件读取**        | `beforeReadFile`（读取前拦截）                                  | 统一走 `PreToolUse` + `matcher: "Read"`                                       |
| **子代理事件**       | 子代理的中间 hook 能触发（但缺首尾）                                    | `SubagentStart` / `SubagentStop` / `TaskCreated` / `TaskCompleted`（完整生命周期） |


### 5.2 Claude Code 的 Hooks 是否也这样拆分？

**不是**。Claude Code 的设计完全不同：

1. **Claude Code 不暴露模型内部状态**：没有 `afterAgentThought`、`afterAgentResponse` 这样的 hook。模型的 thinking 和 text 输出对 hooks 系统完全不可见。
2. **Claude Code 只关注工具动作**：所有可观测性围绕 `PreToolUse` / `PostToolUse` 展开。你能看到"Agent 调用了 Write 工具写了什么文件"，但看不到"Agent 在想什么"或"Agent 回复了什么文字"。
3. **Claude Code 对子代理有完整事件**：`SubagentStart`、`SubagentStop`、`TaskCreated`、`TaskCompleted` 提供了子代理完整的生命周期观测。而 Cursor 的子代理只能通过中间的工具 hook 间接观测。
4. **Claude Code 的内部 hook 是隐藏的**：`PostSampling Hooks`（如 `extractSessionMemory`）在模型采样后执行，但不暴露给用户。这些内部 hook 处理了 Cursor 中由 `afterAgentThought` / `afterAgentResponse` 承担的职责。

### 5.3 事件覆盖对比表


| 能力     | Cursor Hook                                                      | Claude Code Hook                           |
| ------ | ---------------------------------------------------------------- | ------------------------------------------ |
| 用户输入   | `beforeSubmitPrompt`                                             | `UserPromptSubmit`                         |
| 模型思维链  | `afterAgentThought` ✅                                            | ❌ 无                                        |
| 模型回复文本 | `afterAgentResponse` ✅                                           | ❌ 无（内部 PostSampling 处理）                    |
| 工具调用前  | `beforeReadFile` / `beforeShellExecution` / `beforeMCPExecution` | `PreToolUse`（统一入口 + matcher）               |
| 工具调用后  | `afterFileEdit` / `afterShellExecution` / `afterMCPExecution`    | `PostToolUse`（统一入口）                        |
| 回合结束   | `stop`                                                           | `Stop`                                     |
| 子代理开始  | ❌（仅中间 hook 可见）                                                   | `SubagentStart` ✅                          |
| 子代理结束  | ❌                                                                | `SubagentStop` ✅                           |
| 异步任务   | ❌                                                                | `TaskCreated` / `TaskCompleted` ✅          |
| 上下文压缩  | ❌                                                                | `PreCompact` / `PostCompact` ✅             |
| 权限管理   | ❌                                                                | `PermissionRequest` / `PermissionDenied` ✅ |
| Tab 补全 | `beforeTabFileRead` / `afterTabFileEdit` ✅                       | ❌（CLI 无 Tab）                               |


### 5.4 可观测性总结

- **Cursor 更深**：能看到模型的 thinking 和 response 文本，对分析模型推理过程和回复质量有价值
- **Claude Code 更广**：覆盖 27 种事件，包括子代理生命周期、上下文压缩、权限管理、文件/配置变更等系统级事件
- **两者互补**：Cursor 擅长"模型在想什么"，Claude Code 擅长"系统在做什么"

---

## 六、多 IDE Adapter 层设计

### 6.1 目的

让同一套 handler 和 Langfuse 上报逻辑支持不同 IDE 的 hook 格式。

### 6.2 Adapter 接口

每个 Adapter 实现 4 个方法：

```javascript
{
  name: 'cursor',              // 标识名
  detect(input) {},            // 自动检测输入是否属于该 IDE
  normalize(input) {},         // 将 IDE 特有字段标准化为 canonical 格式
  transformResponse(event, response) {},  // 将 canonical 响应转回 IDE 格式
}
```

### 6.3 已实现的 Adapter


| Adapter       | 检测条件                                 | 字段映射                                                                                                    | 响应转换                          |
| ------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------- | ----------------------------- |
| **cursor**    | `conversation_id` + `cursor_version` | 直传（字段名一致）                                                                                               | 直传                            |
| **codebuddy** | `session_id` + `agent_version`       | `session_id→conversation_id`、`message_id→generation_id`、`workspace→workspace_roots`、`user_name→user_id` | `true/false → "allow"/"deny"` |
| **claude**    | 预留（`detect` 始终返回 false）              | 直传                                                                                                      | 直传                            |


### 6.4 CodeBuddy 的特殊适配

- `**afterFileEdit`**：CodeBuddy 的 edits 格式为 `{ code_edit: "..." }`，需转换为 `{ old_string: "", new_string: "..." }`
- `**afterSearchRplaceFileEdit`**：CodeBuddy 独有的事件名（注意 `Rplace` 拼写），映射到 `after_search_replace_edit`
- **响应格式**：CodeBuddy 的 `beforeSubmitPrompt` 期望 `{ continue: "allow"/"deny" }` 而非 `{ continue: true/false }`

---

## 七、test-trace.js 测试套件

`.cursor/hooks/test-trace.js` 是 hooks 系统的完整测试套件，分 6 个部分：


| Part  | 名称               | 测试内容                                                                                                                             |
| ----- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **1** | 工具函数单元测试         | `generateTraceName`、`generateSessionId`、`generateTags`、`calculateEditStats`、`getFileExtension`、`formatDuration`、`determineLevel` |
| **2** | Adapter 测试       | Cursor/CodeBuddy adapter 的 detect、normalize、transformResponse 逻辑，含 CodeBuddy 的 edit 格式适配和自动检测                                    |
| **3** | Handler 路由测试     | 14 种 canonical 事件名全量路由，验证每个 handler 都正确调用了 Langfuse trace 方法                                                                     |
| **4** | 端到端 Cursor 路径    | 从 Cursor 原始输入 → adapter.normalize → routeHookHandler → adapter.transformResponse 的完整链路                                           |
| **5** | 端到端 CodeBuddy 路径 | 同上，覆盖 CodeBuddy 的 beforeSubmitPrompt、afterSearchRplaceFileEdit、afterFileRead 等场景                                                 |
| **6** | Langfuse 集成测试    | 需要 `.env` 中配置 API Key，向 Langfuse 实际发送 Cursor 和 CodeBuddy 两条测试 trace                                                              |


运行方式：`node .cursor/hooks/test-trace.js`

---

## 八、LLM API 代理层：概念、LiteLLM 与可观测性原理

### 8.1 什么是 LLM API 代理层

LLM API 代理层（也叫 LLM Gateway / AI Gateway）是一个部署在**你的应用**与**LLM 提供商 API**之间的中间服务。所有发往 OpenAI、Anthropic、Google 等模型的请求都先经过这个代理，再转发到真正的 API 端点。

```
你的应用 / IDE / Agent
       │
       ▼  HTTP 请求（OpenAI 兼容格式）
┌──────────────────────────┐
│  LLM API 代理层           │   ← 在这一层可以做：
│  (LiteLLM / Portkey /     │      - 统一多家 LLM 的 API 格式
│   OpenRouter / Helicone)  │      - 记录完整的 request/response
│                           │      - 统计 token 用量和成本
│                           │      - 负载均衡 / 故障转移
│                           │      - 缓存 / 限速 / 鉴权
└──────────┬───────────────┘
           │
           ▼  转发到实际 LLM API
   ┌───────────────┐
   │ OpenAI API     │
   │ Anthropic API  │
   │ Azure OpenAI   │
   │ Google Vertex   │
   │ AWS Bedrock    │
   │ ...            │
   └───────────────┘
```

**类比**：就像 Nginx 是 HTTP 请求的反向代理，LLM Gateway 是 LLM API 调用的反向代理。它不改变请求的语义，但在中间层获得了完整的请求/响应可见性。

### 8.2 为什么需要代理层

不使用代理层时，LLM 调用对你来说是一个黑盒：

```
Agent 框架 → LLM API → 返回
      ↑                    ↑
   你知道你发了什么      你知道你收到了什么
   但不知道花了多少钱    但没有集中记录
```

使用代理层后：

```
Agent 框架 → 代理层 → LLM API → 返回
                 │
                 ├── 记录到 Langfuse：完整 messages、token 用量、延迟、成本
                 ├── 记录到日志系统：审计合规
                 └── 触发告警：超预算、异常延迟
```

### 8.3 LiteLLM 是什么

[LiteLLM](https://github.com/BerriAI/litellm) 是目前最主流的开源 LLM API 代理层（MIT 协议），支持 100+ LLM 提供商，将它们统一到 OpenAI 兼容的 API 格式下。

**两种使用方式**：


| 方式               | 适用场景    | 部署                               |
| ---------------- | ------- | -------------------------------- |
| **Python SDK**   | 个人开发、原型 | `pip install litellm`，在代码中直接调用   |
| **Proxy Server** | 团队/生产环境 | Docker/K8s 部署，提供 REST API + 管理后台 |


**核心能力**：

```python
# 不使用 LiteLLM：每个提供商需要不同的 SDK 和格式
import openai        # OpenAI
import anthropic     # Anthropic
import google.cloud  # Google

# 使用 LiteLLM：统一一行代码调用任意模型
import litellm
response = litellm.completion(
    model="anthropic/claude-sonnet-4-6",  # 或 "openai/gpt-4o" 或 "vertex_ai/gemini-pro"
    messages=[{"role": "user", "content": "Hello"}]
)
```

### 8.4 LiteLLM + Langfuse 集成

LiteLLM 内置了 Langfuse callback，只需两行配置即可将**每一次 LLM API 调用**自动上报到 Langfuse：

```python
import litellm
litellm.success_callback = ["langfuse"]
litellm.failure_callback = ["langfuse"]
```

或在 Proxy Server 的配置中：

```yaml
# litellm_config.yaml
litellm_settings:
  success_callback: ["langfuse"]

environment_variables:
  LANGFUSE_PUBLIC_KEY: "pk-..."
  LANGFUSE_SECRET_KEY: "sk-..."
  LANGFUSE_HOST: "https://cloud.langfuse.com"
```

上报到 Langfuse 的每个 `litellm.completion.LLM` span 包含：


| 字段           | 内容                                                     |
| ------------ | ------------------------------------------------------ |
| **Input**    | 完整的 `messages` 数组（system + user + assistant + tool 消息） |
| **Output**   | 模型的完整响应（`[{type:"text",...}, {type:"tool_use",...}]`）  |
| **Model**    | 实际调用的模型名（如 `claude-sonnet-4-6`）                        |
| **Usage**    | `input_tokens`、`output_tokens`、`total_tokens`          |
| **Cost**     | 基于 token 用量的费用估算                                       |
| **Latency**  | 该次 API 调用的精确耗时                                         |
| **Metadata** | trace_id、user_id、tags 等自定义字段                           |


这就是截图中 `litellm.completion.LLM` span 能显示完整输入输出的原因——它拦截了 Agent 与 LLM 之间的**实际 HTTP 请求/响应**。

### 8.5 主流 LLM Gateway 对比


| 产品                        | 类型      | 模型数      | 可观测性                  | 成本         | 自部署    |
| ------------------------- | ------- | -------- | --------------------- | ---------- | ------ |
| **LiteLLM**               | 开源自部署   | 100+     | 需外接 Langfuse/Helicone | 免费（MIT）    | ✅      |
| **Portkey**               | 托管 SaaS | 250+     | 内置                    | 按日志量计费     | ❌（仅边缘） |
| **OpenRouter**            | SaaS 市场 | 200-300+ | 基础统计                  | 5.5% 信用手续费 | ❌      |
| **Helicone**              | 开源/托管   | 多家       | 内置（核心卖点）              | 免费层 + 付费   | ✅      |
| **Cloudflare AI Gateway** | 托管      | 多家       | 内置                    | 免费层        | ❌      |


### 8.6 代理层的真正意义：不只是多模型路由

一个常见的误解是：**"代理层 = 切换不同的 LLM 提供商"**。如果只用一家的模型，就不需要代理层。

这个认知**不完全正确**。多模型路由只是代理层 7 大价值之一：

```
代理层的 7 大价值
│
├── 1. 统一 API 格式 ← 多模型路由（最直观但不是唯一价值）
│     不同提供商的 SDK/格式/鉴权全部统一
│
├── 2. 可观测性 ← 🔑 即使只用一家模型也需要
│     每次 API 调用的完整 input/output/token/latency 记录
│     集中式仪表盘，而非散落在各处的日志
│
├── 3. 成本追踪与预算控制
│     按项目/用户/团队统计费用
│     设置预算上限和告警
│
├── 4. 自动故障转移
│     2025 年每家主要 LLM 提供商都经历过重大服务中断
│     代理层可自动切到备用端点（同提供商不同区域 / 不同提供商）
│
├── 5. 智能路由
│     简单任务 → 便宜模型（$0.10/M tokens）
│     复杂推理 → 旗舰模型
│     可实现 50-70% 成本节省
│
├── 6. 缓存
│     网关级语义缓存，生产环境典型命中率 15-30%
│     重复请求直接返回，零 token 消耗
│
└── 7. 安全与密钥管理
      应用代码中只需一个网关 key
      真实的提供商 API key 不出代理层
```

**所以即使你只用 Anthropic 一家的模型**，代理层仍然在可观测性、成本追踪、缓存、故障转移等方面有独立价值。

### 8.7 Claude Code 真的只支持 Anthropic 吗？

**不是。** 这是另一个常见误解。Claude Code 实际支持多种 API 提供商：


| 部署方式                            | 配置                           | 说明                       |
| ------------------------------- | ---------------------------- | ------------------------ |
| **Anthropic Console**           | `ANTHROPIC_API_KEY`          | 个人开发者直连                  |
| **Claude for Teams/Enterprise** | 组织级配置                        | 企业推荐方式                   |
| **Amazon Bedrock**              | `ANTHROPIC_BEDROCK_BASE_URL` | AWS 原生部署                 |
| **Google Vertex AI**            | Vertex 配置                    | GCP 原生部署                 |
| **Microsoft Foundry**           | Foundry 配置                   | Azure 原生部署               |
| **自定义端点（任意代理）**                 | `ANTHROPIC_BASE_URL`         | 可指向 OpenRouter、LiteLLM 等 |


关键环境变量：

```bash
# 指向 OpenRouter，可访问 300+ 模型（包括 OpenAI、Google、Meta、Mistral 等）
export ANTHROPIC_BASE_URL="https://openrouter.ai/api"
export ANTHROPIC_AUTH_TOKEN="<your-openrouter-api-key>"
export ANTHROPIC_API_KEY=""  # 显式留空
```

通过 `ANTHROPIC_BASE_URL`，Claude Code 可以将 API 请求路由到**任何兼容 Anthropic Messages API 格式的端点**——包括 OpenRouter（300+ 模型）、LiteLLM Proxy、自建网关等。

### 8.8 那 Claude Code 为什么不内置代理层？

Claude Code 选择了一种**不同但等效的架构策略**：

```
CodeBuddy 的选择：                    Claude Code 的选择：
在 Agent 内部集成代理层                 让用户在外部自行部署代理层

Agent Server                          Claude Code CLI
  │                                     │
  ├── litellm（内嵌）                    ├── ANTHROPIC_BASE_URL（环境变量）
  │     ├── 路由                         │     → 指向用户自建的代理
  │     ├── 观测                         │     → 或指向 OpenRouter / LiteLLM
  │     └── 缓存                         │     → 或直连 Anthropic（默认）
  │                                     │
  ▼                                     ▼
LLM API                               LLM API（或代理）
```


| 维度            | CodeBuddy（内嵌 LiteLLM） | Claude Code（外部代理）        |
| ------------- | --------------------- | ------------------------ |
| 可观测性          | ✅ 开箱即用                | 需用户自行配置代理 + Langfuse     |
| 多模型路由         | ✅ 内置                  | 需配置 OpenRouter / LiteLLM |
| 部署复杂度         | 低（一体化）                | 高（需额外部署代理）               |
| 灵活性           | 受限于内置的 LiteLLM 版本     | 用户完全控制代理层选型              |
| 数据主权          | 数据经过 CodeBuddy 服务器    | 用户自控（可纯本地）               |
| 离线使用          | ❌                     | ✅（直连 + 本地模型）             |
| transcript 兜底 | ❌                     | ✅ 本地 .jsonl 保留完整数据       |


**Claude Code 的设计哲学**：不替用户做选择。默认直连 Anthropic（最简单），需要代理层时通过 `ANTHROPIC_BASE_URL` 指出去（最灵活）。同时用 transcript 文件兜底，确保即使没有代理层，数据也不会丢失。

### 8.9 截图中的架构还原

结合截图中的 Trace 结构，CodeBuddy 的完整观测链路如下：

```
用户在 CodeBuddy IDE 中提问
  │
  ▼
CodeBuddy Agent Server（chat.AGENT）
  │
  ├── [1] check_and_handle_limits.TASK      ← 鉴权/限流
  ├── [2] batch_check_user_gray_async.TASK  ← 灰度检查
  │
  ├── [3] litellm.completion()              ← 通过 LiteLLM 调用 LLM
  │     ├── Input:  完整 messages（含 system prompt + MCP 配置）
  │     ├── Output: [{text: "..."}, {tool_use: "fetch_mcp_tools"}]
  │     └── Langfuse callback 自动记录 → litellm.completion.LLM span
  │
  ├── [4] fetch_mcp_tools.TOOL              ← 执行模型请求的工具
  ├── [5] get_server_mcp_tools_async.TASK
  │
  ├── [6] litellm.completion()              ← 第二轮 LLM（带工具结果）
  │     ├── Input:  messages + tool_result
  │     ├── Output: [{text: "..."}, {tool_use: "use_mcp_tool"}]
  │     └── → litellm.completion.LLM span
  │
  ├── [7] use_mcp_tool.TOOL
  ├── ...（循环直到模型不再调用工具）
  │
  └── [N] litellm.completion()              ← 最终轮，纯文本回复
        └── → litellm.completion.LLM span
```

**关键区别**：CodeBuddy 的 Agent Server 是自研的，它直接调用 `litellm.completion()` 来与 LLM 交互，因此 LiteLLM 的 callback 能拦截到每一次 API 调用。而 Cursor / Claude Code 是**封闭的 IDE 客户端**，LLM 调用发生在它们的内部代码中，你无法在中间插入代理层。

### 8.10 Agent ↔ LLM 交互全流程对比图

#### 流程图 A：经 LiteLLM 代理层的 Agent（CodeBuddy 架构）

以用户提问 "在 dpar 项目中 pak 加载逻辑所有人有修改吗？" 为例，展示 Agent 与 LLM 之间经代理层的完整交互循环：

```
用户                   Agent Server               LiteLLM 代理层             Anthropic API         Langfuse
 │                        │                           │                        │                    │
 │  "pak加载逻辑谁改过?"   │                           │                        │                    │
 │───────────────────────>│                           │                        │                    │
 │                        │                           │                        │                    │
 │                        │  ┌─────────────────────┐  │                        │                    │
 │                        │  │ 组装 messages:       │  │                        │                    │
 │                        │  │  system: [系统提示+   │  │                        │                    │
 │                        │  │    MCP配置+工具描述]  │  │                        │                    │
 │                        │  │  user: [用户问题]     │  │                        │                    │
 │                        │  │  tools: [22个工具定义] │  │                        │                    │
 │                        │  └─────────────────────┘  │                        │                    │
 │                        │                           │                        │                    │
 │                        │ ══════ 第 1 轮 LLM ══════ │                        │                    │
 │                        │                           │                        │                    │
 │                        │  litellm.completion()     │                        │                    │
 │                        │──────────────────────────>│  POST /v1/messages     │                    │
 │                        │                           │───────────────────────>│                    │
 │                        │                           │                        │  SSE streaming     │
 │                        │                           │<───────────────────────│                    │
 │                        │                           │                        │                    │
 │                        │                           │  ┌──── callback ─────┐ │                    │
 │                        │                           │  │ 记录完整 req/resp  │ │                    │
 │                        │                           │  │ input_tokens: 5980│ │                    │
 │                        │                           │  │ output_tokens: 342│ │                    │
 │                        │                           │  │ latency: 15.78s   │ │                    │
 │                        │                           │  └───────────────────┘─│───────────────────>│
 │                        │                           │                        │   litellm.         │
 │                        │  Response:                │                        │   completion.LLM   │
 │                        │  [{type:"text",           │                        │   (完整input/output)│
 │                        │    text:"我需要先了解..."},│                        │                    │
 │                        │   {type:"tool_use",       │                        │                    │
 │                        │    name:"fetch_mcp_tools",│                        │                    │
 │                        │    input:{}}]             │                        │                    │
 │                        │<──────────────────────────│                        │                    │
 │                        │                           │                        │                    │
 │                        │  ┌─────────────────────┐  │                        │                    │
 │                        │  │ 解析 tool_use →      │  │                        │                    │
 │                        │  │ 本地执行工具:         │  │                        │                    │
 │                        │  │ fetch_mcp_tools()    │  │                        │                    │
 │                        │  │ → 返回 shadow-folk   │  │                        │                    │
 │                        │  │   的工具列表          │  │                        │                    │
 │                        │  └─────────────────────┘  │                        │                    │
 │                        │                           │                        │                    │
 │                        │  ┌─────────────────────┐  │                        │                    │
 │                        │  │ 将 tool_result 追加  │  │                        │                    │
 │                        │  │ 到 messages 数组     │  │                        │                    │
 │                        │  └─────────────────────┘  │                        │                    │
 │                        │                           │                        │                    │
 │                        │ ══════ 第 2 轮 LLM ══════ │                        │                    │
 │                        │                           │                        │                    │
 │                        │  litellm.completion()     │                        │                    │
 │                        │──────────────────────────>│  POST /v1/messages     │                    │
 │                        │                           │───────────────────────>│                    │
 │                        │                           │<───────────────────────│                    │
 │                        │                           │  ┌──── callback ─────┐ │                    │
 │                        │                           │  │ 记录第 2 轮       │─│───────────────────>│
 │                        │                           │  └───────────────────┘ │   litellm.         │
 │                        │  Response:                │                        │   completion.LLM   │
 │                        │  [{type:"text",...},       │                        │                    │
 │                        │   {type:"tool_use",       │                        │                    │
 │                        │    name:"use_mcp_tool",   │                        │                    │
 │                        │    input:{knowledgebase_  │                        │                    │
 │                        │      search...}}]         │                        │                    │
 │                        │<──────────────────────────│                        │                    │
 │                        │                           │                        │                    │
 │                        │  ┌ 执行 use_mcp_tool ──┐  │                        │                    │
 │                        │  │ → 调用 shadow-folk   │  │                        │                    │
 │                        │  │   MCP Server 查询    │  │                        │                    │
 │                        │  │ → 返回搜索结果       │  │                        │                    │
 │                        │  └─────────────────────┘  │                        │                    │
 │                        │                           │                        │                    │
 │                        │      ... 可能还有更多轮 ...│                        │                    │
 │                        │                           │                        │                    │
 │                        │ ══════ 第 N 轮 LLM ══════ │                        │                    │
 │                        │                           │                        │                    │
 │                        │  litellm.completion()     │                        │                    │
 │                        │──────────────────────────>│───────────────────────>│                    │
 │                        │                           │<───────────────────────│                    │
 │                        │                           │  ┌──── callback ─────┐ │                    │
 │                        │                           │  │ 记录第 N 轮       │─│───────────────────>│
 │                        │                           │  └───────────────────┘ │                    │
 │                        │  Response:                │                        │                    │
 │                        │  [{type:"text",           │                        │                    │
 │                        │    text:"根据查询结果...   │                        │                    │
 │                        │    pak加载逻辑的修改..."}] │                        │                    │
 │                        │  stop_reason: "end_turn"  │                        │                    │
 │                        │<──────────────────────────│                        │                    │
 │                        │                           │                        │                    │
 │  "根据查询结果..."      │                           │                        │                    │
 │<───────────────────────│                           │                        │                    │
```

**Langfuse 中的最终 Trace 结构**（每一轮 LLM 调用都有完整记录）：

```
chat.AGENT (总耗时)
  ├── litellm.completion.LLM  ← 第1轮：Input=完整messages, Output=[text+tool_use], tokens=5980
  ├── fetch_mcp_tools.TOOL
  ├── litellm.completion.LLM  ← 第2轮：Input=messages+tool_result, Output=[text+tool_use]
  ├── use_mcp_tool.TOOL
  ├── litellm.completion.LLM  ← 第3轮 ...
  └── litellm.completion.LLM  ← 第N轮：Output=[text], stop_reason=end_turn
```

---

#### 流程图 B：Claude Code 直连 Anthropic API（无代理层）

同样的 Agent Loop 模式，但 LLM 调用直接走 Anthropic SDK，无中间层拦截：

```
用户                   Claude Code CLI            Anthropic SDK              Anthropic API         transcript.jsonl
 │                        │                           │                        │                    │
 │  "修复购物车并发问题"    │                           │                        │                    │
 │───────────────────────>│                           │                        │                    │
 │                        │                           │                        │                    │
 │                        │  ┌─────────────────────┐  │                        │                    │
 │                        │  │ buildSystemPrompt()  │  │                        │                    │
 │                        │  │  静态: "You are      │  │                        │                    │
 │                        │  │   Claude Code..."    │  │                        │                    │
 │                        │  │  动态: CWD, Date,    │  │                        │                    │
 │                        │  │   OS, 工具列表...     │  │                        │                    │
 │                        │  │  cache_control 标记   │  │                        │                    │
 │                        │  └─────────────────────┘  │                        │                    │
 │                        │                           │                        │                    │
 │                        │ ══════ 第 1 轮 LLM ══════ │                        │                    │
 │                        │                           │                        │                    │
 │                        │  client.messages.create() │                        │                    │
 │                        │──────────────────────────>│  POST /v1/messages     │                    │
 │                        │                           │───────────────────────>│                    │
 │                        │                           │                        │                    │
 │                        │                           │  （无中间层拦截，        │                    │
 │                        │                           │   无 callback，         │                    │
 │                        │                           │   无实时上报）           │                    │
 │                        │                           │                        │                    │
 │                        │                           │  SSE streaming         │                    │
 │                        │                           │<───────────────────────│                    │
 │                        │  Response:                │                        │                    │
 │                        │  { content: [             │                        │                    │
 │                        │      {type:"thinking"...},│                        │                    │
 │                        │      {type:"text",...},   │                        │                    │
 │                        │      {type:"tool_use",    │                        │                    │
 │                        │       name:"Grep",...}],  │                        │                    │
 │                        │    usage: {               │                        │                    │
 │                        │      input_tokens: 8234,  │                        │                    │
 │                        │      output_tokens: 567,  │                        │                    │
 │                        │      cache_read: 7800 },  │                        │                    │
 │                        │    stop_reason:"tool_use"}│                        │                    │
 │                        │<──────────────────────────│                        │                    │
 │                        │                           │                        │                    │
 │                        │  ┌─────────────────────┐  │                        │                    │
 │                        │  │ 写入 transcript:     │  │                        │                    │
 │                        │  │ {"type":"assistant",  │──│────────────────────── │───────────────────>│
 │                        │  │  "message":{完整响应}} │  │                        │  append .jsonl     │
 │                        │  └─────────────────────┘  │                        │                    │
 │                        │                           │                        │                    │
 │                        │  ┌─────────────────────┐  │                        │                    │
 │                        │  │ 解析 tool_use →      │  │                        │                    │
 │                        │  │ 本地执行 Grep 工具   │  │                        │                    │
 │                        │  │ → 搜索 cart.*concur  │  │                        │                    │
 │                        │  │ → 返回匹配结果       │  │                        │                    │
 │                        │  └─────────────────────┘  │                        │                    │
 │                        │                           │                        │                    │
 │                        │  ┌─────────────────────┐  │                        │                    │
 │                        │  │ 写入 transcript:     │  │                        │                    │
 │                        │  │ {"type":"tool_result",│──│────────────────────── │───────────────────>│
 │                        │  │  "content":"..."}    │  │                        │  append .jsonl     │
 │                        │  └─────────────────────┘  │                        │                    │
 │                        │                           │                        │                    │
 │                        │ ══════ 第 2 轮 LLM ══════ │                        │                    │
 │                        │                           │                        │                    │
 │                        │  client.messages.create() │                        │                    │
 │                        │──────────────────────────>│───────────────────────>│                    │
 │                        │                           │<───────────────────────│                    │
 │                        │  Response + usage         │                        │                    │
 │                        │<──────────────────────────│                        │                    │
 │                        │  → 写入 transcript ───────│────────────────────────│───────────────────>│
 │                        │                           │                        │                    │
 │                        │      ... 循环直到 end_turn ...                      │                    │
 │                        │                           │                        │                    │
 │                        │ ══════ 第 N 轮 LLM ══════ │                        │                    │
 │                        │                           │                        │                    │
 │                        │  stop_reason: "end_turn"  │                        │                    │
 │                        │  → 写入 transcript ───────│────────────────────────│───────────────────>│
 │                        │                           │                        │                    │
 │                        │  ┌─────────────────────┐  │                        │                    │
 │                        │  │ [Stop hook 触发]     │  │                        │                    │
 │                        │  │ → 读取 transcript    │  │                        │                    │
 │                        │  │ → 提取所有轮次的     │  │                        │     Langfuse       │
 │                        │  │   messages + usage   │  │                        │        │          │
 │                        │  │ → 事后批量上报       │──│────────────────────── │────────│─────────>│
 │                        │  └─────────────────────┘  │                        │  (事后, 非实时)     │
 │                        │                           │                        │                    │
 │  "已修复并发问题..."     │                           │                        │                    │
 │<───────────────────────│                           │                        │                    │
```

**Langfuse 中的 Trace 结构**（事后从 transcript 重建，非实时）：

```
Claude Code Session
  ├── Generation: User Prompt    ← 用户输入
  ├── Generation: Claude Response ← 包含 usage（从 transcript 提取）
  │     input_tokens: 8234
  │     output_tokens: 567
  │     cache_read: 7800
  ├── Span: Grep (tool_use)
  ├── Generation: Claude Response ← 第2轮
  └── Generation: Claude Response ← 最终轮
```

---

#### 流程图 C：关键差异一图总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     CodeBuddy + LiteLLM（代理层架构）                         │
│                                                                             │
│  Agent ──→ litellm.completion() ──→ [LiteLLM Proxy] ──→ Anthropic API      │
│                                          │                                  │
│                                     callback 拦截                            │
│                                          │                                  │
│                                     ┌────▼────┐                             │
│                                     │ Langfuse │  ✅ 实时                    │
│                                     │  每轮LLM │  ✅ 完整 messages           │
│                                     │  都有记录 │  ✅ token 用量              │
│                                     └─────────┘  ✅ 结构化 tool_use output  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                     Claude Code（SDK 直连架构）                               │
│                                                                             │
│  Agent ──→ anthropic.messages.create() ──────────→ Anthropic API            │
│    │                （无中间层）                                               │
│    │                                                                        │
│    ▼                                                                        │
│  transcript.jsonl  ─── [Stop hook] ──→ 读取 ──→ ┌────────┐                 │
│  (本地文件,                                      │ Langfuse │  ⚠️ 事后       │
│   每轮都写入完整                                  │  批量重建 │  ✅ 有 usage   │
│   response+usage)                                └────────┘  ❌ 非实时       │
│                                                              ❌ 无 system prompt│
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                     Cursor（封闭客户端架构）                                   │
│                                                                             │
│  Agent ──→ [内部封闭调用] ──────────────────────→ LLM API                    │
│    │          （不可介入）                                                     │
│    │                                                                        │
│    ▼                                                                        │
│  IDE hooks  ─── 逐个事件上报 ──→ ┌─────────┐                                │
│  (thinking/                      │ Langfuse │  ⚠️ 片段化                     │
│   response/                      │  拼凑式   │  ❌ 无 system prompt           │
│   tool 分离)                     └─────────┘  ❌ 无 token 用量               │
│                                               ❌ 无结构化 output             │
│                                               ✅ 有 thinking 文本            │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 九、与 LLM API 代理层 Trace 的差距分析

### 9.1 目标 Trace 结构（来自 CodeBuddy + LiteLLM）

以 CodeBuddy + LiteLLM 在 Langfuse 中产生的 Trace 为参照（见截图），其 Trace 树结构如下：

```
chat.AGENT (207.71s)                              ← 顶层 Agent 会话
  ├── check_and_handle_limits.TASK (117ms)         ← 前置检查
  ├── batch_check_user_gray_async.TASK (15ms)
  │
  ├── litellm.completion.LLM (15.78s, 5.98k tok)  ← 🔑 关键：完整 LLM 调用
  │     Input:  完整 system prompt（含 MCP Server 描述）
  │     Output: [
  │       {"type": "text", "text": "我需要先了解 shadowfolk MCP..."},
  │       {"type": "tool_use", "name": "fetch_mcp_tools", "id": "tooluse_r7Ad...", "input": ""},
  │       {"type": "tool_use", "name": "", "input": "{\"serverName\": \"shadow-folk\"}"}
  │     ]
  │
  ├── fetch_mcp_tools.TOOL (311ms)                 ← 工具执行
  ├── get_server_mcp_tools_async.TASK (305ms)
  ├── publish_async.TASK
  │
  ├── litellm.completion.LLM (21.15s)              ← 第二轮 LLM 调用
  ├── use_mcp_tool.TOOL
  ├── publish_async.TASK
  │
  ├── litellm.completion.LLM (37.79s, 7.3k tok)   ← 第三轮
  ├── use_mcp_tool.TOOL (377ms)
  ├── ...
  └── litellm.completion.LLM (48.00k, 12.3s)      ← 最终轮
```

核心特征：**每一轮 LLM API 调用都是一个独立的 `litellm.completion.LLM` span**，包含：


| 字段                          | 内容                                                                  | 价值                                  |
| --------------------------- | ------------------------------------------------------------------- | ----------------------------------- |
| **Input (messages)**        | 完整的 messages 数组：system prompt + user message + 历史 assistant/tool 消息 | 能看到模型「看到了什么」——完整上下文窗口               |
| **Output (content blocks)** | `[{type:"text",...}, {type:"tool_use",...}]` 结构化数组                  | 能看到模型「决定做什么」——text 和 tool_use 的完整组合 |
| **Token 用量**                | input_tokens / output_tokens / total_tokens                         | 精确的 token 消耗                        |
| **Latency**                 | 该次 API 调用的耗时                                                        | 模型推理性能                              |
| **Model**                   | 实际调用的模型名                                                            | 路由验证                                |


### 9.2 Cursor Hooks 能提供什么

将 Cursor hooks 能采集到的信息逐项对比目标结构：

```
目标：litellm.completion.LLM span
  Input:  完整 messages 数组（system + user + history）
  Output: [{type:"text",...}, {type:"tool_use",...}] 结构化内容块

Cursor hooks 实际能拿到的：
  beforeSubmitPrompt  → input.prompt（仅用户本轮输入文本）
  afterAgentThought   → input.text（仅 thinking 纯文本）
  afterAgentResponse  → input.text（仅回复纯文本）
  beforeReadFile      → input.file_path（仅文件路径）
  beforeMCPExecution  → input.tool_name + input.tool_input（仅工具名+参数）
  afterMCPExecution   → input.result_json（仅工具返回值）
  afterShellExecution → input.command + input.output（仅命令+输出）
```

逐项差距：


| 目标信息                               | Cursor Hooks           | 差距                                                                                                         |
| ---------------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------- |
| **完整 system prompt**               | ❌ 完全不可见                | hooks 不接触 system prompt 的组装过程                                                                              |
| **历史 messages 数组**                 | ❌ 完全不可见                | 只能看到当前轮的片段，无法还原完整上下文窗口                                                                                     |
| **结构化 output（text + tool_use 数组）** | ❌ 被拆散为独立事件             | `afterAgentThought` 只有 thinking 文本，`afterAgentResponse` 只有 response 文本，tool_use 被分散到各 before/after 工具 hook |
| **Token 用量**                       | ❌ 所有 usage 字段为 0       | hooks 不经过 API 调用层，无法获取 token 计数                                                                            |
| **单次 LLM 调用延迟**                    | ⚠️ 可近似估算               | 从 `afterAgentThought.duration_ms` 可以拿到 thinking 耗时，但这不等于完整的 API 调用延迟                                       |
| **模型名**                            | ✅ 可从 `input.model` 获取  | 每个 hook 事件都携带 model 字段                                                                                     |
| **工具名 + 参数**                       | ✅ 可从 before 工具 hook 获取 | `beforeMCPExecution` 有 `tool_name` 和 `tool_input`                                                          |
| **工具返回值**                          | ✅ 可从 after 工具 hook 获取  | `afterMCPExecution` 有 `result_json`                                                                        |


### 9.3 Claude Code Hooks 能提供什么


| 目标信息                 | Claude Code Hooks                            | 差距                                                               |
| -------------------- | -------------------------------------------- | ---------------------------------------------------------------- |
| **完整 system prompt** | ❌ 不可见                                        | `InstructionsLoaded` 事件能看到加载了哪些指令文件，但不是最终拼装的 system prompt       |
| **历史 messages 数组**   | ❌ 不可见                                        | hooks 不接触上下文窗口的组装                                                |
| **结构化 output**       | ❌ 不暴露                                        | 不存在 `afterAgentThought` / `afterAgentResponse`，模型输出对 hooks 完全不可见 |
| **Token 用量**         | ❌ 不可见                                        | hooks 不经过 API 调用层                                                |
| **工具名 + 参数**         | ✅ `PreToolUse` 提供 `tool_name` + `tool_input` | 与目标结构中的 `tool_use` 块对应                                           |
| **工具返回值**            | ✅ `PostToolUse` 提供 `tool_response`           | 完整的工具执行结果                                                        |
| **子代理生命周期**          | ✅ `SubagentStart` / `SubagentStop`           | 优于 Cursor                                                        |


### 9.4 根因：三层可观测性架构

```
┌─────────────────────────────────────────────┐
│ Layer 3: IDE 事件层（Cursor / Claude Code Hooks）  │
│   能看到：用户输入、工具调用、文件读写、thinking    │
│   看不到：system prompt、完整 messages、token 用量  │
│   特点：事件粒度，每个动作一个 hook                 │
└─────────────────────┬───────────────────────┘
                      │
┌─────────────────────▼───────────────────────┐
│ Layer 2: LLM API 代理层（LiteLLM / Gateway）       │
│   能看到：完整 request/response、token 用量、成本    │
│   看不到：IDE 上下文（工作区、文件系统、用户行为）   │
│   特点：请求粒度，每次 API 调用一个 span             │
└─────────────────────┬───────────────────────┘
                      │
┌─────────────────────▼───────────────────────┐
│ Layer 1: LLM 推理层（模型内部）                     │
│   能看到：attention 权重、KV cache、token 概率分布   │
│   完全不可观测（黑盒）                              │
└─────────────────────────────────────────────┘
```

**截图中的 Trace 来自 Layer 2**（LiteLLM 代理层），而 **Cursor / Claude Code hooks 只能工作在 Layer 3**（IDE 事件层）。两者不在同一层级，无法互相替代。

### 9.5 能否通过 Hooks 重建目标结构

理论上可以**近似重建**，但有不可逾越的信息缺失：

#### 可重建的部分

通过在 hooks handler 中缓存同一 `generation_id` 的所有事件，可以拼装出近似的单轮结构：

```javascript
// 伪代码：在 handler 层聚合同一 generation_id 的事件
const turnBuffer = {};

function aggregateToTurn(input) {
  const gid = input.generation_id;
  if (!turnBuffer[gid]) turnBuffer[gid] = { text: null, thinking: null, tool_calls: [] };

  switch (input.hook_event_name) {
    case 'after_thought':
      turnBuffer[gid].thinking = input.text;
      break;
    case 'after_response':
      turnBuffer[gid].text = input.text;
      break;
    case 'before_shell':
    case 'before_file_read':
    case 'before_mcp':
      turnBuffer[gid].tool_calls.push({
        type: 'tool_use',
        name: input.tool_name || input.command || input.file_path,
        input: input.tool_input || input.command,
      });
      break;
  }
}
```

重建后的近似输出：

```json
{
  "thinking": "我需要先了解 shadowfolk MCP 提供的工具...",
  "text": "...",
  "tool_calls": [
    {"type": "tool_use", "name": "fetch_mcp_tools", "input": ""},
    {"type": "tool_use", "name": "use_mcp_tool", "input": {"serverName": "shadow-folk"}}
  ]
}
```

#### 不可重建的部分


| 缺失信息               | 原因                      | 影响                                  |
| ------------------ | ----------------------- | ----------------------------------- |
| **System prompt**  | IDE 内部组装，hooks 无法接触     | 无法分析 system prompt 膨胀、MCP 工具描述注入等问题 |
| **完整 messages 历史** | 上下文窗口管理在 IDE 内部         | 无法判断上下文长度、是否触发了压缩                   |
| **Token 用量**       | API 计费信息不经过 hooks       | 无法做成本分析和 token 预算控制                 |
| **tool_use 的 ID**  | hooks 不传递 `tool_use_id` | 无法将 tool_use 请求与 tool_result 精确配对   |
| **精确的 LLM 调用边界**   | 一次 LLM 调用被拆散为多个异步 hook  | 无法精确区分「第几轮 LLM 调用」                  |


### 9.6 达到目标结构的可行路径


| 路径                    | 方式                                                         | 可行性                            | 适用场景                         |
| --------------------- | ---------------------------------------------------------- | ------------------------------ | ---------------------------- |
| **A. LLM API 代理**     | 在 IDE 和 LLM API 之间插入 LiteLLM/自建 proxy，proxy 内置 Langfuse 埋点 | ✅ CodeBuddy 已实现（截图所示）          | 自研 IDE / 可控 API 路由           |
| **B. SDK 层集成**        | 在 Agent 框架中直接使用 Langfuse SDK 包裹每次 `model.generate()` 调用    | ✅ 代码侵入性强但信息最全                  | 自研 Agent 框架                  |
| **C. Hooks + 聚合**     | 利用现有 hooks 在 handler 层做事件聚合，拼装近似结构                         | ⚠️ 可近似，缺 system prompt 和 token | Cursor / Claude Code 用户的最佳妥协 |
| **D. Transcript 后处理** | 从 `agent-transcripts/*.jsonl` 读取完整对话日志，离线分析                | ⚠️ 非实时，但信息较全                   | 离线分析、审计                      |


**结论**：截图中的 `litellm.completion.LLM` 结构**无法**通过 Cursor 或 Claude Code 的 hooks 原生实现。hooks 只能工作在 IDE 事件层（Layer 3），而该结构需要 LLM API 代理层（Layer 2）的介入。最佳实践是两层同时部署——hooks 负责 IDE 行为追踪，API 代理负责 LLM 调用追踪，在 Langfuse 中通过 `session_id` 关联。

---

## 十、已知问题与改进方向

### 10.1 子代理 Trace 缺失首尾

**现状**：子代理的 `beforeSubmitPrompt`、`afterAgentResponse`、`stop` 不触发 hook。
**影响**：子代理 Trace 的 Input/Output 为空，无完成状态评分。
**改进思路**：在 `stop` handler 中检测是否有 `generation_id` 关联的子代理 Trace，主动补充 metadata；或在 handler 层缓存父 Task 调用的 prompt，在子代理的首个 hook 事件中回填。

### 10.2 单次 LLM 调用不可见

**现状**：无法获取底层 API 调用的 token 用量和成本。
**影响**：所有 observation 的 `inputUsage`/`outputUsage`/`totalCost` 均为 0。
**根因**：Cursor hooks 是**事件级**（IDE 行为层），不是**API 代理级**（LLM 请求层）。

### 10.3 父子 Trace 关联

**现状**：父子 Trace 在 Langfuse 中是独立的两条记录，无 `parentObservationId` 关联。
**改进思路**：在子代理首个 hook 事件中注入父 `conversation_id` 到 metadata，或利用 Langfuse 的 session 机制在同一 session 下展示关联 trace。

### 10.4 跨天会话追踪

**现状**：Langfuse Trace 时间戳取决于首次 hook 事件时间。长时间运行的会话（跨天）在按日期筛选时容易被遗漏。
**改进思路**：在每个 hook 事件中更新 trace 的 `updatedAt`，或在 Langfuse 中按 session 而非时间筛选。