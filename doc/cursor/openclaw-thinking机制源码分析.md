# OpenClaw Thinking 机制源码分析

> 基于 openclaw 源码（2026-04-11）的静态分析，解读 extended thinking 的完整开启条件链、参数构造逻辑，以及 `latest_llm_call.json` 中 thinking 缺失的根因定位。

---

## 一、Thinking 开启的双层门控

Thinking 能否生效，取决于 **两个独立条件同时满足**。代码入口位于 `src/agents/anthropic-transport-stream.ts`：

```typescript
// 构造 Anthropic API 请求参数时的核心判断
if (model.reasoning) {                    // ← 第一层：模型级能力标记
    if (options?.thinkingEnabled) {        // ← 第二层：运行时开关
        if (supportsAdaptiveThinking(model.id)) {
            params.thinking = { type: "adaptive" };
        } else {
            params.thinking = { type: "enabled", budget_tokens: N };
        }
    } else if (options?.thinkingEnabled === false) {
        params.thinking = { type: "disabled" };
    }
}
```

### 1.1 第一层：`model.reasoning` — 模型目录能力标记

模型能力来自 **Model Catalog**，定义在 `src/agents/model-catalog.types.ts`：

```typescript
type ModelCatalogEntry = {
    provider: string;
    id: string;
    reasoning?: boolean;   // ← 标记该模型是否支持推理/thinking
    // ...
};
```

只有 `reasoning === true` 的模型才能进入 thinking 参数构造逻辑。如果为 `false` 或 `undefined`，即使 `thinkingEnabled = true` 也不会发送 thinking 参数。

### 1.2 第二层：`options.thinkingEnabled` — 运行时开关

该值在 `resolveAnthropicTransportStreamOptions` 中确定：

```typescript
// 简化后的关键逻辑
if (!options?.reasoning) {          // reasoning 来自 ThinkLevel
    resolved.thinkingEnabled = false;
    return resolved;
}
// 否则 thinkingEnabled = true
resolved.thinkingEnabled = true;
```

`options.reasoning` 的值来源于 **ThinkLevel**，即用户或系统配置的思维等级。只要 ThinkLevel 不是 `"off"`，`thinkingEnabled` 就为 `true`。

---

## 二、ThinkLevel 类型定义与可选值

定义在 `src/auto-reply/thinking.shared.ts`：

```typescript
type ThinkLevel = "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "adaptive";
```

| 等级 | 含义 |
|---|---|
| `off` | 关闭 thinking |
| `minimal` | 最小思维（budget 1024 tokens） |
| `low` | 低（budget 2048 tokens） |
| `medium` | 中（budget 8192 tokens） |
| `high` | 高（budget 16384 tokens） |
| `xhigh` | 超高（仅 opus-4-6 可用 `effort: "max"`） |
| `adaptive` | 自适应（仅 Claude 4.6 系列，由模型自行决定是否 think） |

---

## 三、ThinkLevel 的确定：四层优先级

运行时 ThinkLevel 的最终值，按以下优先级从高到低确定（`src/auto-reply/status.ts`）：

```typescript
const thinkLevel =
    args.resolvedThink               // ① 消息级 /think 指令
    ?? args.sessionEntry?.thinkingLevel   // ② Session 级设置
    ?? args.agent?.thinkingDefault        // ③ Agent 级默认
    ?? "off";                             // ④ 兜底值
```

### 3.1 消息级 `/think` 指令

用户可在消息中输入 `/think` 或 `/think:high` 等指令，解析逻辑在 `src/auto-reply/reply/directive-handling.parse.ts`：

```
/think          → thinkLevel = undefined（使用默认）
/think:high     → thinkLevel = "high"
/think:adaptive → thinkLevel = "adaptive"
/think:off      → thinkLevel = "off"
```

### 3.2 Session 级设置

通过 Gateway API 的 `sessions.patch` 接口修改：

```typescript
await this.gateway.request("sessions.patch", {
    key: session.sessionKey,
    thinkingLevel: params.modeId,   // 如 "adaptive"
});
```

### 3.3 Agent 级默认

配置文件中 `agents.list[].thinkingDefault` 或 `agents.defaults.thinkingDefault`（`src/config/types.agents.ts`）：

```typescript
thinkingDefault?: "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "adaptive";
```

### 3.4 模型自动推断（兜底逻辑）

当以上三层均未设置时，`resolveThinkingDefault()`（`src/agents/model-selection.ts`）提供兜底：

```typescript
export function resolveThinkingDefault(params) {
    // 1. 检查配置中针对该模型的 per-model thinking 设置
    const perModelThinking = configuredModels?.[canonicalKey]?.params?.thinking;
    if (perModelThinking) return perModelThinking;

    // 2. 检查全局 thinkingDefault 配置
    const configured = params.cfg.agents?.defaults?.thinkingDefault;
    if (configured) return configured;

    // 3. 特殊逻辑：Anthropic Claude 4.6 系列自动 adaptive
    if (
        normalizedProvider === "anthropic" &&       // ← 关键：provider 必须是 "anthropic"
        explicitModelConfigured &&
        /4\.6\b/.test(catalogCandidate.name) &&
        (normalizedModel.startsWith("claude-opus-4-6") ||
         normalizedModel.startsWith("claude-sonnet-4-6"))
    ) {
        return "adaptive";
    }

    // 4. 通用兜底：catalog 中 reasoning: true → "low"，否则 → "off"
    return resolveThinkingDefaultForModel({ provider, model, catalog });
}
```

`resolveThinkingDefaultForModel` 的逻辑（`src/auto-reply/thinking.shared.ts`）：

```typescript
export function resolveThinkingDefaultForModel(params) {
    const candidate = params.catalog?.find(/* match provider + model */);
    if (candidate?.reasoning) return "low";
    return "off";
}
```

---

## 四、Thinking 参数构造——两条路径

当 thinking 确认开启后（`thinkingEnabled = true`），根据模型是否支持 adaptive thinking 选择不同的 API 参数构造路径。

### 4.1 判断是否支持 Adaptive Thinking

```typescript
function supportsAdaptiveThinking(modelId: string): boolean {
    return (
        modelId.includes("opus-4-6") || modelId.includes("opus-4.6") ||
        modelId.includes("sonnet-4-6") || modelId.includes("sonnet-4.6")
    );
}
```

只有 **Claude 4.6 系列**（Opus 4.6 / Sonnet 4.6）支持 adaptive thinking。

### 4.2 路径 A：Adaptive Thinking（Claude 4.6）

```typescript
params.thinking = { type: "adaptive" };
if (options.effort) {
    params.output_config = { effort: options.effort };
}
```

ThinkLevel → effort 映射（`mapThinkingLevelToEffort`）：

| ThinkLevel | effort 值 | 说明 |
|---|---|---|
| `minimal` / `low` | `"low"` | 轻量思维 |
| `medium` | `"medium"` | 中等思维 |
| `high` / `adaptive` | `"high"` | 深度思维 |
| `xhigh`（opus-4-6） | `"max"` | 最大思维深度 |
| `xhigh`（sonnet-4-6） | `"high"` | sonnet 不支持 max |

### 4.3 路径 B：Budget Thinking（非 4.6 模型）

```typescript
params.thinking = { type: "enabled", budget_tokens: N };
```

各等级对应的 budget_tokens（`adjustMaxTokensForThinking`）：

| ThinkLevel | budget_tokens |
|---|---|
| `minimal` | 1024 |
| `low` | 2048 |
| `medium` | 8192 |
| `high` / `xhigh` | 16384 |

---

## 五、`latest_llm_call.json` Thinking 缺失的根因分析

### 5.1 现象

`latest_llm_call.json` 中的关键数据：

| 字段 | 值 |
|---|---|
| `model` | `claude-opus-4-6` |
| `reasoning_tokens` | `0` |
| Response 中 thinking 内容 | 无 |
| System Prompt 末尾 Runtime 元数据 | `thinking=off` |

### 5.2 根因：Thinking 被显式设为 off，并非模型自主判断

System Prompt 的 Runtime 行明确记录了当前会话配置：

```
Runtime: agent=main | ... | model=venus/claude-opus-4-6 | ... | thinking=off
Reasoning: off (hidden unless on/stream). Toggle /reasoning; /status shows Reasoning when enabled.
```

**这证明 thinking 在会话层面就已经被设为 off**，请求根本不会发送 `thinking` 参数给 Anthropic API，模型没有机会进行扩展思维。

### 5.3 为什么 ThinkLevel 是 off？——Provider 不匹配问题

从 Runtime 元数据看：`model=venus/claude-opus-4-6`，provider 是 **`venus`** 而不是 **`anthropic`**。

回顾 `resolveThinkingDefault` 的兜底逻辑：

```typescript
if (
    normalizedProvider === "anthropic" &&    // ← venus ≠ anthropic，条件不满足！
    explicitModelConfigured &&
    /4\.6\b/.test(catalogCandidate.name) &&
    ...
) {
    return "adaptive";   // ← 这行永远不会执行
}
```

**因为 provider 是 `venus`（内部代理网关），不是 `anthropic`，所以 Claude 4.6 系列专属的 `adaptive` 自动默认逻辑不会触发。**

最终走到通用兜底 `resolveThinkingDefaultForModel`：
- 如果 venus provider 的 model catalog 中没有为 `claude-opus-4-6` 标记 `reasoning: true` → 返回 `"off"`
- 即使标记了 `reasoning: true` → 也只返回 `"low"`，不是 `"adaptive"`

### 5.4 结论

| 排查项 | 结果 |
|---|---|
| 模型本身是否支持 thinking？ | 是。`claude-opus-4-6` 在代码中被识别为支持 adaptive thinking |
| 是否是模型自己判断不需要 thinking？ | **否。** thinking 在请求层面就被禁用了（`thinking=off`），模型根本没有机会判断 |
| 真正原因是什么？ | **provider 为 `venus` 而非 `anthropic`，导致 Claude 4.6 的 adaptive 默认逻辑未触发**；同时用户未通过 `/think` 指令、Session 设置或配置文件显式开启 thinking |

### 5.5 流程图

```
resolveThinkingDefault()
    │
    ├─ per-model config thinking? → 未配置
    ├─ agents.defaults.thinkingDefault? → 未配置
    ├─ provider === "anthropic" && claude-4.6? → provider 是 "venus"，条件不满足
    └─ catalog 中 reasoning: true?
         ├─ true → "low"（不是 adaptive）
         └─ false/undefined → "off" ← 最终结果
```

---

## 六、如何开启 Thinking

根据源码分析，有以下方式可以为 `venus/claude-opus-4-6` 开启 thinking：

### 方式一：消息级指令（临时生效）

在消息中添加 `/think` 或 `/think:high`：

```
/think:adaptive 帮我分析一下这个问题
```

### 方式二：Session 级切换（当前会话生效）

通过 openclaw 的 session 控制切换 thinking 模式。

### 方式三：Agent 配置文件（持久生效）

在 openclaw 配置中设置默认 thinking 级别：

```json
{
    "agents": {
        "defaults": {
            "thinkingDefault": "adaptive"
        }
    }
}
```

或针对特定模型配置：

```json
{
    "agents": {
        "defaults": {
            "models": {
                "venus/claude-opus-4-6": {
                    "params": {
                        "thinking": "adaptive"
                    }
                }
            }
        }
    }
}
```

### 方式四：Provider 插件注册（根本解决）

通过 `provider-thinking.ts` 的插件注册机制，为 venus provider 注册 thinking 策略，使其被视为与 anthropic 等价：

```typescript
const venusPlugin: ThinkingProviderPlugin = {
    id: "venus",
    resolveDefaultThinkingLevel: (ctx) => {
        if (ctx.modelId.includes("opus-4-6") || ctx.modelId.includes("sonnet-4-6")) {
            return "adaptive";
        }
        return ctx.reasoning ? "low" : "off";
    },
    supportsXHighThinking: (ctx) => ctx.modelId.includes("opus-4-6"),
};
```

---

## 七、关键源码文件索引

| 文件路径 | 职责 |
|---|---|
| `src/auto-reply/thinking.shared.ts` | ThinkLevel 类型定义、`normalizeThinkLevel`、`resolveThinkingDefaultForModel` |
| `src/auto-reply/thinking.ts` | 带插件系统的 thinking 解析入口 |
| `src/agents/anthropic-transport-stream.ts` | Anthropic API 参数构造、`supportsAdaptiveThinking`、`mapThinkingLevelToEffort`、`adjustMaxTokensForThinking` |
| `src/agents/model-selection.ts` | `resolveThinkingDefault`、`resolveReasoningDefault` |
| `src/plugins/provider-thinking.ts` | Provider 级 thinking 策略插件注册表 |
| `src/auto-reply/status.ts` | ThinkLevel 优先级合并 |
| `src/auto-reply/reply/directive-handling.parse.ts` | `/think` 指令解析 |
| `src/config/types.agent-defaults.ts` | `thinkingDefault` 配置字段定义 |
| `src/agents/model-catalog.types.ts` | `ModelCatalogEntry.reasoning` 字段定义 |
