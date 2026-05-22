# Claude Code Sandbox 是否依赖 VM 及源码实现

## 1. 结论：Sandbox 不等于 VM

Claude Code 的 sandbox 不是通过给每条命令启动一个 VM 或 microVM 来实现的。

更准确地说：

```text
sandbox = 受限运行环境
VM / microVM = 实现 sandbox 的一种方式
Claude Code sandbox = Claude Code 适配层 + @anthropic-ai/sandbox-runtime + OS 隔离机制
```

也就是说，sandbox 是目标：让命令只能在被授权的文件系统、网络和临时目录范围内运行。

VM 只是达成这个目标的一种技术路线，而且通常是隔离更强、成本也更高的路线。

---

## 2. microVM 是什么

microVM 可以理解成“为云原生、函数计算、短任务沙箱场景优化过的极简虚拟机”。

它比传统虚拟机轻，但安全边界比普通容器更硬。

传统虚拟机大致是：

```text
App
Guest OS
完整虚拟硬件
Hypervisor
Host OS / Hardware
```

容器大致是：

```text
App
共享宿主机内核
namespace / cgroup / seccomp
Host OS
```

microVM 介于两者之间：

```text
App
极简 Guest Linux
极简虚拟设备
KVM / Hypervisor
Host OS / Hardware
```

典型代表是 Firecracker。它的目标不是提供一个完整桌面系统，而是快速启动一个最小 Linux 环境，只暴露极少虚拟设备，例如网络、磁盘、串口，从而降低攻击面。

microVM 的优势是隔离边界硬：任务跑在 guest kernel 里，和宿主机 kernel 隔开。

代价是启动和资源成本更高：即使是轻量 microVM，也仍然要创建 VM、加载内核、挂载 rootfs、配置网络和磁盘。对 Agent 来说，如果每执行一个很小的命令都启动一次 microVM，就会显得过重。

---

## 3. Sandbox 的几种实现层级

Sandbox 可以有不同强度的实现方式。

从轻到重，大致可以分成三类：

```text
进程级限制
- 权限降级
- ACL / 文件权限
- seccomp
- AppArmor / SELinux
- macOS seatbelt
- Windows AppContainer / Job Object

容器级隔离
- Linux namespace
- cgroups
- bind mount
- Docker / bubblewrap / Flatpak

虚拟机级隔离
- Firecracker microVM
- Kata Containers
- Windows Sandbox
```

所以：

```text
container sandbox 通常不启动完整 VM
microVM sandbox 才走虚拟机边界
Claude Code 当前不是 microVM 路线
```

Claude Code 这种本地开发 Agent 的命令 sandbox，更关注“每条命令快速进出”和“减少误操作影响面”。因此它更适合使用系统级沙箱和容器级原语，而不是为每条命令启动一个 VM。

---

## 4. Claude Code 的源码分层

从源码结构看，Claude Code 的 sandbox 不是集中在一个文件里，而是由三层协作完成。

```text
第一层：LLM / Agent 行为层
- Prompt 告诉模型默认使用 sandbox
- 模型可以在特殊情况下请求绕过 sandbox

第二层：Claude Code 适配层
- 判断命令是否需要进入 sandbox
- 把 settings / permissions 转成 runtime config
- 包装 Bash / PowerShell 命令
- 处理网络访问审批、错误展示和 cleanup

第三层：sandbox-runtime / OS 隔离层
- 真正调用系统级隔离机制
- macOS 走 seatbelt / sandbox profile
- Linux / WSL2 走 bubblewrap、namespace、seccomp、mount 等机制
```

关键源码入口包括：

| 模块 | 作用 |
|---|---|
| `src/utils/sandbox/sandbox-adapter.ts` | Claude Code 对 `@anthropic-ai/sandbox-runtime` 的适配层，负责配置转换、初始化、状态判断和清理 |
| `src/tools/BashTool/shouldUseSandbox.ts` | 判断某条 Bash 命令是否应该进入 sandbox |
| `src/utils/Shell.ts` | 真实执行命令的位置，如果需要 sandbox，会调用 `SandboxManager.wrapWithSandbox()` 包装命令 |
| `src/entrypoints/sandboxTypes.ts` | sandbox settings 的类型定义和 schema |
| `src/tools/BashTool/bashPermissions.ts` | Bash 权限系统与 sandbox auto-allow 的联动 |
| `src/tools/BashTool/prompt.ts` | 把当前 sandbox 限制写入 Bash 工具提示词，让模型知道边界 |
| `src/screens/REPL.tsx` | REPL 模式下初始化 sandbox，并处理网络访问审批 |
| `src/components/permissions/SandboxPermissionRequest.tsx` | sandbox 网络访问请求的用户确认 UI |

核心判断是：Claude Code 自己主要做“接入与编排”，底层 OS 级隔离能力交给 `@anthropic-ai/sandbox-runtime`。

---

## 5. 命令什么时候进入 sandbox

是否使用 sandbox 的核心判断在 `src/tools/BashTool/shouldUseSandbox.ts`。

逻辑可以简化为：

```text
如果 sandbox 没启用 → 不进
如果用户/模型显式要求绕过，且策略允许 → 不进
如果没有命令内容 → 不进
如果命令匹配 excludedCommands → 不进
否则 → 进入 sandbox
```

对应源码形态类似：

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

这里的 `dangerouslyDisableSandbox` 是一个显式逃生门，不是默认路径。它通常只应在两种情况下使用：

1. 用户明确要求绕过 sandbox。
2. 命令刚失败，并且有证据表明失败来自 sandbox 限制。

---

## 6. 命令是怎么被包装执行的

真正执行命令的位置在 `src/utils/Shell.ts`。

整体链路是：

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

所以 Claude Code 不是另外启动一个“VM 管理器”，而是在普通 shell 命令外面包一层由 sandbox runtime 生成的执行命令。

概念上，原始命令：

```bash
npm test
```

在 Linux / WSL2 上可能变成类似：

```bash
bwrap ... /bin/bash -c 'npm test'
```

在 macOS 上则会变成由系统 sandbox / seatbelt 规则约束的执行形式。

具体字符串由 `@anthropic-ai/sandbox-runtime` 根据平台和配置生成。

---

## 7. runtime config：settings 如何变成沙箱规则

`src/utils/sandbox/sandbox-adapter.ts` 是 Claude Code sandbox 体系中最关键的适配层。

它的职责不是从零实现隔离，而是把 Claude Code 的配置系统、权限系统和 UI 逻辑接到外部 runtime 上。

转换链路可以理解为：

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

主要转换内容包括：

```text
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

这意味着 Claude Code 原有权限规则并没有被废弃，而是进一步下沉成 runtime 可执行的约束。

---

## 8. 文件系统和网络边界

Claude Code 的 sandbox 主要约束两类能力：文件系统和网络。

文件系统规则包括：

| 配置 | 含义 |
|---|---|
| `allowWrite` | 允许写入的路径 |
| `denyWrite` | 禁止写入的路径 |
| `denyRead` | 禁止读取的路径 |
| `allowRead` | 在 denyRead 区域中重新允许读取的路径 |

源码中默认会允许写当前目录和 Claude 临时目录：

```ts
const allowWrite: string[] = ['.', getClaudeTempDir()]
```

同时会默认禁止写高风险路径，例如 settings 文件、managed settings 目录、`.claude/skills` 等。

原因是这些路径会影响 Claude Code 后续行为。如果 sandboxed 命令能修改它们，就可能污染下一轮 agent 行为，形成一种“延迟逃逸”：

```text
当前命令本来被 sandbox 限制
        ↓
命令修改 .claude/skills 或 settings
        ↓
下一轮 Claude Code 自动加载被污染的规则/技能
        ↓
攻击代码获得更高权限或诱导模型执行危险操作
```

网络规则包括：

| 配置 | 含义 |
|---|---|
| `allowedDomains` | 允许访问的域名 |
| `allowManagedDomainsOnly` | 只允许 managed policy 中的域名 |
| `allowUnixSockets` | 允许访问的 Unix socket |
| `allowAllUnixSockets` | 允许所有 Unix socket |
| `allowLocalBinding` | 是否允许本地端口绑定 |
| `httpProxyPort` / `socksProxyPort` | sandbox 网络代理端口 |

当命令访问未允许的 host 时，sandbox runtime 会触发 ask callback，REPL 再把请求展示给用户审批。

直觉上：

```text
文件系统限制像围墙
网络限制像门禁
访问白名单域名可以直接出门
访问新域名时需要用户放行
```

---

## 9. 平台支持情况

Claude Code 当前只在部分平台启用 sandbox。

| 平台 | 支持情况 | 底层机制 |
|---|---|---|
| macOS | 支持 | 系统内置 sandbox / seatbelt |
| Linux | 支持 | bubblewrap / bwrap、namespace、seccomp、mount 等 |
| WSL2 | 支持 | 类 Linux 路径，依赖相关 Linux 机制 |
| Windows 原生 | 不支持 | Claude Code 当前没有启用原生 Windows sandbox |
| WSL1 | 不支持 | 缺少必要 Linux 隔离能力 |

这说明 Claude Code 没有尝试在所有系统上做一个“看起来一致”的伪沙箱，而是只在底层机制足够可靠的平台上启用。

---

## 10. 为什么不直接用 microVM

microVM 的隔离边界更强，但对 Claude Code 这种本地 Agent 命令执行场景，成本通常偏高。

主要原因有三个：

1. 启动延迟更高  
   microVM 仍然要创建虚拟机、加载 guest kernel、挂载 rootfs、配置网络和磁盘。对 `ls`、`rg`、`npm test` 这类短命令来说，启动成本可能比命令本身还重。

2. 资源占用更高  
   microVM 需要为 guest kernel、rootfs、虚拟设备和网络栈预留资源。Agent 场景还可能有多个并发会话或后台任务，资源占用会快速累积。

3. 工程复杂度更高  
   需要管理镜像、内核版本、网络代理、文件同步、命令输入输出、临时目录、生命周期清理等问题。本地 CLI 工具如果每条命令都走这套机制，体验和维护成本都会变重。

因此 Claude Code 当前更像是采用“系统级沙箱 + runtime 编排”的方案：安全边界足够实用，启动和交互成本也更适合本地开发场景。

---

## 11. 总结

Claude Code sandbox 的实现可以用一句话概括：

> Claude Code 自己不实现 VM，也不从零实现内核隔离；它把用户配置和权限规则整理成 `SandboxRuntimeConfig`，再交给 `@anthropic-ai/sandbox-runtime`，由 runtime 在 macOS、Linux、WSL2 上调用系统级隔离机制来包装执行命令。

所以它的核心不是“给每条命令开一个虚拟机”，而是：

```text
判断命令是否需要隔离
        ↓
把权限规则转换成沙箱配置
        ↓
用 runtime 包装 shell 命令
        ↓
通过 OS 机制限制文件系统和网络访问
        ↓
命令结束后清理临时状态
```

这也是它和 microVM sandbox 的关键区别：

```text
microVM sandbox：用虚拟机边界隔离任务，安全更硬，成本更高
Claude Code sandbox：用平台原生沙箱 / Linux 隔离原语包装命令，更轻，更适合本地 CLI 命令执行
```
