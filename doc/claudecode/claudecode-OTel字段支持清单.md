# Claude Code OTel 字段支持清单

本文整理官方 `@anthropic-ai/claude-code` 的 OpenTelemetry（OTel）能力边界：哪些字段会被稳定输出，哪些字段只在特定开关下输出，以及哪些字段不支持或默认不会输出。

依据：

- 官方 Monitoring 文档：`https://code.claude.com/docs/en/monitoring-usage.md`
- 本机官方包源码：`@anthropic-ai/claude-code@2.1.87` 的 `cli.js`

> 说明：本文只讨论官方 Claude Code 包本身，不讨论第三方包装层、网关或外部 hook 方案。

---

## 一、结论速览

| 问题 | 结论 |
| --- | --- |
| Claude Code 是否支持 OTel | 支持。通过 `CLAUDE_CODE_ENABLE_TELEMETRY=1` 开启。 |
| 支持哪些 OTel 信号 | Metrics、Logs/Events、Traces beta。 |
| 是否默认输出用户 prompt 正文 | 不默认输出。需要 `OTEL_LOG_USER_PROMPTS=1`。 |
| 是否默认输出工具参数和输入 | 不默认输出。需要 `OTEL_LOG_TOOL_DETAILS=1`。 |
| 是否默认输出工具输入/输出正文 | 不默认输出。需要 traces beta + `OTEL_LOG_TOOL_CONTENT=1`。 |
| 是否支持 raw API body | 支持，但需要 `OTEL_LOG_RAW_API_BODIES=1` 或 `file:`。 |
| 是否支持输出 Thinking 内容 | 不支持作为稳定 OTel 字段输出；raw API body 中 extended-thinking content 会被 redacted。 |
| 是否把 `OTEL_*` 变量传给 Bash/hooks/MCP 子进程 | 不传。子进程需要单独配置自己的 OTel。 |

---

## 二、相关开关

| 环境变量 | 含义 | 默认行为 |
| --- | --- | --- |
| `CLAUDE_CODE_ENABLE_TELEMETRY` | 开启 Claude Code telemetry。 | 未开启时不导出 OTel。 |
| `OTEL_METRICS_EXPORTER` | Metrics exporter，支持 `console`、`otlp`、`prometheus`、`none`。 | 未配置则不导出 metrics。 |
| `OTEL_LOGS_EXPORTER` | Logs/events exporter，支持 `console`、`otlp`、`none`。 | 未配置则不导出 events。 |
| `OTEL_TRACES_EXPORTER` | Traces exporter，支持 `console`、`otlp`、`none`。 | traces beta 未开启时无效。 |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | 开启 span tracing。`ENABLE_ENHANCED_TELEMETRY_BETA` 也可用。 | 默认关闭。 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | OTLP 协议，支持 `grpc`、`http/json`、`http/protobuf`。 | 由 OTel SDK 默认值决定。 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint。 | 未配置时由 exporter 默认值决定。 |
| `OTEL_EXPORTER_OTLP_HEADERS` | OTLP 请求头，常用于鉴权。 | 默认无额外鉴权头。 |
| `OTEL_METRIC_EXPORT_INTERVAL` | metrics 导出周期，毫秒。 | 默认 `60000`。 |
| `OTEL_LOGS_EXPORT_INTERVAL` | logs/events 导出周期，毫秒。 | 默认 `5000`。 |
| `OTEL_LOG_USER_PROMPTS` | 允许记录用户 prompt 正文。 | 默认 redacted。 |
| `OTEL_LOG_TOOL_DETAILS` | 允许记录工具参数、命令、MCP server/tool 名、skill 名等细节。 | 默认省略或归一化。 |
| `OTEL_LOG_TOOL_CONTENT` | 允许 trace span event 记录工具输入/输出正文。需要 tracing。 | 默认不记录正文。 |
| `OTEL_LOG_RAW_API_BODIES` | 记录 Anthropic Messages API request/response JSON。`1` 为内联截断，`file:` 为写文件并输出引用。 | 默认不输出 raw body。 |
| `OTEL_METRICS_INCLUDE_SESSION_ID` | metrics 是否包含 `session.id`。 | 默认 `true`。 |
| `OTEL_METRICS_INCLUDE_VERSION` | metrics 是否包含 `app.version`。 | 默认 `false`。 |
| `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` | metrics 是否包含 `user.account_uuid` / `user.account_id`。 | 默认 `true`。 |

---

## 三、Claude Code 支持的字段

### 3.1 通用标准属性

这些属性会出现在 metrics 和 events 中；traces 的 span 也会携带标准属性。

| 字段 | 含义 | 适用范围 | 是否可控 |
| --- | --- | --- | --- |
| `session.id` | 当前 Claude Code session 的唯一标识。用于按会话聚合。 | metrics、events、traces | metrics 中可由 `OTEL_METRICS_INCLUDE_SESSION_ID` 控制。 |
| `app.version` | Claude Code 版本号。 | metrics、events、traces | metrics 中可由 `OTEL_METRICS_INCLUDE_VERSION` 控制。 |
| `organization.id` | 登录账号所属组织 UUID。 | metrics、events、traces | 有组织信息时包含。 |
| `user.account_uuid` | Claude 账号 UUID。 | metrics、events、traces | metrics 中可由 `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` 控制。 |
| `user.account_id` | 与 Anthropic admin API 对齐的用户 ID，例如 `user_...`。 | metrics、events、traces | metrics 中可由 `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` 控制。 |
| `user.id` | 本机安装级匿名设备/用户标识。 | metrics、events、traces | 始终尽量包含。 |
| `user.email` | OAuth 登录用户邮箱。 | metrics、events、traces | 登录后可用。 |
| `terminal.type` | 终端类型，例如 `iTerm.app`、`vscode`、`cursor`、`tmux`。 | metrics、events、traces | 检测到时包含。 |
| `prompt.id` | 单次用户 prompt 的 UUID，用于关联该 prompt 触发的 API/tool/hook 事件。 | events | 只用于 events，不进入 metrics，避免高基数。 |
| `workspace.host_paths` | 桌面端选择的 workspace host 路径数组。 | events | events 专用。 |

### 3.2 Metrics

| Metric | 含义 | 单位 | 主要附加属性 |
| --- | --- | --- | --- |
| `claude_code.session.count` | CLI session 启动次数。 | `count` | `start_type`：`fresh`、`resume`、`continue`。 |
| `claude_code.lines_of_code.count` | 代码增删行数。 | `count` | `type`：`added` 或 `removed`。 |
| `claude_code.pull_request.count` | 通过 Claude Code 创建 PR 的次数。 | `count` | 通用标准属性。 |
| `claude_code.commit.count` | 通过 Claude Code 创建 git commit 的次数。 | `count` | 通用标准属性。 |
| `claude_code.cost.usage` | API 请求估算成本。 | `USD` | `model`、`query_source`、`speed`、`effort`。 |
| `claude_code.token.usage` | API token 使用量。 | `tokens` | `type`、`model`、`query_source`、`speed`、`effort`。 |
| `claude_code.code_edit_tool.decision` | Edit、Write、NotebookEdit 等代码编辑工具的权限决策次数。 | `count` | `tool_name`、`decision`、`source`、`language`。 |
| `claude_code.active_time.total` | 活跃使用时间，不含 idle 时间。 | `s` | `type`：`user` 或 `cli`。 |

### 3.3 Metrics 附加字段解释

| 字段 | 含义 |
| --- | --- |
| `start_type` | session 启动方式：新开、恢复、继续。 |
| `type` | 在不同 metric 下含义不同：token 类型、代码增删类型或活跃时间类型。 |
| `model` | 使用的模型标识，例如 `claude-sonnet-4-6`。 |
| `query_source` | 发起请求的子系统类别，常见为 `main`、`subagent`、`auxiliary`。 |
| `speed` | 是否使用 fast mode；通常为 `fast`，未使用时可能缺省。 |
| `effort` | 模型 effort level，例如 `low`、`medium`、`high`、`xhigh`、`max`。 |
| `tool_name` | 工具名，例如 `Edit`、`Write`、`NotebookEdit`。 |
| `decision` | 权限决策结果，通常为 `accept` 或 `reject`。 |
| `source` | 权限决策来源，例如 `config`、`hook`、`user_permanent`、`user_temporary`、`user_abort`、`user_reject`。 |
| `language` | 被编辑文件的语言类型，无法识别时为 `unknown`。 |

### 3.4 Logs/Events

| Event | 触发时机 | 支持字段 | 字段含义 |
| --- | --- | --- | --- |
| `claude_code.user_prompt` | 用户提交 prompt。 | `prompt_length` | prompt 长度。 |
| `claude_code.user_prompt` | 用户提交 prompt。 | `prompt` | prompt 正文。默认 redacted，需 `OTEL_LOG_USER_PROMPTS=1`。 |
| `claude_code.user_prompt` | 用户提交 slash command。 | `command_name` | 命令名。自定义/plugin/MCP 命令默认折叠，需 `OTEL_LOG_TOOL_DETAILS=1` 显示细节。 |
| `claude_code.user_prompt` | 用户提交 slash command。 | `command_source` | 命令来源：`builtin`、`custom`、`mcp`。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `model` | 使用的模型。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `cost_usd` | 估算成本，单位美元。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `duration_ms` | 请求耗时，毫秒。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `input_tokens` | 输入 token 数。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `output_tokens` | 输出 token 数。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `cache_read_tokens` | 从 prompt cache 读取的 token 数。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `cache_creation_tokens` | 写入 prompt cache 的 token 数。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `request_id` | Anthropic API response header 中的 `request-id`。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `speed` | `fast` 或 `normal`。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `query_source` | 发起请求的子系统，例如 `repl_main_thread`、`compact` 或 subagent 名称。 |
| `claude_code.api_request` | 每次成功的 Claude API 请求。 | `effort` | 请求使用的 effort level。 |
| `claude_code.api_error` | Claude API 请求失败。 | `model` | 使用的模型。 |
| `claude_code.api_error` | Claude API 请求失败。 | `error` | 错误信息。 |
| `claude_code.api_error` | Claude API 请求失败。 | `status_code` | HTTP 状态码；非 HTTP 错误可能缺省。 |
| `claude_code.api_error` | Claude API 请求失败。 | `duration_ms` | 失败请求耗时。 |
| `claude_code.api_error` | Claude API 请求失败。 | `attempt` | 总尝试次数，包含初次请求。 |
| `claude_code.api_error` | Claude API 请求失败。 | `request_id` | API 返回时携带的 request ID。 |
| `claude_code.api_request_body` | `OTEL_LOG_RAW_API_BODIES` 开启时，每次 API request attempt。 | `body` | Messages API 请求 JSON，内联模式下截断到约 60KB；历史 assistant turns 中 extended-thinking 会被 redacted。 |
| `claude_code.api_request_body` | `OTEL_LOG_RAW_API_BODIES=file:`。 | `body_ref` | 未截断 request body 文件路径。 |
| `claude_code.api_request_body` | raw body 输出。 | `body_length` | 原始 body 长度。 |
| `claude_code.api_request_body` | raw body 输出。 | `body_truncated` | 内联 body 被截断时为 `true`。 |
| `claude_code.api_response_body` | `OTEL_LOG_RAW_API_BODIES` 开启时，每次成功 API response。 | `body` | Messages API 响应 JSON，内联模式下截断到约 60KB；extended-thinking content 会被 redacted。 |
| `claude_code.api_response_body` | `OTEL_LOG_RAW_API_BODIES=file:`。 | `body_ref` | 未截断 response body 文件路径。 |
| `claude_code.api_response_body` | raw body 输出。 | `body_length` | 原始 response body 长度。 |
| `claude_code.api_response_body` | raw body 输出。 | `body_truncated` | 内联 body 被截断时为 `true`。 |
| `claude_code.tool_result` | 工具执行完成。 | `tool_name` | 工具名。 |
| `claude_code.tool_result` | 工具执行完成。 | `tool_use_id` | 工具调用唯一 ID，可与 hooks 关联。 |
| `claude_code.tool_result` | 工具执行完成。 | `success` | 工具是否成功。 |
| `claude_code.tool_result` | 工具执行完成。 | `duration_ms` | 工具执行耗时。 |
| `claude_code.tool_result` | 工具执行失败。 | `error_type` | 错误类别，例如 `Error:ENOENT`、`ShellError`。 |
| `claude_code.tool_result` | `OTEL_LOG_TOOL_DETAILS=1` 且工具失败。 | `error` | 完整错误信息。 |
| `claude_code.tool_result` | 工具权限决策存在时。 | `decision_type` | `accept` 或 `reject`。 |
| `claude_code.tool_result` | 工具权限决策存在时。 | `decision_source` | 决策来源。 |
| `claude_code.tool_result` | 工具执行完成。 | `tool_input_size_bytes` | JSON 序列化后的工具输入大小。 |
| `claude_code.tool_result` | 工具执行完成。 | `tool_result_size_bytes` | 工具结果大小。 |
| `claude_code.tool_result` | MCP 工具。 | `mcp_server_scope` | MCP server 配置作用域。 |
| `claude_code.tool_result` | `OTEL_LOG_TOOL_DETAILS=1`。 | `tool_parameters` | 工具专属参数 JSON；Bash 包含命令，MCP 包含 server/tool 名，Skill 包含 skill 名，Task 包含 subagent 类型。 |
| `claude_code.tool_result` | `OTEL_LOG_TOOL_DETAILS=1`。 | `tool_input` | JSON 序列化后的工具参数。单值超过 512 字符会截断，总体约 4K。 |
| `claude_code.tool_decision` | 工具权限决策产生。 | `tool_name` | 工具名。 |
| `claude_code.tool_decision` | 工具权限决策产生。 | `tool_use_id` | 工具调用 ID。 |
| `claude_code.tool_decision` | 工具权限决策产生。 | `decision` | `accept` 或 `reject`。 |
| `claude_code.tool_decision` | 工具权限决策产生。 | `source` | 决策来源。 |
| `claude_code.permission_mode_changed` | 权限模式变化。 | `from_mode` | 变化前权限模式。 |
| `claude_code.permission_mode_changed` | 权限模式变化。 | `to_mode` | 变化后权限模式。 |
| `claude_code.permission_mode_changed` | 权限模式变化。 | `trigger` | 触发原因，如 `shift_tab`、`exit_plan_mode`、`auto_gate_denied`、`auto_opt_in`。 |
| `claude_code.auth` | `/login` 或 `/logout` 完成。 | `action` | `login` 或 `logout`。 |
| `claude_code.auth` | `/login` 或 `/logout` 完成。 | `success` | 是否成功。 |
| `claude_code.auth` | 认证事件。 | `auth_method` | 认证方式，例如 `oauth`。 |
| `claude_code.auth` | 认证失败。 | `error_category` | 错误类别，不含原始错误全文。 |
| `claude_code.auth` | HTTP 认证失败。 | `status_code` | HTTP 状态码字符串。 |
| `claude_code.mcp_server_connection` | MCP server 连接、断开或失败。 | `status` | `connected`、`failed` 或 `disconnected`。 |
| `claude_code.mcp_server_connection` | MCP server 连接事件。 | `transport_type` | transport 类型，如 `stdio`、`sse`、`http`。 |
| `claude_code.mcp_server_connection` | MCP server 连接事件。 | `server_scope` | server 配置作用域，如 `user`、`project`、`local`。 |
| `claude_code.mcp_server_connection` | MCP server 连接事件。 | `duration_ms` | 连接尝试耗时。 |
| `claude_code.mcp_server_connection` | MCP server 连接失败。 | `error_code` | 错误码。 |
| `claude_code.mcp_server_connection` | `OTEL_LOG_TOOL_DETAILS=1`。 | `server_name` | MCP server 名称。 |
| `claude_code.mcp_server_connection` | `OTEL_LOG_TOOL_DETAILS=1` 且失败。 | `error` | 完整错误信息。 |
| `claude_code.internal_error` | Claude Code 捕获非预期内部错误。 | `error_name` | 错误类名，例如 `TypeError`、`SyntaxError`。 |
| `claude_code.internal_error` | 内部错误。 | `error_code` | Node.js errno 代码，例如 `ENOENT`。 |
| `claude_code.plugin_installed` | plugin 安装完成。 | `marketplace.is_official` | 是否官方 marketplace。 |
| `claude_code.plugin_installed` | plugin 安装完成。 | `install.trigger` | `cli` 或 `ui`。 |
| `claude_code.plugin_installed` | plugin 安装完成。 | `plugin.name` | plugin 名。第三方 marketplace 默认受限，需 `OTEL_LOG_TOOL_DETAILS=1`。 |
| `claude_code.plugin_installed` | plugin 安装完成。 | `plugin.version` | plugin 版本。第三方 marketplace 默认受限。 |
| `claude_code.plugin_installed` | plugin 安装完成。 | `marketplace.name` | marketplace 名。第三方 marketplace 默认受限。 |
| `claude_code.skill_activated` | skill 被调用。 | `skill.name` | skill 名；自定义/第三方 plugin skill 默认用 `custom_skill`，需 `OTEL_LOG_TOOL_DETAILS=1` 输出细节。 |
| `claude_code.skill_activated` | skill 被调用。 | `invocation_trigger` | 触发方式：`user-slash`、`claude-proactive`、`nested-skill`。 |
| `claude_code.skill_activated` | skill 被调用。 | `skill.source` | skill 来源，如 `bundled`、`userSettings`、`projectSettings`、`plugin`。 |
| `claude_code.at_mention` | prompt 中 `@` mention 被解析。 | `mention_type` | mention 类型：`file`、`directory`、`agent`、`mcp_resource`。 |
| `claude_code.at_mention` | `@` mention 解析完成。 | `success` | 是否解析成功。 |
| `claude_code.api_retries_exhausted` | API 多次重试后仍失败。 | `model` | 使用的模型。 |
| `claude_code.api_retries_exhausted` | API 重试耗尽。 | `error` | 最终错误信息。 |
| `claude_code.api_retries_exhausted` | API 重试耗尽。 | `status_code` | HTTP 状态码。 |
| `claude_code.api_retries_exhausted` | API 重试耗尽。 | `total_attempts` | 总尝试次数。 |
| `claude_code.api_retries_exhausted` | API 重试耗尽。 | `total_retry_duration_ms` | 所有尝试总耗时。 |
| `claude_code.hook_execution_start` | hooks 开始执行。 | `hook_event` | hook 事件类型，如 `PreToolUse`。 |
| `claude_code.hook_execution_start` | hooks 开始执行。 | `hook_name` | 完整 hook 名，如 `PreToolUse:Write`。 |
| `claude_code.hook_execution_start` | hooks 开始执行。 | `num_hooks` | 匹配到的 hook 命令数量。 |
| `claude_code.hook_execution_start` | hooks 开始执行。 | `managed_only` | 是否只允许 managed-policy hooks。 |
| `claude_code.hook_execution_start` | hooks 开始执行。 | `hook_source` | `policySettings` 或 `merged`。 |
| `claude_code.hook_execution_start` | detailed beta tracing + `OTEL_LOG_TOOL_DETAILS=1`。 | `hook_definitions` | JSON 序列化 hook 配置。 |
| `claude_code.hook_execution_complete` | hooks 执行完成。 | `num_success` | 成功 hook 数量。 |
| `claude_code.hook_execution_complete` | hooks 执行完成。 | `num_blocking` | 返回 blocking 决策的 hook 数量。 |
| `claude_code.hook_execution_complete` | hooks 执行完成。 | `num_non_blocking_error` | 非阻塞错误数量。 |
| `claude_code.hook_execution_complete` | hooks 执行完成。 | `num_cancelled` | 被取消的 hook 数量。 |
| `claude_code.hook_execution_complete` | hooks 执行完成。 | `total_duration_ms` | 所有 matching hooks 总耗时。 |
| `claude_code.compaction` | 会话压缩完成。 | `trigger` | `auto` 或 `manual`。 |
| `claude_code.compaction` | 会话压缩完成。 | `success` | 是否成功。 |
| `claude_code.compaction` | 会话压缩完成。 | `duration_ms` | 压缩耗时。 |
| `claude_code.compaction` | 会话压缩完成。 | `pre_tokens` | 压缩前近似 token 数。 |
| `claude_code.compaction` | 会话压缩完成。 | `post_tokens` | 压缩后近似 token 数。 |
| `claude_code.compaction` | 压缩失败。 | `error` | 错误信息。 |

### 3.5 Traces beta span 字段

启用条件：`CLAUDE_CODE_ENABLE_TELEMETRY=1`、`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`、配置 `OTEL_TRACES_EXPORTER`。

| Span | 字段 | 含义 | 是否受开关控制 |
| --- | --- | --- | --- |
| `claude_code.interaction` | `user_prompt` | 用户 prompt 正文。默认空或 redacted。 | `OTEL_LOG_USER_PROMPTS=1` |
| `claude_code.interaction` | `user_prompt_length` | prompt 字符长度。 | 否 |
| `claude_code.interaction` | `interaction.sequence` | 当前 session 内第几轮 interaction。 | 否 |
| `claude_code.interaction` | `interaction.duration_ms` | 本轮总耗时。 | 否 |
| `claude_code.llm_request` | `model` | 模型标识。 | 否 |
| `claude_code.llm_request` | `gen_ai.system` | GenAI 语义字段，固定为 `anthropic`。 | 否 |
| `claude_code.llm_request` | `gen_ai.request.model` | GenAI 语义字段，等同于 `model`。 | 否 |
| `claude_code.llm_request` | `query_source` | 请求来源子系统。 | 否 |
| `claude_code.llm_request` | `speed` | `fast` 或 `normal`。 | 否 |
| `claude_code.llm_request` | `llm_request.context` | 请求上下文：`interaction`、`tool`、`standalone`。 | 否 |
| `claude_code.llm_request` | `duration_ms` | 请求总耗时，包含 retries。 | 否 |
| `claude_code.llm_request` | `ttft_ms` | time to first token。 | 否 |
| `claude_code.llm_request` | `input_tokens` | 输入 token 数。 | 否 |
| `claude_code.llm_request` | `output_tokens` | 输出 token 数。 | 否 |
| `claude_code.llm_request` | `cache_read_tokens` | cache read token 数。 | 否 |
| `claude_code.llm_request` | `cache_creation_tokens` | cache creation token 数。 | 否 |
| `claude_code.llm_request` | `request_id` | Anthropic API request ID。 | 否 |
| `claude_code.llm_request` | `gen_ai.response.id` | GenAI 语义字段，等同于 `request_id`。 | 否 |
| `claude_code.llm_request` | `client_request_id` | 最后一次 attempt 的客户端请求 ID。 | 否 |
| `claude_code.llm_request` | `attempt` | 总 attempt 数。 | 否 |
| `claude_code.llm_request` | `success` | 是否成功。 | 否 |
| `claude_code.llm_request` | `status_code` | 失败时 HTTP 状态码。 | 否 |
| `claude_code.llm_request` | `error` | 失败时错误信息。 | 否 |
| `claude_code.llm_request` | `response.has_tool_call` | 响应是否包含 tool-use block。 | 否 |
| `claude_code.llm_request` | `stop_reason` | API `stop_reason`，如 `end_turn`、`tool_use`、`max_tokens`、`stop_sequence`、`pause_turn`、`refusal`。 | 否 |
| `claude_code.llm_request` | `gen_ai.response.finish_reasons` | GenAI 语义字段，`stop_reason` 的数组形式。 | 否 |
| `claude_code.llm_request` event | `gen_ai.request.attempt` | 每次 retry attempt 的 span event。 | 否 |
| `claude_code.tool` | `tool_name` | 工具名。 | 否 |
| `claude_code.tool` | `duration_ms` | 权限等待 + 工具执行的总耗时。 | 否 |
| `claude_code.tool` | `result_tokens` | 工具结果的近似 token 数。 | 否 |
| `claude_code.tool` | `file_path` | Read/Edit/Write 目标文件路径。 | `OTEL_LOG_TOOL_DETAILS=1` |
| `claude_code.tool` | `full_command` | Bash 命令完整字符串。 | `OTEL_LOG_TOOL_DETAILS=1` |
| `claude_code.tool` | `skill_name` | Skill tool 调用的 skill 名。 | `OTEL_LOG_TOOL_DETAILS=1` |
| `claude_code.tool` | `subagent_type` | Task tool 的 subagent 类型。 | `OTEL_LOG_TOOL_DETAILS=1` |
| `claude_code.tool` event | `tool.output` | 工具输入和输出正文，单属性截断约 60KB。 | tracing + `OTEL_LOG_TOOL_CONTENT=1` |
| `claude_code.tool.blocked_on_user` | `duration_ms` | 等待用户权限决策耗时。 | 否 |
| `claude_code.tool.blocked_on_user` | `decision` | `accept` 或 `reject`。 | 否 |
| `claude_code.tool.blocked_on_user` | `source` | 决策来源。 | 否 |
| `claude_code.tool.execution` | `duration_ms` | 工具 body 实际执行耗时。 | 否 |
| `claude_code.tool.execution` | `success` | 是否成功。 | 否 |
| `claude_code.tool.execution` | `error` | 失败类别；开启 `OTEL_LOG_TOOL_DETAILS=1` 后可包含完整错误信息。 | 部分受控 |
| `claude_code.hook` | `hook_event` | hook 事件类型。 | detailed beta tracing |
| `claude_code.hook` | `hook_name` | 完整 hook 名。 | detailed beta tracing |
| `claude_code.hook` | `num_hooks` | 匹配 hook 数量。 | detailed beta tracing |
| `claude_code.hook` | `hook_definitions` | hook 配置 JSON。 | detailed beta tracing + `OTEL_LOG_TOOL_DETAILS=1` |
| `claude_code.hook` | `duration_ms` | hook 总耗时。 | detailed beta tracing |
| `claude_code.hook` | `num_success` | 成功数量。 | detailed beta tracing |
| `claude_code.hook` | `num_blocking` | blocking 数量。 | detailed beta tracing |
| `claude_code.hook` | `num_non_blocking_error` | 非阻塞错误数量。 | detailed beta tracing |
| `claude_code.hook` | `num_cancelled` | 取消数量。 | detailed beta tracing |

### 3.6 详细 beta tracing 的内容型字段

这些字段在源码中能看到相关处理，但官方文档明确说明它们不是稳定 span schema 的一部分。

| 字段 | 含义 | 输出条件 | 稳定性 |
| --- | --- | --- | --- |
| `new_context` | 新增上下文内容，例如用户 prompt、tool result 拼出的上下文。 | detailed beta tracing。 | 非稳定 schema。 |
| `new_context_message_count` | `new_context` 中消息数量。 | detailed beta tracing。 | 非稳定 schema。 |
| `system_reminders` | system-reminder 内容。 | detailed beta tracing。 | 非稳定 schema。 |
| `system_reminders_count` | system-reminder 数量。 | detailed beta tracing。 | 非稳定 schema。 |
| `system_prompt_hash` | system prompt hash。 | detailed beta tracing。 | 非稳定 schema。 |
| `system_prompt_preview` | system prompt 前 500 字符预览。 | detailed beta tracing。 | 非稳定 schema。 |
| `system_prompt_length` | system prompt 长度。 | detailed beta tracing。 | 非稳定 schema。 |
| `tools` | 工具名和 hash 的 JSON 列表。 | detailed beta tracing。 | 非稳定 schema。 |
| `tools_count` | 工具数量。 | detailed beta tracing。 | 非稳定 schema。 |
| `tool_input` | 工具输入内容。 | detailed beta tracing。 | 非稳定 schema。 |
| `response.model_output` | 模型输出文本。 | detailed beta tracing。 | 非稳定 schema。 |
| `user_system_prompt` | 用户通过 SDK option 或 `--system-prompt` / `--append-system-prompt` 提供的 system prompt。 | detailed beta tracing + `OTEL_LOG_USER_PROMPTS=1`，每 session 一次，截断约 60KB。 | 非稳定 schema。 |

---

## 四、Claude Code 不支持或默认不输出的字段

| 字段/能力 | 状态 | 原因或设计边界 | 替代方案 |
| --- | --- | --- | --- |
| `thinking_output` | 不作为稳定 OTel 字段支持。 | 官方公开 schema 没有该字段；源码里虽有 `thinkingOutput` 传参痕迹，但未见稳定 OTel 字段建模。 | 无官方稳定 OTel 输出。 |
| `response.thinking` | 不支持。 | Traces/Events schema 中没有该字段。 | 无。 |
| `reasoning_content` | 不支持。 | Claude Code OTel 没定义通用 reasoning 字段。 | 无。 |
| `redacted_thinking` 原文 | 不支持输出原文。 | raw API body 事件明确会 redact extended-thinking content。 | 只能拿到非 thinking 的 API body 内容。 |
| `thinking_delta` / `signature_delta` SSE 原始片段 | 不支持作为 OTel 字段。 | OTel schema 不暴露 SSE thinking stream 片段。 | 需要另行在 API 代理层采集，但这不属于 Claude Code 官方 OTel。 |
| 完整用户 prompt 正文 | 默认不输出。 | 隐私保护，默认 redacted。 | `OTEL_LOG_USER_PROMPTS=1`。 |
| 完整工具参数 | 默认不输出。 | 隐私与安全保护，默认省略或归一化。 | `OTEL_LOG_TOOL_DETAILS=1`。 |
| 完整工具输入/输出正文 | 默认不输出。 | 内容可能包含代码、密钥或大文本。 | traces beta + `OTEL_LOG_TOOL_CONTENT=1`，且截断约 60KB。 |
| 完整 API request/response body | 默认不输出。 | body 包含完整上下文和工具定义，敏感度高。 | `OTEL_LOG_RAW_API_BODIES=1` 或 `file:`；但 thinking 仍 redacted。 |
| 内部错误完整 message / stack trace | 不支持。 | 官方事件只记录错误类名和 errno 风格 code，避免泄漏本地细节。 | 查看本地 debug log，而不是 OTel。 |
| 无限长正文 | 不支持。 | 多处字段有截断策略：raw body 约 60KB，tool_input 约 4K，单值约 512 字符。 | `OTEL_LOG_RAW_API_BODIES=file:` 可保存未截断 body，但 thinking 仍 redacted。 |
| 将 `OTEL_*` 自动传给 Bash / hooks / MCP / language server | 不支持。 | Claude Code 明确不把自身 OTel 环境变量传给子进程。 | 在子进程命令或服务配置中单独设置 OTel。 |
| 第三方 plugin/marketplace 详细名称 | 默认不输出。 | 默认降低敏感信息和高基数风险。 | `OTEL_LOG_TOOL_DETAILS=1`。 |
| MCP server/tool 详细名称与参数 | 默认不完整输出。 | 默认只保留有限审计字段。 | `OTEL_LOG_TOOL_DETAILS=1`。 |
| `prompt.id` 作为 metrics 属性 | 不支持。 | 高基数字段会导致无限 time series。 | 用 logs/events 查询。 |
| `workspace.host_paths` 作为 metrics 属性 | 不支持。 | 高基数/敏感路径，不适合 metrics。 | 用 logs/events 查询。 |

---

## 五、字段使用建议

| 目标 | 推荐配置 | 说明 |
| --- | --- | --- |
| 只看用量和成本 | `CLAUDE_CODE_ENABLE_TELEMETRY=1` + `OTEL_METRICS_EXPORTER=otlp` | 使用 `claude_code.token.usage` 和 `claude_code.cost.usage`。 |
| 做审计，不看正文 | `OTEL_LOGS_EXPORTER=otlp` | 记录 user/tool/api 事件和身份属性，但正文默认 redacted。 |
| 审计 Bash/MCP/Skill 具体调用 | `OTEL_LOGS_EXPORTER=otlp` + `OTEL_LOG_TOOL_DETAILS=1` | 可看到命令、MCP server/tool、skill、subagent 类型等。 |
| 调试完整上下文问题 | `OTEL_LOG_RAW_API_BODIES=1` 或 `file:` | 可输出 request/response body，但 extended-thinking 仍会被 redacted。 |
| 做链路追踪 | `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` + `OTEL_TRACES_EXPORTER=otlp` | 生成 interaction / llm_request / tool / hook spans。 |
| 分析工具输入输出正文 | tracing + `OTEL_LOG_TOOL_CONTENT=1` | 工具正文进入 span event，截断约 60KB。 |
| 尝试获取 Thinking 内容 | 不建议通过 Claude Code OTel。 | 官方 OTel 设计不提供稳定 Thinking 内容字段，raw body 也会 redact。 |
| 调试 RAG 召回链路 | `OTEL_LOGS_EXPORTER=otlp` + `OTEL_LOG_TOOL_DETAILS=1`，必要时加 raw body。 | 适合确认 RAG tool 是否被调用、参数是否大致正确、耗时和错误；不适合直接分析召回语义质量。 |
| 审计 Skill 是否按流程调用工具 | traces beta + `OTEL_LOGS_EXPORTER=otlp` + `OTEL_LOG_TOOL_DETAILS=1` | 适合检查工具/API/hook 事件顺序；不适合证明纯认知步骤是否发生。 |

---

## 六、关于 Thinking 的单独说明

源码里可以看到 Claude Code 在 API 请求结束后的统计路径中存在 `thinkingOutput` 变量传入遥测聚合函数的痕迹，但公开 OTel schema 并没有暴露稳定的 `thinking_output` / `response.thinking` 字段。

同时，官方文档对 raw body 事件的描述更明确：

- `api_request_body.body`：历史 assistant turns 中的 extended-thinking content 会被 redacted。
- `api_response_body.body`：extended-thinking content 会被 redacted。

因此，结论应写成：

> Claude Code OTel 支持 token、成本、API 请求、工具执行、权限决策、MCP、hook、compaction、trace spans 等观测字段；但不支持通过稳定 OTel 字段输出完整 Thinking 内容。即使启用 raw API body，extended-thinking content 也会被脱敏。

---

## 七、场景分析：RAG 召回不符合预期

### 7.1 Claude Code OTel 能回答什么

当 RAG 能力是通过 MCP tool、Skill tool、Bash command 或其他 Claude Code 工具间接调用时，Claude Code OTel 适合做外围链路观测。

| Debug 问题 | Claude Code OTel 是否能支持 | 可用字段/事件 | 说明 |
| --- | --- | --- | --- |
| RAG tool 有没有被调用 | 支持 | `claude_code.tool_result`、`tool_name`、`tool_use_id`、`prompt.id` | 可以确认某个 prompt 触发了哪个工具调用。 |
| RAG tool 调用是否成功 | 支持 | `success`、`error_type`、`error`、`duration_ms` | `error` 全文需要 `OTEL_LOG_TOOL_DETAILS=1`。 |
| RAG tool 调用耗时是否异常 | 支持 | `duration_ms`、trace span `claude_code.tool` | 可定位慢查询、卡住、超时等链路问题。 |
| RAG tool 输入参数是否大致正确 | 部分支持 | `tool_parameters`、`tool_input` | 需要 `OTEL_LOG_TOOL_DETAILS=1`；字段会截断，不适合存大段 query/context。 |
| RAG tool 输出大小是否异常 | 支持 | `tool_result_size_bytes`、`result_tokens` | 可判断是否召回过多、过少或结果异常为空。 |
| RAG 结果有没有进入最终 LLM 上下文 | 部分支持 | `OTEL_LOG_RAW_API_BODIES=1` / `file:`、`api_request_body.body` | 可看最终 request body；但内容会截断，extended-thinking 会被 redacted。 |
| API 是否因为上下文过大或失败 | 支持 | `claude_code.api_error`、`status_code`、`input_tokens`、`cache_*_tokens` | 可定位 token 膨胀、重试耗尽、请求失败。 |

### 7.2 Claude Code OTel 不擅长回答什么

RAG 召回质量问题通常发生在 retriever/reranker 层，而不是 Claude Code 外围链路层。

| Debug 问题 | Claude Code OTel 是否足够 | 原因 | 建议补充 |
| --- | --- | --- | --- |
| 为什么召回的文档语义不相关 | 不足够 | OTel 默认不记录向量相似度、rerank 分数、候选文档全文。 | 在 RAG tool 内记录 `query`、`doc_id`、`chunk_id`、`score`、`rerank_score`。 |
| 哪些候选 chunk 被召回但被 reranker 丢弃 | 不足够 | Claude Code 只看到工具调用结果，不知道工具内部候选集。 | 在 retriever/reranker 层记录 topN candidate 和 selected set。 |
| filter / namespace / collection 是否用错 | 部分支持 | `tool_input` 可能能看到 filter，但会受开关和截断影响。 | 在 RAG tool 内结构化记录 `filters`、`namespace`、`collection`。 |
| chunk 文本是否切分错误 | 不足够 | Claude Code OTel 不知道索引构建和 chunking 细节。 | 在索引构建或检索层记录 `chunk_text_preview`、`source_path`、`offset`。 |
| 模型为什么忽略了召回内容 | 不足够 | OTel 不输出完整 Thinking；只能看最终输出和工具/API 事件。 | 检查最终上下文、prompt 模板、召回内容排序和截断策略。 |
| 召回内容是否被 prompt 裁剪掉 | 部分支持 | raw body 可帮助确认最终上下文，但不是专门的 RAG debug schema。 | 在 RAG tool 输出前记录 `selected_chunk_ids`，在 prompt assembly 后记录 `included_chunk_ids`。 |

### 7.3 推荐的 RAG Debug 埋点

不要只依赖 Claude Code OTel。更稳的做法是在 RAG 工具本身输出一组结构化 debug 事件。

| 建议字段 | 含义 | 注意事项 |
| --- | --- | --- |
| `rag.query` | RAG 实际收到的查询文本。 | 可脱敏或只记录 hash / preview。 |
| `rag.filters` | namespace、collection、metadata filters。 | 适合排查过滤条件错误。 |
| `rag.top_k` | 召回数量。 | 排查 top_k 设置过小或过大。 |
| `rag.retrieved_doc_ids` | 初始召回文档 ID 列表。 | 比直接记录全文更安全。 |
| `rag.retrieved_chunk_ids` | 初始召回 chunk ID 列表。 | 可与索引库反查。 |
| `rag.scores` | 向量相似度分数。 | 注意不同 embedding/backend 分数不可直接横向比较。 |
| `rag.rerank_scores` | reranker 分数。 | 用于判断 reranker 是否误杀。 |
| `rag.selected_chunk_ids` | 最终送回 agent/LLM 的 chunk ID。 | 用于和最终 prompt 对齐。 |
| `rag.chunk_text_preview` | chunk 文本预览。 | 建议只在 `DEBUG_RAG_CONTENT=1` 下开启，并截断/脱敏。 |
| `rag.prompt_included_chunk_ids` | 最终进入 LLM prompt 的 chunk ID。 | 用于排查召回成功但组装失败。 |

### 7.4 RAG 场景结论

> Claude Code OTel 适合定位 RAG 的“链路、耗时、错误、调用参数、上下文是否进入 LLM”等外围问题；但不适合单独诊断“召回语义质量”。召回质量 debug 应该在 RAG tool / retriever / reranker 层增加专门的结构化日志。

---

## 八、场景分析：Skill 流程是否按顺序执行

### 8.1 Claude Code OTel 能验证什么

如果 Skill 中的步骤会产生外显动作，例如调用工具、发起 API 请求、执行 hook，那么 Claude Code OTel 可以用于验证这些外显动作的顺序。

| 验证目标 | Claude Code OTel 是否能支持 | 可用字段/事件 | 说明 |
| --- | --- | --- | --- |
| Skill 是否被激活 | 支持 | `claude_code.skill_activated`、`skill.name`、`invocation_trigger` | 自定义/第三方 skill 名默认可能被折叠，需要 `OTEL_LOG_TOOL_DETAILS=1`。 |
| Skill 是用户 slash 触发还是模型主动触发 | 支持 | `invocation_trigger` | 常见值包括 `user-slash`、`claude-proactive`、`nested-skill`。 |
| Skill 激活后是否调用了预期工具 | 支持 | `tool_result`、trace span `claude_code.tool` | 通过 `prompt.id` 或 trace 层级关联。 |
| 工具调用是否按顺序发生 | 支持 | `event.sequence`、`event.timestamp`、trace span start/end | 适合验证 Search -> Read -> Edit -> Test 这类流程。 |
| 是否发生了预期 API 请求 | 支持 | `api_request`、trace span `claude_code.llm_request` | 可看到请求顺序、模型、token、耗时。 |
| hook 是否运行 | 支持 | `hook_execution_start`、`hook_execution_complete`、`claude_code.hook` | detailed beta tracing 下信息更完整。 |
| 某一步是否失败导致后续没执行 | 部分支持 | `success`、`error_type`、`error`、trace status | 适合定位工具/API 失败，不一定能解释模型为什么停止。 |

### 8.2 Claude Code OTel 不能可靠证明什么

Skill 的很多步骤是“认知型”或“约束型”的，不一定会产生外部事件。

| 验证目标 | Claude Code OTel 是否足够 | 原因 | 建议补充 |
| --- | --- | --- | --- |
| 模型是否真的读完了 Skill 文本 | 不足够 | OTel 只能看到 skill activated，不能证明模型内部阅读状态。 | Skill 中要求输出或记录显式 checkpoint。 |
| 模型是否按纯文本步骤思考 | 不支持 | Thinking 不作为稳定 OTel 字段输出。 | 将关键步骤转成可观测动作。 |
| 是否跳过了“先评估风险”这类认知步骤 | 不足够 | 没有工具调用就没有可验证事件。 | 增加 `risk_assessment_completed` 等外显记录。 |
| 是否遵守“先提出 3 个方案再实现” | 部分支持 | 如果方案只在自然语言回复里出现，OTel 不稳定适合审计。 | 要求结构化 checklist 或 step log。 |
| 是否在内部纠正过错误路径 | 不支持 | 这通常只存在于 Thinking 或中间推理。 | 依赖最终可观测动作与结果检查。 |
| 是否按顺序执行所有 checklist 项 | 取决于 checklist 是否外显 | 如果 checklist 没有事件化，就无法可靠审计。 | 每个关键步骤写入 hook/event/log。 |

### 8.3 让 Skill 流程可观测的设计方式

如果希望通过 Trace 检查 Skill 是否跳步，应把 Skill 中的关键步骤设计成“外显 checkpoint”。

| 设计方式 | 做法 | 优点 | 代价 |
| --- | --- | --- | --- |
| 结构化 step log | 每步开始/完成时记录 `skill_step_started`、`skill_step_completed`。 | 最容易验证顺序和缺步。 | 需要额外脚本、hook 或 MCP tool。 |
| 轻量 checkpoint tool | Skill 要求每步调用一个轻量工具记录 `step_id`、`status`。 | 可进入 `tool_result` 和 trace。 | 增加工具调用成本和噪声。 |
| Hook 记录 | 在关键 PreToolUse/PostToolUse 点记录当前阶段。 | 不改模型流程也能记录工具阶段。 | 只能覆盖工具相关步骤。 |
| 最终 checklist | Skill 要求最终输出 `completed_steps`。 | 实现简单，便于人工审阅。 | 不能防止事后补写，审计强度较弱。 |
| 状态文件 | 每步向临时状态文件追加 step 状态。 | 可离线检查完整流程。 | 需要文件清理和并发隔离。 |
| Trace span 属性 | 自定义工具或 wrapper 将 `skill.name`、`step.id` 写入 span/log 属性。 | 适合接入现有 OTel 后端查询。 | 需要自定义集成。 |

### 8.4 推荐的 Skill Step 字段

| 字段 | 含义 | 示例 |
| --- | --- | --- |
| `skill.name` | Skill 名称。 | `rag-debugger` |
| `skill.version` | Skill 版本。 | `1.0.0` |
| `skill.run_id` | 单次 skill 执行 ID。 | UUID |
| `skill.step_id` | 步骤编号。 | `01_load_context` |
| `skill.step_name` | 步骤名称。 | `读取上下文` |
| `skill.step_status` | 步骤状态。 | `started`、`completed`、`skipped`、`failed` |
| `skill.step_sequence` | 步骤序号。 | `1`、`2`、`3` |
| `skill.expected_previous_step` | 期望前置步骤。 | `00_validate_input` |
| `skill.skip_reason` | 如果跳步，记录原因。 | `not_applicable`、`blocked` |
| `skill.error` | 步骤失败原因。 | `missing_config` |

### 8.5 Skill 场景结论

> Claude Code OTel 能验证“外显事件层”的顺序，例如 skill 激活、API 请求、工具调用、hook 执行；但不能可靠证明模型内部是否遵守了 Skill 的纯认知流程。若要审计是否跳步，应把关键步骤设计成可观测 checkpoint，而不是依赖 Thinking 或隐式推理过程。
