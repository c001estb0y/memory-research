# Claude Code 搜索工具体系与实现机制

> 本文档基于 Claude Code 源码，系统梳理其代码搜索与文本查找的完整工具链，包括每个工具的定位、参数、底层实现、提示词引导策略以及子代理的访问权限。

---

## 目录

1. [搜索工具全景图](#1-搜索工具全景图)
2. [Grep — 文件内容搜索](#2-grep--文件内容搜索)
3. [Glob — 文件名模式匹配](#3-glob--文件名模式匹配)
4. [WebSearch — 联网搜索](#4-websearch--联网搜索)
5. [LSP — 语言服务器符号搜索](#5-lsp--语言服务器符号搜索)
6. [ToolSearch — 延迟工具检索](#6-toolsearch--延迟工具检索)
7. [嵌入式搜索模式（Ant-Native）](#7-嵌入式搜索模式ant-native)
8. [底层实现：ripgrep 集成](#8-底层实现ripgrep-集成)
9. [提示词引导策略：何时用哪个工具](#9-提示词引导策略何时用哪个工具)
10. [实际搜索工作流程：端到端场景分析](#10-实际搜索工作流程端到端场景分析)
11. [子代理工具访问矩阵](#11-子代理工具访问矩阵)
12. [设计思考与总结](#12-设计思考与总结)

---

## 1. 搜索工具全景图

Claude Code 没有单一的"搜索"能力，而是提供了一组**分层搜索工具**，各有明确的适用场景：

| 工具 | 搜索对象 | 底层实现 | 搜索方式 | 是否只读 |
|------|---------|---------|---------|---------|
| **Grep** | 文件内容 | ripgrep (`rg`) | 正则表达式 | 是 |
| **Glob** | 文件名 | ripgrep (`rg --files`) | Glob 模式 | 是 |
| **WebSearch** | 互联网 | Anthropic `web_search_20250305` | 自然语言查询 | 是 |
| **LSP** | 代码符号 | Language Server Protocol | 符号名 / 位置 | 是 |
| **ToolSearch** | 工具定义 | 内存文本匹配 | 关键词 / 工具名 | 是 |

> **关键发现**：Claude Code 中**没有** `SemanticSearch` 或 `FileSearch` 命名的工具。Cursor 中的 `SemanticSearch` 是 IDE 侧能力，不在 Claude Code 源码内。

---

## 2. Grep — 文件内容搜索

> 源码位置：`src/tools/GrepTool/GrepTool.ts`、`src/tools/GrepTool/prompt.ts`

### 2.1 定位与描述

Grep 是 Claude Code 中**最核心的搜索工具**，基于 ripgrep 实现，用于在文件内容中进行正则表达式搜索。

模型看到的工具描述：

```
A powerful search tool built on ripgrep

Usage:
- ALWAYS use Grep for search tasks. NEVER invoke `grep` or `rg` as a Bash command.
- Supports full regex syntax (e.g., "log.*Error", "function\s+\w+")
- Filter files with glob parameter or type parameter
- Output modes: "content" shows matching lines, "files_with_matches" shows only file paths (default), "count" shows match counts
- Use Agent tool for open-ended searches requiring multiple rounds
- Pattern syntax: Uses ripgrep (not grep) - literal braces need escaping
- Multiline matching: By default patterns match within single lines only. For cross-line patterns, use multiline: true
```

### 2.2 输入参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `pattern` | string | 是 | — | 正则表达式模式 |
| `path` | string | 否 | cwd | 搜索的文件或目录路径 |
| `glob` | string | 否 | — | 文件过滤 glob 模式（如 `*.js`、`*.{ts,tsx}`），映射到 `rg --glob` |
| `output_mode` | enum | 否 | `files_with_matches` | `content`（匹配行）、`files_with_matches`（文件路径）、`count`（计数） |
| `-B` | number | 否 | — | 匹配前显示的上下文行数 |
| `-A` | number | 否 | — | 匹配后显示的上下文行数 |
| `-C` / `context` | number | 否 | — | 匹配前后的上下文行数（优先级高于 -B/-A） |
| `-n` | boolean | 否 | true | 是否显示行号（仅 content 模式） |
| `-i` | boolean | 否 | false | 是否忽略大小写 |
| `type` | string | 否 | — | 文件类型过滤（如 `js`、`py`、`rust`），映射到 `rg --type` |
| `head_limit` | number | 否 | 250 | 限制输出条目数，传 0 表示无限制 |
| `offset` | number | 否 | 0 | 跳过前 N 条结果（用于分页） |
| `multiline` | boolean | 否 | false | 跨行匹配模式，启用 `rg -U --multiline-dotall` |

### 2.3 输出结构

```typescript
{
  mode: 'content' | 'files_with_matches' | 'count',
  numFiles: number,
  filenames: string[],
  content?: string,        // content 模式下的匹配内容
  numLines?: number,       // content 模式下的行数
  numMatches?: number,     // count 模式下的总匹配数
  appliedLimit?: number,   // 实际应用的截断限制
  appliedOffset?: number,  // 实际应用的偏移量
}
```

### 2.4 实现细节

- **底层调用**：`ripGrep()` → 通过 `execFile` 或 `spawn` 执行 `rg` 命令
- **VCS 排除**：自动排除 `.git`、`.svn`、`.hg`、`.bzr`、`.jj`、`.sl` 目录
- **行长限制**：`--max-columns 500`，防止 base64/压缩内容污染输出
- **隐藏文件**：默认包含隐藏文件（`--hidden`）
- **路径转换**：输出结果自动转为相对路径以节省 token
- **结果排序**：`files_with_matches` 模式下按修改时间排序（最近修改的优先）
- **权限校验**：检查文件读取权限，排除用户配置的忽略模式
- **安全防护**：跳过 UNC 路径以防止 NTLM 凭证泄露
- **结果上限**：默认 250 条（`DEFAULT_HEAD_LIMIT`），防止上下文膨胀（20KB 持久化阈值）
- **并发安全**：`isConcurrencySafe: true`，可以多个 Grep 并行执行

### 2.5 关键设计决策

1. **默认输出模式是 `files_with_matches`（仅文件名）**，而非 `content`。这是一个有意的选择——先找到哪些文件匹配，再用 `Read` 工具查看具体内容，比一次性输出所有匹配行更节省 token。

2. **结果默认限制 250 条**。代码注释明确说明原因：
   > Unbounded content-mode greps can fill up to the 20KB persist threshold (~6-24K tokens/grep-heavy session). 250 is generous enough for exploratory searches while preventing context bloat.

---

## 3. Glob — 文件名模式匹配

> 源码位置：`src/tools/GlobTool/GlobTool.ts`、`src/tools/GlobTool/prompt.ts`

### 3.1 定位与描述

Glob 用于按文件名模式查找文件，返回匹配的文件路径列表。

模型看到的工具描述：

```
- Fast file pattern matching tool that works with any codebase size
- Supports glob patterns like "**/*.js" or "src/**/*.ts"
- Returns matching file paths sorted by modification time
- Use this tool when you need to find files by name patterns
- When you are doing an open ended search that may require multiple rounds of globbing and grepping, use the Agent tool instead
```

### 3.2 输入参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `pattern` | string | 是 | — | Glob 匹配模式 |
| `path` | string | 否 | cwd | 搜索目录（**不要传 "undefined" 或 "null"**） |

### 3.3 输出结构

```typescript
{
  durationMs: number,      // 搜索耗时（毫秒）
  numFiles: number,        // 匹配文件数
  filenames: string[],     // 匹配的文件路径列表
  truncated: boolean,      // 是否被截断（超过 100 个文件）
}
```

### 3.4 实现细节

- **底层调用**：并非使用 Node.js 的 glob 库，而是**复用 ripgrep**：`rg --files --glob <pattern> --sort=modified`
- **结果上限**：默认最多返回 100 个文件（`globLimits.maxResults ?? 100`）
- **排序方式**：按修改时间排序（最近修改的优先）
- **隐藏文件**：默认包含（可通过 `CLAUDE_CODE_GLOB_HIDDEN=false` 禁用）
- **gitignore**：默认忽略 `.gitignore`（可通过 `CLAUDE_CODE_GLOB_NO_IGNORE=false` 启用）
- **绝对路径处理**：如果 pattern 是绝对路径，自动提取基目录后转为相对 pattern
- **路径转换**：输出自动转为相对路径

### 3.5 Grep 与 Glob 的协作关系

Grep 和 Glob 在底层都依赖 ripgrep，但面向不同的搜索维度：

```
Glob（找文件名） → 得到文件列表 → Read（读内容）
Grep（找内容）   → 得到匹配行/文件 → Read（读上下文）
```

系统提示明确要求模型**不要在 Bash 中使用 find/grep/rg**，而是使用这两个专用工具。

---

## 4. WebSearch — 联网搜索

> 源码位置：`src/tools/WebSearchTool/WebSearchTool.ts`、`src/tools/WebSearchTool/prompt.ts`

### 4.1 定位与描述

WebSearch 是联网搜索工具，不搜索本地代码库，而是通过 Anthropic 的服务端搜索能力获取互联网信息。

### 4.2 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 搜索查询词（最少 2 字符） |
| `allowed_domains` | string[] | 否 | 仅包含这些域名的结果 |
| `blocked_domains` | string[] | 否 | 排除这些域名的结果 |

> 注意：`allowed_domains` 和 `blocked_domains` 不能同时指定。

### 4.3 实现机制

WebSearch **不是**一个简单的 API 调用，而是一个**嵌套模型调用**：

1. 向 Anthropic API 发送一个新的模型请求，附带 `web_search_20250305` 工具
2. 模型在服务端自动执行搜索（最多 8 次搜索：`max_uses: 8`）
3. 返回的结果包含搜索结果块（`web_search_tool_result`）和模型生成的文本总结
4. 解析后返回给主模型

```
主模型 → WebSearchTool.call() → 新模型请求（带 web_search tool）→ Anthropic 服务端搜索 → 结果
```

### 4.4 启用条件

WebSearch 不是总能使用的，需要满足以下条件之一：
- API Provider 是 `firstParty`（Anthropic 直连）
- API Provider 是 `vertex`（需要 Claude 4.0+ 模型）
- API Provider 是 `foundry`

> **重要限制**：通过 OpenAI 兼容代理（如 Venus Proxy）使用时，WebSearch **不可用**，因为它依赖 Anthropic 服务端的 `web_search_20250305` 工具执行。

### 4.5 输出格式

WebSearch 的输出末尾会强制附加一条提醒：

```
REMINDER: You MUST include the sources above in your response to the user using markdown hyperlinks.
```

提示词还要求模型在回复中附加 `Sources:` 段落，列出所有引用的 URL。

---

## 5. LSP — 语言服务器符号搜索

> 源码位置：`src/tools/LSPTool/LSPTool.ts`、`src/tools/LSPTool/prompt.ts`

### 5.1 定位与描述

LSP 工具提供基于语言服务器协议的代码智能功能，其中包含若干搜索相关操作。

### 5.2 支持的搜索操作

| 操作 | 说明 | 搜索范围 |
|------|------|---------|
| `goToDefinition` | 跳转到符号定义 | 单符号 |
| `findReferences` | 查找所有引用 | 工作区级 |
| `hover` | 获取悬停信息（类型、文档） | 单符号 |
| `documentSymbol` | 列出文件中所有符号 | 单文件 |
| `workspaceSymbol` | **跨工作区搜索符号** | 整个工作区 |
| `goToImplementation` | 查找接口/抽象方法的实现 | 工作区级 |
| `prepareCallHierarchy` | 获取调用层次结构 | 单符号 |
| `incomingCalls` | 查找调用当前函数的所有位置 | 工作区级 |
| `outgoingCalls` | 查找当前函数调用的所有位置 | 工作区级 |

### 5.3 输入参数

所有操作共享相同的输入结构：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `operation` | enum | 是 | 要执行的 LSP 操作 |
| `filePath` | string | 是 | 文件路径 |
| `line` | number | 是 | 行号（1-based） |
| `character` | number | 是 | 字符偏移（1-based） |

### 5.4 启用条件

- 需要环境支持 LSP 服务器（通过 `ENABLE_LSP_TOOL` 控制）
- 需要对应语言的 LSP 服务器已配置
- 文件大小不超过 10MB（`MAX_LSP_FILE_SIZE_BYTES`）

### 5.5 与 Grep 的区别

| 维度 | Grep | LSP |
|------|------|-----|
| 搜索方式 | 文本正则匹配 | 语义理解 |
| 精确度 | 可能有误报（同名变量） | 精确到语义层 |
| 跨语言 | 支持任何文本文件 | 需要对应语言的 LSP 服务器 |
| 典型用途 | 搜索字符串/模式 | 查找定义、引用、实现 |

---

## 6. ToolSearch — 延迟工具检索

> 源码位置：`src/tools/ToolSearchTool/ToolSearchTool.ts`、`src/tools/ToolSearchTool/prompt.ts`

### 6.1 定位

ToolSearch **不是代码搜索工具**，而是 Claude Code 的**工具元数据搜索**机制。它用于在延迟加载（deferred）的工具列表中，按名称或关键词查找工具定义，获取完整的 JSON Schema 后才能调用。

### 6.2 使用场景

Claude Code 有大量工具（特别是 MCP 工具），不可能在每次对话开始时就把所有工具的完整 schema 都放入提示词。因此采用**延迟加载**策略：

```
初始提示词 → 仅包含工具名列表（无参数 schema）
模型需要某工具 → 调用 ToolSearch 获取完整 schema
获得 schema 后 → 可以正常调用该工具
```

### 6.3 查询语法

| 查询形式 | 示例 | 说明 |
|---------|------|------|
| `select:` 前缀 | `select:Read,Edit,Grep` | 按名称直接获取工具 |
| 关键词 | `notebook jupyter` | 关键词搜索，返回最佳匹配 |
| `+` 必选词 | `+slack send` | "slack" 必须出现在工具名中 |

### 6.4 哪些工具会被延迟加载

```typescript
// 以下工具会被延迟加载（需要 ToolSearch 拉取）：
// 1. 所有 MCP 工具（除非标记了 anthropic/alwaysLoad）
// 2. 标记了 shouldDefer: true 的内置工具（如 WebSearch）

// 以下工具永远不会被延迟：
// - ToolSearch 自身
// - Agent 工具（当 Fork Subagent 启用时）
// - Brief 工具
```

---

## 7. 嵌入式搜索模式（Ant-Native）

> 源码位置：`src/utils/embeddedTools.ts`

### 7.1 什么是嵌入式搜索

在 Anthropic 内部构建（ant-native）版本中，`bfs`（文件查找）和 `ugrep`（内容搜索）被静态编译到 Bun 二进制文件中。此时：

1. **Glob 和 Grep 工具从注册表中移除**
2. `find` 和 `grep` 命令在 Bash 中被 shell function 重定向到内嵌实现
3. 提示词中关于"不要用 Bash 的 find/grep"的引导也会被移除

### 7.2 判定逻辑

```typescript
function hasEmbeddedSearchTools(): boolean {
  // 需要同时满足：
  // 1. EMBEDDED_SEARCH_TOOLS 环境变量为 true
  // 2. 入口点不是 SDK 或 local-agent
  if (!isEnvTruthy(process.env.EMBEDDED_SEARCH_TOOLS)) return false
  const e = process.env.CLAUDE_CODE_ENTRYPOINT
  return e !== 'sdk-ts' && e !== 'sdk-py' && e !== 'sdk-cli' && e !== 'local-agent'
}
```

### 7.3 对工具注册的影响

```typescript
// src/tools.ts
export function getAllBaseTools(): Tools {
  return [
    AgentTool,
    TaskOutputTool,
    BashTool,
    // 嵌入式构建时不注册独立的 Glob/Grep
    ...(hasEmbeddedSearchTools() ? [] : [GlobTool, GrepTool]),
    // ...其他工具
  ]
}
```

### 7.4 对提示词的影响

| 场景 | 标准模式 | 嵌入式模式 |
|------|---------|-----------|
| 系统提示 | "To search files use Glob, not find" | 不显示此提示 |
| Bash 提示 | "Never invoke grep or rg as Bash command" | 不显示此提示 |
| Explore Agent | "用 Glob 和 Grep 搜索" | "用 Bash 中的 find 和 grep 搜索" |

---

## 8. 底层实现：ripgrep 集成

> 源码位置：`src/utils/ripgrep.ts`、`src/utils/glob.ts`

### 8.1 ripgrep 的三种运行模式

Claude Code 支持三种 ripgrep 运行方式：

| 模式 | 触发条件 | 执行方式 |
|------|---------|---------|
| **system** | `USE_BUILTIN_RIPGREP=false` 且系统安装了 `rg` | 直接调用系统 `rg` |
| **embedded** | Bun 内嵌构建 | `spawn(process.execPath, args, { argv0: 'rg' })` |
| **builtin** | 默认（npm 安装） | 使用 `vendor/ripgrep/` 目录下的预编译二进制 |

### 8.2 关键配置参数

| 参数/环境变量 | 说明 | 默认值 |
|-------------|------|--------|
| `USE_BUILTIN_RIPGREP` | 设为 `false` 使用系统 rg | 未设置（使用内置） |
| `CLAUDE_CODE_GLOB_TIMEOUT_SECONDS` | 搜索超时（秒） | Linux/Mac: 20s, WSL: 60s |
| `CLAUDE_CODE_GLOB_NO_IGNORE` | 是否忽略 .gitignore | `true` |
| `CLAUDE_CODE_GLOB_HIDDEN` | 是否包含隐藏文件 | `true` |

### 8.3 错误处理与重试

```
ripGrep 调用
  ├─ 退出码 0 → 正常返回结果
  ├─ 退出码 1 → 无匹配（正常）
  ├─ EAGAIN 错误 → 单线程模式重试（-j 1）
  ├─ 超时 → 如有部分结果则返回，否则抛 RipgrepTimeoutError
  ├─ 缓冲区溢出 → 返回部分结果（丢弃最后一行）
  └─ 关键错误（ENOENT/EACCES/EPERM）→ 直接拒绝
```

> EAGAIN 重试的设计注释：仅对当次调用使用 `-j 1`，不持久化为全局配置。因为在大型仓库中全局单线程会导致超时，而 EAGAIN 通常只是启动时的瞬态错误。

### 8.4 macOS 代码签名

在 macOS 上首次使用内置 ripgrep 时，会自动检查并执行代码签名（`codesign --sign -`），防止 Gatekeeper 拦截。

---

## 9. 提示词引导策略：何时用哪个工具

Claude Code 通过多层提示词引导模型选择正确的搜索工具。

### 9.1 系统提示中的工具使用指南

来源：`src/constants/prompts.ts` 的 `getUsingYourToolsSection()`

```
- To search for files use Glob instead of find or ls
- To search the content of files, use Grep instead of grep or rg
- Reserve using the Bash exclusively for system commands...
```

### 9.2 Bash 工具中的限制指引

来源：`src/tools/BashTool/prompt.ts`

```
File search:    Use Glob  (NOT find or ls)
Content search: Use Grep  (NOT grep or rg)
```

### 9.3 PowerShell 中的限制指引

来源：`src/tools/PowerShellTool/prompt.ts`

```
File search:    Use Glob  (NOT Get-ChildItem -Recurse)
Content search: Use Grep  (NOT Select-String)
```

### 9.4 Agent 工具中的搜索升级建议

来源：`src/tools/AgentTool/prompt.ts`

对于简单搜索不需要使用 Agent，直接用 Glob/Grep：

- 读取确定路径 → 用 Read
- 搜索类名等 → 用 Glob
- 在少数文件中搜索 → 用 Read

但对于**需要多轮搜索**的开放式探索，应该使用 Agent（Explore 子代理），至少需要 3 个以上查询才值得启动：

```
For broader codebase exploration ... use the Agent tool with subagent_type=Explore
... slower ... only when ... more than 3 queries
```

### 9.5 搜索工具选择决策树

```
需要搜索内容？
├─ 是的，在本地代码库中搜索
│   ├─ 按文件名查找 → Glob
│   ├─ 按文件内容搜索
│   │   ├─ 简单正则/关键词 → Grep
│   │   ├─ 语义级（定义、引用、实现） → LSP
│   │   └─ 多轮探索（>3 次查询） → Agent(Explore)
│   └─ 不确定在哪里 → 先 Glob 定位，再 Grep 搜内容
├─ 是的，在互联网上搜索 → WebSearch
└─ 是的，查找可用工具 → ToolSearch
```

---

## 10. 实际搜索工作流程：端到端场景分析

上面各章节介绍了单个搜索工具的能力，但实际使用中模型**不是**孤立调用某一个工具，而是按照**分层升级**策略，根据任务复杂度组合多个工具完成搜索。本章通过具体场景还原真实的搜索过程。

### 10.1 核心搜索链路：Glob → Grep → Read

Claude Code 的本地搜索不是"某一个搜索工具"，而是三个工具的组合链路：

```
Glob（找到文件）→ Grep（找到内容）→ Read（读取上下文）
```

这是系统提示词中反复强调的基本模式。LSP 和 Agent(Explore) 是在此基础上的**精度增强**和**自动化升级**。

### 10.2 场景一：搜索文档（Markdown 等文本文件）

**需求**："找一下之前写的关于 hooks 的文档"

**路径 A — 文件名已知或可猜测：**

```
第 1 步：Glob("**/*hooks*.md")
  → 返回：claudecode-hooks系统设计与插件协同.md  ✅ 命中

第 2 步：Read("doc/claudecode/claudecode-hooks系统设计与插件协同.md")
  → 读取全文
```

**路径 B — 只知道内容关键词：**

例如用户说"之前有个讲代码控制和提示词控制区别的文档"。

```
第 1 步：Grep("代码控制.*提示词|提示词.*代码控制", glob: "*.md")
  → 默认 output_mode=files_with_matches，返回匹配文件列表

第 2 步：Read(命中的文件)
  → 读取确认内容
```

> **要点**：搜索文档时 **LSP 完全不参与**——LSP 只理解代码符号（函数名、类名），对 Markdown/文本内容无效。

### 10.3 场景二：搜索代码定义

**需求**："找到 ripGrep 函数的定义"

根据信息的确定程度，有三种递进路径：

**路径 A — 直接 Grep（最常见，90% 场景）：**

```
第 1 步：Grep("function ripGrep|export.*ripGrep|async function ripGrep", type: "ts")
  → output_mode=files_with_matches
  → 返回：src/utils/ripgrep.ts

第 2 步：Grep("export.*function ripGrep|export async function ripGrep", 
              path: "src/utils/ripgrep.ts", output_mode: "content")
  → 返回具体行号和内容

第 3 步：Read("src/utils/ripgrep.ts", offset: 345, limit: 50)
  → 读取函数完整实现
```

> 系统提示原文：*"For simple, directed codebase searches (e.g. for a specific file/class/function) use Grep/Glob directly."*

**路径 B — Grep 定位 + LSP 精确跳转（需要语义精度时）：**

```
第 1 步：Grep("ripGrep", type: "ts", output_mode: "content", head_limit: 10)
  → 找到某个调用 ripGrep 的文件和行号，如 GrepTool.ts:441

第 2 步：LSP(operation: "goToDefinition", filePath: "GrepTool.ts", line: 441, character: 50)
  → 精确跳转到 ripgrep.ts 中的定义位置

第 3 步（可选）：LSP(operation: "findReferences", ...)
  → 找到所有调用 ripGrep() 的位置
```

> **LSP 的前提限制**：必须先知道一个符号在某文件中的具体位置（行号 + 列号），不能凭空搜索。所以 LSP 通常**不是第一步**，而是 Grep 找到位置后的精确补充。

**路径 C — 搜索更具体的语义关系：**

| 需求 | 工具组合 |
|------|---------|
| "ripGrep 在哪里定义的？" | Grep → Read 或 Grep → LSP(goToDefinition) |
| "谁调用了 ripGrep？" | LSP(findReferences) 或 Grep("ripGrep\\(") |
| "ripGrep 的返回类型是什么？" | LSP(hover) |
| "ripgrep.ts 里有哪些导出函数？" | LSP(documentSymbol) |
| "跨工作区找所有叫 search 的符号" | LSP(workspaceSymbol) |

### 10.4 场景三：开放式代码探索

**需求**："Claude Code 是怎么处理搜索超时的？"

这类问题没有明确的搜索目标，需要多轮探索。系统提示明确规定：**超过 3 次查询时应升级为 Agent(Explore)**。

```
第 1 步：主模型判断 → 这是开放式问题，需要 >3 次查询

第 2 步：启动 Agent(subagent_type=Explore, prompt="分析搜索超时处理机制")

第 3 步：Explore 子代理内部自主执行（多步并行）：
  ┌─ Grep("timeout", path: "src/utils/ripgrep.ts", output_mode: "content")
  ├─ Grep("RipgrepTimeoutError")                    ← 并行
  ├─ Grep("CLAUDE_CODE_GLOB_TIMEOUT")               ← 并行
  └─ Read("src/utils/ripgrep.ts", 相关行范围)

第 4 步：Explore 根据第一轮结果继续深入：
  ├─ Grep("defaultTimeout|timeout.*=", path: "src/utils/ripgrep.ts")
  ├─ Read(发现的关联文件)
  └─ 汇总分析结论

第 5 步：Explore 返回结构化结论给主模型
```

> 系统提示原文：*"For broader codebase exploration and deep research, use the Agent tool with subagent_type=Explore. This is slower than using Grep/Glob directly, so use this only when a simple, directed search proves to be insufficient or when your task will clearly require more than 3 queries."*

Explore 子代理的关键特性：
- **只读**：不能修改任何文件
- **并行搜索**：鼓励同时发起多个 Grep/Glob/Read 调用
- **自主决策**：不需要主模型逐步指挥，自己规划搜索策略

### 10.5 场景四：联网搜索

**需求**："React 19 的 use() hook 有什么新变化？"

```
第 1 步：主模型判断 → 需要最新信息，超出训练数据范围

第 2 步：WebSearch(query: "React 19 use hook changes 2026")
  → 内部：新模型请求 + Anthropic 服务端执行最多 8 次搜索
  → 返回：搜索结果 + 文本总结

第 3 步：主模型基于搜索结果回答用户，附带 Sources 段落
```

### 10.6 完整搜索决策流程图

```
用户提出搜索需求
│
├─ 搜索互联网信息？ ──── 是 → WebSearch
│
├─ 搜索可用工具的 schema？ ──── 是 → ToolSearch
│
└─ 搜索本地代码库
    │
    ├─ 知道文件名或类型？
    │   └─ 是 → Glob("**/*.xxx") → Read
    │
    ├─ 搜索文件内容中的文本/模式？
    │   ├─ 简单关键词/正则 → Grep → Read
    │   └─ 多个关键词组合 → Grep(pattern1) + Grep(pattern2) 并行 → Read
    │
    ├─ 需要精确语义信息？（定义/引用/实现/类型）
    │   └─ 先 Grep 定位文件和行 → LSP（goToDefinition / findReferences / hover）
    │
    ├─ 开放式问题，预计需要 >3 次查询？
    │   └─ Agent(Explore) 子代理
    │       └─ 内部自主编排：Glob + Grep + Read（并行多轮）
    │
    └─ 不确定从哪里找？
        └─ 先 Glob 大范围扫描 → 缩小范围 → Grep 精确搜内容 → Read
```

### 10.7 各场景搜索路径总结

| 搜索场景 | 核心工具链 | LSP 参与？ | Agent 参与？ |
|---------|-----------|-----------|------------|
| 按文件名找文档 | Glob → Read | ❌ | ❌ |
| 按内容找文档 | Grep → Read | ❌ | ❌ |
| 找代码定义（简单） | Grep → Read | ❌ | ❌ |
| 找代码定义（精确） | Grep → LSP(goToDefinition) → Read | ✅ 补充 | ❌ |
| 找所有引用/调用方 | Grep 或 LSP(findReferences) | ✅ 核心 | ❌ |
| 理解函数类型/文档 | LSP(hover) | ✅ 核心 | ❌ |
| 开放式代码探索 | Agent(Explore) = Glob + Grep + Read | ❌ | ✅ 核心 |
| 搜索互联网 | WebSearch | ❌ | ❌ |

> **核心结论**：搜索不是 Grep + LSP 的简单叠加。**Glob → Grep → Read 三件套**是最基本、最高频的搜索链路，LSP 只在需要语义精确度时补充登场，而 Agent(Explore) 是将"人类反复搜索 → 阅读 → 再搜索"的多轮流程自动化。

---

## 11. 子代理工具访问矩阵

不同类型的子代理对搜索工具有不同的访问权限：

### 11.1 工具权限表

| 子代理类型 | Grep | Glob | WebSearch | LSP | ToolSearch |
|-----------|------|------|-----------|-----|------------|
| **主代理** | ✅ | ✅ | ✅ | ✅* | ✅ |
| **Explore** | ✅ | ✅ | — | ✅* | ✅ |
| **Plan** | ✅ | ✅ | — | ✅* | ✅ |
| **Verification** | ✅ | ✅ | — | ✅* | ✅ |
| **General Purpose** | ✅ | ✅ | ✅ | ✅* | ✅ |
| **异步后台代理** | ✅ | ✅ | ✅ | ❌ | ✅ |
| **claude-code-guide** | ✅ | ✅ | ✅ | ❌ | ❌ |

> `*` 表示取决于 LSP 是否启用和注册。

### 11.2 异步代理白名单

异步后台代理使用严格的白名单，搜索相关工具明确列入：

```typescript
// src/constants/tools.ts
export const ASYNC_AGENT_ALLOWED_TOOLS = new Set([
  GREP_TOOL_NAME,      // ✅ 明确包含
  GLOB_TOOL_NAME,      // ✅ 明确包含
  WEB_SEARCH_TOOL_NAME, // ✅ 明确包含
  TOOL_SEARCH_TOOL_NAME, // ✅ 明确包含
  FILE_READ_TOOL_NAME,
  // ...其他非搜索工具
])
```

### 11.3 Explore 子代理的搜索策略

Explore 是 Claude Code 中最常用的搜索型子代理，其提示词要求：

1. **只读**：不能修改文件
2. **并行搜索**：鼓励同时发起多个 Grep/Read 调用
3. **工具选择**：非嵌入式构建用 Glob + Grep，嵌入式构建用 Bash 的 find + grep

---

## 12. 设计思考与总结

### 12.1 为什么 Grep/Glob 的底层都是 ripgrep？

Claude Code 选择将 ripgrep 作为**唯一的文件系统搜索引擎**，有以下好处：
- **统一依赖**：只需维护一个搜索二进制
- **性能优异**：ripgrep 在大型代码库中表现极佳
- **跨平台**：支持 Linux、macOS、Windows
- **功能灵活**：`--files` 模式变身文件查找器，正则模式做内容搜索

### 12.2 为什么要禁止在 Bash 中使用 grep/find？

1. **权限管控**：Grep/Glob 工具内置了文件读取权限检查（`checkReadPermissionForTool`），直接在 Bash 中运行 grep 会绕过这一安全层
2. **输出标准化**：工具返回结构化 JSON，而 Bash 输出是原始文本，不利于后续处理
3. **Token 管控**：工具有内置的结果截断机制（`head_limit`、`maxResultSizeChars`），防止搜索结果过大导致上下文爆炸
4. **路径优化**：工具自动将绝对路径转为相对路径以节省 token

### 12.3 搜索体系的分层设计

```
┌─────────────────────────────────────────────────────┐
│                     模型决策层                        │
│  系统提示 + 工具描述 → 选择正确的搜索工具              │
├─────────────────────────────────────────────────────┤
│                     工具接口层                        │
│  Grep / Glob / WebSearch / LSP / ToolSearch          │
│  参数校验、权限检查、结果格式化、token 管控            │
├─────────────────────────────────────────────────────┤
│                     执行引擎层                        │
│  ripgrep (内置/系统/内嵌) │ Anthropic API │ LSP 服务器 │
├─────────────────────────────────────────────────────┤
│                     安全与治理层                       │
│  文件权限、UNC 路径拦截、VCS 排除、忽略模式            │
└─────────────────────────────────────────────────────┘
```

### 12.4 与 Cursor 搜索工具的对应关系

| Cursor 工具 | Claude Code 对应 | 说明 |
|------------|-----------------|------|
| Grep | Grep | 完全对应，都基于 ripgrep |
| Glob | Glob | 完全对应，都基于 ripgrep `--files` |
| SemanticSearch | **无对应** | Cursor 的 SemanticSearch 是 IDE 侧能力，利用向量索引实现语义搜索 |
| WebSearch | WebSearch | 类似，但 Cursor 版可能不依赖 Anthropic 服务端工具 |
| — | LSP | Claude Code 额外提供了 LSP 工具，部分替代了 SemanticSearch 的"精确语义查找"能力 |
| — | ToolSearch | Claude Code 特有的工具元数据搜索，Cursor 不需要（IDE 管理工具注册） |
