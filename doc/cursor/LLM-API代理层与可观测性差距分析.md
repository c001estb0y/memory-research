# LLM API 代理层：概念、LiteLLM 与可观测性原理

基于 Cursor Hooks / Claude Code Hooks / CodeBuddy + LiteLLM 三种架构的对比分析，解析 IDE 事件层与 LLM API 代理层的可观测性差距及弥合路径。

---

## 一、什么是 LLM API 代理层

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

## 二、为什么需要代理层

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

## 三、LiteLLM 是什么

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

## 四、LiteLLM + Langfuse 集成

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

## 五、主流 LLM Gateway 对比


| 产品                        | 类型      | 模型数      | 可观测性                  | 成本         | 自部署    |
| ------------------------- | ------- | -------- | --------------------- | ---------- | ------ |
| **LiteLLM**               | 开源自部署   | 100+     | 需外接 Langfuse/Helicone | 免费（MIT）    | ✅      |
| **Portkey**               | 托管 SaaS | 250+     | 内置                    | 按日志量计费     | ❌（仅边缘） |
| **OpenRouter**            | SaaS 市场 | 200-300+ | 基础统计                  | 5.5% 信用手续费 | ❌      |
| **Helicone**              | 开源/托管   | 多家       | 内置（核心卖点）              | 免费层 + 付费   | ✅      |
| **Cloudflare AI Gateway** | 托管      | 多家       | 内置                    | 免费层        | ❌      |


## 六、代理层的真正意义：不只是多模型路由

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

## 七、Claude Code 真的只支持 Anthropic 吗？

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

## 八、那 Claude Code 为什么不内置代理层？

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

## 九、截图中的架构还原

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

## 十、Agent ↔ LLM 交互全流程对比图

### 流程图 A：经 LiteLLM 代理层的 Agent（CodeBuddy 架构）

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

### 流程图 B：Claude Code 直连 Anthropic API（无代理层）

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

### 流程图 C：关键差异一图总览

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

## 十一、与 LLM API 代理层 Trace 的差距分析

### 11.1 目标 Trace 结构（来自 CodeBuddy + LiteLLM）

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


### 11.2 Cursor Hooks 能提供什么

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


### 11.3 Claude Code Hooks 能提供什么


| 目标信息                 | Claude Code Hooks                            | 差距                                                               |
| -------------------- | -------------------------------------------- | ---------------------------------------------------------------- |
| **完整 system prompt** | ❌ 不可见                                        | `InstructionsLoaded` 事件能看到加载了哪些指令文件，但不是最终拼装的 system prompt       |
| **历史 messages 数组**   | ❌ 不可见                                        | hooks 不接触上下文窗口的组装                                                |
| **结构化 output**       | ❌ 不暴露                                        | 不存在 `afterAgentThought` / `afterAgentResponse`，模型输出对 hooks 完全不可见 |
| **Token 用量**         | ❌ 不可见                                        | hooks 不经过 API 调用层                                                |
| **工具名 + 参数**         | ✅ `PreToolUse` 提供 `tool_name` + `tool_input` | 与目标结构中的 `tool_use` 块对应                                           |
| **工具返回值**            | ✅ `PostToolUse` 提供 `tool_response`           | 完整的工具执行结果                                                        |
| **子代理生命周期**          | ✅ `SubagentStart` / `SubagentStop`           | 优于 Cursor                                                        |


### 11.4 根因：三层可观测性架构

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

### 11.5 能否通过 Hooks 重建目标结构

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


### 11.6 达到目标结构的可行路径


| 路径                    | 方式                                                         | 可行性                            | 适用场景                         |
| --------------------- | ---------------------------------------------------------- | ------------------------------ | ---------------------------- |
| **A. LLM API 代理**     | 在 IDE 和 LLM API 之间插入 LiteLLM/自建 proxy，proxy 内置 Langfuse 埋点 | ✅ CodeBuddy 已实现（截图所示）          | 自研 IDE / 可控 API 路由           |
| **B. SDK 层集成**        | 在 Agent 框架中直接使用 Langfuse SDK 包裹每次 `model.generate()` 调用    | ✅ 代码侵入性强但信息最全                  | 自研 Agent 框架                  |
| **C. Hooks + 聚合**     | 利用现有 hooks 在 handler 层做事件聚合，拼装近似结构                         | ⚠️ 可近似，缺 system prompt 和 token | Cursor / Claude Code 用户的最佳妥协 |
| **D. Transcript 后处理** | 从 `agent-transcripts/*.jsonl` 读取完整对话日志，离线分析                | ⚠️ 非实时，但信息较全                   | 离线分析、审计                      |


**结论**：截图中的 `litellm.completion.LLM` 结构**无法**通过 Cursor 或 Claude Code 的 hooks 原生实现。hooks 只能工作在 IDE 事件层（Layer 3），而该结构需要 LLM API 代理层（Layer 2）的介入。最佳实践是两层同时部署——hooks 负责 IDE 行为追踪，API 代理负责 LLM 调用追踪，在 Langfuse 中通过 `session_id` 关联。

---

## 十二、实战验证：为 Claude Code 配置 LiteLLM 代理层

### 12.1 源码证据：Claude Code 原生支持外部代理

通过分析 Claude Code 源码（`ClaudeCode/src/`），确认其**原生支持**通过 `ANTHROPIC_BASE_URL` 对接 LiteLLM 等外部代理层。关键证据如下：

#### 证据 1：`ANTHROPIC_BASE_URL` 环境变量

`utils/model/providers.ts` 中的 `isFirstPartyAnthropicBaseUrl()` 函数明确区分直连与代理访问：

```typescript
// utils/model/providers.ts
export function isFirstPartyAnthropicBaseUrl(): boolean {
  const baseUrl = process.env.ANTHROPIC_BASE_URL
  if (!baseUrl) return true  // 未设置 → 直连 Anthropic
  const host = new URL(baseUrl).host
  return ['api.anthropic.com'].includes(host)  // 非 Anthropic → 代理模式
}
```

#### 证据 2：主动识别 LiteLLM 等已知 Gateway

`services/api/logging.ts` 中定义了已知 Gateway 的指纹识别，**LiteLLM 排在首位**：

```typescript
// services/api/logging.ts
type KnownGateway =
  | 'litellm'      // ← 第一位
  | 'helicone'
  | 'portkey'
  | 'cloudflare-ai-gateway'
  | 'databricks'
  // ...

const GATEWAY_FINGERPRINTS = {
  litellm: { prefixes: ['x-litellm-'] },  // 通过响应头 x-litellm-* 识别
  helicone: { prefixes: ['helicone-'] },
  portkey: { prefixes: ['x-portkey-'] },
  // ...
}
```

Claude Code 在每次 API 调用后自动检测响应头，识别出经过了哪个 Gateway，并记录到分析数据中。

#### 证据 3：专为代理层设计的兼容模式

`utils/api.ts` 中有一个关键的兼容性开关，注释中**直接提到了 LiteLLM**：

```typescript
// utils/api.ts
// CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS is the kill switch for beta API
// shapes. Proxy gateways (ANTHROPIC_BASE_URL → LiteLLM → Bedrock) reject
// fields like defer_loading with "Extra inputs are not permitted".
if (isEnvTruthy(process.env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS)) {
  // 剥离所有实验性字段，只保留 name / description / input_schema / cache_control
}
```

同样，`eager_input_streaming`（细粒度工具流式传输）也被限制为仅在直连 Anthropic 时启用：

```typescript
// Gated to direct api.anthropic.com: proxies (LiteLLM etc.) and Bedrock/Vertex
// with Claude 4.5 reject this field with 400.
if (getAPIProvider() === 'firstParty' && isFirstPartyAnthropicBaseUrl()) {
  base.eager_input_streaming = true
}
```

### 12.2 配置方法

```bash
# 基础配置：将 Claude Code 的 API 请求路由到 LiteLLM Proxy
export ANTHROPIC_BASE_URL="http://your-litellm-proxy:4000"
export ANTHROPIC_API_KEY="sk-..."

# 推荐：开启兼容模式，避免 LiteLLM 不认识实验性 API 字段而返回 400
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
```

配置后的数据流：

```
Claude Code CLI
  │
  │  anthropic.beta.messages.create({ ...params, stream: true })
  │  ↓ 标准 Anthropic Messages API 格式（HTTP POST）
  │
LiteLLM Proxy
  │  ├── Langfuse callback：记录完整 request/response
  │  ├── Token 计数 & 成本估算
  │  └── 转发请求
  │  ↓
Anthropic API
  │  ↓ SSE streaming response
  │
LiteLLM Proxy
  │  ├── 重组完整响应后触发 callback → Langfuse
  │  └── 透传 SSE 流回 Claude Code
  │  ↓
Claude Code CLI（正常处理响应）
```

### 12.3 代理层能采集到的完整数据

每次 LLM API 调用，LiteLLM 的 Langfuse callback 能采集到以下结构化 JSON：

```json
{
  "input": {
    "system": "完整的 system prompt（含工具描述、MCP 配置、项目上下文等）",
    "messages": [
      {"role": "user", "content": "用户问题"},
      {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "完整的思维链文本"},
        {"type": "text", "text": "回复内容"},
        {"type": "tool_use", "name": "Read", "id": "tooluse_xxx",
         "input": {"file_path": "/src/main.ts"}}
      ]},
      {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tooluse_xxx",
         "content": "文件内容..."}
      ]}
    ],
    "tools": ["全部工具定义（Read、Write、Grep、Shell、MCP 等）"]
  },
  "output": {
    "content": [
      {"type": "thinking", "thinking": "我需要先读取这个文件来理解..."},
      {"type": "text", "text": "让我来帮你分析..."},
      {"type": "tool_use", "name": "Grep", "id": "tooluse_yyy",
       "input": {"pattern": "async.*function", "path": "/src/"}}
    ],
    "stop_reason": "tool_use",
    "usage": {
      "input_tokens": 8234,
      "output_tokens": 567,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 7800
    }
  }
}
```

逐项对比 Hooks（Layer 3）与代理层（Layer 2）的采集能力：


| 信息                           | Hooks（Layer 3）         | LiteLLM 代理（Layer 2）                |
| ---------------------------- | ---------------------- | ---------------------------------- |
| **完整 system prompt**         | ❌ 不可见                  | ✅ 请求体中的 `system` 字段                |
| **历史 messages 数组**           | ❌ 不可见                  | ✅ 请求体中的 `messages` 数组              |
| **tools 定义**                 | ❌ 不可见                  | ✅ 请求体中的 `tools` 数组                 |
| **thinking 思维链**             | ⚠️ 纯文本片段（仅 Cursor）     | ✅ 结构化 JSON，与 text/tool_use 在同一响应中  |
| **text 回复**                  | ⚠️ 纯文本片段               | ✅ 结构化 JSON                         |
| **tool_use 调用**              | ⚠️ 分散在各 before hook 中  | ✅ 结构化 JSON，含 tool_use_id           |
| **tool_result 返回**           | ⚠️ 分散在各 after hook 中   | ✅ 下一轮请求的 messages 中完整包含            |
| **token 用量**                 | ❌ 全部为 0                | ✅ 精确的 input/output/cache tokens    |
| **单次 API 调用边界**              | ❌ 无法区分第几轮              | ✅ 每次调用独立一个 span                    |
| **cost 成本**                  | ❌ 不可见                  | ✅ LiteLLM 自动估算                     |
| **latency 延迟**               | ⚠️ 仅 thinking duration | ✅ 完整 API 调用耗时                      |
| **模型名**                      | ✅                      | ✅                                  |
| **stop_reason**              | ❌ 不可见                  | ✅ end_turn / tool_use / max_tokens |
| **cache 命中（prompt caching）** | ❌ 不可见                  | ✅ cache_read_input_tokens          |


### 12.4 Thinking 可见性的意外发现

源码分析揭示了一个重要细节：**通过代理层访问时，thinking 内容反而不会被 redact**。

Claude Code 的 thinking redaction 逻辑（`utils/betas.ts`）：

```typescript
// 只有满足所有条件时才 redact thinking：
if (
  includeFirstPartyOnlyBetas &&   // ← 必须是直连 Anthropic
  modelSupportsISP(model) &&
  !getIsNonInteractiveSession() &&
  getInitialSettings().showThinkingSummaries !== true
) {
  betaHeaders.push(REDACT_THINKING_BETA_HEADER)
}
```

而 `includeFirstPartyOnlyBetas` 的值取决于：

```typescript
export function shouldIncludeFirstPartyOnlyBetas(): boolean {
  return (
    (getAPIProvider() === 'firstParty' || getAPIProvider() === 'foundry') &&
    !isEnvTruthy(process.env.CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS)
  )
}
```

**推导链**：

```
ANTHROPIC_BASE_URL = "http://your-litellm:4000"（非 api.anthropic.com）
  → isFirstPartyAnthropicBaseUrl() = false
  → 但 getAPIProvider() 仍然返回 'firstParty'（因为没设 USE_BEDROCK/VERTEX）
  → shouldIncludeFirstPartyOnlyBetas() 看的是 provider，不看 base URL
  → 如果未设 CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS → includeFirstPartyOnlyBetas = true
  → REDACT_THINKING_BETA_HEADER 会被加上 → thinking 被 redact
  
但如果设了 CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1（推荐配置）：
  → shouldIncludeFirstPartyOnlyBetas() = false
  → includeFirstPartyOnlyBetas = false
  → REDACT_THINKING_BETA_HEADER 不会被加上
  → Anthropic API 返回完整的 thinking 文本 ✅
```

**结论**：当使用推荐配置 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 时，不仅避免了代理层的 400 错误，还附带获得了**完整的 thinking 内容**（而非直连模式下的 redacted 版本）。


| 场景                                            | thinking 是否被 redact | 代理层能否看到完整 thinking |
| --------------------------------------------- | ------------------- | ------------------ |
| 直连 api.anthropic.com（默认交互模式）                  | ✅ 被 redact          | N/A                |
| 代理层 + **未设** DISABLE_EXPERIMENTAL_BETAS       | ✅ 被 redact          | ❌ 只有加密的 data 字段    |
| 代理层 + **已设** DISABLE_EXPERIMENTAL_BETAS=1（推荐） | ❌ 不 redact          | ✅ 完整思维链文本          |
| 非交互模式（SDK / --print）                          | ❌ 不 redact          | ✅ 完整思维链文本          |


### 12.5 Langfuse 中的最终 Trace 结构

配置 LiteLLM 代理后，Langfuse 中 Claude Code 的每个会话将呈现类似 CodeBuddy 的完整 Trace 结构：

```
Claude Code Session (总耗时)
  │
  ├── litellm.completion.LLM (12.3s, 8.2k tokens)    ← 第 1 轮
  │     Input:  system prompt + user message + tools
  │     Output: [thinking + text + tool_use(Read)]
  │     Usage:  input=8234, output=567, cache_read=7800
  │
  ├── litellm.completion.LLM (8.7s, 6.1k tokens)     ← 第 2 轮
  │     Input:  messages + tool_result(Read 结果)
  │     Output: [thinking + text + tool_use(Grep)]
  │     Usage:  input=6100, output=423, cache_read=5900
  │
  ├── litellm.completion.LLM (15.2s, 9.8k tokens)    ← 第 3 轮
  │     Input:  messages + tool_result(Grep 结果)
  │     Output: [thinking + text + tool_use(Write)]
  │     Usage:  input=9800, output=1203, cache_read=8500
  │
  └── litellm.completion.LLM (5.1s, 4.2k tokens)     ← 最终轮
        Input:  messages + tool_result(Write 结果)
        Output: [thinking + text]
        stop_reason: end_turn
        Usage:  input=4200, output=856, cache_read=3800
```

这与第十一章中"无法实现的目标 Trace 结构"完全一致——**通过配置外部代理层，Claude Code 用户可以获得与 CodeBuddy + LiteLLM 同等的 Layer 2 可观测性**。

### 12.6 注意事项与最佳实践

1. **必须设置兼容模式**：`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`，否则 `defer_loading`、`eager_input_streaming`、`strict` 等实验性字段会导致 LiteLLM 返回 `400 Extra inputs are not permitted`。
2. **两层观测互补**：Hooks（Layer 3）和代理层（Layer 2）**不是替代关系**，可以同时部署：
  - Layer 2（LiteLLM）：完整的 LLM 调用追踪——messages、thinking、token、cost
  - Layer 3（Hooks）：IDE 行为追踪——文件读写路径、Shell 命令执行、MCP 调用参数
  - 通过 `session_id` 在 Langfuse 中关联两层数据
3. **Streaming 兼容**：Claude Code 使用 `stream: true`，LiteLLM 的 `success_callback` 会在流结束后重组完整响应再上报 Langfuse，无需额外处理。
4. **性能影响**：增加一跳网络延迟。建议将 LiteLLM Proxy 部署在与 Claude Code 同一网络区域，或直接在本地运行（`litellm --config config.yaml --port 4000`）。

