# Claude Code Cache 缓存体系深度分析

> 基于 Claude Code 源码的全面缓存模块逆向分析

## 1. 缓存体系总览

Claude Code 的缓存系统是一个**多层次、多维度**的设计，覆盖从 Anthropic API 侧的 Prompt Cache，到本地文件 I/O 缓存，再到 UI 渲染缓存等各个层面。其核心目标可以归纳为三点：

1. **降低 API 成本** — 最大化利用 Anthropic 服务端的 Prompt Cache，减少 `cache_creation` tokens
2. **提升响应速度** — 避免重复磁盘读取、重复计算、重复渲染
3. **保证前缀稳定性** — 确保多轮对话中发送给 API 的字节流前缀不变，以命中服务端缓存

```
┌─────────────────────────────────────────────────────────────────┐
│                     Claude Code 缓存架构                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │           Layer 1: API Prompt Cache 层                     │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐  │  │
│  │  │getCacheControl│ │toolSchemaCache│ │cacheSafeParams   │  │  │
│  │  │(ephemeral/1h) │ │(schema锁定)   │ │(fork共享前缀)    │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘  │  │
│  │  ┌──────────────┐ ┌──────────────────────────────────┐   │  │
│  │  │queryContext   │ │promptCacheBreakDetection          │   │  │
│  │  │(前缀构建)     │ │(失效检测+根因分析)                │   │  │
│  │  └──────────────┘ └──────────────────────────────────┘   │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │           Layer 2: 本地文件/状态缓存层                     │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐  │  │
│  │  │fileReadCache  │ │fileStateCache │ │statsCache         │  │  │
│  │  │(mtime失效)    │ │(LRU+25MB上限) │ │(磁盘持久化+锁)    │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘  │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐  │  │
│  │  │settingsCache  │ │syncCacheState │ │completionCache    │  │  │
│  │  │(三级分层)     │ │(远端设置缓存) │ │(shell补全脚本)    │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │           Layer 3: 插件 Zip 缓存层                         │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐  │  │
│  │  │zipCache       │ │zipCacheAdapter│ │cacheUtils         │  │  │
│  │  │(ZIP压缩存储)  │ │(原子写入I/O)  │ │(孤儿版本GC)       │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │           Layer 4: 通用缓存工具层                           │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐  │  │
│  │  │memoizeWithTTL │ │memoizeWithLRU │ │cachePaths         │  │  │
│  │  │(写穿TTL缓存)  │ │(LRU淘汰策略)  │ │(磁盘路径布局)     │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │           Layer 5: UI 渲染缓存层                            │  │
│  │  ┌──────────────┐ ┌──────────────────────────────────┐   │  │
│  │  │node-cache     │ │line-width-cache                   │   │  │
│  │  │(WeakMap布局)  │ │(stringWidth测量缓存)               │   │  │
│  │  └──────────────┘ └──────────────────────────────────┘   │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │           统一清理: clearSessionCaches()                     │  │
│  │  清理 30+ 个独立缓存状态，涵盖上述所有层次                    │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## 2. Layer 1: API Prompt Cache — 成本优化的核心

### 2.1 Anthropic Prompt Cache 原理

Anthropic API 支持 **Prompt Cache** — 当连续请求的前缀（system prompt + tools + 消息历史前N条）字节完全一致时，服务端缓存 KV 状态，后续请求可以直接复用，只需支付 `cache_read` 的费用（约为 `cache_creation` 的 1/10）。

Claude Code 为此构建了一套精密的 **前缀稳定性保障体系**。

### 2.2 `getCacheControl()` — 缓存标记策略

```typescript
// src/services/api/claude.ts
export function getCacheControl({
  scope,
  querySource,
}: {
  scope?: CacheScope
  querySource?: QuerySource
} = {}): {
  type: 'ephemeral'
  ttl?: '1h'
  scope?: CacheScope
} {
  return {
    type: 'ephemeral',
    ...(should1hCacheTTL(querySource) && { ttl: '1h' }),
    ...(scope === 'global' && { scope }),
  }
}
```

**关键设计点：**

| 特性 | 说明 |
|------|------|
| **默认 TTL** | `ephemeral` 类型，服务端默认 5 分钟有效 |
| **1 小时 TTL** | 符合条件时升级到 `1h`，需用户为 ant 内部员工或有效订阅者 |
| **Scope 控制** | 支持 `global` scope（跨组织缓存共享），通过 beta header 启用 |
| **TTL 锁定** | 使用 `STATE` 全局锁定 TTL 策略，防止 GrowthBook 配置变更导致中途切换 TTL，引发缓存失效 |
| **Bedrock 支持** | 3P Bedrock 用户通过 `ENABLE_PROMPT_CACHING_1H_BEDROCK` 环境变量启用 1h TTL |

### 2.3 `cache_control` 标记放置策略

Claude Code 遵循 **严格的单标记原则**：

```
每次 API 请求只放置一个 message 级别的 cache_control 标记
```

原因在代码注释中说明：

> Mycro 的 turn-to-turn eviction (page_manager/index.rs) 会释放任何**不在** `cache_store_int_token_boundaries` 中的缓存前缀位置的 local-attention KV pages。两个标记会导致倒数第二个位置被保护，即使没有请求会从该位置恢复。

**标记放置位置规则：**

1. **System Prompt 块** — 每个 `cacheScope !== null` 的文本块都标记 `cache_control`
2. **Tool Schema 最后一项** — 在 tool 列表末尾添加 `cache_control`
3. **最后一条 User 消息** — 在消息内容最后一个 content block 上标记
4. **cache_reference** — 在 `cache_control` 标记之前的 `tool_result` 块上添加

### 2.4 `toolSchemaCache` — 工具 Schema 锁定

```typescript
// src/utils/toolSchemaCache.ts
const TOOL_SCHEMA_CACHE = new Map<string, CachedSchema>()
```

**设计目的：** 工具 Schema 在 API 请求中位于 position 2（system prompt 之后），任何字节级变化都会导致整个 ~11K token 的工具块及其下游所有内容的缓存失效。

**稳定性威胁来源：**
- GrowthBook 特性开关变化（`tengu_tool_pear`, `tengu_fgts` 等）
- MCP 服务器重连导致的工具列表变化
- 工具 `prompt()` 方法中的动态内容

**锁定策略：** 每个会话首次渲染工具 Schema 后，将结果缓存在 Map 中。后续的 GrowthBook 刷新不再影响 Schema 字节。

### 2.5 `CacheSafeParams` — Fork 子代理共享缓存前缀

这是一个非常精妙的设计：当 Claude Code 需要运行"侧问题"（side question）、投机执行（speculation）、后续摘要（post-turn summary）等子任务时，会 fork 出子代理。子代理需要与父代理**共享相同的 API 缓存前缀**才能命中 Prompt Cache。

```typescript
// src/utils/forkedAgent.ts
export type CacheSafeParams = {
  systemPrompt: SystemPrompt
  userContext: { [k: string]: string }
  systemContext: { [k: string]: string }
  toolUseContext: ToolUseContext
  forkContextMessages: Message[]
}
```

**共享条件要求：**
- system prompt 完全相同
- tools 列表完全相同
- model 完全相同
- messages 前缀（历史消息）完全相同
- thinking config 完全相同（fork 子代理继承父代理的 thinkingConfig）

**实现细节：**
- 每轮结束后通过 `saveCacheSafeParams()` 保存快照
- 子代理通过 `getLastCacheSafeParams()` 获取
- `skipCacheWrite` 选项：一次性 fork 不写入新缓存条目

### 2.6 前缀字节稳定性的极致追求

Claude Code 在多个层面确保发送给 API 的前缀字节不变：

**a) 工具排序稳定性：**
```typescript
// src/utils/toolPool.ts
// 内置工具保持连续前缀，MCP 工具追加在后
const [mcp, builtIn] = partition(
  uniqBy([...initialTools, ...assembled], 'name'),
  /* ... */
)
```

**b) Tool Result 替换稳定性：**
```typescript
// src/utils/toolResultStorage.ts
// 一旦决定了某个 tool_result 是否替换，该决定在整个会话中冻结
// 之前替换过的结果使用完全相同的替换字符串（零 I/O，字节级一致）
// 之前未替换过的结果永不事后替换
```

**c) 时间信息稳定性：**
```typescript
// src/utils/attachments.ts
// 记忆文件的 "saved 3 days ago" 在附件创建时冻结 header
// 避免 Date.now() 重算导致 "saved 4 days ago" → 字节变化 → 缓存失效
```

**d) 日期变更处理：**
```typescript
// messages[0] 中的日期信息故意保持"过期"
// 新日期通过 date_change 附件追加到对话尾部
// 避免清理前缀缓存导致 ~920K effective tokens 的重新计算
```

**e) 临时目录路径规范化：**
```typescript
// src/tools/BashTool/prompt.ts
// 将 /private/tmp/claude-1001/ 替换为 $TMPDIR
// 确保跨用户的 global prompt cache 命中
```

## 3. Layer 2: Prompt Cache Break Detection — 缓存失效检测系统

### 3.1 两阶段检测架构

`promptCacheBreakDetection.ts` 实现了一套**两阶段缓存失效检测系统**，堪称整个缓存体系中最复杂的模块：

```
Phase 1 (Pre-call): recordPromptState()
  → 记录 system prompt / tools / model / betas 等的 hash
  → 与上一次调用比对，标记 pendingChanges

Phase 2 (Post-call): checkResponseForCacheBreak()
  → 检查 API 返回的 cache_read_tokens 是否下降 >5%
  → 结合 Phase 1 的 pendingChanges 生成根因诊断
  → 触发 analytics 事件 + 写入 diff 文件
```

### 3.2 监控的状态维度

| 维度 | Hash/比对字段 | 说明 |
|------|-------------|------|
| System Prompt | `systemHash` | 去除 `cache_control` 后的内容 hash |
| Cache Control | `cacheControlHash` | 带 `cache_control` 的完整 hash（捕获 scope/TTL 切换） |
| Tool Schemas | `toolsHash` + `perToolHashes` | 聚合 hash + 逐工具 hash（定位是哪个工具变了） |
| Model | 字符串比对 | 模型切换 |
| Fast Mode | 布尔比对 | 快速模式切换 |
| Betas | 排序后比对 | Beta header 增减 |
| Auto Mode | 布尔比对 | AFK 模式（已 sticky-on 锁定，理论不应再触发） |
| Overage | 布尔比对 | 超额状态（已 TTL 锁定，理论不应再触发） |
| Cached MC | 布尔比对 | Cached Microcompact 开关 |
| Effort | 字符串比对 | Effort 值变化 |
| Extra Body | hash 比对 | `CLAUDE_CODE_EXTRA_BODY` 等额外参数变化 |

### 3.3 误报排除机制

系统设计了多层误报排除：

1. **TTL 过期排除** — 如果距上次 assistant 消息 >5min 或 >1h，标记为 TTL 过期而非客户端 bug
2. **Compaction 排除** — `notifyCompaction()` 重置 baseline，压缩后 token 下降是预期行为
3. **Cache Deletion 排除** — `notifyCacheDeletion()` 标记 cached microcompact 删除操作
4. **最小阈值** — token 下降必须 ≥2000 才触发警告
5. **百分比阈值** — 下降必须 >5% 才触发
6. **Haiku 排除** — Haiku 模型有不同缓存行为，排除检测
7. **服务端归因** — 无客户端变化 + <5min 间隔 → 归因为 "likely server-side"

### 3.4 诊断输出

当检测到缓存失效时，输出包含：

```
[PROMPT CACHE BREAK] system prompt changed (+150 chars), tools changed (+1/-0 tools)
  [source=repl_main_thread, call #5, cache read: 45000 → 2000, creation: 43000,
   diff: /tmp/claude-xxx/cache-break-a1b2.diff]
```

- **分析事件** (`tengu_prompt_cache_break`) 发送到 analytics
- **Diff 文件** 写入临时目录，方便开发者调试

## 4. Layer 3: 本地文件/状态缓存层

### 4.1 `FileReadCache` — 文件内容缓存

```typescript
// src/utils/fileReadCache.ts
class FileReadCache {
  private cache = new Map<string, CachedFileData>()
  private readonly maxCacheSize = 1000
}
```

| 特性 | 实现 |
|------|------|
| **缓存键** | 文件路径 |
| **失效策略** | 基于 `mtime`（修改时间）— 每次读取先 `statSync` 检查时间戳 |
| **容量限制** | 最多 1000 个文件条目，超出时 FIFO 淘汰 |
| **编码感知** | 自动检测文件编码（`detectFileEncoding`） |
| **行尾规范** | 自动将 `\r\n` 转为 `\n` |
| **单例模式** | 全局唯一实例 `fileReadCache` |

**主要应用场景：** FileEditTool 操作中避免重复读盘。

### 4.2 `FileStateCache` — 文件状态缓存（LRU）

```typescript
// src/utils/fileStateCache.ts
export class FileStateCache {
  private cache: LRUCache<string, FileState>
  constructor(maxEntries: number, maxSizeBytes: number) {
    this.cache = new LRUCache<string, FileState>({
      max: maxEntries,
      maxSize: maxSizeBytes,         // 默认 25MB
      sizeCalculation: value => Math.max(1, Buffer.byteLength(value.content)),
    })
  }
}
```

| 特性 | 实现 |
|------|------|
| **淘汰策略** | LRU（Least Recently Used） |
| **容量限制** | 条目数 + 内存字节数双重限制（默认 100 条 / 25MB） |
| **路径规范化** | 所有 key 经过 `normalize()` 处理，解决相对/绝对路径不一致 |
| **Partial View** | 支持标记自动注入内容（如 CLAUDE.md）不匹配磁盘的情况 |
| **序列化支持** | 提供 `dump()` / `load()` 用于 compact 持久化 |
| **合并语义** | `mergeFileStateCaches()` 按时间戳合并，新覆盖旧 |
| **克隆支持** | `cloneFileStateCache()` 保留 size limit 配置 |

**FileState 结构：**
```typescript
type FileState = {
  content: string        // 文件内容
  timestamp: number      // 读取时间戳
  offset: number | undefined   // 读取偏移
  limit: number | undefined    // 读取行数限制
  isPartialView?: boolean      // 是否为部分视图（自动注入内容可能不匹配磁盘）
}
```

### 4.3 `StatsCache` — 统计数据持久化缓存

这是一个完整的**磁盘持久化缓存系统**，带版本控制和原子写入：

```typescript
// src/utils/statsCache.ts
export type PersistedStatsCache = {
  version: number                    // 缓存版本号（当前 v3）
  lastComputedDate: string | null    // 最后计算日期
  dailyActivity: DailyActivity[]     // 每日活跃数据（有界）
  dailyModelTokens: DailyModelTokens[]  // 每日模型 token 用量（有界）
  modelUsage: { [modelName: string]: ModelUsage }  // 模型用量（按模型数有界）
  totalSessions: number              // 总会话数
  totalMessages: number              // 总消息数
  longestSession: SessionStats | null // 最长会话
  hourCounts: { [hour: number]: number } // 每小时计数（24 条有界）
  totalSpeculationTimeSavedMs: number // 投机节省时间
  shotDistribution?: { [shotCount: number]: number } // Shot 分布
}
```

**核心设计亮点：**

1. **原子写入** — `temp file + fsync + rename` 模式防止数据损坏
2. **并发锁** — `withStatsCacheLock` 内存锁防止并发读写
3. **版本迁移** — 从 v1 到 v3 的向前兼容迁移，保留历史数据
4. **有界设计** — 所有集合类字段都有明确的上界，防止无限增长
5. **增量合并** — `mergeCacheWithNewStats()` 只追加新数据

### 4.4 `SettingsCache` — 三级设置缓存

```typescript
// src/utils/settings/settingsCache.ts
let sessionSettingsCache: SettingsWithErrors | null = null       // 合并后总设置
const perSourceCache = new Map<SettingSource, SettingsJson>()    // 按来源缓存
const parseFileCache = new Map<string, ParsedSettings>()         // 按路径缓存解析结果
```

**三级结构：**

| 层级 | 缓存目标 | 说明 |
|------|---------|------|
| 文件解析缓存 | `parseFileCache` | 同一路径的 JSON 文件只读取 + zod 解析一次 |
| 来源缓存 | `perSourceCache` | 每个 `SettingSource` 的设置独立缓存 |
| 合并缓存 | `sessionSettingsCache` | 所有来源合并后的最终设置 |

**失效触发点：** 设置写入、`--add-dir`、插件初始化、hooks 刷新时统一 `resetSettingsCache()`。

### 4.5 `syncCacheState` — 远端托管设置缓存

解决了一个架构难题：`settings.ts → syncCache.ts → auth.ts → settings.ts` 的循环依赖。

**拆分策略：**
- `syncCacheState.ts` — 叶子模块，只依赖 path/envUtils/file 等基础模块
- `syncCache.ts` — 持有 `isRemoteManagedSettingsEligible()`（需要 auth.ts 的部分）

**三态设计：** `eligible` 字段为 `undefined | false | true`：
- `undefined` — 尚未确定
- `false` — 不符合条件（3P provider、自定义 base URL 等）
- `true` — 符合条件（Console API key 或 Enterprise/Team OAuth）

## 5. Layer 4: 插件 Zip 缓存层

### 5.1 Zip Cache 架构

面向 **headless/ephemeral 容器** 场景（如 Claude Code Remote），插件通过 ZIP 格式存储在持久挂载卷上：

```
/mnt/plugins-cache/
  ├── known_marketplaces.json        # 已知市场索引
  ├── installed_plugins.json          # 已安装插件清单
  ├── marketplaces/
  │   ├── official-marketplace.json   # 市场 JSON
  │   └── company-marketplace.json
  └── plugins/
      ├── official-marketplace/
      │   └── plugin-a/
      │       └── 1.0.0.zip           # 插件 ZIP 包
      └── company-marketplace/
          └── plugin-b/
              └── 2.1.3.zip
```

### 5.2 会话级提取

```
ZIP Cache (持久卷) ──提取──> Session Cache (本地 tmpdir)
                              /tmp/claude-plugin-session-{random}/
```

- 启动时从 ZIP 缓存提取到会话临时目录
- 会话结束时 `cleanupSessionPluginCache()` 清理
- 原子写入 (`atomicWriteToZipCache`) 防止并发写入损坏

### 5.3 孤儿版本清理（GC）

```typescript
// src/utils/plugins/cacheUtils.ts
const CLEANUP_AGE_MS = 7 * 24 * 60 * 60 * 1000 // 7 days
```

**清理流程：**
1. Pass 1: 移除已安装版本的 `.orphaned_at` 标记（防止重装后误删）
2. Pass 2: 未安装的版本
   - 无标记 → 创建 `.orphaned_at`
   - 有标记且 >7 天 → 删除
   - 有标记且 ≤7 天 → 保留

## 6. Layer 5: 通用缓存工具层

### 6.1 `memoizeWithTTL` — 写穿 TTL 缓存

```typescript
// src/utils/memoize.ts
export function memoizeWithTTL<Args extends unknown[], Result>(
  f: (...args: Args) => Result,
  cacheLifetimeMs: number = 5 * 60 * 1000
): MemoizedFunction<Args, Result>
```

**Write-Through Cache 模式：**

```
请求到达
  ├─ 无缓存 → 阻塞计算 → 返回 + 缓存
  ├─ 缓存新鲜 → 立即返回
  └─ 缓存过期 → 立即返回过期值 + 后台刷新
                  ↓
               异步刷新（不阻塞调用者）
```

**并发安全设计：**
- `refreshing` 标记防止多个并行刷新
- Identity guard: `.then`/`.catch` 中检查 `cache.get(key) === cached`，防止 `cache.clear()` 后覆盖新条目

### 6.2 `memoizeWithTTLAsync` — 异步版本

额外增加了 **Cold-miss 去重** (`inFlight` Map)：

```typescript
const inFlight = new Map<string, Promise<Result>>()
```

多个并发调用在冷启动时共享同一个 Promise，避免多次执行昂贵操作（如 `aws sso login`）。

### 6.3 `memoizeWithLRU` — LRU 淘汰缓存

```typescript
export function memoizeWithLRU<Args extends unknown[], Result>(
  f: (...args: Args) => Result,
  cacheFn: (...args: Args) => string,
  maxCacheSize: number = 100
): LRUMemoizedFunction<Args, Result>
```

**解决的问题：** 消息处理函数使用 lodash.memoize 导致 300MB+ 无界内存增长。LRU 策略确保缓存在可控范围内。

**额外能力：**
- `peek()` 观察不更新 recency
- `delete()` / `has()` 手动管理

### 6.4 `cachePaths` — 磁盘缓存路径布局

```typescript
// src/utils/cachePaths.ts
export const CACHE_PATHS = {
  baseLogs: () => join(paths.cache, getProjectDir(cwd)),    // 项目级缓存根
  errors: () => join(paths.cache, getProjectDir(cwd), 'errors'),
  messages: () => join(paths.cache, getProjectDir(cwd), 'messages'),
  mcpLogs: (serverName) => join(paths.cache, getProjectDir(cwd), `mcp-logs-${sanitize(serverName)}`),
}
```

- 使用 `env-paths('claude-cli')` 获取系统标准缓存目录
- 项目名通过 `djb2Hash` 哈希化，确保跨版本稳定（不用 `Bun.hash`）
- 路径长度限制 200 字符 + hash 后缀

## 7. Layer 6: UI 渲染缓存层

### 7.1 `node-cache` — 布局缓存

```typescript
// src/ink/node-cache.ts
export const nodeCache = new WeakMap<DOMElement, CachedLayout>()
export const pendingClears = new WeakMap<DOMElement, Rectangle[]>()
```

使用 **WeakMap** 缓存每个 Ink DOM 节点的布局信息（x, y, width, height, top），优化：
- ScrollBox 视口裁剪：O(dirty) 而非 O(mounted)
- Blit 渲染和脏区清除

### 7.2 `line-width-cache` — 行宽测量缓存

```typescript
// src/ink/line-width-cache.ts
const cache = new Map<string, number>()
const MAX_CACHE_SIZE = 4096
```

流式输出时，已完成的行是不可变的。缓存 `stringWidth` 结果避免对数百行未变内容重复测量，约 **50x 减少** `stringWidth` 调用次数。

超过 4096 条时全部清空（简单策略，一帧即可重建）。

## 8. 统一缓存清理机制

### 8.1 `clearSessionCaches()` — 会话缓存清理

定义在 `src/commands/clear/caches.ts`，在 `/clear` 命令和 `--resume`/`--continue` 时调用。清理 **30+ 个独立缓存状态**：

| 清理目标 | 缓存类型 |
|---------|---------|
| `getUserContext.cache` | memoize 缓存 |
| `getSystemContext.cache` | memoize 缓存 |
| `getGitStatus.cache` | memoize 缓存 |
| `getSessionStartDate.cache` | memoize 缓存 |
| File suggestion caches | @ 提及搜索 |
| Commands cache | 命令列表 |
| Prompt cache break detection | 检测状态 |
| System prompt injection | 缓存打破器 |
| Post-compact cleanup | 系统提示区段、microcompact 跟踪等 |
| Sent skill names | Skill 列表重发标记 |
| Memory files cache | CLAUDE.md 等记忆文件 |
| Stored image paths | 图片路径 |
| Session ingress | UUID 映射、顺序追加 |
| Swarm permission callbacks | 权限回调 |
| Tungsten usage | Tungsten 会话用量 |
| Attribution caches | 文件内容 + 待处理 bash 状态 |
| Repository detection | 仓库检测 |
| Bash command prefix | Haiku 提取的命令前缀 |
| Dump prompts state | Prompt dump 状态 |
| Invoked skills | 技能内容缓存 |
| Git dir resolution | Git 目录解析 |
| Dynamic skills | 动态技能 |
| LSP diagnostic state | LSP 诊断跟踪 |
| Magic docs | 魔法文档跟踪 |
| Session env vars | 会话环境变量 |
| WebFetch URL cache | 最大 50MB 页面缓存 |
| ToolSearch descriptions | 工具描述缓存（~500KB / 50 MCP tools） |
| Agent definitions | 代理定义（EnterWorktreeTool 累积） |
| SkillTool prompt cache | 技能提示缓存 |

### 8.2 `preservedAgentIds` — 选择性清理

当存在需保留的后台代理时，部分缓存**不会**清理：
- `resetPromptCacheBreakDetection` — 跳过（无法安全按 agent 范围清理）
- `clearAllPendingCallbacks` — 跳过
- `clearAllDumpState` — 跳过
- `clearInvokedSkills` — 选择性清理（只清除不在保留列表中的）

### 8.3 `clearAllPluginCaches()` — 插件缓存清理

```typescript
function clearAllPluginCaches(): void {
  clearPluginCache()
  clearPluginCommandCache()
  clearPluginAgentCache()
  clearPluginHookCache()
  pruneRemovedPluginHooks()  // 异步剪枝，fire-and-forget
  clearPluginOptionsCache()
  clearPluginOutputStyleCache()
  clearAllOutputStylesCache()
}
```

## 9. 设计哲学总结

### 9.1 核心原则

| 原则 | 体现 |
|------|------|
| **字节级稳定性** | 工具排序、tool result 替换决定、时间信息冻结、路径规范化 |
| **有界增长** | LRU 淘汰、字节数上限、条目数上限、Map 容量限制 |
| **优雅降级** | 缓存未命中时正常工作，只是性能下降；写穿缓存返回过期值 |
| **原子一致性** | 磁盘写入使用 temp+rename 模式；内存更新使用 identity guard |
| **诊断可观测** | 缓存失效检测系统提供根因分析、diff 文件、analytics 事件 |
| **循环依赖规避** | settings/syncCache 拆分叶子模块打破依赖环 |

### 9.2 与竞品对比

| 维度 | Claude Code | 典型 AI Agent |
|------|------------|---------------|
| Prompt Cache 优化 | 前缀字节级稳定性保障 + 失效检测 + 根因分析 | 通常无特殊处理 |
| 缓存层次 | 5+ 层，从 API 到 UI | 通常 1-2 层 |
| 缓存清理 | 30+ 状态统一清理入口 | 手动 ad-hoc |
| Fork 缓存共享 | CacheSafeParams 精确同步 | 通常不考虑 |
| 磁盘持久化 | 原子写入 + 版本迁移 | 简单 JSON 读写 |
| 内存控制 | LRU + 字节级 size limit | 通常无上界 |

### 9.3 这套缓存体系的成本节省估算

以一个典型的长会话（50 轮对话）为例：

- System prompt + Tools ≈ 11K tokens
- 假设每轮对话前缀 cache hit：节省 11K × 49 次 × (creation - read 单价差) 的 API 费用
- 假设 fork 子代理平均每轮 2 次（speculation + side question）：额外节省 11K × 100 次
- 工具 Schema 锁定避免了 GrowthBook 刷新导致的意外 cache miss

**总结：Claude Code 的缓存体系不只是一个"优化"，而是其能以合理成本运行长会话、高频子代理调用的基础设施。**

## 10. 关键源码文件索引

| 文件 | 作用 |
|------|------|
| `src/services/api/claude.ts` | API 调用核心，`getCacheControl()` + `cache_control` 标记放置 |
| `src/services/api/promptCacheBreakDetection.ts` | 缓存失效两阶段检测系统 |
| `src/utils/toolSchemaCache.ts` | 工具 Schema 会话级锁定 |
| `src/utils/forkedAgent.ts` | CacheSafeParams + fork 缓存共享 |
| `src/utils/queryContext.ts` | API cache-key 前缀构建 |
| `src/utils/toolResultStorage.ts` | Tool result 替换决定冻结 |
| `src/utils/fileReadCache.ts` | 文件内容 mtime 缓存 |
| `src/utils/fileStateCache.ts` | LRU 文件状态缓存 |
| `src/utils/statsCache.ts` | 统计数据磁盘持久化 |
| `src/utils/settings/settingsCache.ts` | 三级设置缓存 |
| `src/utils/memoize.ts` | 写穿 TTL / 异步去重 / LRU memoize |
| `src/utils/cachePaths.ts` | 磁盘缓存路径布局 |
| `src/utils/plugins/zipCache.ts` | 插件 ZIP 缓存 |
| `src/utils/plugins/cacheUtils.ts` | 插件缓存清理 + 孤儿 GC |
| `src/services/remoteManagedSettings/syncCacheState.ts` | 远端设置缓存状态 |
| `src/ink/node-cache.ts` | UI 布局缓存 |
| `src/ink/line-width-cache.ts` | 行宽测量缓存 |
| `src/commands/clear/caches.ts` | 统一缓存清理入口 |
