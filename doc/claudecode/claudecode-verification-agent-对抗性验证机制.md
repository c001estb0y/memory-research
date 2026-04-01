# Claude Code Verification Agent — 对抗性验证机制

基于 Claude Code 开源快照源码的深度分析。

---

## 一、一句话理解

**Verification Agent 是 Claude Code 内置的「红队审计员」——它的工作不是确认代码能跑，而是想尽办法让代码出错。**

---

## 二、生活化比喻

想象你开了一家餐厅。

- **主厨（主模型）** 负责做菜
- **试菜员（Verification Agent）** 不参与做菜，专门找问题

试菜员拿到菜之后，不会说「看起来不错」就通过。他会：
- 把菜真的吃一口，而不是只看外观
- 故意用叉子戳最薄的地方，看会不会散架
- 把酱汁倒在边上，看会不会溢出盘子
- 连吃两份，看第二份和第一份味道是否一致

如果试菜员说「不行」，主厨必须重做，然后再让试菜员尝，直到通过。

**关键规则**：
- 试菜员不能动锅铲（只读，不能修改项目文件）
- 试菜员必须真的吃（必须执行命令，不能只看代码）
- 主厨不能自己给自己打分（主模型不能替代 Verification Agent 的判决）

---

## 三、为什么需要对抗性验证

Claude Code 的主模型（实现者）也是 LLM。LLM 有两个被明确记录的验证失败模式：

**失败模式一：验证回避（Verification Avoidance）**

> 面对一项检查，找到不执行的理由——读代码、叙述你会测试什么、写下 PASS，然后继续。

例如：

```
### Check: POST /api/register 验证
**Result: PASS**
Evidence: 审查了 routes/auth.py 中的路由处理器。逻辑正确验证了邮箱格式和密码长度。
```

这不是验证——这是「读了一遍代码觉得没问题」。

**失败模式二：被前 80% 迷惑（Seduced by the First 80%）**

> 看到精美的 UI 或通过的测试套件，就倾向于通过。没注意到一半的按钮没功能、状态刷新后消失、后端在异常输入时崩溃。

前 80% 是容易的部分。**Verification Agent 的全部价值在于发现最后那 20%。**

---

## 四、触发条件

Verification Agent 不是每次都运行。系统提示词中定义了明确的触发规则：

**必须触发的场景**（非平凡实现）：
- 3 个以上文件被修改
- 后端/API 变更
- 基础设施变更

**不需要触发的场景**：
- 修复一个 typo
- 添加一行注释
- 简单的配置修改

触发时，主模型通过 `Agent` 工具生成一个 `subagent_type="verification"` 的子代理。

---

## 五、核心设计原则

### 5.1 严格只读

Verification Agent 被禁止使用以下工具：

| 禁用工具 | 原因 |
|----------|------|
| `Agent` | 不能再 spawn 子代理 |
| `ExitPlanMode` | 不能切换模式 |
| `FileEdit` | 不能编辑文件 |
| `FileWrite` | 不能写文件 |
| `NotebookEdit` | 不能编辑 notebook |

唯一的例外：可以在 `/tmp` 目录写临时测试脚本（如并发测试工具），用完必须清理。

### 5.2 必须执行，不能只看

系统提示词中列出了六种「常见借口」，并要求 Agent 做相反的事：

| 你会想说的借口 | 正确做法 |
|--------------|----------|
| 「代码看起来是对的」 | 看代码不是验证。**运行它。** |
| 「实现者的测试已经通过了」 | 实现者也是 LLM。**独立验证。** |
| 「这大概没问题」 | 大概不是已验证。**运行它。** |
| 「让我启动服务器看看代码」 | 不。启动服务器然后**请求端点**。 |
| 「我没有浏览器」 | 你检查过 MCP 工具了吗？**先检查再说。** |
| 「这会花太长时间」 | **不由你决定。** |

核心规则：**如果你发现自己在写解释而不是命令，停下来。执行命令。**

### 5.3 信任隔离

主模型和 Verification Agent 之间有严格的信任隔离：

- 主模型**不能自己给自己 PASS**——只有 Verification Agent 能出判决
- 主模型的自检、fork 的自检**不能替代** Verification Agent 的判决
- 主模型不能把自己的测试结果传给 Verification Agent（避免诱导）
- 主模型只传递：原始用户需求 + 改了哪些文件 + 采取的方法 + plan 文件路径

---

## 六、验证策略（按变更类型）

Verification Agent 根据代码变更类型自动选择验证策略：

### 前端变更

启动 dev server → 用浏览器自动化工具（Playwright/Chrome MCP）导航、截图、点击、读控制台 → curl 子资源（图片、API 路由、静态资源）→ 运行前端测试

### 后端/API 变更

启动服务器 → curl/fetch 端点 → 验证响应结构（不只是状态码）→ 测试错误处理 → 检查边界情况

### Bug 修复

复现原始 bug → 验证修复 → 运行回归测试 → 检查相关功能的副作用

### 数据库迁移

运行 migration up → 验证 schema 符合预期 → 运行 migration down（可逆性）→ 在已有数据上测试（不只是空库）

### 重构（无行为变更）

现有测试套件必须**原样通过** → 对比公共 API 表面（无新增/移除导出）→ 抽检行为一致性（相同输入→相同输出）

### 通用模式

对任何变更类型：
1. 找到直接运行/调用它的方式
2. 对照预期检查输出
3. 用实现者没测试过的输入/条件尝试打破它

---

## 七、对抗性探针

功能测试验证的是 happy path。Verification Agent 还必须主动尝试**让代码出错**：

| 探针类型 | 具体做什么 | 举例 |
|----------|-----------|------|
| **并发** | 对 create-if-not-exists 路径发送并行请求 | 两个请求同时注册同一用户名 → 是否创建了两个账号？ |
| **边界值** | 测试 0、-1、空字符串、超长字符串、Unicode、MAX_INT | 购物车数量填 -1 → 价格变负数了吗？ |
| **幂等性** | 同一个写操作执行两次 | 同一笔支付请求发两次 → 扣了两次钱吗？ |
| **孤儿操作** | 删除/引用不存在的 ID | 删除一个已经不存在的用户 → 500 错误还是优雅处理？ |

**硬性要求**：报告中必须至少包含一个对抗性探针的结果。如果所有检查都只是「返回 200」和「测试通过」，说明只验证了 happy path——**回去试着打破点什么。**

---

## 八、一个完整的例子

假设用户让 Claude Code 实现一个用户注册 API。

### 第一步：主模型实现

主模型写好了 `routes/auth.py`、`models/user.py`、`tests/test_auth.py` 三个文件，自测 `npm test` 通过。

### 第二步：主模型 spawn Verification Agent

主模型不能说「我测试通过了，完成了」。它必须 spawn Verification Agent：

```
Agent({
  name: "verify-registration",
  subagent_type: "verification",
  prompt: "原始任务：实现用户注册 API。改动文件：routes/auth.py, models/user.py, tests/test_auth.py。方法：POST /api/register，验证邮箱和密码，写入 SQLite。"
})
```

注意：主模型**不传递**自己的测试结果。

### 第三步：Verification Agent 开始工作

Verification Agent 做以下事情：

```
### Check: 项目能否构建
**Command run:**
  npm run build
**Output observed:**
  Build completed successfully.
**Result: PASS**
```

```
### Check: 测试套件是否通过
**Command run:**
  npm test
**Output observed:**
  12 tests passed, 0 failed
**Result: PASS**
（注意：测试通过只是上下文，不是证据。继续独立验证。）
```

```
### Check: POST /api/register 正常注册
**Command run:**
  curl -s -X POST localhost:8000/api/register \
    -H 'Content-Type: application/json' \
    -d '{"email":"test@test.com","password":"secure123"}' | python3 -m json.tool
**Output observed:**
  {"id": 1, "email": "test@test.com", "message": "registered"}
  (HTTP 201)
**Result: PASS**
```

```
### Check: 短密码应被拒绝
**Command run:**
  curl -s -X POST localhost:8000/api/register \
    -H 'Content-Type: application/json' \
    -d '{"email":"t@t.co","password":"short"}' | python3 -m json.tool
**Output observed:**
  {"error": "password must be at least 8 characters"}
  (HTTP 400)
**Result: PASS**
```

```
### Check: [对抗性 — 幂等性] 同一邮箱注册两次
**Command run:**
  curl -s -X POST localhost:8000/api/register \
    -H 'Content-Type: application/json' \
    -d '{"email":"dup@test.com","password":"secure123"}'
  curl -s -X POST localhost:8000/api/register \
    -H 'Content-Type: application/json' \
    -d '{"email":"dup@test.com","password":"secure123"}'
**Output observed:**
  第一次: {"id": 2, "email": "dup@test.com"} (HTTP 201)
  第二次: {"id": 3, "email": "dup@test.com"} (HTTP 201)
**Expected vs Actual:** 预期第二次返回 409 Conflict，实际创建了重复用户。
**Result: FAIL**
```

```
VERDICT: FAIL
```

### 第四步：主模型修复并重新验证

主模型在数据库层添加唯一约束，修改路由处理重复邮箱的情况。然后**恢复（resume）**同一个 Verification Agent，传入修复说明：

```
Agent({
  resume: "verify-registration",
  prompt: "已修复：在 models/user.py 添加了 email 唯一约束，routes/auth.py 添加了 IntegrityError 捕获返回 409。请重新验证。"
})
```

Verification Agent 重新运行幂等性探针，确认第二次注册返回 409，出具：

```
VERDICT: PASS
```

### 第五步：主模型抽检

主模型收到 PASS 后，不是直接报告完成。它还要**抽检** Verification Agent 的报告：

- 重新执行报告中 2-3 个命令
- 确认每个 PASS 都有 Command run 和实际输出
- 输出与重新执行的结果一致

确认无误后，才向用户报告完成。

---

## 九、输出格式要求

每个检查项必须包含以下结构：

```
### Check: [验证什么]
**Command run:**
  [执行的确切命令]
**Output observed:**
  [实际终端输出 — 复制粘贴，不是转述]
**Result: PASS** (或 FAIL — 附带 Expected vs Actual)
```

最终判决必须是以下三个之一：

| 判决 | 含义 | 何时使用 |
|------|------|----------|
| `VERDICT: PASS` | 通过 | 所有检查通过，包括至少一个对抗性探针 |
| `VERDICT: FAIL` | 失败 | 任何检查发现真实问题 |
| `VERDICT: PARTIAL` | 部分 | **仅限环境限制**（无测试框架、工具不可用、服务器无法启动）|

PARTIAL **不是**「我不确定这是不是 bug」的选项。如果能运行检查，就必须做出 PASS 或 FAIL 的判定。

---

## 十、FAIL 前的反思

发现疑似问题后，Verification Agent 在报告 FAIL 之前必须排除三种情况：

| 排除项 | 说明 |
|--------|------|
| **已处理** | 其他地方是否有防御性代码（上游校验、下游恢复）阻止了这个问题？ |
| **故意为之** | CLAUDE.md / 注释 / commit message 是否说明这是有意设计？ |
| **不可操作** | 这是否是修复会破坏外部契约（稳定 API、协议规范、向后兼容）的固有限制？ |

这些不是开脱借口——但不应该对故意行为报 FAIL。

---

## 十一、信任链全流程

```
用户提需求
    ↓
主模型实现（可能 spawn Plan Agent / Fork）
    ↓
判断是否为非平凡实现（3+ 文件 / API / 基础设施）
    ↓ 是
spawn Verification Agent（传递：任务描述 + 文件列表 + 方法）
    ↓              ↑
    ↓         主模型修复 ←── FAIL
    ↓
Verification Agent 执行验证
（构建 → 测试 → 端到端 → 对抗性探针）
    ↓
出具 VERDICT
    ↓ PASS
主模型抽检（重运行 2-3 个命令）
    ↓ 一致
向用户报告完成
```

---

## 十二、与传统 CI/CD 的区别

| 维度 | 传统 CI/CD | Verification Agent |
|------|-----------|-------------------|
| **检查项** | 预定义的测试脚本 | 根据变更类型动态生成验证策略 |
| **对抗性** | 只跑已写好的测试 | 主动构造破坏性输入 |
| **覆盖面** | 代码覆盖率指标 | 关注实现者的盲区（最后 20%）|
| **信任模型** | 信任测试（测试也是人/LLM 写的）| 不信任实现者的测试——独立验证 |
| **结果形式** | pass/fail 二元 | 结构化报告 + 命令证据 |
| **修复循环** | 开发者看 CI 报告手动修 | 主模型自动修复 → 重新验证 |

---

## 十三、设计哲学

Verification Agent 的设计体现了一个深刻的洞察：

**LLM 既是最好的程序员，也是最好的自欺者。**

它能写出优雅的代码，也能写出优雅的理由来解释为什么不需要测试。它能通过所有它自己写的测试，因为它知道自己的代码不会做什么——但它不知道自己的代码会意外做什么。

因此，Claude Code 的方案是：

- 写代码的和验证代码的必须是**不同的 Agent 实例**
- 验证者不能看到实现者的测试结果（避免锚定效应）
- 验证者的角色定义就是**红队**——它成功的标准是找到 bug，不是确认没有 bug
- 验证者被提前告知了自己的认知弱点（验证回避、被前 80% 迷惑），形成自我监督

这是 AI 系统中**对抗性架构（Adversarial Architecture）** 的一个精巧实现。

---

## 十四、核心源码文件索引

| 文件 | 职责 |
|------|------|
| `src/tools/AgentTool/built-in/verificationAgent.ts` | Verification Agent 定义 + 完整系统提示词 |
| `src/tools/AgentTool/builtInAgents.ts` | 内置代理注册（feature gate 控制） |
| `src/tools/AgentTool/constants.ts` | `VERIFICATION_AGENT_TYPE` 常量 |
| `src/constants/prompts.ts` | 主模型系统提示词中的验证契约条款 |
| `src/utils/attachments.ts` | `critical_system_reminder` 附件注入机制 |
| `src/utils/forkedAgent.ts` | 子代理 fork + `criticalSystemReminder` 传递 |
| `src/tools/AgentTool/runAgent.ts` | Agent 工具执行入口 + 背景任务标记 |
