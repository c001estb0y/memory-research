# Shadow-Folk 项目经验总结

> 基于 2026-03-30 ~ 2026-04-03 期间的开发记忆数据，提炼出可复用的工程经验。

---

## 经验一：MCP 从 stdio 迁移到 HTTP 端点

### 问题描述
原有 MCP 接入方式基于 stdio 本地进程（`shadowfolk-query-mcp`），用户需要 clone 仓库、安装 Node.js、执行 `npm install` 才能使用。分发成本高，每个新用户接入都需要完整搭建本地环境。

### 局限环境
- Cursor IDE 作为 MCP 客户端
- 远端 CVM 开发服务器（9.134.128.138）
- Docker 容器化部署（shadow-folk-dev）
- Nginx 反向代理（8080 端口）
- Hono 作为后端框架

### 项目
Shadow-Folk — AI Agent 记忆服务系统

### 处理时间
2026-03-30（约 4 小时完成设计 + 实现 + 部署 + 验证）

### 解决过程

1. **方案设计阶段**
   - 对比了无状态（JSON 同步）和有状态（SSE 流式）两种方案
   - 确认现有 13 个工具均为查询返回型，无流式需求 → 选择**无状态 JSON 模式**
   - 决策：`enableJsonResponse: true`，`sessionIdGenerator: undefined`
   - 不改 Nginx 配置，不改数据库，不改 Docker Compose

2. **架构拆分**
   - 将原 `server.ts` 中的工具定义和处理逻辑抽离为共享模块：
     - `tools.ts` — 13 个共享工具定义
     - `handlers.ts` — CallTool 处理逻辑，直接调用 Service 层
     - `http.ts` — HTTP MCP 端点，Hono 路由 + Bearer Token 鉴权
   - stdio 和 HTTP 两个入口共用同一套 tools + handlers，避免实现分叉

3. **关键设计决策**
   - 身份来源：**只从 Token 解析 `personId`**，不信任工具参数中传入的身份
   - 路由注册顺序：`/api/mcp` 挂载在 TOF 认证中间件**之前**，只走 Bearer Token 校验
   - 每次请求新建 Server + Transport，处理完即销毁（无状态）

4. **部署验证**
   - Git 提交 → `deploy-dev.sh` 远程部署
   - 通过 SSH 在开发服上验证三个场景：initialize 握手、无效 Token 拒绝、tools/list 查询

### 解决结果
- 用户接入从"搭环境 + clone + npm install"降低为**粘贴一段 JSON 配置到 `~/.cursor/mcp.json`**
- HTTP MCP 在开发环境稳定运行，所有工具正常工作
- 原 stdio MCP 保留用于本地开发调试，`shadowfolk-query-mcp` 包标记废弃

**经验提炼**：
- stdio MCP 适合本地开发，但**不适合分发**——每个用户都要搭环境。HTTP 模式把分发成本降为零
- 迁移前先盘点现有工具是否都是无状态查询型。如果是，直接选**无状态 JSON 模式**，不要过度设计有状态 session
- 工具定义和处理逻辑一定要**抽离为共享模块**，否则 stdio/HTTP 两套入口会逐渐分叉，维护成本翻倍
- 路由注册顺序决定认证链路：需要 Token 认证的端点必须挂载在 TOF 等全局中间件**之前**
- 部署验证建议固化为三步脚本：`initialize` 握手 → 无效 Token 拒绝 → `tools/list` 查询，一次跑通即可确认端点可用

---

## 经验二：Bearer Token 鉴权 401 排查

### 问题描述
团队成员 yuantongliu 配置 MCP 后，Cursor 界面持续显示红色错误，`search_knowledge` 调用返回空结果。怀疑 MCP 服务不可用。

### 局限环境
- Cursor IDE MCP 客户端
- Bearer Token 认证
- 服务端 `validateApiToken` 中间件

### 项目
Shadow-Folk

### 处理时间
2026-04-03（约 30 分钟定位 + 修复）

### 解决过程

1. **排查服务端日志** — SSH 进入远程容器查看 Docker logs，发现服务正常运行、所有请求返回 200
2. **识别噪音** — 大量 `GET /api/mcp` 请求是 Cursor 客户端在无状态模式下反复尝试建立 SSE 流的**轮询探测**，返回 200 但无实际业务意义
3. **定位根因** — 从截图发现 yuantongliu 的 `Authorization` 请求头**只传了令牌本身，缺少 `Bearer` 前缀**
4. **代码验证** — `validateApiToken` 首先校验 `startsWith('Bearer ')`，缺失前缀直接返回 null → 401

### 解决结果
- 修复：将配置改为 `Authorization: Bearer <token>` 即可
- 空结果问题：`search_knowledge` 返回空是因为知识库中无对应数据，应改用 `search_memory_like`
- Cursor 红色报错是 SSE 轮询噪音，不影响 POST 工具调用

**经验提炼**：Bearer Token 认证问题中，**前缀格式错误**是最常见的低级错误之一。建议在文档和错误提示中明确给出完整格式示例 `Authorization: Bearer sf_xxx`。

---

## 经验三：跨平台 Shell 脚本执行失败（CRLF / PowerShell 兼容性）

### 问题描述
在 Windows 上通过 SSH 将测试脚本上传到 Linux 远程服务器执行，脚本报 `$'\r': command not found` 和 `-H: command not found` 等错误，无法正常运行。

### 局限环境
- 本地：Windows + PowerShell（Cursor 终端）
- 远程：Linux Docker 容器
- 涉及 SSH、SCP、curl、Bash 脚本

### 项目
Shadow-Folk — MCP HTTP 端点验证

### 处理时间
2026-03-30（约 20 分钟排查 + 修复）

### 解决过程

1. **第一次失败** — SSH 直接执行 curl 命令，PowerShell 对 `&&`、heredoc (`<<'EOF'`)、`$(...)` 等 Bash 语法解析报错 `ParserError: InvalidEndOfLine`
2. **第二次失败** — 改为 SCP 上传脚本再远程执行，但脚本在 Windows 编辑器中保存为 CRLF 行尾，Linux Bash 将 `\r` 作为命令名的一部分，导致 curl 的多行参数全部断裂
3. **第三次失败** — Python one-liner 内联生成 JSON 文件，嵌套引号在 PowerShell → SSH → Bash → Python 四层转义下崩溃
4. **最终修复** — 在远程执行前加入 `sed` 转换行尾：`sed 's/\r$//' /tmp/test-mcp.sh > /tmp/test-mcp-clean.sh && bash /tmp/test-mcp-clean.sh`

### 解决结果
- 测试脚本成功执行，验证 MCP initialize、无效 Token 拒绝、tools/list 三个场景全部通过
- 后续固化为标准做法：所有上传到 Linux 的脚本先 `sed 's/\r$//'` 清洗

**经验提炼**：
- Windows → Linux 跨平台脚本传输，**必须处理 CRLF → LF** 转换
- 避免在 PowerShell 中嵌套 Bash 语法（`&&`、heredoc、`$()`），改为上传独立脚本文件执行
- 多层嵌套（PowerShell → SSH → Bash → Python/Node）的引号转义几乎不可能手写正确，应拆分为文件传输 + 远程执行

---

## 经验四：Docker 部署因 Lockfile 过期而失败

### 问题描述
远程 DEV 部署执行到 Docker 镜像构建阶段时，`pnpm install --frozen-lockfile` 报错 `ERR_PNPM_OUTDATED_LOCKFILE`，构建中断。

### 局限环境
- pnpm workspace（7 个子项目）
- Docker 多阶段构建
- `--frozen-lockfile` 严格模式
- CI/CD 通过 SSH + `deploy-dev.sh` 触发

### 项目
Shadow-Folk

### 处理时间
2026-03-30（约 10 分钟定位 + 修复）

### 解决过程

1. **报错分析** — `pnpm-lock.yaml` 中记录的 `@modelcontextprotocol/sdk` 版本为 `^1.0.0`，但 `packages/server/package.json` 已更新为 `^1.28.0`
2. **原因** — 开发者在修改 `package.json` 升级依赖后，忘记运行 `pnpm install` 重新生成 lockfile 就直接提交了
3. **修复** — 本地执行 `pnpm install`，将更新后的 `pnpm-lock.yaml` 单独提交（`chore: update lockfile for modelcontextprotocol/sdk 1.28.0`），重新推送并部署

### 解决结果
- 部署成功，容器正常重建启动
- 后续验证 initialize、auth rejection、tools/list 三项测试全部通过

**经验提炼**：
- 使用 `--frozen-lockfile` 的项目，**每次修改 `package.json` 后必须同步提交 lockfile**
- 建议在 CI 中增加 pre-commit hook 或 CI check，自动检测 lockfile 是否与 manifest 同步
- Git staging 时注意 lockfile 的行尾警告（LF/CRLF），避免跨平台 diff 噪音

---

## 经验五：MCP 服务端认证链路的多层排查

### 问题描述
通过 SSH 在远程服务器上调用 `POST /api/mcp` 的 initialize 请求，输出中出现 `authz success` 但接口返回 `401 Unauthorized`，提示"请先登录 TOF 认证中心或提供有效的 API Token"。表面看矛盾——一层认证成功了但另一层失败。

### 局限环境
- 服务端存在**两层认证**：SSH 层鉴权 + 应用层 Bearer Token 校验
- TOF 认证中间件与 API Token 认证并存
- 路由注册顺序影响中间件触发

### 项目
Shadow-Folk

### 处理时间
2026-03-30（约 1 小时，贯穿整个部署验证过程）

### 解决过程

1. **健康检查** — `curl localhost:3000/health` 返回 `{"status":"ok"}`，确认服务存活
2. **容器日志** — Docker logs 显示 `authz success`、Server running、Database initialized，服务正常
3. **源码定位** — 通过 `grep "请先登录"` 在远程容器中找到提示语位于 `tof-auth.ts:280`
4. **代码审查** — 远程读取 `index.ts`，发现 `/api/mcp` 路由虽然注册在 TOF auth 之前，但新代码尚未部署
5. **根因** — 首次测试时 HTTP MCP 端点代码还未提交/部署，请求打到了旧的路由上，被 TOF 中间件拦截
6. **部署后验证** — 代码提交 + 部署后，`/api/mcp` 正确走 Bearer Token 校验，initialize 成功

### 解决结果
- 明确了 `authz success` 来自 SSH 层，而非应用层认证
- 确认路由注册顺序的重要性：`/api/mcp` 和 `/api/push` 必须挂载在 TOF 中间件**之前**
- 建立了标准化的远程验证三步骤：health check → docker logs → curl API

**经验提炼**：
- 出现"部分认证成功但最终 401"时，先区分**哪一层**返回了成功信号
- 服务端存在多套认证（TOF + Bearer Token）时，路由注册顺序决定了请求走哪条认证链路
- 远程调试建议固化为脚本化流程，避免每次手动拼 curl

---

## 经验六：MCP 接入的最小化分发方案

### 问题描述
需要让团队其他成员（如 yuantongliu、ziyadyao）快速接入 Shadow-Folk MCP 服务，但不希望每个人都搭建本地开发环境。

### 局限环境
- 团队成员使用 Cursor IDE
- 远端开发服已部署 HTTP MCP
- 需要零本地依赖

### 项目
Shadow-Folk

### 处理时间
2026-03-31（方案设计 + 文档输出约 15 分钟）

### 解决过程

1. 确认 HTTP MCP 稳定后，设计两步接入流程：
   - **Step 1**：在 Shadow-Folk 网页端生成个人 API Token
   - **Step 2**：在 `~/.cursor/mcp.json` 中添加配置：
     ```json
     {
       "mcpServers": {
         "shadow-folk": {
           "url": "http://<server>:8080/api/mcp",
           "headers": {
             "Authorization": "Bearer sf_xxx"
           }
         }
       }
     }
     ```
2. 重启 Cursor 即可使用全部 13 个 MCP 工具

### 解决结果
- 分发成本从"安装 Node + clone + npm install"降低为**粘贴一段 JSON**
- 与现有的 plane、iWiki 等 MCP 配置格式完全一致，用户无学习成本
- 确认 stdio MCP（`shadowfolk-query-mcp`）可停止维护，后续清理 `server.ts` 中的远程模式遗留代码

**经验提炼**：
- HTTP MCP 的核心价值是**把分发成本从安装环境降低为粘贴配置**
- 对于查询型工具集，无状态 HTTP JSON 模式是最优选择
- 在 stdio → HTTP 迁移过程中，保留 stdio 入口一段时间作为过渡，待 HTTP 稳定后再清理

---

## 方法论：minusjiang 的问题解决思路

从六条经验的解决过程中，可以提炼出一套一致的问题解决方法论：

### 一、先确认"什么在正常工作"，再追"什么坏了"

minusjiang 排查问题时从不直接猜根因，而是**先证明哪些层是好的**，然后逐层收窄。

| 经验 | 他的第一步 | 为什么有效 |
|------|-----------|-----------|
| 经验二（401 排查） | 先查服务端日志，确认服务正常、请求 200 | 排除了"MCP 服务挂了"的可能 |
| 经验五（多层认证） | 先 `curl /health` 确认服务存活 | 排除了进程崩溃、端口不通 |
| 经验三（CRLF） | 第一次失败后没有怀疑远程服务，而是检查本地命令构造 | 避免了在错误方向上浪费时间 |

**核心原则**：不要从最复杂的假设入手。先用最低成本的检查排除最简单的可能性，**像剥洋葱一样从外层往内层剥**。

### 二、每次失败都是一条线索，不是浪费

经验三中，minusjiang 连续失败了三次才解决 CRLF 问题：

```
第一次失败（PowerShell 语法报错）→ 排除了"直接内联命令"这条路
第二次失败（CRLF 行尾）→ 定位到问题是文件格式而非命令逻辑
第三次失败（四层引号嵌套）→ 确认了"多层嵌套引号不可行"
第四次成功（sed 清洗）→ 最终方案
```

他没有在第一次失败后卡住，也没有反复尝试同一种方式。**每次失败都缩小了问题空间**，迫使他切换到更靠谱的路径。这不是试错，而是**有方向的排除法**。

### 三、方案先行，代码后写

经验一中，minusjiang 没有上来就写代码，而是：

1. 先明确需求边界（只改服务端、不改 Nginx/DB/Docker Compose）
2. 对比两种方案（无状态 JSON vs 有状态 SSE），用现有工具特征做判断
3. 设计共享模块拆分（tools.ts / handlers.ts / http.ts）
4. 确认设计后才进入编码

**4 小时完成从设计到部署验证**，效率来自于动手之前已经想清楚了架构。反模式是"边写边想"——对于涉及多层（路由 / 认证 / 部署）的改动，不设计直接写往往会返工。

### 四、最小改动原则

贯穿所有经验的一条隐性原则：**能不改的就不改**。

| 决策 | 他的选择 | 他没选的替代方案 |
|------|---------|----------------|
| 传输模式 | 无状态 JSON | 有状态 SSE（更复杂，需改 Nginx） |
| Nginx 配置 | 不改 | 加 proxy_buffering / 超时配置 |
| 旧 stdio 入口 | 保留，暂不删 | 立即删除（有回退风险） |
| 工具定义 | 抽离共享 | 复制一份给 HTTP 用（会分叉） |

这不是偷懒，而是工程判断——**改动越少，引入新问题的概率越低**。尤其是在远程部署环境中，每多一个变量就多一个排查维度。

### 五、噪音过滤能力

经验二中有一个关键判断：Cursor 界面的红色错误**不是真正的问题**，而是 SSE 轮询探测产生的噪音。如果被这个表象带偏，可能会花大量时间排查一个不存在的问题。

minusjiang 的做法是**先看服务端日志而不是客户端表现**。客户端 UI 可能放大噪音，但服务端日志不会撒谎。他在确认服务端一切正常后，才回过头审视客户端配置，最终从截图中一眼看到缺少 `Bearer` 前缀。

**核心原则**：当客户端报错和服务端日志矛盾时，**信服务端**。

### 六、解决完技术问题后，立刻想"怎么让别人少踩坑"

经验六不是被动触发的——不是有人抱怨接入困难，而是 minusjiang 在 HTTP MCP 上线后**主动设计了两步接入方案**并输出文档。

这个思维跳转很关键：

```
技术问题解决 → 立刻想"分发成本是什么" → 设计最低门槛的接入方式 → 文档化
```

经验二中也是同样的模式：定位了 Bearer 前缀问题后，他没有止步于"告诉 yuantongliu 改一下"，而是在经验提炼中建议**在错误提示和文档中明确给出完整格式示例**，从机制上减少同类问题。

### 方法论总结

```
minusjiang 的问题解决框架：

  ┌─────────────────────────────────────────────┐
  │  1. 先证明什么在工作（排除法，由外向内）       │
  │  2. 每次失败提取一条线索（缩小问题空间）       │
  │  3. 想清楚再动手（方案对比 → 设计确认 → 编码） │
  │  4. 改动最小化（能不动的不动）                 │
  │  5. 区分信号与噪音（信服务端，不信客户端表象）  │
  │  6. 解决后立刻降低他人踩坑成本（文档化 + 机制化）│
  └─────────────────────────────────────────────┘
```

如果用一句话概括：**先排除、再定位、想清楚再改、改完让别人也别踩坑**。

---

## 总结：关键 Takeaways

| # | 经验 | 一句话 |
|---|------|--------|
| 1 | MCP stdio → HTTP 迁移 | 无状态 JSON 模式 + 共享工具层 = 最小改动最大收益 |
| 2 | Bearer Token 401 排查 | 90% 的鉴权失败是格式问题，先查 `Authorization: Bearer ` 前缀 |
| 3 | 跨平台脚本执行 | Windows → Linux 必须 `sed 's/\r$//'`，永远不要手写四层嵌套引号 |
| 4 | Lockfile 同步 | `--frozen-lockfile` 环境下，改 package.json 必须同步提交 lockfile |
| 5 | 多层认证排查 | 先确认"哪一层成功了"，再定位"哪一层失败了" |
| 6 | MCP 零成本分发 | HTTP 模式的终极优势：用户只需粘贴一段 JSON 配置 |
