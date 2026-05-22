# Claude Code Sandbox 设计与源码解析

## 0. 先用一个最小 sandbox 理解原理

很多人第一次听到“命令进了 sandbox 就更安全”，直觉上会疑惑：

> 同样是 `npm test`、`curl`、`rm`，为什么套一层 sandbox 就安全了？

关键点是：**sandbox 并不会让命令变“善良”，而是让命令看到的世界变小。**

普通命令运行时，看到的是用户机器上的真实世界：

```text
真实世界：
  /Users/me/.ssh
  /Users/me/.config
  /Users/me/project
  /tmp
  网络
  系统 socket
  各种命令和环境变量
```

进 sandbox 后，命令看到的是一个被裁剪过的小房间：

```text
sandbox 里的世界：
  /work        ← 当前项目目录，可写
  /tmp         ← 专用临时目录，可写
  /usr /bin    ← 运行命令所需的基础程序，只读
  网络          ← 默认阻断，或只允许白名单域名
  ~/.ssh       ← 看不见或不可读
  settings     ← 看得见也不能写，甚至直接看不见
```

所以 sandbox 的本质不是“检查这条命令是不是坏命令”，而是：

```text
即使命令想干坏事，也只能在被允许的小世界里活动。
```

### 0.1 sandbox 是 Linux 自带的吗？

严格说，**Linux 没有一个叫 sandbox 的统一开关**。

Linux 提供的是一组底层隔离原语，sandbox 工具把这些原语组合起来：

| Linux 原语 | 直觉含义 |
|------------|----------|
| mount namespace | 让进程看到一套被裁剪过的文件系统 |
| user namespace | 让进程在 sandbox 里像 root，但在宿主机上不是 root |
| pid namespace | 让进程看不到宿主机上的其他进程 |
| network namespace | 给进程一个独立网络空间，或直接断网 |
| seccomp | 限制进程能调用哪些系统调用 |
| capabilities | 去掉进程的高危内核权限 |
| cgroups | 限制 CPU、内存、进程数等资源 |
| chroot / pivot_root | 改变进程看到的根目录 |

`bubblewrap`、Docker、Flatpak、Chrome sandbox 等都是这些能力的不同组合。

所以更准确的说法是：

```text
Linux 自带的是“砖块”
bubblewrap / Docker / sandbox-runtime 是“用砖块搭出来的房间”
Claude Code 则是“决定什么时候把命令放进这个房间”
```

### 0.2 如果自己写一个最小 Linux sandbox

如果不用 `bubblewrap`，自己写一个最小 Linux sandbox，大致不是写一个 shell wrapper，而是写一个小程序，流程类似这样：

```text
parent process
  1. 创建子进程
  2. 给子进程启用新的 namespace
  3. 准备一个临时 rootfs
  4. 只把允许的目录 bind mount 进去
  5. 把敏感目录留在外面
  6. drop capabilities
  7. 安装 seccomp 规则
  8. 设置 HOME / TMPDIR / PATH
  9. exec 真正的命令
```

伪代码可以写成这样：

```c
int main() {
  // 1. 创建隔离命名空间：文件系统、进程、网络、用户等
  unshare(CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWUSER);

  // 2. 防止 mount 变化传播回宿主机
  mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL);

  // 3. 准备一个新的根目录
  mkdir("/tmp/sandbox-root", 0700);
  mkdir("/tmp/sandbox-root/work", 0700);
  mkdir("/tmp/sandbox-root/tmp", 0700);
  mkdir("/tmp/sandbox-root/bin", 0755);
  mkdir("/tmp/sandbox-root/usr", 0755);

  // 4. 只挂载允许看到的目录
  mount("/current/project", "/tmp/sandbox-root/work", NULL, MS_BIND, NULL);
  mount("tmpfs", "/tmp/sandbox-root/tmp", "tmpfs", 0, "size=256m");

  // 5. 基础命令目录只读挂载
  mount("/bin", "/tmp/sandbox-root/bin", NULL, MS_BIND | MS_RDONLY, NULL);
  mount("/usr", "/tmp/sandbox-root/usr", NULL, MS_BIND | MS_RDONLY, NULL);

  // 6. 切换根目录，让进程以为 /tmp/sandbox-root 就是整个世界
  chroot("/tmp/sandbox-root");
  chdir("/work");

  // 7. 清理环境变量，避免指向宿主机敏感路径
  setenv("HOME", "/tmp", 1);
  setenv("TMPDIR", "/tmp", 1);

  // 8. 降低权限：丢 capabilities，安装 seccomp 过滤
  drop_all_capabilities();
  install_seccomp_filter();

  // 9. 执行用户命令
  execl("/bin/bash", "bash", "-lc", "echo hello > result.txt; curl https://example.com", NULL);
}
```

这只是“玩具级最小实现”，真实实现还要处理大量细节：

- `chroot` 单独使用不安全，通常要配合 mount namespace、pivot_root、权限下降。
- 只读挂载、bind mount、符号链接、设备文件都要仔细处理。
- 如果要允许部分网络，就不能简单 `CLONE_NEWNET` 断网，需要代理、DNS、域名白名单或防火墙规则。
- seccomp 规则太松没意义，太紧又会让正常命令跑不起来。
- 子进程、后台进程、僵尸进程、超时和资源限制都要处理。

但这个最小例子已经说明了 sandbox 的核心：

```text
不是判断命令是否危险，
而是创建一个让危险动作没有入口的小世界。
```

### 0.3 如果在 Windows 下实现 sandbox

Windows 下没有 Linux namespace 这一套，所以实现方式不一样。通常会组合这些 Windows 原语：

| Windows 机制 | 直觉含义 |
|--------------|----------|
| Restricted Token | 创建一个权限被削弱的用户令牌 |
| Integrity Level | 把进程降到 Low Integrity，减少它写高完整性对象的能力 |
| AppContainer | Windows 应用沙箱，默认能力很少，需要显式授予 capabilities |
| Job Object | 把子进程关进一个 job，限制进程树、内存、CPU、退出清理 |
| ACL | 在文件系统层面只给 workspace / temp 授权 |
| WFP | Windows Filtering Platform，用于做网络阻断或白名单 |
| Desktop / Window Station 隔离 | 隔离 GUI 资源，减少跨进程窗口消息攻击 |
| Process Mitigation Policy | 禁用某些危险行为，例如动态代码、子进程等 |

如果自己写一个 Windows 最小 sandbox，大致流程是：

```text
1. 创建一个临时工作目录和临时 HOME
2. 给 workspace / temp 设置允许访问的 ACL
3. 创建 restricted token，去掉管理员权限和高危 privileges
4. 设置 Low Integrity 或 AppContainer
5. 创建 Job Object，限制进程树和资源
6. 用 CreateProcessAsUser / CreateProcessWithTokenW 启动命令
7. 用 WFP 或 AppContainer capabilities 控制网络
8. 命令结束后杀掉 job 中残留进程并清理临时目录
```

伪代码可以这样理解：

```c
HANDLE token = OpenCurrentUserToken();
HANDLE restricted = CreateRestrictedToken(token, REMOVE_ADMIN_AND_DANGEROUS_PRIVS);

SetTokenIntegrityLevel(restricted, LOW_INTEGRITY);

HANDLE job = CreateJobObject();
SetJobLimits(job, {
  kill_process_tree_on_close: true,
  max_memory: "512MB",
  max_processes: 32
});

GrantAcl("D:\\project", restricted_user_sid, READ_WRITE);
GrantAcl("D:\\sandbox-temp", restricted_user_sid, READ_WRITE);
DenyAcl("C:\\Users\\me\\.ssh", restricted_user_sid);

PROCESS_INFORMATION pi = CreateProcessAsUser(
  restricted,
  "C:\\Windows\\System32\\cmd.exe",
  "/c npm test",
  cwd = "D:\\project",
  env = {
    "USERPROFILE": "D:\\sandbox-temp",
    "TEMP": "D:\\sandbox-temp"
  }
);

AssignProcessToJobObject(job, pi.hProcess);
```

如果要更接近浏览器级别的 Windows sandbox，通常会使用 AppContainer：

```text
AppContainer 默认没有广泛文件系统权限
        ↓
只给它 workspace / temp 访问能力
        ↓
不授予 internetClient capability 就不能直接联网
        ↓
需要访问网络时走代理或审批通道
```

Windows 上还有一个产品级功能叫 **Windows Sandbox**，但它更像轻量虚拟机，适合跑整个隔离桌面环境，不太适合 Claude Code 这种“每条命令快速进出”的细粒度命令沙箱。

所以 Windows 下的直觉是：

```text
Linux 更像：给进程换一个“世界视图”
Windows 更像：给进程发一张“权限极少的通行证”，再用 ACL / Job / AppContainer 限制它能去哪
```

### 0.4 用房间类比

可以把普通命令想象成把施工队放进整栋房子：

```text
普通命令：
  客厅能进
  卧室能进
  保险柜可能也能碰
  还能从大门把东西带出去
```

sandbox 则是临时搭了一个施工隔间：

```text
sandbox 命令：
  只能在施工隔间里干活
  只给它必要工具
  只开放项目目录
  大门上锁，出门要审批
  保险柜那面墙根本不存在
```

这就是“进了 sandbox 后更安全”的原理：

> 安全性来自操作系统强制隔离，而不是来自模型自律，也不是来自命令字符串看起来无害。

Claude Code 的完整实现比这些最小例子复杂得多：它还会把 settings、permissions、网络域名、临时目录、Git 逃逸防护、权限 UI 等都接进来。但底层直觉就是这一个：**给命令一个被裁剪过、可控的小世界。**

> 基于 `ClaudeCode/src` 中的 sandbox 相关源码，说明 Claude Code 如何把 Bash/PowerShell 命令放进操作系统级沙箱中执行，并用直觉化方式理解它的安全边界、权限联动和设计取舍。

## 1. 一句话理解

Claude Code 的 sandbox 不是让模型“自觉不要乱动”，而是在命令真正执行时，把它放进一个由操作系统机制约束的执行环境里。

可以把它理解成：

```text
模型决定要跑什么命令
        ↓
Claude Code 判断这条命令是否应该进 sandbox
        ↓
如果进 sandbox，就把命令包上一层 OS 级隔离壳
        ↓
命令只能访问被允许的目录、网络域名和临时目录
        ↓
如果触碰限制，运行时阻断或触发用户审批
```

所以 sandbox 的核心目标不是替代模型判断，而是给模型行为加一层真实世界的硬边界。

---

## 2. 关键源码入口

Claude Code 的 sandbox 不是集中在一个文件里，而是由几类模块协作完成。

| 模块 | 作用 |
|------|------|
| `src/utils/sandbox/sandbox-adapter.ts` | Claude Code 对 `@anthropic-ai/sandbox-runtime` 的适配层，负责配置转换、初始化、状态判断和清理 |
| `src/tools/BashTool/shouldUseSandbox.ts` | 判断某条 Bash 命令是否应该进入 sandbox |
| `src/utils/Shell.ts` | 真实执行命令的位置，如果需要 sandbox，会调用 `SandboxManager.wrapWithSandbox()` 包装命令 |
| `src/entrypoints/sandboxTypes.ts` | sandbox settings 的类型定义和 schema |
| `src/tools/BashTool/bashPermissions.ts` | Bash 权限系统与 sandbox auto-allow 的联动 |
| `src/tools/BashTool/prompt.ts` | 把当前 sandbox 限制写入 Bash 工具提示词，让模型知道边界 |
| `src/screens/REPL.tsx` | REPL 模式下初始化 sandbox，并处理网络访问审批 |
| `src/components/permissions/SandboxPermissionRequest.tsx` | sandbox 网络访问请求的用户确认 UI |

整体看，Claude Code 自己主要做“接入与编排”，底层 OS 级隔离能力交给 `@anthropic-ai/sandbox-runtime`。

---

## 3. 总体架构与后文展开方式

### 3.1 三层结构

Claude Code 的 sandbox 可以分成三层：

```text
┌────────────────────────────────────────────┐
│ LLM / Agent 行为层                          │
│ - 模型决定要不要调用 Bash                   │
│ - prompt 告诉模型默认使用 sandbox           │
└────────────────────────────────────────────┘
                    ↓
┌────────────────────────────────────────────┐
│ Claude Code 适配层                          │
│ - settings → runtime config                │
│ - permissions → allow/deny                 │
│ - shouldUseSandbox                         │
│ - wrapWithSandbox                          │
│ - violation display / permission UI        │
└────────────────────────────────────────────┘
                    ↓
┌────────────────────────────────────────────┐
│ sandbox-runtime / OS 隔离层                 │
│ - macOS: seatbelt                          │
│ - Linux/WSL2: bubblewrap, socat, seccomp    │
│ - 文件系统限制                              │
│ - 网络限制                                  │
└────────────────────────────────────────────┘
```

这是一种很典型的 agent 安全设计：上层用提示词引导模型，中层用权限系统做决策，下层用 OS 机制兜底。

后文也按这三层展开：

```text
第 4 章：LLM / Agent 行为层
  解释模型看到什么提示、为什么默认进 sandbox、什么时候允许请求绕过。

第 5 章：Claude Code 适配层
  解释 settings / permissions 如何变成 runtime config，命令如何被判断、包装、执行、清理和展示。

第 6 章：sandbox-runtime / OS 隔离层
  解释底层依赖哪些系统能力，以及 Linux、macOS、Windows 的实现思路差异。
```

其中 `runtime config` 放在第 5 章讲，因为它不是 OS 自己的概念，而是 Claude Code 在运行时整理出来、传给 `sandbox-runtime` 的配置对象。

### 3.2 为什么不是只靠权限弹窗

如果没有 sandbox，每次 Bash 命令都可能直接访问用户机器上的全部文件和网络。权限系统只能在命令执行前做“是否允许”的判断，但很难精确理解复杂命令的真实副作用。

例如：

```bash
npm test
```

表面上是跑测试，但测试脚本内部可能：

- 读取环境变量
- 访问网络
- 写缓存目录
- 运行 postinstall 脚本
- 调用 git、docker、ssh 等外部程序

只靠静态判断很难知道它会做什么。sandbox 的直觉是：

> 不需要完全理解命令会做什么，先把它关在一个边界明确的空间里。

---

## 4. 第一层：LLM / Agent 行为层

第一层回答的问题是：

> 模型在决定调用 Bash/PowerShell 时，怎么知道“默认应该进 sandbox”，以及什么时候可以请求绕过？

这一层不是安全边界，而是行为引导层。它的作用是减少模型误操作，让模型在生成命令前就知道当前环境有哪些限制。

### 4.1 Prompt 告诉模型默认使用 sandbox

`BashTool/prompt.ts` 会把 sandbox 规则写进 Bash 工具提示词。模型会看到类似这样的说明：

```text
## Command sandbox
By default, your command will be run in a sandbox.
This sandbox controls which directories and network hosts commands may access or modify without an explicit override.
```

这句话的意义是：模型在选择执行命令时，默认假设命令会在 sandbox 中运行，而不是直接跑在用户完整机器环境里。

直觉上，prompt 层像“施工说明书”：

```text
你可以施工，但默认只能在围起来的施工区域里施工。
临时文件要放到 $TMPDIR。
不要主动要求打开保险柜或卧室门。
```

### 4.2 Prompt 会展示当前限制

Claude Code 还会把文件系统和网络限制写进提示词：

```text
Filesystem: ...
Network: ...
Ignored violations: ...
```

这不是用来做强制安全的。真正的强制安全在第三层 OS 隔离里完成。

Prompt 的作用是让模型提前避开明显会失败或越界的命令。例如：

- 如果网络只允许 `github.com`，模型就不应该贸然访问其他域名。
- 如果临时目录要求 `$TMPDIR`，模型就不应该直接写 `/tmp/foo`。
- 如果某些路径被 deny，模型就不应该建议把敏感路径加入 allowlist。

### 4.3 模型可以请求绕过，但必须有理由

Bash/PowerShell 工具里有一个参数：

```ts
dangerouslyDisableSandbox?: boolean
```

这不是普通开关，而是一个明确命名为“危险”的逃生门。

Prompt 中要求模型默认不要设置它，除非：

1. 用户明确要求绕过 sandbox。
2. 某个命令刚失败，并且有证据表明失败来自 sandbox 限制。

所以第一层的行为原则是：

```text
默认进 sandbox
        ↓
遇到限制先判断是不是 sandbox 导致
        ↓
只有有明确证据或用户要求时才请求绕过
        ↓
绕过仍然会进入权限审批流程
```

### 4.4 第一层不是安全边界

这一点非常重要：prompt 只是告诉模型应该怎么做，但不能保证模型永远遵守。

如果只靠 prompt，模型仍然可能生成危险命令：

```bash
cat ~/.ssh/id_rsa
curl https://unknown.example.com/upload
rm -rf ~/.config
```

这些命令是否真的能读到密钥、访问网络、删除配置，不由 prompt 决定，而由下面两层决定：

- 第二层 Claude Code 判断是否允许、是否需要包装、是否需要提示用户。
- 第三层 OS sandbox 决定进程实际能看到什么、写什么、连到哪里。

---

## 5. 第二层：Claude Code 适配层

第二层回答的问题是：

> Claude Code 如何把用户 settings、permission rules、工具调用参数，转换成 sandbox-runtime 能执行的限制？

这一层是整套设计的中枢。它既不只是 prompt，也不只是 OS 隔离，而是把上层意图和下层隔离能力连接起来。

### 5.1 `sandbox-adapter.ts` 与 runtime config

`sandbox-adapter.ts` 是 Claude Code sandbox 体系最核心的文件。文件开头已经说明了它的定位：

```ts
/**
 * Adapter layer that wraps @anthropic-ai/sandbox-runtime with Claude CLI-specific integrations.
 * This file provides the bridge between the external sandbox-runtime package and Claude CLI's
 * settings system, tool integration, and additional features.
 */
```

这说明 Claude Code 并不是在这里从零实现沙箱，而是把自己的配置系统、权限系统和 UI 逻辑接到外部 runtime 上。

这里的 `runtime config`，就是 Claude Code 在命令运行前临时整理出来的一份“沙箱规则说明书”。它不是用户直接写的原始 settings，也不是 OS 自带的配置文件，而是中间层对象：

```text
用户 settings / permissions
        ↓
Claude Code 合并、解析、补默认值
        ↓
SandboxRuntimeConfig
        ↓
传给 @anthropic-ai/sandbox-runtime
        ↓
底层 runtime 按它创建隔离环境
```

所以，如果问“runtime config 在第几章讲”，本文放在 **5.1 和 5.2**：它属于第二层 Claude Code 适配层。

### 5.2 配置转换：settings 如何变成 runtime config

核心函数是：

```ts
export function convertToSandboxRuntimeConfig(
  settings: SettingsJson,
): SandboxRuntimeConfig
```

它负责把 Claude Code 的 settings 转成 sandbox runtime 能理解的结构。

主要转换内容包括：

```text
Claude Code settings
        ↓
permissions.allow / permissions.deny
        ↓
WebFetch(domain:...) → allowedDomains / deniedDomains
Edit(path)           → allowWrite / denyWrite
Read(path)           → denyRead / allowRead
sandbox.network      → 网络限制
sandbox.filesystem   → 文件系统限制
        ↓
SandboxRuntimeConfig
```

也就是说，Claude Code 中已有的权限规则并没有被废弃，而是被进一步下沉成 sandbox runtime 的执行约束。

### 5.3 默认可写路径

源码中默认把当前目录和 Claude 临时目录加入可写列表：

```ts
const allowWrite: string[] = ['.', getClaudeTempDir()]
```

直觉上很合理：

- 当前目录是用户让 Claude 工作的地方，需要能写代码、生成文件、跑构建。
- Claude 临时目录用于存放命令执行过程中的 cwd tracking、临时输出等。

如果连这些都不允许写，agent 基本无法正常工作。

### 5.4 默认禁止写高风险路径

源码会主动把 settings 文件加入 `denyWrite`：

```ts
const settingsPaths = SETTING_SOURCES.map(source =>
  getSettingsFilePathForSource(source),
).filter((p): p is string => p !== undefined)
denyWrite.push(...settingsPaths)
denyWrite.push(getManagedSettingsDropInDir())
```

还会禁止写 `.claude/skills`：

```ts
denyWrite.push(resolve(originalCwd, '.claude', 'skills'))
if (cwd !== originalCwd) {
  denyWrite.push(resolve(cwd, '.claude', 'skills'))
}
```

这点非常关键。settings、commands、agents、skills 这类路径不是普通文件，而是会影响 Claude Code 后续行为的配置入口。

如果一个 sandboxed 命令能偷偷修改这些文件，就可能出现这样的逃逸链路：

```text
当前命令本来被 sandbox 限制
        ↓
命令修改 .claude/skills 或 settings
        ↓
下一轮 Claude Code 自动加载被污染的规则/技能
        ↓
攻击代码获得更高权限或诱导模型执行危险操作
```

所以这里的设计直觉是：

> sandbox 不只要防“当前命令破坏文件”，还要防“当前命令污染未来的 Claude 行为”。

---

### 5.5 平台与依赖检查

Claude Code 只在部分平台启用 sandbox：

```ts
/**
 * Check if the current platform is supported for sandboxing (memoized)
 * Supports: macOS, Linux, and WSL2+ (WSL1 is not supported)
 */
const isSupportedPlatform = memoize((): boolean => {
  return BaseSandboxManager.isSupportedPlatform()
})
```

结合其他源码注释可以看出：

| 平台 | 底层机制 |
|------|----------|
| macOS | 系统内置 `seatbelt` |
| Linux | `bubblewrap` / `bwrap`、`socat`、`seccomp` |
| WSL2 | 类 Linux 路径，依赖相关 Linux 机制 |
| Windows 原生 | 不支持 sandbox |
| WSL1 | 不支持 |

`/sandbox` 命令中也会检查平台和依赖：

```ts
if (!SandboxManager.isSupportedPlatform()) {
  const errorMessage = platform === 'wsl'
    ? 'Error: Sandboxing requires WSL2. WSL1 is not supported.'
    : 'Error: Sandboxing is currently only supported on macOS, Linux, and WSL2.'
}
```

这里体现了一个现实取舍：Claude Code 没有尝试在所有系统上做一个“看起来一致”的伪沙箱，而是只在底层机制足够可靠的平台上启用。

---

### 5.6 启用条件：什么时候会进 sandbox

是否使用 sandbox 的核心判断在 `shouldUseSandbox.ts`：

```ts
export function shouldUseSandbox(input: Partial<SandboxInput>): boolean {
  if (!SandboxManager.isSandboxingEnabled()) {
    return false
  }

  if (
    input.dangerouslyDisableSandbox &&
    SandboxManager.areUnsandboxedCommandsAllowed()
  ) {
    return false
  }

  if (!input.command) {
    return false
  }

  if (containsExcludedCommand(input.command)) {
    return false
  }

  return true
}
```

可以翻译成更直白的判断：

```text
如果 sandbox 没启用 → 不进
如果用户/模型显式要求绕过，且策略允许 → 不进
如果没有命令内容 → 不进
如果命令匹配 excludedCommands → 不进
否则 → 进入 sandbox
```

#### 5.6.1 `dangerouslyDisableSandbox`

Bash/PowerShell 工具 schema 中有一个参数：

```ts
dangerouslyDisableSandbox: semanticBoolean(z.boolean().optional())
  .describe('Set this to true to dangerously override sandbox mode and run commands without sandboxing.')
```

从命名就能看出它不是常规路径，而是危险逃生门。

Bash prompt 中也明确要求模型默认使用 sandbox，只有两类情况才允许绕过：

```text
1. 用户明确要求 bypass sandbox
2. 某个命令失败，并且有证据表明是 sandbox 限制造成的
```

这背后的直觉是：

> sandbox 可以被绕过，但绕过必须显式、逐次、可被用户感知。

#### 5.6.2 `excludedCommands`

`excludedCommands` 是用户配置的“不进 sandbox 的命令模式”。源码中特别说明：

```ts
// NOTE: excludedCommands is a user-facing convenience feature, not a security boundary.
// It is not a security bug to be able to bypass excludedCommands — the sandbox permission
// system (which prompts users) is the actual security control.
```

也就是说，`excludedCommands` 只是为了兼容某些无法在 sandbox 中正常运行的命令，例如 Docker、某些本地服务管理命令、特殊构建系统等。

它不是安全边界，不能拿它当防护机制。

---

### 5.7 命令执行链路

真正执行命令的地方在 `src/utils/Shell.ts`。

大致流程是：

```text
BashTool / PowerShellTool
        ↓
shouldUseSandbox(input)
        ↓
Shell.exec(command, options)
        ↓
provider.buildExecCommand(command)
        ↓
如果 shouldUseSandbox:
    SandboxManager.wrapWithSandbox(commandString, shell)
        ↓
spawn(shell, args)
        ↓
命令运行结束后 cleanupAfterCommand()
```

关键代码：

```ts
if (shouldUseSandbox) {
  commandString = await SandboxManager.wrapWithSandbox(
    commandString,
    sandboxBinShell,
    undefined,
    abortSignal,
  )

  const fs = getFsImplementation()
  await fs.mkdir(sandboxTmpDir, { mode: 0o700 })
}
```

这说明 sandbox 的实现方式不是新建一个独立工具，而是在普通 shell 命令外面包了一层 runtime 生成的执行命令。

例如概念上可能从：

```bash
npm test
```

变成类似：

```bash
bwrap ... /bin/bash -c 'npm test'
```

或在 macOS 上变成由 `sandbox-exec` / seatbelt 类机制约束的执行形式。具体字符串由 `@anthropic-ai/sandbox-runtime` 生成。

#### 5.7.1 PowerShell 的特殊处理

`Shell.ts` 中对 PowerShell 有特殊逻辑：

```ts
const isSandboxedPowerShell = shouldUseSandbox && shellType === 'powershell'
const sandboxBinShell = isSandboxedPowerShell ? '/bin/sh' : binShell
```

对应 `powershellProvider.ts` 中的注释解释了原因：sandbox runtime 会用 `<binShell> -c '<cmd>'` 形式包装命令，如果直接把 `pwsh` 当 shell，会丢失 `-NoProfile -NonInteractive` 等参数。因此在 sandbox 路径下，Claude Code 会预先构造：

```text
pwsh -NoProfile -NonInteractive -EncodedCommand <base64>
```

再交给 `/bin/sh -c` 去执行。

这体现了一个工程细节：sandbox 不只是安全问题，还会碰到 shell quoting、profile 加载、交互模式、编码等一堆实际兼容性问题。

---

### 5.8 文件系统隔离配置

Claude Code 的文件系统限制主要包括四类：

| 配置 | 含义 |
|------|------|
| `allowWrite` | 允许写入的路径 |
| `denyWrite` | 禁止写入的路径 |
| `denyRead` | 禁止读取的路径 |
| `allowRead` | 在 denyRead 区域中重新允许读取的路径 |

类型定义在 `sandboxTypes.ts`：

```ts
filesystem: {
  allowWrite?: string[]
  denyWrite?: string[]
  denyRead?: string[]
  allowRead?: string[]
}
```

#### 5.8.1 路径规则的两套语义

源码中特别区分了两类路径解析：

1. `permissions.allow/deny` 中的路径规则
2. `sandbox.filesystem.*` 中的路径规则

`resolvePathPatternForSandbox()` 处理 permission rule：

```text
//path  → 从文件系统根目录开始的绝对路径
/path   → 相对 settings 文件所在目录
~/path  → 交给 sandbox-runtime 处理
./path  → 交给 sandbox-runtime 处理
```

而 `resolveSandboxFilesystemPath()` 处理 `sandbox.filesystem.*`：

```text
/path   → 真正的绝对路径
~/path  → 展开到 home
./path  → 相对 settings 文件目录
//path  → legacy absolute 写法
```

这个差异看起来繁琐，但反映了向后兼容与用户直觉之间的平衡：

- permission rule 里 `/foo` 过去被设计成“相对 settings 文件目录”
- sandbox.filesystem 里用户更自然地认为 `/Users/foo/.cargo` 就是绝对路径

所以 Claude Code 在适配层里明确区分，避免用户配置出乎意料。

#### 5.8.2 临时目录

Bash prompt 中明确要求：

```text
For temporary files, always use the `$TMPDIR` environment variable.
TMPDIR is automatically set to the correct sandbox-writable directory in sandbox mode.
Do NOT use `/tmp` directly - use `$TMPDIR` instead.
```

这是因为 sandbox 下系统 `/tmp` 不一定可写，Claude Code 会准备一个专用的 sandbox temp dir。

直觉上，这像是给施工队单独划出一个材料堆放区：

```text
不要随便把临时材料丢到城市公共区域 /tmp
请放到沙箱分配给你的 TMPDIR
```

---

### 5.9 网络隔离配置

网络配置定义在 `sandboxTypes.ts`：

```ts
network: {
  allowedDomains?: string[]
  allowManagedDomainsOnly?: boolean
  allowUnixSockets?: string[]
  allowAllUnixSockets?: boolean
  allowLocalBinding?: boolean
  httpProxyPort?: number
  socksProxyPort?: number
}
```

`sandbox-adapter.ts` 会从两个地方提取域名：

1. `settings.sandbox.network.allowedDomains`
2. `permissions.allow` 中的 `WebFetch(domain:...)`

如果设置了 `allowManagedDomainsOnly`，则只接受 managed policy 里的域名，忽略用户、本地项目、flag 等来源中的 allowed domain。

#### 5.9.1 网络访问审批

REPL 中的 `sandboxAskCallback` 用于处理 sandbox runtime 发来的网络访问请求。

普通流程：

```text
命令访问未允许的 host
        ↓
sandbox-runtime 触发 ask callback
        ↓
REPL 把请求放入 sandboxPermissionRequestQueue
        ↓
UI 展示 “Allow network connection to xxx?”
        ↓
用户选择 Yes / Yes, don't ask again / No
```

对应 UI 在 `SandboxPermissionRequest.tsx` 中：

```ts
options = [
  { label: "Yes", value: "yes" },
  { label: "Yes, and don't ask again for <host>", value: "yes-dont-ask-again" },
  { label: "No, and tell Claude what to do differently", value: "no" },
]
```

直觉上，文件系统限制像围墙，网络限制像门禁。访问白名单域名可以直接出门；访问新域名时，门卫会问用户是否放行。

#### 5.9.2 HTTP Hook 也走 sandbox proxy

`execHttpHook.ts` 中也会读取 sandbox proxy：

```ts
const proxyPort = SandboxManager.getProxyPort()
return { host: '127.0.0.1', port: proxyPort, protocol: 'http' }
```

这说明 Claude Code 不只是约束 Bash 命令本身，还会尽量让相关 HTTP hook 也通过 sandbox 网络代理，从而保持统一的网络策略。

---

### 5.10 权限系统联动

sandbox 开启后，Claude Code 可以减少 Bash 权限弹窗，但不是完全跳过安全判断。

`bashPermissions.ts` 中有 `checkSandboxAutoAllow()`：

```ts
/**
 * Checks if a command should be auto-allowed when sandboxed.
 * Returns early if there are explicit deny/ask rules that should be respected.
 */
```

它的逻辑是：

```text
如果完整命令命中 deny → deny
如果复合命令中的任一子命令命中 deny → deny
如果命中 ask → ask
否则 → allow，因为 sandbox 已经提供边界
```

也就是说：

```text
sandbox auto-allow ≠ 无条件允许
sandbox auto-allow = 没有显式 deny/ask 时，借助 OS 沙箱降低询问频率
```

#### 5.10.1 为什么这样设计

如果 sandbox 开启后仍然每条 Bash 都弹窗，用户体验会很差。

但如果 sandbox 开启后完全跳过权限，又会忽略用户明确写下的 deny/ask 规则。

所以 Claude Code 采用中间路线：

```text
用户显式规则优先
        ↓
没有显式规则时
        ↓
如果命令会进 sandbox
        ↓
自动允许 Bash 执行
```

这是一种“强策略优先，sandbox 降噪”的设计。

---

### 5.11 Prompt 层提示如何进入工具说明

Claude Code 不只在 runtime 上做限制，还会把当前 sandbox 规则写进 Bash 工具提示词。

`BashTool/prompt.ts` 中会读取：

```ts
const filesystemConfig = SandboxManager.getFsWriteConfig()
const networkRestrictionConfig = SandboxManager.getNetworkRestrictionConfig()
const ignoreViolations = SandboxManager.getIgnoreViolations()
```

然后生成类似提示：

```text
## Command sandbox
By default, your command will be run in a sandbox.
This sandbox controls which directories and network hosts commands may access or modify without an explicit override.

The sandbox has the following restrictions:
Filesystem: ...
Network: ...
```

这层提示的作用不是安全兜底，而是减少模型误操作。

例如模型看到网络只允许 `github.com`，就不应该贸然执行访问其他域名的命令；看到临时目录要求 `$TMPDIR`，就不应该直接写 `/tmp/foo`。

可以理解为：

```text
runtime 是硬围栏
prompt 是施工图纸
权限系统是门卫
```

---

### 5.12 违规展示与错误处理

命令触发 sandbox 限制后，stderr 中可能包含：

```xml
<sandbox_violations>
...
</sandbox_violations>
```

`BashToolResultMessage.tsx` 会把这些标签从普通 stderr 中清理掉，避免 UI 直接展示内部结构：

```ts
function extractSandboxViolations(stderr: string): {
  cleanedStderr: string;
} {
  const violationsMatch = stderr.match(/<sandbox_violations>([\s\S]*?)<\/sandbox_violations>/);
  if (!violationsMatch) {
    return { cleanedStderr: stderr };
  }

  const cleanedStderr = removeSandboxViolationTags(stderr).trim();
  return { cleanedStderr };
}
```

同时 `sandbox-adapter.ts` 暴露：

```ts
annotateStderrWithSandboxFailures
getSandboxViolationStore
```

说明底层 runtime 会记录 violation，并由 Claude Code 负责把它转成更适合 UI 和模型理解的信息。

---

### 5.13 Git 相关逃逸防护

源码中有一段非常值得注意的安全逻辑：防止 bare git repo 结构被用来逃逸 sandbox。

相关注释：

```ts
// SECURITY: Git's is_git_directory() treats cwd as a bare repo if it has
// HEAD + objects/ + refs/. An attacker planting these (plus a config with
// core.fsmonitor) escapes the sandbox when Claude's unsandboxed git runs.
```

风险链路大致是：

```text
sandboxed command 在 cwd 中种下 HEAD / objects / refs / hooks / config
        ↓
后续 Claude Code 或用户执行未沙箱 git 命令
        ↓
git 把 cwd 当成 bare repo
        ↓
恶意 hooks / config 被触发
        ↓
逃出原本的 sandbox 约束
```

Claude Code 的处理方式是：

1. 如果这些文件已经存在，就加入 `denyWrite`
2. 如果配置时不存在，就记录到 `bareGitRepoScrubPaths`
3. sandbox 命令执行后调用 `scrubBareGitRepoFiles()` 清理新种下的路径

```ts
function scrubBareGitRepoFiles(): void {
  for (const p of bareGitRepoScrubPaths) {
    try {
      rmSync(p, { recursive: true })
      logForDebugging(`[Sandbox] scrubbed planted bare-repo file: ${p}`)
    } catch {
      // ENOENT is the expected common case — nothing was planted
    }
  }
}
```

这说明 Claude Code 的 sandbox 设计不只是“限制当前命令”，还考虑了命令结束后留下的陷阱。

直觉上说，它防的是：

> 犯人虽然被关在房间里，但可能在房间门口埋了一个机关，等管理员下次进来时触发。

---

### 5.14 CWD 与输出文件的安全处理

`Shell.ts` 中除了 sandbox 包装，还处理了 cwd tracking 和输出文件。

命令执行时，Claude Code 会记录命令结束后的 cwd：

```text
shell provider 构造命令
        ↓
命令结束时写 cwd 到 cwdFilePath
        ↓
Claude Code 读取 cwdFilePath
        ↓
更新内部 cwd state
```

sandbox 模式下，cwd tracking 文件也必须放在 sandbox 可写的 temp dir 中，否则命令无法写入。

输出文件方面，`Shell.ts` 和 `utils/task/diskOutput.ts` 都有 `O_NOFOLLOW` 相关注释，用于防止符号链接攻击：

```text
攻击者在 sandbox 可写目录中创建 symlink
        ↓
指向宿主机敏感文件
        ↓
Claude Code 以宿主权限写输出
        ↓
敏感文件被覆盖
```

所以代码在打开输出文件时尽量使用 `O_NOFOLLOW`，避免跟随 symlink。

这类细节体现了一个核心原则：

> sandbox 中的进程不可信，连它创建的输出路径和临时文件也不能完全信任。

---

### 5.15 与 Subagent / Swarm 的协同

REPL 中的 sandbox 网络审批还考虑了 swarm worker 场景。

当当前进程是 worker 时，网络请求不会只在本地处理，而是通过 mailbox 转发给 leader：

```text
worker 中的 sandbox 命令请求访问 host
        ↓
worker 生成 sandbox request id
        ↓
sendSandboxPermissionRequestViaMailbox(host, requestId)
        ↓
leader 收到请求并审批
        ↓
worker 继续或拒绝网络访问
```

这说明 Claude Code 把 sandbox 权限也纳入多 agent 协作模型中。

直觉上：

```text
worker 是执行者
leader 是审批者
sandbox permission request 是跨 agent 的门禁申请单
```

---

### 5.16 `/sandbox` 命令

`src/commands/sandbox-toggle/sandbox-toggle.tsx` 实现了 `/sandbox` 命令。

它主要做几件事：

1. 检查当前平台是否支持 sandbox
2. 检查依赖是否可用
3. 检查 settings 是否被上层 policy 锁定
4. 无参数时展示交互式配置界面
5. 支持 `/sandbox exclude <command>` 添加排除命令

例如：

```text
/sandbox exclude "npm run test:*"
```

会把命令模式加入 local settings 的 `sandbox.excludedCommands`。

这说明 sandbox 不是一个隐藏功能，而是 Claude Code 希望用户可以显式理解和管理的运行策略。

---

---

## 6. 第三层：sandbox-runtime / OS 隔离层

第三层回答的问题是：

> 命令真正执行时，为什么它不能随便读写文件、访问网络或修改宿主机环境？

这一层才是 sandbox 的硬边界。Claude Code 的第二层负责生成 `SandboxRuntimeConfig`，但真正把命令关进“小世界”的，是 `@anthropic-ai/sandbox-runtime` 和操作系统提供的隔离能力。

### 6.1 这一层和前两层的关系

三层之间可以这样理解：

```text
第一层：告诉模型“默认在施工区工作”
        ↓
第二层：把用户规则整理成 runtime config
        ↓
第三层：操作系统按规则真的搭出施工区
```

如果没有第三层，前两层都只是“约定”和“审批”。有了第三层，命令即使想越界，也会被 OS 机制限制。

### 6.2 Linux / WSL2：用 namespace、seccomp、mount 等原语搭小世界

Linux 下的 sandbox 通常不是一个单独能力，而是多种内核能力组合：

- mount namespace 控制进程看到的文件系统。
- network namespace 控制进程是否能访问网络。
- user namespace 降低宿主机权限。
- seccomp 限制系统调用。
- capabilities 去掉高危权限。
- cgroups 限制资源消耗。

本文开头的“最小 Linux sandbox”已经用伪代码说明了这个思路：创建新 namespace，只挂载允许目录，把 `HOME` 和 `TMPDIR` 指向临时位置，最后再执行真实命令。

### 6.3 macOS：用系统 sandbox / seatbelt 类机制

macOS 有系统级 sandbox 机制，常见做法是通过 profile 描述哪些路径、网络、系统资源可以访问。

直觉上，Linux 更像是“给进程重新搭一个世界”，macOS 更像是“给进程套一份系统访问规则”。

Claude Code 源码中没有在本仓库直接展开底层 macOS profile 的完整实现，因为这部分在外部 `@anthropic-ai/sandbox-runtime` 中。但从适配层可以看到，Claude Code 会把文件系统和网络规则统一转成 runtime config，再交给底层 runtime。

### 6.4 Windows：不是 Linux namespace，而是权限令牌、ACL、Job、AppContainer

Windows 原生没有 Linux namespace 这套机制。要实现类似命令 sandbox，通常会组合：

- Restricted Token：降低用户令牌权限。
- Low Integrity：让进程不能写高完整性对象。
- ACL：只给 workspace 和 temp 目录访问权限。
- Job Object：限制并管理整个子进程树。
- AppContainer：更接近应用沙箱的安全模型。
- WFP：做网络阻断或白名单。

所以 Windows 下的直觉是：

```text
Linux 更像：给进程换一个“世界视图”
Windows 更像：给进程发一张“权限很小的通行证”
```

也正因为这些差异，Claude Code 当前源码里明确写了：sandbox 支持 macOS、Linux、WSL2，不支持 Windows 原生和 WSL1。

### 6.5 第三层的核心价值

第三层最重要的价值是把安全从“模型是否听话”变成“进程实际能做什么”。

```text
模型可能误判
权限分类器可能漏判
命令文本可能伪装
脚本内部可能有副作用
        ↓
但 sandbox 中的进程只能访问被挂进去、被授权、被代理允许的资源
```

这就是 sandbox 对 agent 特别重要的原因。

---

## 7. 直觉化类比

可以用一个“装修队进屋施工”的比喻理解 Claude Code sandbox。

### 7.1 没有 sandbox

```text
用户把钥匙给装修队
装修队可以进任何房间
可以打开保险柜
可以从窗户把东西搬出去
用户只能在开工前问一句：你准备干什么？
```

这对应无 sandbox 的 Bash：

- 命令可以读写很多路径
- 可以访问网络
- 可以调用系统上的其他程序
- 权限判断很难完全理解复杂命令

### 7.2 有 sandbox

```text
用户只开放客厅和工具间
卧室、保险柜、文件柜上锁
大门有门禁，出门要问用户
装修队可以正常施工，但不能随便乱跑
```

这对应 sandbox 后的 Bash：

- 当前工作目录可写
- Claude temp dir 可写
- settings、skills、敏感路径被禁止
- 网络访问按域名控制
- 需要时触发用户确认

### 7.3 `dangerouslyDisableSandbox`

```text
如果装修队说：这个活必须进卧室才能做
用户可以临时开门
但这次开门要明确、单次、可感知
```

这就是 `dangerouslyDisableSandbox` 的定位。

---

## 8. 设计优点

### 8.1 降低权限弹窗噪音

sandbox 开启后，很多 Bash 命令可以 auto-allow，因为它们已经被 OS 层限制住。

用户体验会更顺滑：

```text
没有 sandbox:
  每条 Bash 都可能需要问

有 sandbox:
  普通命令直接执行
  触碰边界时才问
```

### 8.2 不要求模型完美判断

LLM 不擅长完全预测 shell 命令副作用，尤其是复杂脚本、包管理器、构建系统、测试框架。

sandbox 把安全问题从“模型能不能判断对”转成“运行时能不能限制住”。

这是一种更可靠的安全思路。

### 8.3 与现有权限系统兼容

Claude Code 没有另起一套权限模型，而是把已有的：

- `permissions.allow`
- `permissions.deny`
- `WebFetch(domain:...)`
- `Edit(path)`
- `Read(path)`
- managed settings

统一转换到 sandbox runtime。

这减少了用户理解成本，也避免了两套权限规则互相打架。

### 8.4 支持企业策略

源码中有多个 enterprise / managed settings 相关设计：

- `allowManagedDomainsOnly`
- `allowManagedReadPathsOnly`
- `enabledPlatforms`
- `failIfUnavailable`
- policy settings 锁定 sandbox 配置

这说明 sandbox 不只是个人用户的安全功能，也面向企业管控场景。

---

## 9. 设计限制

### 9.1 平台支持不完整

Windows 原生不支持，WSL1 也不支持。这意味着在这些环境中，sandbox 不能作为安全边界。

源码中如果用户显式启用 sandbox 但不可用，会给出 warning；如果设置了 `failIfUnavailable`，则直接拒绝启动。

### 9.2 底层依赖复杂

Linux/WSL 需要 `bubblewrap`、`socat`、`seccomp` 等依赖。依赖缺失时 sandbox 可能无法启用。

这也是为什么源码中特别修复了一个安全 footgun：用户设置了 `sandbox.enabled: true`，但依赖缺失导致实际没启用时，必须显式提醒用户。

### 9.3 不可能消除所有风险

sandbox 能限制文件系统和网络，但仍然有很多复杂边界：

- 本地 socket
- Docker daemon
- SSH agent
- Git hooks
- 编译器和构建工具的特殊行为
- 符号链接
- 背景任务
- cwd 变化

所以源码里才会有大量额外补丁，例如 Git bare repo scrub、`O_NOFOLLOW`、Unix socket 规则等。

### 9.4 `dangerouslyDisableSandbox` 仍然存在

为了兼容真实开发场景，Claude Code 允许绕过 sandbox。只要存在绕过机制，就需要依赖用户审批和策略约束。

这不是设计缺陷，而是实用工具必须面对的现实：

```text
完全禁止绕过 → 很多真实命令跑不了
随便绕过 → sandbox 形同虚设
Claude Code 选择 → 默认 sandbox，必要时显式逐次绕过
```

---

## 10. 与传统权限系统的差异

| 维度 | 传统权限弹窗 | Claude Code sandbox |
|------|--------------|---------------------|
| 判断时机 | 命令执行前 | 命令执行时和执行前结合 |
| 判断依据 | 命令文本、规则、模型分类 | OS 级文件系统/网络行为 |
| 用户体验 | 容易频繁打断 | 普通命令少打断，越界才打断 |
| 安全边界 | 偏应用层 | 偏操作系统层 |
| 对复杂脚本的处理 | 难以预测副作用 | 不需要完全预测，运行时限制 |
| 失败模式 | 可能误放行 | 可能阻断，需要用户放行或配置 |

直觉上：

```text
权限弹窗问的是：你声称要做什么？
sandbox 管的是：你实际能做什么？
```

两者结合，才比较适合 agent 场景。

---

## 11. 对 Agent 框架的启发

如果自己设计一个 coding agent，Claude Code 的 sandbox 方案有几个值得借鉴的点。

### 11.1 不要只相信 LLM

提示词可以减少误操作，但不能作为安全边界。

真正的边界应该放在：

- OS sandbox
- 文件系统权限
- 网络代理
- 审批系统
- policy settings

### 11.2 权限规则要能下沉到 runtime

如果用户已经写了：

```json
{
  "permissions": {
    "allow": ["Edit(src/**)", "WebFetch(domain:github.com)"],
    "deny": ["Read(~/.ssh/**)"]
  }
}
```

那么这些规则不应该只影响模型提示词，也应该影响命令真实执行环境。

Claude Code 的 `convertToSandboxRuntimeConfig()` 就是在做这件事。

### 11.3 默认安全，保留逃生门

真实开发环境很复杂，完全禁止非 sandbox 命令会造成大量兼容问题。

更合理的路线是：

```text
默认 sandbox
明确证据下允许绕过
绕过需要用户可感知
企业策略可以禁用绕过
```

### 11.4 要防“未来行为污染”

`.claude/skills`、settings、agents、commands 这类文件本身就是 agent 的执行入口。

保护它们和保护源码文件不一样，它们更像“控制平面”。

Claude Code 禁止 sandboxed command 写这些路径，是非常重要的设计。

---

## 12. 总结

Claude Code sandbox 的设计可以概括为：

```text
以 OS 级隔离作为硬边界
以 settings / permissions 作为策略来源
以 prompt 作为模型行为引导
以 UI 审批处理越界访问
以 cleanup 和安全补丁处理现实逃逸路径
```

它不是一个单点功能，而是一套贯穿命令执行链路的三层机制：

```text
第一层：LLM / Agent 行为层
  prompt 告诉模型默认使用 sandbox，绕过必须有理由
        ↓
第二层：Claude Code 适配层
  settings / permissions → runtime config
  shouldUseSandbox 判断是否进 sandbox
  Shell.exec 调用 wrapWithSandbox 包装命令
  violation / permission UI / cleanup 处理执行反馈
        ↓
第三层：sandbox-runtime / OS 隔离层
  macOS / Linux / WSL2 的系统隔离能力真正限制文件、网络和进程行为
```

最核心的直觉是：

> Agent 需要能高效执行命令，但命令的副作用必须被真实系统边界约束。模型负责“想做什么”，sandbox 负责“最多能做到哪里”。

