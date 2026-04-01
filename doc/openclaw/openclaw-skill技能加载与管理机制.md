# OpenClaw Skill 技能加载与管理机制

> 基于 OpenClaw 源码分析，阐述 Skill 的发现、加载、渐进式注入、会话快照与热更新的完整流程。

---

## 1. 整体架构概览

OpenClaw 的 Skill 系统采用 **「目录全量扫描 + Prompt 预算分层 + 正文按需读取」** 的渐进式加载策略。在 Agent Loop 开始时，只将技能目录（名称 + 路径 + 可选描述）注入系统提示，完整的 SKILL.md 正文由模型在后续 turn 中通过 read 工具按需加载。

**加载流程总览（6 步）**：

1. **多源发现**（6 级来源）：extraDirs → bundled → managed → personal → project → workspace
2. **合并去重**：同名后者覆盖前者，`loadSkillEntries()` 输出 `Map<name, Skill>`
3. **门控过滤**：`shouldIncludeSkill()` 按 OS / bins / env / config / enabled / allowlist 过滤
4. **Prompt 预算裁剪**：`applySkillsPromptLimits()` 决定用完整格式（name+desc+location）还是 Compact 格式（仅 name+location）
5. **注入系统提示**（仅目录，不含正文）：`buildAgentSystemPrompt()` 中写入 skillsSection
6. **模型按需读取** SKILL.md 正文：模型调用 read 工具读取选中的技能全文

### 关键源码文件

- `src/agents/skills/workspace.ts` — 核心枢纽：多源加载、合并、prompt 生成、compact 格式、快照构建
- `src/agents/skills/types.ts` — 类型定义：SkillEntry, SkillSnapshot, OpenClawSkillMetadata
- `src/agents/skills/config.ts` — 门控逻辑：shouldIncludeSkill (OS/bins/env/config 过滤)
- `src/agents/skills/frontmatter.ts` — YAML frontmatter 解析、metadata 提取、invocation 策略
- `src/agents/skills/refresh.ts` — 文件监听 (chokidar)、版本管理、热更新触发
- `src/agents/skills/plugin-skills.ts` — 插件技能路径发现
- `src/agents/skills/env-overrides.ts` — 运行时环境变量注入/恢复
- `src/agents/pi-embedded-runner/skills-runtime.ts` — 嵌入式运行时：决定是否从磁盘加载或复用快照
- `src/agents/pi-embedded-runner/run/attempt.ts` — Agent 运行入口：串联 skill 加载 → prompt 构建
- `src/agents/system-prompt.ts` — 系统提示构建：buildSkillsSection
- `src/auto-reply/reply/session-updates.ts` — 会话快照管理：ensureSkillSnapshot
- `src/config/types.skills.ts` — 配置 schema：SkillsConfig
- `docs/tools/skills.md` — 官方 Skill 文档

---

## 2. 技能的定义与结构

### 2.1 磁盘结构

每个 Skill 是一个目录，内含 SKILL.md 文件（带 YAML frontmatter）：

```
skills/
  image-lab/
    SKILL.md          ← 唯一必需文件
    assets/           ← 可选资源
  summarize/
    SKILL.md
```

### 2.2 SKILL.md 格式

```yaml
---
name: image-lab
description: Generate or edit images via a provider-backed image workflow
metadata: {"openclaw": {"requires": {"bins": ["uv"], "env": ["GEMINI_API_KEY"]}, "primaryEnv": "GEMINI_API_KEY"}}
user-invocable: true
disable-model-invocation: false
---

（此处为完整的技能指令正文...）
```

### 2.3 核心类型定义

**SkillEntry**：

- `skill` — pi-coding-agent 的 Skill 对象（name / description / filePath / baseDir）
- `frontmatter` — 解析后的 YAML frontmatter
- `metadata` — openclaw 专属元数据（OpenClawSkillMetadata）
- `invocation` — 调用策略（SkillInvocationPolicy）

**OpenClawSkillMetadata**：

- `always` — 跳过门控，始终包含
- `skillKey` — 配置键名覆盖
- `primaryEnv` — 主要环境变量
- `emoji` — UI 展示 emoji
- `os` — 平台限制（darwin / linux / win32）
- `requires.bins` — 必须全部在 PATH 上
- `requires.anyBins` — 至少一个在 PATH 上
- `requires.env` — 必须存在的环境变量
- `requires.config` — 必须为 truthy 的配置路径
- `install` — 安装器规格数组

**SkillSnapshot**：

- `prompt` — 预渲染的 skills prompt 字符串
- `skills` — 元数据摘要（name / primaryEnv / requiredEnv）
- `skillFilter` — 过滤器
- `resolvedSkills` — 已解析的完整 Skill 对象
- `version` — 版本号，用于失效判断

---

## 3. 六级来源与合并策略

### 3.1 加载来源（按优先级从低到高）

1. **Extra 目录 + 插件目录**（最低）— `skills.load.extraDirs` + 插件 manifest 中的 skills — source: `openclaw-extra`
2. **捆绑技能** — npm 包/App 内部 — source: `openclaw-bundled`
3. **托管/本地技能** — `~/.openclaw/skills` — source: `openclaw-managed`
4. **个人 Agent 技能** — `~/.agents/skills` — source: `agents-skills-personal`
5. **项目 Agent 技能** — `<workspace>/.agents/skills` — source: `agents-skills-project`
6. **工作区技能**（最高）— `<workspace>/skills` — source: `openclaw-workspace`

### 3.2 合并逻辑

```typescript
// workspace.ts:492-511
const merged = new Map<string, Skill>();
// 按优先级依次写入 Map，后者覆盖前者
for (const skill of extraSkills)           merged.set(skill.name, skill); // 最低
for (const skill of bundledSkills)         merged.set(skill.name, skill);
for (const skill of managedSkills)         merged.set(skill.name, skill);
for (const skill of personalAgentsSkills)  merged.set(skill.name, skill);
for (const skill of projectAgentsSkills)   merged.set(skill.name, skill);
for (const skill of workspaceSkills)       merged.set(skill.name, skill); // 最高
```

同名技能：**高优先级来源完全覆盖低优先级**。用户可以在 `<workspace>/skills` 中放一个同名 skill 来覆盖内置技能。

### 3.3 安全限制

- `maxCandidatesPerRoot` — 每个来源目录最大候选数 — 配置路径 `skills.limits.maxCandidatesPerRoot`
- `maxSkillsLoadedPerSource` — 每个来源最终保留数 — 配置路径 `skills.limits.maxSkillsLoadedPerSource`
- `maxSkillFileBytes` — 单个 SKILL.md 最大字节 — 配置路径 `skills.limits.maxSkillFileBytes`
- `maxSkillsInPrompt` — 注入 prompt 的最大技能数 — 配置路径 `skills.limits.maxSkillsInPrompt`
- `maxSkillsPromptChars` — skills prompt 最大字符数 — 配置路径 `skills.limits.maxSkillsPromptChars`

---

## 4. 门控过滤（Load-time Gating）

合并后的每个 SkillEntry 需要通过 `shouldIncludeSkill` 门控检查，任一条件不满足则被排除。

```typescript
// config.ts:72-104
export function shouldIncludeSkill(params) {
  const { entry, config, eligibility } = params;
  // ① 配置显式禁用
  if (skillConfig?.enabled === false) return false;
  // ② bundled 白名单检查
  if (!isBundledSkillAllowed(entry, allowBundled)) return false;
  // ③ 运行时条件检查
  return evaluateRuntimeEligibility({
    os: entry.metadata?.os,
    always: entry.metadata?.always,
    requires: entry.metadata?.requires,
    hasBin: hasBinary,
    hasEnv: (envName) => Boolean(
      process.env[envName] ||
      skillConfig?.env?.[envName] ||
      (skillConfig?.apiKey && entry.metadata?.primaryEnv === envName)
    ),
    isConfigPathTruthy: (configPath) => isConfigPathTruthy(config, configPath),
  });
}
```

**门控决策树**（按顺序判断，任一不通过即排除）：

1. `enabled === false` ? → 排除
2. bundled allowlist 检查 → 不在白名单则排除
3. `always === true` ? → 直接包含，跳过后续
4. OS 平台匹配？ → 不匹配则排除
5. `requires.bins` 全部存在？ → 缺少则排除
6. `requires.env` 全部满足？ → 缺少则排除
7. `requires.config` 全部 truthy？ → 否则排除
8. 以上全部通过 → 包含

---

## 5. 渐进式加载——Agent Loop 开始时加载了什么？

这是 OpenClaw Skill 系统的核心设计。OpenClaw **不是** 在系统提示中注入所有 SKILL.md 的完整正文，而是采用 **两阶段渐进式加载**。

### 5.1 阶段一：系统提示中注入技能目录（Agent Loop 开始）

在 `runEmbeddedAttempt` 的启动阶段（attempt.ts:341-362）：

**Step 1** — 决定是否需要从磁盘加载 skill entries：

```typescript
const { shouldLoadSkillEntries, skillEntries } = resolveEmbeddedRunSkillEntries({
  workspaceDir: effectiveWorkspace,
  config: params.config,
  skillsSnapshot: params.skillsSnapshot,
});
```

**Step 2** — 注入环境变量：

```typescript
restoreSkillEnv = params.skillsSnapshot
  ? applySkillEnvOverridesFromSnapshot({ snapshot, config })
  : applySkillEnvOverrides({ skills: skillEntries, config });
```

**Step 3** — 生成 skillsPrompt（只含目录，不含正文）：

```typescript
const skillsPrompt = resolveSkillsPromptForRun({
  skillsSnapshot: params.skillsSnapshot,
  entries: shouldLoadSkillEntries ? skillEntries : undefined,
  config: params.config,
  workspaceDir: effectiveWorkspace,
});
```

### 注入系统提示的内容是什么？

有两种格式，根据 token 预算自动选择：

**完整格式**（formatSkillsForPrompt）— 含 description：

```xml
<available_skills>
  <skill>
    <name>image-lab</name>
    <description>Generate or edit images via a provider-backed image workflow</description>
    <location>~/.openclaw/skills/image-lab/SKILL.md</location>
  </skill>
  <skill>
    <name>summarize</name>
    <description>Summarize text using the summarize CLI</description>
    <location>~/workspace/skills/summarize/SKILL.md</location>
  </skill>
</available_skills>
```

每个 skill 的 token 开销公式：`total = 195(基础) + sum(97 + len(name) + len(description) + len(location))`

**Compact 格式**（formatSkillsCompact）— 无 description：

当完整格式超出预算时自动降级，只保留 name + location，去掉 description。

### 预算裁剪决策流程

```typescript
// workspace.ts:567-613
function applySkillsPromptLimits(params) {
  // ① 按 maxSkillsInPrompt 截断数量
  const byCount = params.skills.slice(0, limits.maxSkillsInPrompt);
  // ② 完整格式能否在 maxSkillsPromptChars 以内？
  if (fitsFull(byCount)) return { compact: false };       // 完整格式
  // ③ Compact 格式能否在预算内？
  if (fitsCompact(byCount)) return { compact: true };     // 降级但不丢技能
  // ④ 二分搜索在 Compact 格式下找到能放入的最大技能数
  let lo = 0, hi = byCount.length;
  while (lo < hi) { /* 二分法 */ }
  return { skills: byCount.slice(0, lo), compact: true, truncated: true };
}
```

**Token 优化**：路径中 home 目录前缀被替换为 `~`（compactSkillPaths），每个技能省约 5-6 tokens。

### 5.2 系统提示中的 Skills 使用规则

```typescript
// system-prompt.ts:21-37
function buildSkillsSection(params) {
  return [
    "## Skills (mandatory)",
    "Before replying: scan <available_skills> <description> entries.",
    "- If exactly one skill clearly applies: read its SKILL.md at <location> with read, then follow it.",
    "- If multiple could apply: choose the most specific one, then read/follow it.",
    "- If none clearly apply: do not read any SKILL.md.",
    "Constraints: never read more than one skill up front; only read after selecting.",
    trimmed,  // ← 这里是 <available_skills> XML 块
  ];
}
```

**关键约束**：

- 模型 **必须先扫描** 目录中的 description 条目
- **最多只读一个** SKILL.md（不要一次读多个）
- 选定后才用 read 工具读取完整正文
- 没有匹配的就不读

### 5.3 阶段二：模型按需读取 SKILL.md 正文

当模型判断某个 Skill 与当前任务匹配时，它通过 read 工具读取 SKILL.md 的完整内容：

1. 模型收到用户请求："帮我生成一张图片"
2. 扫描 `<available_skills>` 目录 → 发现 image-lab 匹配
3. 调用 read 工具：`read("~/.openclaw/skills/image-lab/SKILL.md")`
4. 获得完整的技能指令 → 按指令执行

### 5.4 渐进式加载的设计意图

**问题**：20 个 Skill x 平均 2000 tokens/个 = 40,000 tokens，全部塞进系统提示会严重浪费 token + 上下文空间。

**解决方案**：

- 系统提示仅包含目录：20 个 skill x ~100 chars ≈ 2000 chars ≈ **500 tokens**
- 按需读取单个 SKILL.md 正文：1 个 skill x ~2000 tokens = **2000 tokens**
- 总计：500 + 2000 = **2500 tokens**（vs 40,000 全量），节省约 94%

---

## 6. 会话快照与缓存优化

### 6.1 SkillSnapshot——会话级缓存

OpenClaw 在会话首次 turn 时构建 SkillSnapshot，后续 turn 复用：

```typescript
// session-updates.ts:38-98
export async function ensureSkillSnapshot(params) {
  const snapshotVersion = getSkillsSnapshotVersion(workspaceDir);
  ensureSkillsWatcher({ workspaceDir, config: cfg });
  // 版本比较：快照版本 < 当前版本 → 需要刷新
  const shouldRefreshSnapshot =
    snapshotVersion > 0 && (nextEntry?.skillsSnapshot?.version ?? 0) < snapshotVersion;
  if (isFirstTurnInSession || shouldRefreshSnapshot) {
    const skillSnapshot = buildWorkspaceSkillSnapshot(workspaceDir, {
      config: cfg, skillFilter, eligibility: { remote: remoteEligibility }, snapshotVersion,
    });
    // 写入 session store
  }
}
```

**快照内容**：

- `prompt` — 预渲染的 skills prompt 字符串（XML 目录）
- `skills` — 元数据摘要（name / primaryEnv / requiredEnv）
- `resolvedSkills` — 已解析的完整 Skill 对象数组
- `version` — 版本号，用于失效判断

### 6.2 运行时快照复用

```typescript
// skills-runtime.ts:5-21
export function resolveEmbeddedRunSkillEntries(params) {
  // 如果快照存在且 resolvedSkills 已定义 → 不需要从磁盘重新加载
  const shouldLoadSkillEntries = !params.skillsSnapshot || !params.skillsSnapshot.resolvedSkills;
  return {
    shouldLoadSkillEntries,
    skillEntries: shouldLoadSkillEntries
      ? loadWorkspaceSkillEntries(params.workspaceDir, { config }) // 从磁盘加载
      : [],                                                         // 复用快照
  };
}
```

**Prompt 复用**：

```typescript
// workspace.ts:695-713
export function resolveSkillsPromptForRun(params) {
  const snapshotPrompt = params.skillsSnapshot?.prompt?.trim();
  if (snapshotPrompt) return snapshotPrompt; // 快照中有 prompt → 零成本复用
  return buildWorkspaceSkillsPrompt(params.workspaceDir, { entries, config }); // 否则重新构建
}
```

### 6.3 缓存策略总结

- **会话快照 (SkillSnapshot)** — 首 turn 构建，后续 turn 复用 — 失效条件：snapshotVersion 变更
- **Prompt 字符串** — 快照中预渲染 — 失效条件：快照重建时
- **磁盘加载** — 有快照时跳过 loadWorkspaceSkillEntries — 失效条件：resolvedSkills 未定义时
- **路径压缩** — compactSkillPaths 替换 home 前缀为 `~` — 每次构建时执行
- **环境变量** — 注入 → 运行 → 恢复 — 失效条件：agent run 结束时恢复

---

## 7. 热更新机制（Skills Watcher）

### 7.1 文件监听

```typescript
// refresh.ts:132-207
export function ensureSkillsWatcher(params) {
  const watchTargets = resolveWatchTargets(workspaceDir, config);
  // 监听: "<workspace>/skills/*/SKILL.md", "~/.openclaw/skills/*/SKILL.md" 等
  const watcher = chokidar.watch(watchTargets, {
    ignoreInitial: true,
    awaitWriteFinish: { stabilityThreshold: debounceMs, pollInterval: 100 },
    ignored: DEFAULT_SKILLS_WATCH_IGNORED, // .git, node_modules, dist 等
  });
  watcher.on("add", (p) => schedule(p));    // 新增 SKILL.md
  watcher.on("change", (p) => schedule(p)); // 修改 SKILL.md
  watcher.on("unlink", (p) => schedule(p)); // 删除 SKILL.md
}
```

### 7.2 版本递增与传播

1. SKILL.md 文件变更
2. chokidar watcher 检测到变更（debounce 250ms）
3. `bumpSkillsSnapshotVersion({ workspaceDir, reason: "watch" })`，globalVersion 递增
4. emit SkillsChangeEvent，通知所有注册的 listener
5. 下次 agent turn 时，`ensureSkillSnapshot()` 对比版本
6. 如果 snapshotVersion > entry.skillsSnapshot.version → 重建快照
7. 否则复用现有快照

### 7.3 配置

```json
{
  "skills": {
    "load": {
      "watch": true,
      "watchDebounceMs": 250
    }
  }
}
```

- `watch` — 是否启用文件监听，默认 true
- `watchDebounceMs` — 防抖延迟，默认 250ms

---

## 8. 环境变量注入

每次 Agent Run 开始时，OpenClaw 为符合条件的技能注入环境变量：

1. **Agent Run 开始** — 读取 skill metadata 和配置
2. **注入环境变量** — 对每个技能，应用 `skills.entries.<key>.env`（仅在变量不存在时注入，不覆盖已有值）；`skills.entries.<key>.apiKey` 注入到 `process.env[primaryEnv]`
3. **Agent 正常运行**
4. **Agent Run 结束** — 恢复原始 process.env（`restoreSkillEnv()`）

环境变量注入是 **运行级作用域**，不影响全局 shell 环境。

---

## 9. 完整时序示例

### 示例：用户请求生成图片

**阶段一：Agent Run 启动（attempt.ts）**

1. `resolveEmbeddedRunSkillEntries()` — 检查 skillsSnapshot（首 turn 为空），shouldLoadSkillEntries = true
2. `loadWorkspaceSkillEntries()` — 扫描 6 级来源目录，找到 20 个 skill，合并去重 → 18 个，读取 frontmatter → 18 个 SkillEntry
3. `applySkillEnvOverrides()` — 注入 GEMINI_API_KEY 到 process.env
4. `resolveSkillsPromptForRun()` → `resolveWorkspaceSkillPromptState()` — filterSkillEntries() → 15 个通过门控 — 过滤 disableModelInvocation → 13 个面向模型 — compactSkillPaths() 路径压缩 — applySkillsPromptLimits()：fitsFull = Yes（2800 chars < 预算），使用完整格式
5. `buildWorkspaceSkillSnapshot()` — 保存快照到 session store
6. `buildAgentSystemPrompt()` — skillsSection 中写入 "## Skills (mandatory)" + `<available_skills>` XML 块（13 个技能的 name/description/location）

**阶段二：模型推理（第一个 turn）**

7. 模型扫描 `<available_skills>` → 匹配 "image-lab"（description 含 "Generate or edit images"）→ 调用 read 读取 SKILL.md
8. 获得 SKILL.md 完整正文（约 2000 tokens）→ 按技能指令执行图片生成流程

**阶段三：后续 turn**

9. 用户追问："换个风格" → ensureSkillSnapshot() 检查版本未变 → 复用快照中的 prompt → 模型已在上下文中有 image-lab 的完整指令 → 直接执行，无需重新加载

### 示例：技能热更新

1. 管理员修改 `<workspace>/skills/summarize/SKILL.md`
2. chokidar 检测到 change 事件（debounce 250ms）
3. `bumpSkillsSnapshotVersion({ reason: "watch" })`，globalVersion 从 1711800000000 → 1711800001000
4. 下一次 agent turn，`ensureSkillSnapshot()` 发现 snapshotVersion > entry.version → shouldRefreshSnapshot = true
5. `buildWorkspaceSkillSnapshot()` — 重新扫描所有来源 → 合并 → 过滤 → 构建 prompt
6. 新的 prompt 生效

---

## 10. Skill 调用的两条路径

**模型调用**：模型自行判断需要使用某技能 → 扫描目录 → read SKILL.md → 按指令执行

**用户调用**：用户输入 slash 命令（如 `/image-lab`）→ 解析命令 → 读取 SKILL.md → 注入为用户消息 → 模型执行

用户调用路径额外涉及 `buildWorkspaceSkillCommandSpecs`（将 `userInvocable: true` 的技能注册为 slash 命令）和 `command-dispatch` 机制（部分命令可直接分发到工具，绕过模型）。

---

## 11. 关键设计决策

### 11.1 为什么不把 SKILL.md 正文全部注入系统提示？

- **Token 成本**：20 个 skill x 2000 tokens = 40,000 tokens，远超预算
- **注意力稀释**：过多指令文本会降低模型对关键指令的遵循度
- **不必要**：大多数 turn 只需要 0-1 个 skill

### 11.2 为什么用 XML 目录而不是 JSON？

- 模型对 XML 标签（`<name>`, `<description>`）的理解和遵循度更好
- XML 允许嵌套且分隔清晰
- 与 Claude/Pi 的 prompt 工程最佳实践一致

### 11.3 为什么有 Compact 降级机制？

- 场景：技能数量很多，完整格式（含 description）超出字符预算
- Compact 格式去掉 description，仅保留 name + location
- **优先保留技能数量**，而非少数技能的完整描述
- 二分搜索确保在预算内放入最多数量的技能

### 11.4 为什么使用会话快照而非每 turn 重新扫描？

- **避免重复磁盘 I/O**：每个 turn 都扫描文件系统代价高
- **一致性**：同一会话内技能列表保持稳定
- **热更新可控**：通过版本号机制，仅在文件变更后刷新

---

## 12. 配置参考

```json
{
  "skills": {
    "allowBundled": ["image-lab", "summarize"],
    "load": {
      "extraDirs": ["/path/to/shared-skills"],
      "watch": true,
      "watchDebounceMs": 250
    },
    "limits": {
      "maxSkillsInPrompt": 50,
      "maxSkillsPromptChars": 10000,
      "maxCandidatesPerRoot": 100,
      "maxSkillsLoadedPerSource": 50,
      "maxSkillFileBytes": 65536
    },
    "entries": {
      "image-lab": {
        "enabled": true,
        "apiKey": "YOUR_API_KEY",
        "env": { "GEMINI_API_KEY": "..." },
        "config": { "endpoint": "https://..." }
      },
      "deprecated-skill": { "enabled": false }
    },
    "install": {
      "preferBrew": true,
      "nodeManager": "npm"
    }
  }
}
```

配置字段说明：

- `allowBundled` — bundled 技能白名单（仅影响内置技能）
- `load.extraDirs` — 额外技能目录
- `load.watch` — 文件监听开关
- `load.watchDebounceMs` — 防抖延迟
- `limits.*` — 各项数量/大小限制
- `entries.<skillKey>.enabled` — 是否启用
- `entries.<skillKey>.apiKey` — API Key
- `entries.<skillKey>.env` — 环境变量
- `entries.<skillKey>.config` — 自定义配置字段
- `install.preferBrew` — 安装偏好
- `install.nodeManager` — npm / pnpm / yarn / bun

---

*文档基于 OpenClaw 源码 openclaw/src/agents/skills/ 目录分析，最后更新：2026-03-30*
