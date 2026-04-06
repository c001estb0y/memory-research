# 工程经验叙事

> 自动生成于 2026-04-05 06:57 UTC，基于 Shadow-Folk 记忆数据蒸馏重建。

## stdio 不是远程传输：从「伪远程 MCP」到真正的 HTTP MCP 直连

### 问题描述
ShadowFolk 的 MCP 接入方案被描述为「远程模式」，但实际上 Cursor 连接的仍是本地 Node.js 进程（shadowfolk-query-mcp），该进程再通过 HTTP fetch 转发请求到远端 API。每个客户端都需要本地安装 Node.js、clone 仓库、npm install，接入门槛高，且「远程模式」这一命名造成了严重的架构理解混乱。

### 局限环境
TypeScript/Node.js 后端，Hono 框架，MCP SDK（modelcontextprotocol/sdk 1.28.0），Nginx 反向代理，Cursor MCP 客户端，Docker 容器化部署，CVM 开发服务器（9.134.128.138:8080），pnpm monorepo（packages/server）

### 项目
ShadowFolk

### 处理时间
2026-03-30 至 2026-03-31，核心实现约 1 天，验证部署约半天

### 解决过程
**第 1 步（线索）：审计现有「远程模式」实现**

排查从一个命名疑问开始：现有代码里有个 `isRemoteMode` 标志，server.ts 里还有 `callRemote` 函数，但翻开代码一看，传输层仍然是 stdio。这说明所谓「远程」只是数据源是远端 API，传输层本身并没有变。

关键认知：stdio 是 POSIX 标准输入输出，物理上只能在同一台机器的进程间传递字节流。任何「通过 stdio 连接远端」的说法，背后必然隐藏着一个本地转发进程。这个隐藏的代理层正是部署负担的根源——每个 Cursor 用户都要在本地维护一个 Node.js 进程。

**第 2 步（失败尝试）：评估是否能在 stdio 层面做改造**

一度考虑过在 stdio 进程内做更多优化（缓存、连接复用），但很快意识到这是在错误的抽象层上打补丁。无论 stdio 进程内部多聪明，客户端本地必须有可执行环境这个事实无法改变。这条路放弃。

**第 3 步（转折点）：确认 HTTP MCP 是正确方向**

翻阅 MCP SDK 文档，发现 `WebStandardStreamableHTTPServerTransport` 支持两种模式：SSE 长连接和无状态 JSON。SSE 模式需要 Nginx 关闭 `proxy_buffering`、延长 `proxy_read_timeout`，还要新增 location 配置——这是额外的运维成本。

关键判断：当前所有 MCP 工具（get_shadow、search_memory 等）均为一次调用一次返回的查询型，没有流式输出或服务端主动推送需求。SSE 的复杂度没有对应收益。

更重要的是：如果把端点挂在 `/api/mcp` 而非 `/mcp`，就能直接命中现有 Nginx 的 `location /api` 规则，零运维改动。这个路径选择直接消除了基础设施改动成本。

**第 4 步（实现）：设计无状态 HTTP MCP 端点**

新增三个文件：
- `tools.ts`：集中定义 13 个工具，供 stdio 和 HTTP 两个入口共享，避免实现分叉
- `handlers.ts`：处理 CallTool 逻辑，直接调用 Service 层
- `http.ts`：`WebStandardStreamableHTTPServerTransport`，设 `sessionIdGenerator: undefined`（无状态），`enableJsonResponse: true`（JSON 模式）

关键安全决策：stdio 模式下可以信任本地 `~/.shadow/config.json` 里的 `person_id`，因为只有本机用户能启动该进程。HTTP 模式下请求来自网络，`person_id` 不能从请求参数读取，必须从经过验证的 Bearer Token 中解析。handlers.ts 里严格从 `authInfo` 获取身份，拒绝信任工具参数中的身份声明。

在 `index.ts` 中，MCP 路由挂载在 TOF 中间件之前，确保鉴权链路正确。

**第 5 步（失败：部署报错）：lockfile 过期导致 Docker 构建失败**

提交 `2f7bcf3`（feat(mcp): add HTTP MCP endpoint）后执行 `deploy-dev.sh`，Docker 镜像构建阶段失败。错误原因：引入了 MCP SDK 1.28.0 的新依赖，但 pnpm lockfile 没有同步更新。补充提交 `4779678`（chore: update lockfile）后重新部署成功。

这是一个典型的「本地可以跑但 CI/CD 挂了」问题，根因是 monorepo 下 lockfile 更新容易被遗漏。

**第 6 步（验证）：三场景冒烟测试**

通过开发服 Nginx 8080 端口验证三个场景：
1. `initialize` 握手 ✓
2. 无效 token 被拒绝（返回 401）✓
3. `tools/list` 返回 13 个工具定义 ✓

**第 7 步（客户端切换）：从 stdio 命令改为 HTTP URL**

修改 `C:\Users\minusjiang\.cursor\mcp.json`，将 shadow-folk 从 stdio 命令格式改为 `url + headers` 格式，与已有的 plane、iWiki 配置结构一致。服务地址 `http://9.134.128.138:8080/api/mcp`，Authorization 头携带 Bearer Token。重启 Cursor 后生效。

接入成本从「安装 Node.js + clone 仓库 + npm install」降为「粘贴一段 JSON 配置」。

### 解决结果
在服务端新增 `packages/server/src/mcp/http.ts`，使用 `WebStandardStreamableHTTPServerTransport` 的无状态 JSON 模式暴露 `POST /api/mcp` 端点，通过 Bearer Token 鉴权复用现有 `validateApiToken` 体系。工具定义抽离到 `tools.ts`，处理逻辑抽离到 `handlers.ts`，供 stdio 和 HTTP 两个入口共享。端点挂载在 `/api/mcp` 路径，复用现有 Nginx `/api` 代理规则，零运维改动。Cursor 客户端配置改为直连 HTTP 端点。原 stdio 中转层（shadowfolk-query-mcp）标记为待废弃，HTTP MCP 稳定后再清理 `isRemoteMode` 等遗留逻辑。

### 经验提炼
- 在描述 MCP 架构时，明确区分传输层（stdio/HTTP）和数据源（本地DB/远端API）：stdio 永远只是本地 IPC，数据源可以是远端的，但这不等于 stdio 是远程传输
- 选择 MCP 传输模式前，先评估现有 Nginx 的兼容性：JSON 模式可复用 /api 代理规则，SSE 模式需要额外配置 proxy_buffering 和超时，应作为显式成本列出
- HTTP MCP 端点的身份认证必须从 Bearer Token 中解析，不能信任请求参数中的 person_id——迁移传输层时必须同步迁移信任模型
- 将工具定义和处理逻辑抽离到共享模块（tools.ts/handlers.ts），避免 stdio 和 HTTP 两条路径产生实现分叉
- monorepo 中引入新依赖后，部署前检查 lockfile 是否同步更新，避免 Docker 构建失败
- 架构演进时为被替代方案设置明确的废弃时间线，避免两套方案长期并存增加维护负担
- 新增 MCP transport 时同步补充集成测试脚本，覆盖 initialize、invalid token 拒绝、tools/list 三个基础场景作为部署验收门禁

**方法论标签**: 传输层与业务层解耦 / 第一性原理：能不改基础设施就不改 / 信任模型随安全边界变化而变化 / 路径选择驱动运维成本最小化 / lockfile 一致性检查

---

## MCP get_projects 返回项目不完整：shadow 依赖掩盖了权限模型的真实边界

### 问题描述
用户 ziyadyao 通过前端能看到项目 tank，但 MCP 的 get_projects 接口不返回该项目，导致 AI Agent 在执行「查找 ziyadyao 在 tank 项目的工作内容」时，进行了 7 轮、20+ 次工具调用（search_project_memories、get_project_tasks 等），全部返回空结果。事后分析发现失败原因不是关键词选择问题，而是 tank 的 project_id 根本不在 Agent 的可访问项目列表中。

### 局限环境
TypeScript/Node.js 后端，MCP server（packages/server/src/mcp/server.ts），项目权限模型（owner/member/org_members），shadow 机制，PostgreSQL，Cursor MCP 客户端

### 项目
ShadowFolk

### 处理时间
2026-03-31，问题发现于 Agent 执行失败的 BadCase 分析，修复约半天

### 解决过程
**第 1 步（触发）：Agent 20+ 次调用全部失败，排查开始**

Agent 被要求查找「ziyadyao 在 tank 项目的工作内容」。观察到 Agent 先调用 `get_projects` 获取项目列表，拿到 7 个项目，然后开始对其中某个父项目反复调用 `search_project_memories`，换了十几个关键词，全部返回空数组。

初步假设：关键词选择有问题，或者 ziyadyao 确实没有在该项目留下记忆记录。

**第 2 步（失败尝试）：调整关键词策略**

尝试让 Agent 用更宽泛的关键词（如 ziyadyao 的用户名、邮箱片段），仍然全部返回空。换用 `get_project_tasks` 也没有结果。

此时注意到一个细节：Agent 始终在用同一个 project_id 调用，而这个 project_id 对应的是父项目，不是 tank。

**第 3 步（转折点）：对比前端可见项目与 MCP 返回项目**

直接调用 `get_projects`，返回 7 个项目，逐一比对前端项目列表。发现 tank 不在 MCP 返回的列表里。这是关键转折：问题不是搜索逻辑，而是项目发现能力存在盲区。

**第 4 步（根因定位）：审计 get_projects 实现**

翻开 get_projects 的查询逻辑，发现它依赖 shadow 记录作为项目可见性的判断条件——只有「创建过 shadow」的用户才能在该接口看到对应项目。

问题链条：
- ziyadyao 对 tank 有项目权限（前端可见）
- 但 ziyadyao 未在 tank 下创建过 shadow 记录
- get_projects 的 shadow 依赖导致 tank 不被枚举
- 此外 tank 作为子项目，子项目枚举逻辑也存在缺失

**第 5 步（噪音识别）：「关键词不对」是误导性假设**

20+ 次调用失败后，最容易归因的方向是「Agent 的关键词策略有问题」。但这是噪音——当工具集存在系统性盲区时，增加重试次数和调整关键词都无法突破接口边界限制。识别这个噪音的关键信号是：所有调用都指向同一个错误的 project_id，而不是随机分散的失败。

**第 6 步（修复）：引入 ProjectAccessService**

重写 get_projects 查询逻辑，按「有项目权限即可返回」原则，统一覆盖三种访问来源：
1. 用户创建的项目（owner）
2. 用户被直接邀请加入的项目（member）
3. 用户所在组织下的项目（org_members 继承）

引入 `ProjectAccessService`（project-access.service.ts），提供 `getAccessibleProjects` 和 `hasProjectAccess` 两个核心方法，将组织成员继承逻辑收敛其中，解耦对 shadow 的强依赖。将 projects.ts、push.ts、shadows.ts、mcp/server.ts 中分散的权限判断全部替换为对该 Service 的统一调用。

### 解决结果
引入集中式 `ProjectAccessService`，将 owner/shadow/org-member 三种访问来源的判断逻辑统一封装，暴露 `getAccessibleProjects` 和 `hasProjectAccess` 方法。重写 get_projects 查询逻辑，以权限模型为唯一数据源，不再依赖 shadow 关联关系作为可见性判断依据。中期补充按人员跨项目检索能力，允许在不指定 project_id 的情况下搜索某人的记忆，从工具能力层面补全发现路径。

### 经验提炼
- 当 Agent 多轮调用全部失败时，先检查工具的「发现能力」覆盖范围，而不是优化关键词策略——系统性盲区无法通过重试突破
- 设计项目枚举接口时，以权限模型为唯一数据源，禁止引入 shadow 等中间状态作为可见性判断依据
- 保证「发现工具」和「访问工具」的覆盖范围严格一致：如果访问工具需要 project_id，发现工具必须能返回所有用户有权访问的 project_id
- 权限判断是横切关注点，必须单点实现：新增 ProjectAccessService，禁止在业务逻辑层内联权限判断代码
- 上线前补充「无 shadow 用户」「组织成员」「直接项目成员」等边界场景的集成测试
- Code Review 阶段检查新增接口是否复用了统一权限服务，拒绝绕过统一权限层的直接数据库查询
- 为 Agent 工具集建立覆盖率测试：枚举前端可见的所有资源，验证工具链能否完整发现并访问

**方法论标签**: 系统性盲区识别优先于局部优化 / 权限模型单一数据源原则 / 横切关注点集中实现 / 发现能力与访问能力对称性 / BadCase 驱动接口能力补全

---

## CRLF 换行符让 curl 命令「凭空消失」——Windows 脚本上传 Linux 的隐形陷阱

### 问题描述
通过 SSH 在远程 Linux 主机上执行 MCP HTTP 接口测试脚本，三个测试用例（initialize、invalid token、tools/list）全部未正确执行。奇怪的是，输出中确实出现了 authz success 字样，看起来像是部分逻辑跑通了，但 curl 请求的响应体完全不对，-H、-d 等参数像是没有被传入。

### 局限环境
Windows 本地编辑器（VS Code/Cursor）+ SCP 上传 + 远程 Linux 主机 + bash + curl + MCP HTTP endpoint

### 项目
shadow-folk / MCP 服务端接口测试

### 处理时间
单次排查，约 30 分钟

### 解决过程
1. [失败] 首先怀疑是 MCP 服务端本身的问题——authz success 出现在输出里，说明服务端至少在响应，于是检查服务端日志，日志显示根本没有收到完整的 HTTP 请求，只有零散的字符串命中了路由。这不像是业务逻辑 bug。

2. [线索] 重新看脚本输出，发现报错里出现了 `$'\r': command not found` 这样的字样。\r 是回车符，这个错误信息在 Linux bash 里意味着 bash 把行尾的 \r 当成了命令名的一部分。这是第一个真正有价值的信号。

3. [噪音识别] authz success 的出现是误导性噪音——它让人以为脚本「跑了一部分」，实际上那行输出是脚本里某个单行命令（恰好没有行继续符）碰巧被正确解析了，而多行 curl 命令因为 \ 行继续符失效被拆散，-H 和 -d 变成了独立的「命令」被 bash 尝试执行，自然失败。

4. [确认根因] 用 `cat -A /tmp/test-mcp.sh` 查看文件，每行末尾都出现了 `^M`，这是 CRLF 中 \r 的可见形式。确认脚本是在 Windows 下编辑后通过 SCP 上传的，编辑器默认保存为 CRLF，Linux bash 不容忍行尾 \r，行继续符 \ 后面跟着 \r 导致续行失效，多行 curl 命令被拆散成碎片。

5. [成功] 在远端执行 `sed -i 's/\r//' /tmp/test-mcp.sh`，再次运行脚本，三个测试用例全部正常执行，curl 请求和响应均符合预期。

### 解决结果
上传脚本后在远端执行 `sed -i 's/\r//' <script_path>` 去除 CRLF，或在上传前本地用 `dos2unix` 转换。根本预防措施：在 Git 仓库根目录配置 `.gitattributes`，对 `*.sh` 文件强制 `eol=lf`，确保无论在哪个平台提交，shell 脚本始终以 LF 入库。

### 经验提炼
- 看到 `$'\r': command not found` 或 `command not found` 后面跟着奇怪字符，立刻用 `cat -A` 检查文件行尾，不要继续往业务逻辑方向排查
- 多行 curl 命令执行异常时，优先怀疑行继续符 \ 是否被破坏，而不是参数拼写
- 从 Windows 上传到 Linux 的脚本，上传后第一步加 `sed -i 's/\r//' <file>`，养成习惯
- 用 authz 日志等服务端信号判断「请求是否到达」，而不是用客户端脚本输出判断「命令是否执行」——两者是独立的排查维度
- 在 .gitattributes 中为 *.sh 配置 eol=lf，把行尾策略纳入版本控制，而不是依赖个人编辑器设置

**方法论标签**: 可见字符诊断法 / 噪音隔离 / 环境假设验证 / 根因 vs 表象分离

---

## PowerShell 里的 && 是语法错误，不是「命令失败」——shell 环境识别的必修课

### 问题描述
在 Windows PowerShell 环境中，多次尝试用 `&&` 串联 git、scp、ssh 命令，每次都报 `ParserError: InvalidEndOfLine`，命令完全不执行。同样的命令在 Git Bash 里一行就过。这个问题在同一个项目里反复出现了至少三次，涉及 git commit、scp+ssh 部署、健康检查等不同场景。

### 局限环境
Windows 10/11 + PowerShell 5.x + Git + SCP + SSH + curl

### 项目
shadow-folk / Windows 开发环境日常操作

### 处理时间
多次独立触发，每次 5-15 分钟

### 解决过程
**第一次（git add && git commit）**
1. [失败] 直接执行 `git add pnpm-lock.yaml && git commit -m 'chore: ...'`，报 `ParserError: InvalidEndOfLine`。第一反应是命令拼写有问题，检查了一遍参数，没发现错误。
2. [线索] 错误信息是 ParserError，不是命令执行失败——这说明 PowerShell 在解析阶段就放弃了，根本没有尝试执行任何命令。
3. [转折] 意识到 `&&` 在 PowerShell 5.x 中是不合法的语法（PowerShell 7+ 才引入），不是「命令失败后继续」，而是「解析器直接报错」。
4. [成功] 拆成两行：先 `git add pnpm-lock.yaml`，再 `git commit ...`，两条命令均正常执行。

**第二次（scp && ssh 部署）**
1. [失败] 在本地生成的 .ps1 脚本中内联了 `scp ... && ssh ...`，脚本在解析阶段就失败，从未发起任何网络连接。排查时一度以为是网络问题或 SSH 密钥问题。
2. [噪音识别] 网络和密钥是噪音——错误发生在 PowerShell 解析阶段，连 TCP 握手都没有发生，用网络工具排查是浪费时间。
3. [成功] 将 scp 和 ssh 拆为独立命令分步执行，或封装为 .sh 文件通过 `bash.exe` 调用。

**第三次（curl 健康检查）**
1. [失败] 执行 `curl -s --connect-timeout 5 http://...`，报「参数无法绑定」。以为是 curl 版本问题或 URL 格式问题。
2. [转折] 发现 PowerShell 中 `curl` 是 `Invoke-WebRequest` 的别名，`-s` 和 `--connect-timeout` 是 Unix curl 参数，对 `Invoke-WebRequest` 完全无效。
3. [成功] 改用 `curl.exe` 显式调用 Windows 系统安装的 curl 二进制，参数完全兼容。

### 解决结果
在 PowerShell 环境中：① 用 `;` 或分行替代 `&&`；② 用 `curl.exe` 替代 `curl`；③ 用 PowerShell here-string `@'...'@` 或 `git commit -F <file>` 替代 bash heredoc；④ 统一使用 Git Bash 或 WSL 作为 shell 脚本执行环境，避免在 PowerShell 中内联 POSIX 语法。

### 经验提炼
- 看到 ParserError 先判断当前 shell 类型，不要往命令参数方向排查
- 在 PowerShell 中统一用 `curl.exe` 而非 `curl`，或在脚本顶部加 `Remove-Item Alias:curl`
- Windows 开发机上将 Git Bash 或 WSL 设为默认终端，减少 PowerShell/bash 语法混用的概率
- 编写自动化脚本前先用 `$PSVersionTable` 或 `$SHELL` 检测当前 shell 环境，再选择对应语法
- PowerShell 的 `&&` 问题在 5.x 和 7+ 行为不同，跨版本脚本应避免依赖此操作符
- 把 bash heredoc 和 `$()` 命令替换视为 PowerShell 的红色警报语法，遇到立刻改写

**方法论标签**: 环境识别优先 / 解析错误 vs 运行错误区分 / 别名陷阱 / 语法兼容性矩阵

---

## 远程脚本执行双重失败：CRLF + 错误数据库文件，两个无关问题叠加的排查策略

### 问题描述
在远程 Linux 主机上执行数据库查询脚本，遭遇两个同时出现的错误：一是 bash 报 `$'\r': command not found`，脚本解析失败；二是即使脚本能跑，查询结果显示 projects、shadows、org_members 等预期表不存在。两个问题同时出现，初期难以判断是同一根因还是独立问题。

### 局限环境
Windows 本地 + SCP + 远程 Linux + bash + SQLite3

### 项目
shadow-folk / 数据库状态验证

### 处理时间
单次排查，约 45 分钟

### 解决过程
1. [失败] 第一反应是数据库 schema 问题——表不存在，怀疑是迁移没有跑完。准备去查迁移日志，但脚本连解析都过不了，无法执行任何查询。

2. [拆分问题] 意识到两个错误需要独立处理，不能混在一起排查。先解决脚本能不能跑，再解决查询结果对不对。这是这次排查最关键的思维转折。

3. [解决 CRLF] 用 `cat -A` 确认脚本行尾为 `^M`（CRLF），执行 `sed -i 's/\r//' <script>`，脚本解析恢复正常。这个问题已经是第二次遇到，处理很快。

4. [失败] 脚本跑通后，查询 shadow.db，结果是空的——不是表不存在，而是数据库本身是空文件。用 `ls -la` 查看，shadow.db 大小为 0 字节。

5. [噪音识别] 「表不存在」的错误信息是噪音——它让人以为是 schema 问题，实际上是文件选错了。0 字节的数据库文件会让所有表查询都返回「表不存在」，和 schema 迁移失败的表现完全一样，但根因完全不同。

6. [成功] 用 `ls -la *.db` 列出所有数据库文件，发现实际数据在 sqlite.db（大小非零），shadow.db 是一个空的占位文件。切换到 sqlite.db 后，所有预期表均存在，查询正常。

### 解决结果
1. SCP 传输前用 `dos2unix` 或 `sed -i 's/\r//'` 处理行尾；2. 执行数据库查询前用 `ls -la *.db` 确认目标文件 size > 0，用 `sqlite3 <db> .tables` 确认 schema 包含预期表，再执行业务查询。

### 经验提炼
- 多个错误同时出现时，先拆分为独立问题，逐一击破，不要试图找一个统一根因
- 执行数据库查询前先 `ls -la` 确认目标文件非空，0 字节文件会伪装成 schema 缺失
- 「表不存在」错误出现时，先验证数据库文件本身是否有效，再查 schema 迁移状态
- 建立远程脚本执行 checklist：① 行尾 LF；② 目标文件 size > 0；③ `.tables` 确认 schema
- CRLF 问题第二次遇到应该 30 秒内解决，建立肌肉记忆：上传脚本 → 立刻 sed 去 CRLF

**方法论标签**: 问题拆分 / 文件状态验证优先 / 错误信息去噪 / checklist 驱动排查

---

## git add 的 CRLF 警告不是噪音——.gitattributes 是跨平台项目的基础设施

### 问题描述
在 Windows 环境下执行 `git add` 时，push.ts 文件触发了 `LF will be replaced by CRLF` 警告。这个警告在日常开发中很容易被忽视，但在跨平台团队协作和 CI 环境中，它会持续产生无意义的 diff 噪音，甚至让 Code Review 充斥着行尾变更，掩盖真实的代码改动。

### 局限环境
Windows 开发环境 + Git + TypeScript 项目 + GitHub CI

### 项目
shadow-folk / 跨平台开发规范

### 处理时间
预防性配置，非紧急问题

### 解决过程
1. [观察] git add 时出现 `warning: LF will be replaced by CRLF in packages/server/src/api/push.ts`。这个警告本身不阻断操作，很多人选择忽略。

2. [预判影响] 如果不处理，后果是：Windows 开发者提交的文件在仓库中为 LF，本地存储为 CRLF；Mac/Linux 开发者 checkout 后文件为 LF；Windows 开发者下次 checkout 后文件变回 CRLF。每次 Windows 开发者修改文件，diff 里都会混入大量行尾变更，Code Review 无法聚焦真实改动。

3. [方案选择] 有三个层次的解决方案：① 每个开发者配置本地 `core.autocrlf`（治标，依赖个人配置，不可靠）；② 在 CI 中加行尾检查（亡羊补牢，报错时已经污染了提交历史）；③ 在仓库根目录配置 `.gitattributes`（治本，仓库级策略，优先级高于本地配置，所有贡献者和 CI 行为一致）。

4. [执行] 在 `.gitattributes` 中添加 `*.ts text eol=lf`，同时建议团队 Windows 开发者配置 `core.autocrlf=true`，Mac/Linux 配置 `core.autocrlf=input`。

### 解决结果
在仓库根目录创建或更新 `.gitattributes`，对 `*.ts`、`*.sh`、`*.json` 等文本文件明确指定 `eol=lf`。这是仓库级别的行尾策略，优先级高于所有本地 Git 配置，一次配置，全员生效。

### 经验提炼
- 新建跨平台项目第一步就配置 .gitattributes，不要等到出现行尾 diff 噪音再补救
- 把 .gitattributes 视为基础设施而非可选配置，和 .gitignore 同等地位
- CRLF 警告出现时不要忽略，立刻评估是否需要配置 .gitattributes
- 对 *.sh 文件强制 eol=lf 尤其重要，CRLF 的 shell 脚本在 Linux 上会静默失败

**方法论标签**: 根治 vs 治标 / 仓库级策略优先于本地配置 / 预防性基础设施配置

---

## PowerShell → SSH → Node.js 四层嵌套引号地狱：从内联脚本到文件传输的血泪转变

### 问题描述
需要通过 PowerShell 脚本远程 SSH 到服务器，执行一段 Node.js 内联脚本（node -e '...'）来完成某项自动化任务。脚本在本地测试时逻辑正确，但一旦放入 SSH 命令字符串中执行，就报各种语法错误，且错误信息指向的行号和内容与实际 JS 代码对不上，完全无法定位问题。

### 局限环境
Windows 本地 PowerShell 5.1 / PowerShell 7，远程服务器 Ubuntu 22.04 + bash，Node.js 18，部分场景涉及 Docker exec 链路，无 CI/CD 平台介入，纯手工脚本自动化

### 项目
workflow 内部自动化工具链

### 处理时间
单次排查耗时约 3-4 小时，期间经历多轮转义尝试后才彻底转换思路

### 解决过程
**第 1 步：直接内联执行（失败，产生第一条线索）**

最初写法是最自然的：
```powershell
ssh user@host "node -e 'console.log(require(\"fs\").readFileSync(\"/etc/hostname\", \"utf8\"))'"
```
报错是 `SyntaxError: Invalid or unexpected token`，但 Node 报错指向的是第 1 列第 1 个字符，而不是任何引号位置。**线索**：错误不像是 JS 语法错误，更像是 Node 根本没收到完整字符串。

**第 2 步：怀疑是双引号转义问题，改用单引号包裹（失败，产生噪音）**

改成：
```powershell
ssh user@host 'node -e "console.log(require(fs).readFileSync('/etc/hostname', 'utf8'))"'
```
PowerShell 直接在本地报错，命令根本没发出去。**噪音识别**：这个报错让我一度以为是 PowerShell 版本问题，花了约 30 分钟对比 PS5 和 PS7 的引号行为，实际上两者在这个场景下表现一致，版本差异是个假线索。

**第 3 步：尝试转义地狱——逐层手工转义（失败，线索升级）**

参考 PowerShell 文档，尝试用反引号转义双引号，用 `\"` 转义 SSH 层，用 `\\"` 转义 bash 层：
```powershell
ssh user@host "node -e `"require('fs').readFileSync('/etc/hostname','utf8')`""
```
这次命令发出去了，但远程报 `bash: node: command not found`，说明 bash 把整个字符串解析成了命令名而不是参数。**关键线索**：错误层级从「本地 PS 解析」变成了「远程 bash 解析」，说明 PowerShell → SSH 这一跳已经通了，问题在 SSH → bash → node 这段。

**第 4 步：加上 bash -c 包裹（部分成功，暴露真正复杂度）**

```powershell
ssh user@host "bash -c \"node -e 'console.log(1)'\""
```
简单的 `console.log(1)` 跑通了。但一旦 JS 代码里出现任何字符串字面量（哪怕是 `'hello'`），立刻又崩。**思维转折点**：这一步让我意识到，问题不是某一层转义写错了，而是**四层嵌套（PowerShell → SSH 参数 → bash -c → node -e）本质上无法安全承载含引号的代码**。每一层都有自己的引号语义，JS 代码里的单引号、双引号、模板字符串反引号，在任意一层都可能被提前消费掉。这不是转义技巧问题，是结构性问题。

**第 5 步：尝试 base64 编码绕过引号（可行但引入新问题）**

把 JS 代码 base64 编码后传入：
```powershell
$code = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($jsCode))
ssh user@host "echo $code | base64 -d | node"
```
引号问题消失了，但调试体验极差——报错时看不到原始代码，行号无意义，且 `$code` 变量在 PowerShell 字符串插值时如果含特殊字符仍会出问题。**判断**：这是一个 workaround，不是解法，不应该进入团队规范。

**第 6 步：切换思路——文件是代码的正确载体（成功）**

将 JS 脚本写入本地临时文件，scp 传输，ssh 执行：
```powershell
$script = @'
const fs = require('fs');
const data = fs.readFileSync('/etc/hostname', 'utf8');
console.log(data.trim());
'@
$tmpFile = [System.IO.Path]::GetTempFileName() + '.js'
$script | Out-File -FilePath $tmpFile -Encoding utf8
scp $tmpFile user@host:/tmp/remote_script.js
ssh user@host 'node /tmp/remote_script.js'
Remove-Item $tmpFile
```
动态参数通过环境变量注入：`ssh user@host 'TARGET_DIR=/data node /tmp/remote_script.js'`。完全没有引号冲突，JS 代码原样保留，报错行号准确，调试体验与本地无异。

### 解决结果
放弃所有内联脚本方式，统一改为「本地写文件 → scp 传输 → ssh 执行文件」三步流程。动态参数通过环境变量或 node 的 process.argv 传入，不内联到脚本字符串中。对于需要动态生成脚本内容的场景（如根据配置生成不同逻辑），在本地用 PowerShell 生成完整 .js 文件后再传输，而不是在 SSH 命令字符串里拼接 JS 代码。

### 经验提炼
- 遇到多层 shell 嵌套的引号报错，先用最简单的无引号代码验证每一跳是否通畅，再逐步引入复杂度，快速定位是哪一层在消费引号
- 识别「转义地狱」的信号：当你需要数超过两层的反斜杠时，停下来，这是结构性问题而不是技巧问题
- 用文件传递代码，用参数传递数据——任何超过单行的脚本都不应该通过命令行字符串内联传递给解释器
- base64 编码是绕过引号的临时手段，不要让它进入长期维护的脚本，调试成本会在未来加倍偿还
- 排查多层链路问题时，先确认错误发生在哪一层（本地解析 vs 远程执行），再针对性处理，不要在错误层上浪费时间
- 制定团队规范：远程执行超过一行的脚本，强制使用文件传输方式，在代码审查中拦截 ssh host 'node -e ...' 模式

**方法论标签**: 分层隔离调试（逐层验证，定位故障层） / 噪音识别（版本差异是假线索，引号语义冲突是真根因） / 结构性问题 vs 技巧性问题的判断 / 最小可复现路径（用无引号代码验证链路连通性） / 载体适配原则（代码用文件，数据用参数）

---

## 方法论总结

这两条经验共享同一个底层模式：**命名掩盖了实现的真实边界，导致系统性误判**。「远程模式」掩盖了本地代理的存在，「shadow 可见性」掩盖了权限模型的真实边界。排查的核心动作都是：放弃表象描述，回到第一性原理重新定义边界——传输层的物理约束是什么？权限的唯一数据源是什么？

两条经验还共同指向「分散实现的脆弱性」：无论是分散在各端点的权限判断，还是 stdio/HTTP 两条路径各自维护工具逻辑，分散都会在系统演进时产生不一致。解法也是一致的：识别横切关注点，强制收敛到单一抽象层（ProjectAccessService、tools.ts），让变更只需发生在一处。

方法论提炼：**先画清边界（传输层/权限层），再收敛实现（单一服务/共享模块），最后验证覆盖（集成测试/发现-访问对称性检查）**。

这批经验的共同主线是「环境假设失效」——开发者在 Windows 环境下编写的脚本、命令、配置，在传递到 Linux 或被 PowerShell 解析时，触发了一系列静默失败或误导性报错。归纳出三条核心方法论：

**1. 先识别执行环境，再写命令**
在动手之前，明确当前 shell 是 PowerShell 还是 bash，当前文件系统是 Windows 还是 Linux。PowerShell 的 `&&`、`curl` 别名、heredoc 语法问题，以及 bash 的 CRLF 问题，本质上都是「环境假设错误」——用了当前环境不支持的语法或工具。

**2. ParserError = 停止，RuntimeError = 继续排查**
PowerShell 的 ParserError 意味着命令根本没有执行，任何关于「命令结果」的排查都是无效的。bash 的 `$'\r': command not found` 同理。遇到解析级错误，第一步是修复语法，而不是排查业务逻辑。

**3. 多问题叠加时先拆分，再逐一击破**
当多个错误同时出现（如 CRLF + 错误数据库文件），不要试图找统一根因。先用最小代价确认每个错误是否独立，再分别处理。这比试图一次性找到「大统一解释」效率高得多。

**预防层面**：.gitattributes 的 `eol=lf` 配置、Git Bash/WSL 作为统一执行环境、上传脚本后立刻 `sed -i 's/\r//'`——这三件事能预防这批经验中 80% 的问题重现。
