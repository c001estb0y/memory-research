# GBrain — AI Agent 长期记忆系统 Overview

> 基于 [garrytan/gbrain](https://github.com/garrytan/gbrain) 源码与文档的系统梳理。

---

## 一、项目简介

GBrain 是 Y Combinator CEO **Garry Tan** 为其个人 AI Agent 打造的「大脑」系统，后开源为通用方案。它以 **Git Markdown 仓库**为事实来源（Source of Truth），以 **PGLite/Postgres + pgvector** 为检索引擎，通过 CLI 和 MCP 协议让 AI Agent 能够读写、检索和维护一个持久化的个人知识库。

| 维度 | 说明 |
|------|------|
| **定位** | AI Agent 的个人知识大脑 |
| **Stars** | 9,700+（2026.04） |
| **语言** | TypeScript（Bun 运行时） |
| **License** | MIT |
| **核心理念** | Markdown 为 SoT + 零 LLM 图构建 + 薄运行时厚技能 |
| **作者** | Garry Tan（Y Combinator President & CEO） |

### 一句话概括

> GBrain 让 AI Agent 拥有一个会积累、会遗忘、会自我修复的个人大脑——白天摄入信息，夜间自动巩固，知识越用越聪明。

### 生产规模验证

Garry Tan 的真实部署数据：**17,888 页文档、4,383 人物档案、723 家公司、21 个定时任务自主运行**，涵盖 13 年日历数据、280+ 会议记录、300+ 原创想法。

---

## 二、背景与设计哲学

### 2.1 起源故事

Garry Tan 在配置其 OpenClaw Agent 时，开始用一个 Markdown 仓库作为「大脑」——每人一页、每公司一页，上方是编译后的事实摘要，下方是追加式时间线。一周内积累到 10,000+ 文件、3,000+ 人物，于是把个人实践抽象为开源工具。

### 2.2 核心设计原则

**1. Markdown 即事实来源**

所有知识以人类可读的 Markdown 文件存储在 Git 仓库中，而非锁在某个数据库里。这意味着：
- 人类可以直接阅读和编辑
- Git 提供完整的版本历史
- 不依赖任何专有存储格式

**2. 薄运行时、厚技能（Thin Harness, Fat Skills）**

GBrain 的运行时（CLI + MCP）只做存取和检索，所有智能行为（摄入策略、丰富逻辑、维护规程）都写在 Markdown 格式的 Skill 文件中，由 Agent 自行阅读和执行。这样：
- 运行时足够简单，不容易出 bug
- 技能可以被 Agent 自己创建和改进
- 不同 Agent 平台都能使用同一套技能

**3. 零 LLM 图构建**

关系图的边**不通过 LLM 抽取**，而是从 Markdown 链接、`[[wikilink]]`、frontmatter 字段中用确定性正则推断，避免幻觉和成本。推断规则涵盖 `works_at`、`invested_in`、`founded`、`advises`、`attended` 等关系类型。

**4. 复合知识（Compounding Knowledge）**

每一条新信息到达时，Agent 检查大脑已有知识，更新相关页面，建立新连接——知识不是简单堆叠，而是持续积累和互联。

---

## 三、核心架构

```
┌─────────────────────────────────────────────────────────────┐
│                     Agent（OpenClaw / Hermes / Claude）       │
│                                                              │
│  读取 skills/RESOLVER.md → 按需加载 26+ 技能                  │
├──────────────┬──────────────────────────────────────────────┤
│   CLI 入口    │          MCP Server (stdio)                  │
│  gbrain <cmd>│    gbrain serve → 39 个 MCP 工具               │
├──────────────┴──────────────────────────────────────────────┤
│                   Operations 层（单一契约）                    │
│  页面 CRUD │ search/query │ 标签 │ 链接 │ 图遍历              │
│  时间线 │ 统计/健康 │ sync │ embed │ Minions（后台任务）       │
├─────────────────────────────────────────────────────────────┤
│                      Core 引擎层                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐   │
│  │BrainEngine│ │ Hybrid   │ │ Link     │ │ Embedding    │   │
│  │(抽象)     │ │ Search   │ │ Extract  │ │ (OpenAI)     │   │
│  ├──────────┤ │ RRF 融合  │ │ 零LLM    │ │ 分块+向量化  │   │
│  │ PGLite   │ │ 反链加权  │ │ 确定性   │ │              │   │
│  │ Postgres │ │ 余弦重排  │ │ 正则     │ │              │   │
│  │ Supabase │ │          │ │          │ │              │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────┘   │
├─────────────────────────────────────────────────────────────┤
│                      存储层                                   │
│  ┌────────────────────────┐ ┌────────────────────────────┐  │
│  │ PGLite / Postgres      │ │ Git Markdown 仓库           │  │
│  │ tsvector (关键词索引)   │ │ 人类可读/可编辑的 SoT       │  │
│  │ pgvector HNSW (向量)    │ │ 每页 = compiled truth      │  │
│  │ 关系/图边/元数据         │ │        + timeline           │  │
│  └────────────────────────┘ └────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 架构要点

- **Operations 是 CLI 和 MCP 的单一契约**：`src/core/operations.ts` 定义了所有操作，CLI 和 MCP Server 共享同一套定义，保证行为一致
- **引擎可插拔**：默认 PGLite（2 秒启动），可平滑迁移到 Postgres 或 Supabase
- **嵌入和分块在引擎之外**：`embedding.ts` 和 `chunkers/` 独立于存储引擎，引擎只负责存取向量

---

## 四、知识模型

### 4.1 页面结构

每个知识页面采用统一的双层结构：

```markdown
---
type: person
tags: [vc, founder]
---

# Pedro Franceschi

**Role**: CEO of Brex
**Founded**: 2017
**Previous**: Pagar.me (acquired by Stone)

Pedro prefers email over Slack for async communication.

---

## Timeline

- 2026-04-15: Met at YC Demo Day, discussed Series E plans
- 2026-03-20: Brex announced AI-first banking product
- 2025-11-08: Keynote at Fintech Summit on embedded finance
```

- **Compiled Truth**（`---` 之上）：经过整理的事实摘要，会随新证据变化而被 Agent 重写
- **Timeline**（`---` 之下）：追加式的时间线记录，保留原始来源轨迹

### 4.2 实体类型

| 类型 | 说明 | 典型目录 |
|------|------|---------|
| `person` | 人物档案 | `people/` |
| `company` | 公司信息 | `companies/` |
| `deal` | 交易/投资 | `deals/` |
| `concept` | 概念/想法 | `concepts/` |
| `project` | 项目 | `projects/` |
| `source` | 信息来源 | `sources/` |
| `media` | 媒体内容 | `media/` |
| `writing` | 写作 | `writings/` |
| `analysis` | 分析报告 | `analysis/` |
| `guide` | 指南 | `guides/` |
| `architecture` | 架构设计 | `architecture/` |
| `yc` / `civic` | YC/公共事务 | 特定目录 |

### 4.3 三层信息路由

GBrain 明确区分了三个信息层次（`docs/guides/brain-vs-memory.md`）：

| 层次 | 存储位置 | 示例 |
|------|---------|------|
| **世界知识** | GBrain（永久） | "Pedro 是 Brex 的 CEO" |
| **运营状态** | Agent Memory | "用户偏好简洁格式" |
| **当前对话** | Session Context | "刚才说的那个 bug" |

---

## 五、核心功能

### 5.1 命令与操作

| 命令 | 作用 | 实现要点 |
|------|------|---------|
| `gbrain init` | 初始化大脑 | 默认 PGLite（2s），可选 Postgres/Supabase |
| `gbrain put` | 写入页面 | 分块 → 可选嵌入 → 版本管理；本地自动连边 |
| `gbrain get` | 读取页面 | 支持模糊匹配（`resolveSlugs`） |
| `gbrain search` | 关键词搜索 | tsvector + 去重 |
| `gbrain query`/`ask` | 混合检索 | 向量 + 关键词 + 多查询扩展 |
| `gbrain embed` | 向量化 | 支持单页/批量/`--stale` 增量 |
| `gbrain sync` | 同步 | Git 变更 → 数据库索引 |
| `gbrain graph-query` | 图遍历 | 按 link_type/direction 遍历，深度上限 10 |
| `gbrain doctor` | 健康检查 | 数据库/索引/一致性诊断 |
| `gbrain serve` | 启动 MCP | stdio 协议，暴露 39 个工具 |

### 5.2 混合检索（Hybrid Search）

GBrain 的检索策略融合了多种信号：

```
查询 → ┬→ 关键词检索 (tsvector)      ──┐
       └→ 向量检索 (pgvector HNSW)  ──┤
                                       ├→ RRF 融合 (K=60)
                                       ├→ Compiled Truth 加权
                                       ├→ 余弦重排
                                       ├→ 反链 Boost（被引用越多，排名越高）
                                       └→ Dedup → 最终结果
```

- 有 `OPENAI_API_KEY` 时：关键词 + 向量 + RRF + 反链加权
- 无 API Key 时：退化为关键词 + 反链 Boost + 去重

### 5.3 零 LLM 图构建（Link Extraction）

`link-extraction.ts` 从 Markdown 内容中确定性地抽取关系边：

1. **来源**：Markdown 链接、`[[wikilink]]`、裸 slug、frontmatter 字段
2. **类型推断**：`inferLinkType` 用正则推断关系类型
3. **安全**：代码块内容被剥离，减少误匹配；MCP 远程写入跳过 auto-link 防止注入

支持的关系类型包括：`works_at`、`invested_in`、`founded`、`advises`、`attended`、`related_to` 等。

### 5.4 MCP 工具集（39 个操作）

GBrain 通过 MCP 协议暴露完整的工具集：

| 类别 | 工具 |
|------|------|
| **页面 CRUD** | `get_page`, `put_page`, `list_pages`, `delete_page` |
| **检索** | `search`, `query`, `resolve_slugs` |
| **图操作** | `traverse_graph`, `get_links`, `add_link` |
| **标签** | `add_tags`, `remove_tags`, `list_tags` |
| **时间线** | `add_timeline_entry`, `get_timeline` |
| **嵌入** | `embed_page`, `embed_stale` |
| **同步** | `sync`, `import_file`, `import_url` |
| **Minions** | 10 个后台任务管理操作 |
| **运维** | `doctor`, `stats`, `version`, `find_orphans` |

---

## 六、技能体系（26+ Skills）

### 6.1 设计理念

GBrain 采用「薄运行时、厚技能」设计：

- **运行时**（CLI + MCP）：只做数据存取和检索
- **技能**（`skills/` 目录中的 `SKILL.md`）：用 Markdown 编写的操作手册，Agent 自行阅读和执行

### 6.2 技能路由

`skills/RESOLVER.md` 是技能路由器，Agent 首先阅读它来决定加载哪个技能：

| 类别 | 技能 | 说明 |
|------|------|------|
| **常驻** | signal-detector | 从对话中检测值得记录的信号 |
| **常驻** | brain-ops | 读写大脑的标准操作 |
| **摄入** | ingest-* | 会议/邮件/推文/日历等摄入 |
| **增强** | enrich | 实体页面的自动丰富 |
| **维护** | citation-fixer | 引用修复 |
| **维护** | maintain | 日常维护任务 |
| **运营** | daily-task-* | 每日任务管理 |
| **运营** | cron-scheduler | 定时任务编排 |
| **研究** | data-research | 数据研究配方 |
| **元技能** | skill-creator | 创建新技能 |
| **元技能** | minion-orchestrator | 后台任务编排 |

### 6.3 约定（Conventions）

`skills/conventions/` 目录定义了跨技能的共享约定，如命名规范、页面结构、标签体系等。

---

## 七、Dream Cycle（夜间巩固机制）

Dream Cycle 是 GBrain 最具特色的设计——模仿人类睡眠中的记忆巩固过程：

### 触发方式

通过 cron 配置在夜间自动执行（默认建议 `0 2 * * *`）。

### 四个阶段

```
Dream Cycle
├── 1. 实体扫描
│   └── 遍历当天会话 → 提取实体 → 搜索/创建/丰富/补充时间线
├── 2. 引用修复
│   └── 检查断链/过期引用 → 自动修复
├── 3. 记忆巩固
│   └── 重新嵌入变更页面 → 更新图边 → 优化索引
└── 4. 同步与清理
    └── gbrain sync → gbrain embed --stale → 一致性校验
```

### 核心价值

> "You wake up and the brain is smarter than when you went to sleep."

Agent 白天摄入的信息（会议、邮件、推文等），在夜间被系统性地整理、关联和巩固，第二天 Agent 可以直接使用更完整的知识。

---

## 八、集成配方（Integration Recipes）

GBrain 通过 `recipes/*.md` 定义了自描述的集成配方：

| 配方 | 说明 |
|------|------|
| **email-to-brain** | 邮件自动摄入 |
| **calendar-to-brain** | 日历事件同步 |
| **meeting-sync** | 会议记录摄入 |
| **x-to-brain** | X/Twitter 内容摄入 |
| **twilio-voice-brain** | 语音通话摄入 |
| **ngrok-tunnel** | 远程隧道接入 |
| **credential-gateway** | 凭证管理 |
| **data-research** | 数据研究系列 |

每个配方都是一个 Markdown 文件，包含 frontmatter（`id`、`requires`、`secrets`、`health_checks`）和 Agent 可执行的安装步骤。

---

## 九、与 GStack 的关系

```
┌──────────────────┐    ┌───────────────┐    ┌──────────────────┐
│   Brain Repo     │    │    GBrain     │    │    AI Agent      │
│   (git)          │    │  (retrieval)  │    │  (read/write)    │
│                  │◄───│               │◄───│                  │
│   single source  │    │  index +      │    │  skill-driven    │
│   of truth       │    │  search       │    │  use the brain   │
└──────────────────┘    └───────┬───────┘    └──────────────────┘
                                │
                        ┌───────┴───────┐
                        │   hosts/      │
                        │  gbrain.ts    │
                        │   (bridge)    │
                        └───────┬───────┘
                                │
                        ┌───────┴───────┐
                        │    GStack     │
                        │  (coding      │
                        │   skills)     │
                        └───────────────┘
```

- **GStack** = 编码技能（ship、review、QA、investigate 等）
- **GBrain** = 记忆/运营/摄取技能
- **`hosts/gbrain.ts`** = 桥接层，让编码技能在写代码前先查大脑

---

## 十、技术栈

| 类别 | 技术 |
|------|------|
| **运行时** | Bun |
| **语言** | TypeScript |
| **嵌入式数据库** | PGLite（Postgres 17.5 via WASM） |
| **可选数据库** | Postgres, Supabase |
| **向量检索** | pgvector (HNSW) |
| **关键词检索** | tsvector |
| **MCP** | @modelcontextprotocol/sdk (stdio) |
| **LLM SDK** | OpenAI SDK, Anthropic SDK |
| **Markdown 解析** | gray-matter, marked |
| **云存储** | @aws-sdk/client-s3（可选） |
| **版本管理** | Git |

---

## 十一、与同类项目的差异化

| 维度 | GBrain | Cognee | Mem0 | MemPalace |
|------|--------|--------|------|-----------|
| **SoT** | Git Markdown | 数据库 | API 服务 | ChromaDB |
| **图构建** | 零 LLM（确定性正则） | LLM 抽取 | 规则 | 向量聚类 |
| **语言** | TypeScript/Bun | Python | Python | Python |
| **检索** | tsvector + pgvector + 反链 | 向量 + 图 + Cypher | 向量 + 规则 | 向量 + 图遍历 |
| **人类可读** | 完全（Markdown） | 不直接 | 不直接 | 不直接 |
| **夜间巩固** | Dream Cycle | 无 | 无 | 无 |
| **技能体系** | 26+ Markdown 技能 | Pipeline Tasks | 无 | 无 |
| **Agent 集成** | MCP + CLI | MCP + REST + Plugin | REST | MCP |
| **部署复杂度** | 极低（PGLite 2s） | 中等 | 中等 | 低 |
