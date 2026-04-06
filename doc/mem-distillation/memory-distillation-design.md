# Shadow-Folk 记忆蒸馏服务技术方案

> 将混沌的 AI Agent 工作记忆蒸馏为结构化的可复用经验。

## 1. 背景与目标

Shadow-Folk 系统持续采集两类原始记忆数据：

| 类型 | 表/文件 | 特征 |
|------|---------|------|
| **Session Summary** | `summaries` | 每轮会话的结构化回顾（request / investigated / learned / completed / next_steps） |
| **Observation** | `observations` | 细粒度事件流（text / facts / narrative / concepts / meta_intent） |

这些数据存在两个核心问题：

1. **噪音大** — 包含大量日常操作记录、文件编辑事件、空结果查询等低价值条目
2. **缺乏抽象** — 即使有价值的条目也停留在"发生了什么"的事实层面，缺少"为什么"和"下次怎么做"的经验提炼

**蒸馏目标**：构建一条自动化 DAG 管线，定时从原始记忆中提取、转换、加载（ETL）结构化的工程经验，形成可被团队检索和复用的知识资产。

### 为什么需要两层蒸馏？

仅做结构化提取（JSON Schema 强制输出）会丢失最有价值的部分——**探索过程中的错误路径和思维转折**。

以 CRLF 问题为例，对比单层蒸馏和实际有价值的经验叙事：

| 维度 | 单层结构化蒸馏 | 完整经验叙事 |
|------|-------------|------------|
| 输出 | `"root_cause": "脚本保存为 CRLF 行尾"` | 还原了 4 次失败过程：PowerShell 语法报错 → CRLF 行尾 → 四层引号嵌套 → 最终 sed 清洗 |
| 价值 | 告诉你**答案** | 告诉你**怎么找到答案的** |
| 适用场景 | 机器检索、自动匹配 | 人读、团队分享、知识传承 |

因此管线设计为**两层**：第一层做结构化粗筛（过滤噪音、机器可检索），第二层做叙事重建（保留排查路径、人可阅读）。

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Memory Distillation DAG (两层架构)                │
│                                                                     │
│  ┌──────────┐    ┌──────────────┐    ┌────────────────────┐        │
│  │ Extract  │───▶│ Layer 1      │───▶│     Load L1        │        │
│  │          │    │ 结构化蒸馏    │    │ 结构化 JSON        │        │
│  │ 拉取原始  │    │ Pydantic 强制 │    │ + 去重/置信度过滤  │        │
│  │ 记忆数据  │    │ JSON Schema  │    │                    │        │
│  └──────────┘    └──────────────┘    └────────┬───────────┘        │
│       │                │                      │                     │
│  Shadow-Folk      Venus API              distilled_                 │
│  SQLite/CSV      (claude-sonnet)         experiences.json           │
│                                               │                     │
│                                               ▼                     │
│                  ┌──────────────┐    ┌────────────────────┐        │
│                  │ Layer 2      │───▶│     Load L2        │        │
│                  │ 叙事重建     │    │ Markdown 经验文档   │        │
│                  │ 按主题聚合    │    │ + 方法论提炼        │        │
│                  │ 还原排查路径  │    │                    │        │
│                  └──────────────┘    └────────────────────┘        │
│                        │                      │                     │
│                   Venus API              experience_                 │
│                  (claude-sonnet)          narratives.md              │
└─────────────────────────────────────────────────────────────────────┘
```

**两层各自的定位**：

| | Layer 1：结构化蒸馏 | Layer 2：叙事重建 |
|---|---|---|
| **输入** | 原始记忆（summaries + observations） | Layer 1 的结构化经验（按主题聚合） |
| **输出** | JSON（机器可检索） | Markdown（人可阅读） |
| **LLM 约束** | Pydantic JSON Schema 强制 | 自然语言 + 结构化模板 |
| **核心价值** | 过滤噪音、去重、分类 | 还原过程、提炼方法论、形成可分享叙事 |
| **消费者** | 向量检索、知识图谱、自动匹配 | 团队成员、新人 onboarding、经验分享 |

## 3. 数据模型定义

### 3.1 蒸馏输出模型（Pydantic）

用严格的 Python 数据类约束 LLM 输出，过滤噪音，只沉淀结构化经验：

```python
from pydantic import BaseModel, Field
from typing import List, Optional
from enum import Enum


class ExperienceType(str, Enum):
    """经验类型枚举"""
    debugging = "debugging"           # 排障定位
    architecture = "architecture"     # 架构决策
    deployment = "deployment"         # 部署运维
    configuration = "configuration"   # 配置变更
    cross_platform = "cross_platform" # 跨平台兼容
    workflow = "workflow"             # 流程优化
    tooling = "tooling"              # 工具使用


class ExperienceScope(str, Enum):
    """经验适用范围"""
    architecture = "architecture"   # 项目架构级 — 全员必知，影响系统设计
    engineering = "engineering"     # 通用工程级 — 行业通识，写进团队规范
    environment = "environment"     # 个人环境级 — 仅特定 OS/Shell/IDE 组合下触发


class EnvironmentCondition(BaseModel):
    """环境级经验的触发条件，仅 scope=environment 时填写"""
    os: Optional[str] = Field(default=None, description="操作系统: windows / macos / linux")
    shell: Optional[str] = Field(default=None, description="Shell 环境: powershell / bash / zsh")
    ide: Optional[str] = Field(default=None, description="IDE: cursor / vscode / jetbrains")
    runtime: Optional[str] = Field(default=None, description="运行时: node / python / docker")


class EngineeringExperience(BaseModel):
    """单条蒸馏后的工程经验"""
    issue_context: str = Field(
        description="遇到了什么问题，包含具体错误信息或异常现象"
    )
    root_cause: str = Field(
        description="根因分析，问题的真正原因是什么"
    )
    solution: str = Field(
        description="最终决定怎么做，具体的修复步骤或方案"
    )
    rationale: str = Field(
        description="为什么这么做，背后的第一性原理或权衡取舍"
    )
    experience_type: ExperienceType = Field(
        description="经验类型分类"
    )
    scope: ExperienceScope = Field(
        description="经验适用范围：architecture=全员必知, engineering=通用规范, environment=特定环境"
    )
    environment_conditions: Optional[EnvironmentCondition] = Field(
        default=None,
        description="仅 scope=environment 时填写，标注触发该经验的特定环境条件"
    )
    trigger_patterns: List[str] = Field(
        description="泛化后的触发模式列表，用于 Agent 在更多场景下匹配到该经验。"
                    "每条描述一个可能触发该经验的情境，比 issue_context 更抽象"
    )
    related_components: List[str] = Field(
        description="关联的系统模块、文件或技术栈"
    )
    prevention: Optional[str] = Field(
        default=None,
        description="如何从机制上避免同类问题再次发生"
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="该经验的置信度，0-1 之间"
    )


class DistillationResult(BaseModel):
    """单次蒸馏批次的输出"""
    experiences: List[EngineeringExperience] = Field(
        description="从本批记忆中蒸馏出的工程经验列表，"
                    "如果没有有价值的经验则返回空列表"
    )
    skipped_reason: Optional[str] = Field(
        default=None,
        description="如果本批记忆全部为噪音，说明跳过原因"
    )
```

### 3.2 原始记忆输入格式

从 Shadow-Folk 导出的 CSV/SQLite 中提取，组装为 LLM 输入：

```python
class RawMemoryBatch(BaseModel):
    """一批待蒸馏的原始记忆"""
    session_id: str
    project: str
    summaries: List[dict]      # session summary 记录
    observations: List[dict]   # observation 记录
    time_range: str            # 时间范围描述
```

## 4. ETL 管线实现

### 4.0 日志与可观测性

整个蒸馏过程必须可观测——定时任务跑在后台，出了问题如果没有日志就是黑盒。

#### 设计原则

1. **每个阶段入口/出口都打日志** — Extract 拉了多少条、Transform 蒸馏出多少条、Load 写入多少条
2. **每次 Venus API 调用记录耗时和 token 用量** — 方便追踪成本和排查慢调用
3. **限流等待时打日志** — 知道时间花在哪里
4. **异常带完整上下文** — LLM 返回校验失败时，记录原始输出便于排查 prompt 问题
5. **结构化日志** — 用 JSON 格式输出，方便后续接入日志采集系统

#### 日志初始化

```python
import logging
import sys

def setup_logger(name: str = "distiller", log_file: str = "distill.log") -> logging.Logger:
    """配置结构化日志，同时输出到终端和文件"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 格式：时间 | 级别 | 阶段标签 | 消息
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 终端输出（INFO 及以上）
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # 文件输出（DEBUG 及以上，保留完整细节）
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()
```

#### 各阶段日志埋点规范

| 阶段 | 日志级别 | 记录内容 |
|------|---------|---------|
| Extract 开始 | INFO | 数据源路径、时间窗口 |
| Extract 完成 | INFO | summaries 条数、observations 条数 |
| L1 批次开始 | INFO | 批次编号、本批记录数 |
| Venus API 调用 | DEBUG | 请求 model、输入 tokens 估算、prompt 前 200 字符 |
| Venus API 返回 | INFO | 状态码、输出 tokens、耗时(ms) |
| Venus API 异常 | ERROR | 状态码、完整响应体、重试次数 |
| Pydantic 校验失败 | ERROR | 校验错误详情、LLM 原始输出前 500 字符 |
| L1 批次完成 | INFO | 蒸馏经验数、跳过原因（如有） |
| L1 Load 去重 | DEBUG | 总经验数、去重跳过数、新增数 |
| L1 阶段汇总 | INFO | 总批次、总经验数、总耗时、总 tokens |
| L2 主题聚合 | INFO | 主题组数、各组经验条数 |
| L2 叙事重建 | INFO | 主题名、输出叙事数、耗时 |
| L2 阶段汇总 | INFO | 总叙事数、输出文件路径、总耗时 |
| 限流等待 | WARNING | 当前窗口调用数、等待秒数 |
| 全流程完成 | INFO | 两层总耗时、总 API 调用数、总 tokens |

#### Venus API 调用包装（带日志 + 计时 + 重试）

```python
import time as _time


def call_venus_api(
    payload: dict,
    max_retries: int = 3,
    retry_delay: float = 5.0
) -> dict:
    """统一的 Venus API 调用入口，内置日志、计时、限流、重试"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {VENUS_TOKEN}"
    }

    model = payload.get("model", "unknown")
    # 粗略估算输入 tokens（中文约 1.5 字符/token）
    prompt_text = str(payload.get("messages", ""))
    est_input_tokens = len(prompt_text) // 2

    for attempt in range(1, max_retries + 1):
        # 限流
        _rate_limiter.wait()

        logger.debug(
            f"[Venus] 请求 model={model}, est_input≈{est_input_tokens} tokens, "
            f"attempt={attempt}/{max_retries}"
        )

        start = _time.monotonic()
        try:
            response = requests.post(VENUS_URL, headers=headers, json=payload)
            elapsed_ms = (_time.monotonic() - start) * 1000

            if response.status_code == 429:
                logger.warning(
                    f"[Venus] 429 Too Many Requests, 等待 {retry_delay}s 后重试 "
                    f"(attempt {attempt}/{max_retries})"
                )
                _time.sleep(retry_delay)
                retry_delay *= 2  # 指数退避
                continue

            if response.status_code != 200:
                logger.error(
                    f"[Venus] HTTP {response.status_code}, "
                    f"body={response.text[:500]}, elapsed={elapsed_ms:.0f}ms"
                )
                raise RuntimeError(f"Venus API error {response.status_code}")

            result = response.json()
            usage = result.get("usage", {})
            logger.info(
                f"[Venus] 成功 | {elapsed_ms:.0f}ms | "
                f"input={usage.get('prompt_tokens', '?')} "
                f"output={usage.get('completion_tokens', '?')} tokens"
            )
            return result

        except requests.exceptions.RequestException as e:
            elapsed_ms = (_time.monotonic() - start) * 1000
            logger.error(
                f"[Venus] 网络异常: {e}, elapsed={elapsed_ms:.0f}ms, "
                f"attempt={attempt}/{max_retries}"
            )
            if attempt < max_retries:
                _time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise

    raise RuntimeError(f"Venus API 调用失败，已重试 {max_retries} 次")
```

#### 日志输出示例

日度蒸馏（85 条记忆）的完整日志输出：

```
2026-04-05 03:00:01 | INFO    | ══ 蒸馏开始 ══ 数据源: shadowfolk, 时间窗口: 24h
2026-04-05 03:00:01 | INFO    | [Extract] 拉取完成: 5 summaries, 80 observations
2026-04-05 03:00:01 | INFO    | [Layer 1] 开始结构化蒸馏, 共 85 条记录, 分 5 批
2026-04-05 03:00:01 | INFO    | [L1] 批次 1/5: 20 条记录 (3 summaries + 17 observations)
2026-04-05 03:00:01 | DEBUG   | [Venus] 请求 model=claude-sonnet-4-6, est_input≈8200 tokens, attempt=1/3
2026-04-05 03:00:05 | INFO    | [Venus] 成功 | 3842ms | input=8156 output=1420 tokens
2026-04-05 03:00:05 | INFO    | [L1] 批次 1/5 完成: 蒸馏出 4 条经验
2026-04-05 03:00:05 | DEBUG   | [L1 Load] 4 条经验, 去重跳过 0, 新增 4
2026-04-05 03:00:05 | INFO    | [L1] 批次 2/5: 20 条记录 (2 summaries + 18 observations)
2026-04-05 03:00:05 | DEBUG   | [Venus] 请求 model=claude-sonnet-4-6, est_input≈7800 tokens, attempt=1/3
2026-04-05 03:00:09 | INFO    | [Venus] 成功 | 4210ms | input=7756 output=1180 tokens
2026-04-05 03:00:09 | INFO    | [L1] 批次 2/5 完成: 蒸馏出 3 条经验
2026-04-05 03:00:09 | DEBUG   | [L1 Load] 3 条经验, 去重跳过 1, 新增 2
...
2026-04-05 03:00:22 | INFO    | [L1] 批次 5/5 完成: 蒸馏出 0 条经验, 跳过原因: 本批均为重复的文件编辑事件
2026-04-05 03:00:22 | INFO    | [Layer 1 完成] 5 批 | 新增 12 条经验 | 总耗时 21.3s | 总 tokens 47.2K
2026-04-05 03:00:22 | INFO    | [Layer 2] 开始叙事重建
2026-04-05 03:00:22 | INFO    | [L2] 聚合为 4 个主题组: debugging(5), architecture(3), cross_platform(2), deployment(2)
2026-04-05 03:00:22 | INFO    | [L2] 重建主题: debugging (5 条经验, 12 条原始观测)
2026-04-05 03:00:22 | DEBUG   | [Venus] 请求 model=claude-sonnet-4-6, est_input≈6100 tokens, attempt=1/3
2026-04-05 03:00:30 | INFO    | [Venus] 成功 | 7856ms | input=6080 output=2940 tokens
2026-04-05 03:00:30 | INFO    | [L2] 主题 debugging 完成: 生成 2 条叙事
...
2026-04-05 03:00:58 | INFO    | [Layer 2 完成] 4 主题 | 6 条叙事 | 总耗时 36.1s | 总 tokens 35.8K
2026-04-05 03:00:58 | INFO    | ══ 蒸馏完成 ══ 总耗时 57.4s | API 调用 9 次 | 总 tokens 83.0K
2026-04-05 03:00:58 | INFO    | 输出: distilled_experiences.json (12 条) + experience_narratives.md (6 条叙事)
```

异常场景日志：

```
2026-04-05 03:00:15 | WARNING | [RateLimit] 达到 50 次/分钟上限，等待 12.3s...
2026-04-05 03:00:28 | WARNING | [Venus] 429 Too Many Requests, 等待 5.0s 后重试 (attempt 1/3)
2026-04-05 03:00:38 | ERROR   | [Venus] HTTP 500, body={"error":"internal server error"}, elapsed=2100ms
2026-04-05 03:00:43 | INFO    | [Venus] 成功 | 4520ms | input=8200 output=1380 tokens  (重试后成功)
2026-04-05 03:01:02 | ERROR   | [Pydantic] 校验失败: 1 validation error for DistillationResult - experiences.0.confidence: Input should be <= 1 [type=less_than_equal...]
2026-04-05 03:01:02 | ERROR   | [Pydantic] LLM 原始输出: {"experiences":[{"issue_context":"...","confidence":1.5...
```

### 4.1 Extract — 拉取原始记忆

```python
import sqlite3
import csv
from datetime import datetime, timedelta
from typing import List, Tuple


def extract_from_csv(
    summaries_path: str,
    observations_path: str,
    hours: int = 24
) -> Tuple[List[dict], List[dict]]:
    """从 CSV 文件中提取指定时间窗口内的记忆数据"""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    cutoff_epoch = int(cutoff.timestamp() * 1000)

    summaries = []
    with open(summaries_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row.get('created_at_epoch', 0)) >= cutoff_epoch:
                summaries.append(row)

    observations = []
    with open(observations_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row.get('created_at_epoch', 0)) >= cutoff_epoch:
                observations.append(row)

    return summaries, observations


def extract_from_sqlite(db_path: str, hours: int = 24) -> Tuple[List[dict], List[dict]]:
    """从 SQLite 导出文件中提取记忆数据"""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    summaries = [
        dict(row) for row in conn.execute(
            "SELECT * FROM memories WHERE content_type = 'session_summary' "
            "AND timestamp >= ? ORDER BY timestamp DESC",
            (cutoff.isoformat(),)
        ).fetchall()
    ]

    observations = [
        dict(row) for row in conn.execute(
            "SELECT * FROM memories WHERE content_type = 'observation' "
            "AND timestamp >= ? ORDER BY timestamp DESC",
            (cutoff.isoformat(),)
        ).fetchall()
    ]

    conn.close()
    return summaries, observations
```

### 4.2 Transform Layer 1 — 结构化蒸馏（Venus API）

核心蒸馏逻辑，通过 Venus API 调用 LLM，强制输出符合 `DistillationResult` JSON Schema 的结构化数据：

```python
import os
import json
import requests
from pydantic import ValidationError


# ── Venus API 配置 ──
VENUS_TOKEN = os.environ.get('ENV_VENUS_OPENAPI_SECRET_ID') + "@5172"
VENUS_URL = "http://v2.open.venus.oa.com/llmproxy/chat/completions"
VENUS_MODEL = "claude-sonnet-4-6"
VENUS_RPM_LIMIT = 50  # Venus API 频率限制：50 次/分钟


# ── 蒸馏 System Prompt ──
DISTILL_SYSTEM_PROMPT = """你是一个工程经验蒸馏器。你的任务是从混沌的 AI Agent 工作记忆中提取有价值的工程经验。

## 判断标准
以下类型的记忆应该被蒸馏为经验：
- 排障过程：从发现问题到定位根因到解决的完整链路
- 架构决策：为什么选 A 不选 B，权衡了什么
- 踩坑记录：实际遇到的非显而易见的问题和解决方案
- 流程优化：发现了更好的做法并实施

以下类型应该被跳过（视为噪音）：
- 纯文件编辑事件，没有上下文
- 日常 git 操作（add/commit/push）除非涉及特殊问题
- 空结果的查询操作
- 重复的探测/轮询记录

## 输出要求
- 严格按照提供的 JSON Schema 输出
- issue_context 必须包含具体错误信息或异常现象，不能是泛泛描述
- root_cause 必须是真正的根因，不是表面现象
- rationale 要说明第一性原理或权衡逻辑
- scope 分级规则：
  - architecture: 影响系统设计、全员都该知道的（如权限模型重构、传输层迁移）
  - engineering: 行业通识、可写进团队规范的（如 Bearer Token 格式、lockfile 同步）
  - environment: 仅特定 OS/Shell/IDE 组合下才会遇到的（如 PowerShell 语法差异、CRLF 行尾）
- scope=environment 时必须填写 environment_conditions，标注 os/shell/ide/runtime
- trigger_patterns: 写 2-4 条泛化后的触发场景，比 issue_context 更抽象，让 Agent 在更多情境下能匹配到
- confidence: 如果你不确定某条经验是否准确，降低置信度
- 如果整批记忆都是噪音，返回空 experiences 列表并填写 skipped_reason
"""


# ── Venus API 限流器 ──
import time
from collections import deque


class RateLimiter:
    """滑动窗口限流器，确保不超过 Venus API 频率限制"""

    def __init__(self, max_calls: int = VENUS_RPM_LIMIT, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window = window_seconds
        self.calls = deque()

    def wait(self):
        """在发起请求前调用，必要时阻塞等待直到窗口内有余量"""
        now = time.monotonic()
        # 清理窗口外的旧记录
        while self.calls and self.calls[0] <= now - self.window:
            self.calls.popleft()
        # 如果已达上限，等待最早一条过期
        if len(self.calls) >= self.max_calls:
            sleep_until = self.calls[0] + self.window
            sleep_time = sleep_until - now
            if sleep_time > 0:
                print(f"  [RateLimit] 达到 {self.max_calls} 次/分钟上限，等待 {sleep_time:.1f}s...")
                time.sleep(sleep_time)
        self.calls.append(time.monotonic())


_rate_limiter = RateLimiter(max_calls=VENUS_RPM_LIMIT, window_seconds=60)


def build_distill_prompt(summaries: list, observations: list, time_range: str) -> str:
    """构造蒸馏用户 Prompt"""
    parts = [f"## 时间范围\n{time_range}\n"]

    if summaries:
        parts.append("## Session Summaries\n")
        for s in summaries:
            parts.append(f"### Session {s.get('id', 'N/A')}")
            for key in ['request', 'investigated', 'learned', 'completed', 'next_steps']:
                val = s.get(key, '')
                if val:
                    parts.append(f"- **{key}**: {val}")
            if s.get('meta_intent'):
                parts.append(f"- **meta_intent**: {s['meta_intent']}")
            parts.append("")

    if observations:
        parts.append("## Observations\n")
        for o in observations:
            parts.append(f"### [{o.get('type', 'unknown')}] {o.get('title', 'N/A')}")
            if o.get('text'):
                parts.append(o['text'][:500])
            if o.get('facts'):
                parts.append(f"- **facts**: {o['facts'][:300]}")
            if o.get('meta_intent'):
                parts.append(f"- **meta_intent**: {o['meta_intent']}")
            parts.append("")

    parts.append("请从上述记忆中蒸馏出有价值的工程经验。")
    return "\n".join(parts)


def distill_via_venus(
    summaries: list,
    observations: list,
    time_range: str
) -> DistillationResult:
    """调用 Venus API 执行蒸馏，强制输出结构化 JSON（内置限流）"""

    user_prompt = build_distill_prompt(summaries, observations, time_range)

    # 构造 JSON Schema 约束
    response_schema = DistillationResult.model_json_schema()

    payload = {
        "model": VENUS_MODEL,
        "messages": [
            {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "distillation_result",
                "strict": True,
                "schema": response_schema
            }
        },
        "temperature": 0.3,
        "max_tokens": 4096
    }

    result_json = call_venus_api(payload)
    content = result_json["choices"][0]["message"]["content"]

    # Pydantic 校验 — 如果 LLM 输出不符合 Schema 直接报错
    try:
        return DistillationResult.model_validate_json(content)
    except ValidationError as e:
        logger.error(f"[Pydantic] 校验失败: {e}")
        logger.error(f"[Pydantic] LLM 原始输出: {content[:500]}")
        raise RuntimeError(f"LLM output validation failed: {e}")
```

### 4.3 Load Layer 1 — 写入结构化存储

```python
import hashlib
from pathlib import Path


def compute_experience_hash(exp: EngineeringExperience) -> str:
    """基于 issue_context + solution 生成去重指纹"""
    raw = f"{exp.issue_context}|{exp.solution}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_experiences(
    result: DistillationResult,
    output_path: str = "distilled_experiences.json",
    min_confidence: float = 0.5
) -> int:
    """将蒸馏结果追加写入 JSON 文件，去重 + 低置信度过滤"""
    output = Path(output_path)

    # 加载已有数据
    existing = []
    existing_hashes = set()
    if output.exists():
        existing = json.loads(output.read_text(encoding='utf-8'))
        existing_hashes = {e.get('_hash') for e in existing}

    # 过滤 + 去重 + 追加
    added = 0
    for exp in result.experiences:
        if exp.confidence < min_confidence:
            continue
        h = compute_experience_hash(exp)
        if h in existing_hashes:
            continue
        entry = exp.model_dump()
        entry['_hash'] = h
        entry['_distilled_at'] = datetime.utcnow().isoformat() + 'Z'
        existing.append(entry)
        existing_hashes.add(h)
        added += 1

    # 写回
    output.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    return added
```

### 4.4 Transform Layer 2 — 叙事重建（Venus API）

Layer 1 输出的是压缩过的结构化数据，丢失了排查过程、失败尝试和思维转折。Layer 2 将同一主题下的多条结构化经验 + 原始记忆重新喂给 LLM，要求它**还原完整的问题解决叙事**。

#### 4.4.1 叙事输出模型

```python
class NarrativeExperience(BaseModel):
    """单条叙事重建后的经验"""
    title: str = Field(description="经验标题，简洁有力")
    problem_description: str = Field(description="问题描述，包含具体错误和环境背景")
    environment: str = Field(description="局限环境：技术栈、部署方式、工具链等")
    project: str = Field(description="所属项目")
    timeline: str = Field(description="处理时间和耗时")
    investigation_journey: str = Field(
        description="完整的排查过程，必须包含失败的尝试和每次失败带来的线索。"
                    "用编号步骤描述，标注哪些路走通了、哪些走不通、每步得到了什么结论"
    )
    resolution: str = Field(description="最终解决方案和结果")
    takeaways: List[str] = Field(
        description="可复用的经验提炼，每条以动词开头，可直接指导行动"
    )
    methodology_tags: List[str] = Field(
        description="该经验体现的方法论标签，如：排除法、最小改动、噪音过滤、机制化预防"
    )


class NarrativeBundle(BaseModel):
    """一次叙事重建的输出"""
    narratives: List[NarrativeExperience]
    methodology_summary: Optional[str] = Field(
        default=None,
        description="如果多条经验呈现出一致的解决问题思路，在此总结方法论"
    )
```

#### 4.4.2 按主题聚合 Layer 1 经验

```python
from collections import defaultdict


def group_experiences_by_theme(
    experiences: List[dict],
    observations: List[dict]
) -> dict:
    """将 Layer 1 的结构化经验按 experience_type + 关联组件聚合为主题组"""
    theme_groups = defaultdict(lambda: {"experiences": [], "raw_observations": []})

    for exp in experiences:
        # 用 experience_type 作为主分组键
        theme_key = exp.get("experience_type", "unknown")
        theme_groups[theme_key]["experiences"].append(exp)

    # 将原始 observations 按时间关联到对应主题
    for obs in observations:
        concepts = obs.get("concepts", "")
        matched = False
        for theme_key, group in theme_groups.items():
            for exp in group["experiences"]:
                # 通过 related_components 与 observation 的 concepts 做模糊匹配
                components = set(c.lower() for c in exp.get("related_components", []))
                obs_concepts = set(c.strip().lower() for c in concepts.split(",")) if concepts else set()
                if components & obs_concepts:
                    group["raw_observations"].append(obs)
                    matched = True
                    break
            if matched:
                break

    return dict(theme_groups)
```

#### 4.4.3 叙事重建 Prompt 与调用

```python
NARRATIVE_SYSTEM_PROMPT = """你是一个工程经验叙事重建器。你的任务是把结构化的经验数据和原始工作记忆重建为完整的、可分享的经验叙事。

## 你的目标
写出来的经验应该像一个资深工程师在跟同事讲故事——不是干巴巴的结论，而是完整的排查旅程。

## 必须包含的内容
1. **失败的尝试** — 这是最有价值的部分。不要只写最终方案，要还原走过的弯路和每次失败带来的线索缩小
2. **思维转折点** — 是什么信号让你从一个方向转向另一个方向？
3. **噪音识别** — 排查过程中哪些现象是误导性的，是怎么识别出来的？
4. **方法论标签** — 每条经验背后的通用思维模式是什么？

## 写作风格
- 用中文
- 排查步骤用编号，每步标注成功/失败/线索
- takeaways 用祈使句，直接可执行（如"先查服务端日志，再看客户端表象"）
- 不要泛泛而谈，每个结论都必须有具体事实支撑

## 输出要求
严格按照提供的 JSON Schema 输出。
"""


def build_narrative_prompt(theme_key: str, group: dict) -> str:
    """构造叙事重建的用户 Prompt"""
    parts = [f"## 主题：{theme_key}\n"]

    parts.append("### 结构化经验（Layer 1 蒸馏结果）\n")
    for i, exp in enumerate(group["experiences"], 1):
        parts.append(f"#### 经验 {i}")
        parts.append(f"- **问题**: {exp.get('issue_context', '')}")
        parts.append(f"- **根因**: {exp.get('root_cause', '')}")
        parts.append(f"- **方案**: {exp.get('solution', '')}")
        parts.append(f"- **原理**: {exp.get('rationale', '')}")
        parts.append(f"- **预防**: {exp.get('prevention', '')}")
        parts.append(f"- **组件**: {', '.join(exp.get('related_components', []))}")
        parts.append("")

    if group.get("raw_observations"):
        parts.append("### 原始观测记录（包含排查过程的细节）\n")
        for obs in group["raw_observations"][:15]:  # 限制数量避免超长
            parts.append(f"**[{obs.get('type', '')}] {obs.get('title', '')}**")
            if obs.get('text'):
                parts.append(obs['text'][:400])
            if obs.get('facts'):
                parts.append(f"facts: {obs['facts'][:300]}")
            parts.append("")

    parts.append(
        "请基于以上结构化经验和原始观测记录，重建完整的经验叙事。"
        "重点还原排查过程中的失败尝试、思维转折和噪音识别。"
    )
    return "\n".join(parts)


def rebuild_narrative_via_venus(theme_key: str, group: dict) -> NarrativeBundle:
    """调用 Venus API 执行叙事重建（内置限流）"""

    user_prompt = build_narrative_prompt(theme_key, group)
    response_schema = NarrativeBundle.model_json_schema()

    payload = {
        "model": VENUS_MODEL,
        "messages": [
            {"role": "system", "content": NARRATIVE_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "narrative_bundle",
                "strict": True,
                "schema": response_schema
            }
        },
        "temperature": 0.5,  # 比 Layer 1 稍高，允许更自然的叙事
        "max_tokens": 8192   # 叙事需要更多输出空间
    }

    result_json = call_venus_api(payload)
    content = result_json["choices"][0]["message"]["content"]

    try:
        return NarrativeBundle.model_validate_json(content)
    except ValidationError as e:
        logger.error(f"[Pydantic] L2 校验失败: {e}")
        logger.error(f"[Pydantic] LLM 原始输出: {content[:500]}")
        raise RuntimeError(f"Narrative validation failed: {e}")
```

### 4.5 Load Layer 2 — 生成 Markdown 经验文档

```python
def render_narrative_markdown(bundle: NarrativeBundle, output_path: str) -> str:
    """将叙事重建结果渲染为可读的 Markdown 文档"""
    lines = ["# 工程经验叙事", ""]
    lines.append(f"> 自动生成于 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}，"
                 f"基于 Shadow-Folk 记忆数据蒸馏重建。")
    lines.append("")

    for narrative in bundle.narratives:
        lines.append(f"## {narrative.title}")
        lines.append("")
        lines.append(f"### 问题描述")
        lines.append(narrative.problem_description)
        lines.append("")
        lines.append(f"### 局限环境")
        lines.append(narrative.environment)
        lines.append("")
        lines.append(f"### 项目")
        lines.append(narrative.project)
        lines.append("")
        lines.append(f"### 处理时间")
        lines.append(narrative.timeline)
        lines.append("")
        lines.append(f"### 解决过程")
        lines.append(narrative.investigation_journey)
        lines.append("")
        lines.append(f"### 解决结果")
        lines.append(narrative.resolution)
        lines.append("")
        lines.append(f"### 经验提炼")
        for t in narrative.takeaways:
            lines.append(f"- {t}")
        lines.append("")
        lines.append(f"**方法论标签**: {' / '.join(narrative.methodology_tags)}")
        lines.append("")
        lines.append("---")
        lines.append("")

    if bundle.methodology_summary:
        lines.append("## 方法论总结")
        lines.append("")
        lines.append(bundle.methodology_summary)
        lines.append("")

    content = "\n".join(lines)
    Path(output_path).write_text(content, encoding='utf-8')
    return content
```

## 5. 主入口与调度

### 5.1 单次执行（两层完整流程）

```python
def run_distillation(
    summaries_csv: str = "shadowfolk_summaries_week.csv",
    observations_csv: str = "shadowfolk_observations_week.csv",
    hours: int = 24,
    l1_output: str = "distilled_experiences.json",
    l2_output: str = "experience_narratives.md"
):
    """执行一次完整的两层蒸馏流程"""
    import time as _time
    run_start = _time.monotonic()
    total_api_calls = 0
    total_tokens = 0

    # ═══ Layer 1: 结构化蒸馏 ═══
    logger.info(f"══ 蒸馏开始 ══ 数据源: shadowfolk, 时间窗口: {hours}h")
    summaries, observations = extract_from_csv(summaries_csv, observations_csv, hours)
    logger.info(f"[Extract] 拉取完成: {len(summaries)} summaries, {len(observations)} observations")

    if not summaries and not observations:
        logger.info("[Skip] 无新数据，跳过蒸馏")
        return

    time_range = f"最近 {hours} 小时"
    l1_start = _time.monotonic()
    batch_size = 20
    total_added = 0

    all_records = summaries + observations
    n_batches = -(-len(all_records) // batch_size)  # ceil division
    logger.info(f"[Layer 1] 开始结构化蒸馏, 共 {len(all_records)} 条记录, 分 {n_batches} 批")

    for i in range(0, len(all_records), batch_size):
        batch = all_records[i:i + batch_size]
        batch_num = i // batch_size + 1
        batch_summaries = [r for r in batch if 'request' in r]
        batch_observations = [r for r in batch if 'text' in r]

        logger.info(f"[L1] 批次 {batch_num}/{n_batches}: {len(batch)} 条记录 "
                     f"({len(batch_summaries)} summaries + {len(batch_observations)} observations)")

        result = distill_via_venus(batch_summaries, batch_observations, time_range)
        total_api_calls += 1

        skip_msg = f', 跳过原因: {result.skipped_reason}' if result.skipped_reason else ''
        logger.info(f"[L1] 批次 {batch_num}/{n_batches} 完成: "
                     f"蒸馏出 {len(result.experiences)} 条经验{skip_msg}")

        added = load_experiences(result, l1_output)
        total_added += added

    l1_elapsed = _time.monotonic() - l1_start
    logger.info(f"[Layer 1 完成] {n_batches} 批 | 新增 {total_added} 条经验 | "
                 f"总耗时 {l1_elapsed:.1f}s")

    # ═══ Layer 2: 叙事重建 ═══
    l2_start = _time.monotonic()
    logger.info(f"[Layer 2] 开始叙事重建")

    # 读取 Layer 1 全量经验
    all_experiences = json.loads(Path(l1_output).read_text(encoding='utf-8'))

    # 按主题聚合
    theme_groups = group_experiences_by_theme(all_experiences, observations)
    group_summary = ", ".join(f"{k}({len(v['experiences'])})" for k, v in theme_groups.items())
    logger.info(f"[L2] 聚合为 {len(theme_groups)} 个主题组: {group_summary}")

    all_narratives = []
    methodology_parts = []

    for theme_key, group in theme_groups.items():
        if not group["experiences"]:
            continue
        n_obs = len(group.get("raw_observations", []))
        logger.info(f"[L2] 重建主题: {theme_key} "
                     f"({len(group['experiences'])} 条经验, {n_obs} 条原始观测)")

        bundle = rebuild_narrative_via_venus(theme_key, group)
        total_api_calls += 1
        all_narratives.extend(bundle.narratives)

        logger.info(f"[L2] 主题 {theme_key} 完成: 生成 {len(bundle.narratives)} 条叙事")

        if bundle.methodology_summary:
            methodology_parts.append(bundle.methodology_summary)

    # 合并为一个完整文档
    final_bundle = NarrativeBundle(
        narratives=all_narratives,
        methodology_summary="\n\n".join(methodology_parts) if methodology_parts else None
    )
    render_narrative_markdown(final_bundle, l2_output)

    l2_elapsed = _time.monotonic() - l2_start
    logger.info(f"[Layer 2 完成] {len(theme_groups)} 主题 | "
                 f"{len(all_narratives)} 条叙事 | 总耗时 {l2_elapsed:.1f}s")

    # ═══ 全流程汇总 ═══
    total_elapsed = _time.monotonic() - run_start
    logger.info(f"══ 蒸馏完成 ══ 总耗时 {total_elapsed:.1f}s | "
                 f"API 调用 {total_api_calls} 次")
    logger.info(f"输出: {l1_output} ({total_added} 条) + "
                 f"{l2_output} ({len(all_narratives)} 条叙事)")


if __name__ == "__main__":
    run_distillation()
```

### 5.2 定时调度（cron）

```bash
# 每天凌晨 3 点执行蒸馏
0 3 * * * cd /path/to/memory-research && python distill_memories.py >> /var/log/distill.log 2>&1
```

## 6. 蒸馏输出示例

### 6.1 Layer 1 输出 — 结构化 JSON

基于实际 Shadow-Folk 记忆数据，蒸馏输出示例：

```json
{
  "experiences": [
    {
      "issue_context": "yuantongliu 配置 MCP 后 Cursor 持续显示红色错误，Authorization 请求头只传了令牌本身，缺少 Bearer 前缀，导致 validateApiToken 校验 startsWith('Bearer ') 失败返回 401",
      "root_cause": "Authorization header 格式错误，缺少 'Bearer ' 前缀。validateApiToken 中间件严格校验前缀，缺失时直接返回 null",
      "solution": "将配置改为 Authorization: Bearer <token>。同时确认 Cursor 界面的红色报错是 SSE 轮询探测噪音，不影响 POST 工具调用",
      "rationale": "Bearer Token 认证遵循 RFC 6750 规范，前缀是协议必需部分。客户端表象（红色错误）和实际故障（401）是两个独立问题，需分别排查",
      "experience_type": "debugging",
      "related_components": ["validateApiToken", "Cursor MCP client", "Bearer Token auth"],
      "prevention": "在错误提示中明确给出完整格式示例 'Authorization: Bearer sf_xxx'；在 validateApiToken 中对缺少前缀的情况返回更具体的错误信息",
      "confidence": 0.95
    },
    {
      "issue_context": "Windows 上通过 SSH 上传 Bash 脚本到 Linux 执行，报 $'\\r': command not found，curl 多行参数全部断裂",
      "root_cause": "脚本在 Windows 编辑器中保存为 CRLF 行尾，Linux Bash 将 \\r 解析为命令名的一部分",
      "solution": "在远程执行前加入 sed 's/\\r$//' 转换行尾；避免在 PowerShell 中嵌套 Bash 语法，改为上传独立脚本文件执行",
      "rationale": "跨平台文件传输的行尾不一致是根本原因。多层嵌套（PowerShell → SSH → Bash）的引号转义在实践中不可行，应拆分为文件传输 + 远程执行两步",
      "experience_type": "cross_platform",
      "related_components": ["SSH", "SCP", "PowerShell", "Bash", "CRLF/LF"],
      "prevention": "固化为标准做法：所有上传到 Linux 的脚本先 sed 清洗；在 .gitattributes 中对 .sh 文件强制 LF",
      "confidence": 0.92
    }
  ],
  "skipped_reason": null
}
```

### 6.2 Layer 2 输出 — 叙事重建

同一个 CRLF 问题，经过 Layer 2 叙事重建后的输出：

```json
{
  "narratives": [
    {
      "title": "跨平台远程脚本执行：从四次失败到标准化方案",
      "problem_description": "在验证 MCP HTTP 端点部署时，需要通过 SSH 在远程 Linux 服务器上执行测试脚本（curl 发送 JSON-RPC 请求）。本地开发环境是 Windows + PowerShell（Cursor 终端），远程是 Linux Docker 容器。脚本持续报错 `$'\\r': command not found` 和 `-H: command not found`，无法正常执行。",
      "environment": "本地：Windows 11 + PowerShell 7（Cursor 集成终端）\n远程：CVM Linux + Docker（shadow-folk-dev 容器）\n工具链：SSH、SCP、curl、Bash\n网络：通过 SSH 隧道访问远程 3001/8080 端口",
      "project": "Shadow-Folk — MCP HTTP 端点部署验证",
      "timeline": "2026-03-30，约 20 分钟（4 次尝试）",
      "investigation_journey": "1. **第一次尝试：直接在 SSH 命令中内联 curl** → ❌ 失败\n   - 做法：`ssh user@host \"curl -X POST ... -d '{...}'\"` 直接在 PowerShell 中执行\n   - 现象：PowerShell 报 `ParserError: InvalidEndOfLine`\n   - 线索：PowerShell 不认识 `&&`、heredoc (`<<'EOF'`)、`$(...)` 等 Bash 语法\n   - 结论：**排除了"直接内联命令"这条路**，PowerShell 和 Bash 语法不兼容\n\n2. **第二次尝试：SCP 上传脚本 + SSH 远程执行** → ❌ 失败\n   - 做法：本地写好 `test-mcp.sh`，`scp` 上传到远程 `/tmp/`，再 `ssh ... bash /tmp/test-mcp.sh`\n   - 现象：`$'\\r': command not found`、`-H: command not found`\n   - 线索：`\\r` 是 Windows 换行符 (CR) 的表现，说明文件在 Windows 保存时用了 CRLF\n   - 结论：**问题定位到文件格式层面**，不是命令逻辑错误\n\n3. **第三次尝试：Python one-liner 内联生成 JSON** → ❌ 失败\n   - 做法：`ssh ... \"python3 -c 'import json; ...'\"` 绕过 Bash 脚本\n   - 现象：`SyntaxError: unterminated string literal`\n   - 线索：引号在 PowerShell → SSH → Bash → Python 四层传递中彻底崩溃\n   - 结论：**确认多层嵌套引号转义不可行**，必须放弃内联方案\n\n4. **第四次尝试：sed 清洗行尾 + 脚本执行** → ✅ 成功\n   - 做法：`sed 's/\\r$//' /tmp/test-mcp.sh > /tmp/test-mcp-clean.sh && bash /tmp/test-mcp-clean.sh`\n   - 结果：脚本正常执行，MCP initialize、无效 Token 拒绝、tools/list 三个测试场景全部通过",
      "resolution": "最终方案分两步：先 SCP 上传脚本，再在远程 sed 清洗 CRLF 后执行。后续固化为标准操作：所有上传到 Linux 的脚本统一经过 `sed 's/\\r$//'` 清洗。同时在项目 `.gitattributes` 中对 `.sh` 文件强制 LF 行尾。",
      "takeaways": [
        "Windows → Linux 传输脚本文件，第一件事就是处理 CRLF → LF，用 `sed 's/\\r$//'` 或 `dos2unix`",
        "永远不要在 PowerShell 中手写多层嵌套的 Bash 命令，改为上传独立脚本文件",
        "遇到 `$'\\r': command not found` 立刻想到 CRLF 行尾问题，这是跨平台开发的经典症状",
        "四层嵌套引号（PowerShell → SSH → Bash → Python/Node）在实践中不可能手写正确，这是一条死路"
      ],
      "methodology_tags": ["有方向的排除法", "每次失败缩小问题空间", "拆分复杂链路为独立步骤"]
    }
  ],
  "methodology_summary": null
}
```

### 6.3 Layer 2 渲染后的 Markdown（最终可读形式）

上述 JSON 经过 `render_narrative_markdown()` 渲染后，生成的 Markdown 如下：

---

> **跨平台远程脚本执行：从四次失败到标准化方案**
>
> **问题描述**：在验证 MCP HTTP 端点部署时，需要通过 SSH 在远程 Linux 服务器上执行测试脚本。本地是 Windows + PowerShell，远程是 Linux Docker 容器。脚本持续报错 `$'\r': command not found`。
>
> **解决过程**：
> 1. **直接内联 curl** → ❌ PowerShell 不认识 Bash 语法 → 排除内联方案
> 2. **SCP 上传脚本** → ❌ `$'\r'` 报错 → 定位到 CRLF 行尾问题
> 3. **Python one-liner** → ❌ 四层引号嵌套崩溃 → 确认多层嵌套不可行
> 4. **sed 清洗 + 执行** → ✅ 三个测试场景全部通过
>
> **经验提炼**：
> - Windows → Linux 传输脚本，第一件事处理 CRLF → LF
> - 永远不要手写四层嵌套引号，改为上传文件执行
> - `$'\r': command not found` = CRLF 问题，跨平台经典症状
>
> **方法论**: 有方向的排除法 / 每次失败缩小问题空间 / 拆分复杂链路为独立步骤

---

## 7. 关键设计决策

### 为什么用 Pydantic + JSON Schema 而不是开放式 Prompt？

| 对比 | 开放式 Prompt | Pydantic 强制 Schema |
|------|-------------|---------------------|
| 输出格式 | 不可控，可能返回 Markdown/自然语言 | 严格 JSON，字段缺失直接报错 |
| 噪音过滤 | 依赖 LLM "自觉" | Schema 约束 + confidence 阈值 + 去重 hash |
| 下游消费 | 需要再次解析 | 直接入库/检索 |
| 可测试性 | 难以断言 | `model_validate_json()` 一行验证 |

### 为什么选 Venus API（claude-sonnet）？

- 内部代理，**无需外网访问**，合规性好
- claude-sonnet-4-6 对中文工程语境理解准确
- 支持 `response_format.json_schema` 强制结构化输出
- Token 成本可控，适合定时批量处理

### 为什么按 batch_size=20 分批？

- 单条 observation 平均 300-500 tokens，20 条约 6K-10K tokens 输入
- 加上 system prompt + schema 约束，总量控制在 15K tokens 以内
- 避免上下文过长导致 LLM "遗忘"前面记录中的有价值信息

## 8. Token 用量与耗时估算

以实际 Shadow-Folk 一周数据为基准（5 条 summaries + 80 条 observations）进行估算：

### 8.1 Layer 1 — 结构化蒸馏

| 指标 | 估算值 | 计算依据 |
|------|--------|---------|
| 原始记录数 | ~85 条 | 5 summaries + 80 observations |
| 分批数 | 5 批 | ceil(85 / 20) |
| 每批输入 tokens | ~8K | system prompt ~800 + schema ~500 + 20 条记忆 ~6.5K |
| 每批输出 tokens | ~1.5K | 平均蒸馏出 3-4 条经验 × 350 tokens/条 |
| **Layer 1 总输入** | **~40K tokens** | 5 批 × 8K |
| **Layer 1 总输出** | **~7.5K tokens** | 5 批 × 1.5K |
| 单次 API 延迟 | 3-6 秒 | claude-sonnet，Venus 代理延迟 |
| **Layer 1 总耗时** | **~15-30 秒** | 5 批串行调用 |

### 8.2 Layer 2 — 叙事重建

| 指标 | 估算值 | 计算依据 |
|------|--------|---------|
| 主题组数 | ~4 组 | debugging / architecture / cross_platform / deployment |
| 每组输入 tokens | ~6K | L1 经验 ~1.5K + 关联原始观测 ~4K + system prompt ~800 |
| 每组输出 tokens | ~3K | 叙事文本比结构化 JSON 长约 2 倍 |
| **Layer 2 总输入** | **~24K tokens** | 4 组 × 6K |
| **Layer 2 总输出** | **~12K tokens** | 4 组 × 3K |
| 单次 API 延迟 | 5-10 秒 | 输出更长，生成时间更久 |
| **Layer 2 总耗时** | **~20-40 秒** | 4 组串行调用 |

### 8.3 汇总

| 指标 | Layer 1 | Layer 2 | **合计** |
|------|---------|---------|----------|
| 输入 tokens | 40K | 24K | **~64K** |
| 输出 tokens | 7.5K | 12K | **~19.5K** |
| 总 tokens | 47.5K | 36K | **~83.5K** |
| API 调用次数 | 5 次 | 4 次 | **9 次** |
| 耗时 | 15-30s | 20-40s | **~35-70 秒** |

### 8.4 Venus API 频率限制与应对

Venus API 存在 **50 次/分钟** 的调用频率限制（RPM），超出后返回 429 Too Many Requests。

**对日度蒸馏的影响（~9 次调用）**：
- 远低于 50 RPM 限制，无需等待，全流程 35-70 秒内完成

**对周度蒸馏的影响（~35 次调用）**：
- 仍在 50 RPM 限制内，但如果启用并行调用需注意不要瞬时打满
- 建议并行度控制在 5 以内

**对月度全量回溯的影响（~100+ 次调用）**：
- 会触发限流，滑动窗口限流器自动等待
- 预计额外等待时间：每超限 50 次暂停约 60 秒
- 100 次调用预计总耗时从理论 5 分钟拉长到 ~7-8 分钟

**限流策略**：代码中使用 `RateLimiter` 滑动窗口限流器，Layer 1 和 Layer 2 共享同一个实例，在发起请求前自动检测窗口内调用次数，达到上限时阻塞等待，无需手动管理。

```python
# 限流器初始化（全局单例，两层共享）
_rate_limiter = RateLimiter(max_calls=50, window_seconds=60)
```

### 8.5 成本与扩展性说明

- **日度运行**（每天 85 条记忆）：~83K tokens/天，Venus API 内部代理无额外费用
- **周度运行**（积累 ~600 条记忆）：分批数增至 30+，总量 ~500K tokens，耗时约 5-8 分钟
- **并行优化**：Layer 1 各批次之间无依赖，可用 `asyncio` / `ThreadPoolExecutor` 并行调用，耗时可降至单批延迟 × 1.5
- **月度全量回溯**：建议对 batch_size 调至 30，并启用并行，预计 ~2M tokens，耗时 15-20 分钟

## 9. 面向不同消费者的经验路由

### 9.1 核心发现：人和 Agent 需要完全不同的经验格式

基于 v1 实际运行评估（见 `evaluation/distillation-v1-evaluation.md`），蒸馏经验的消费者分为两类，需求截然不同：

| 维度 | 人（同事/新人） | Agent（Claude Code 等） |
|------|--------------|----------------------|
| **需要什么** | 故事感、可记忆、有主次 | 全量覆盖、精确匹配、直接可执行 |
| **信息密度** | 6 条精选 > 43 条全量 | 43 条全量 > 6 条精选 |
| **最有价值的字段** | `investigation_journey`（排查过程） | `prevention` + `trigger_patterns`（怎么避免 + 何时触发） |
| **叙事 vs 结构化** | 叙事优先（L2 Markdown） | 结构化优先（L1 JSON） |
| **方法论** | 需要跨经验串联的通用框架 | 需要每条经验独立的触发条件 |
| **冗余容忍度** | 低（重复经验浪费阅读时间） | 高（重复不影响检索，遗漏才致命） |

### 9.2 为什么 Claude Code 手动总结对人更友好？

Claude Code 在对话中写 `shadowfolk-experience-summary.md` 时，具备管线不具备的五个能力：

1. **知道读者是谁** — 管线是冷启动 LLM 调用，不知道读者的知识水平
2. **会省略已知背景** — 管线不知道你懂什么，所以什么都解释一遍
3. **做跨经验串联** — "三次失败都在缩小问题空间"，管线只能在单条叙事内串联
4. **选择信息密度** — 告诉你"为什么快"而不是"做了什么"，管线面面俱到但没有主次
5. **有编辑判断力** — 选 6 条最重要的，管线输出 43 条不分主次

本质区别：手动总结**以读者为中心**（reader-centric），管线**以数据为中心**（data-centric）。

### 9.3 为什么 L1 JSON 对 Agent 更好用？

Agent 处理信息的方式跟人不同：
- 不需要故事感来保持注意力——**按需检索**，匹配到就用
- 不需要省略背景——冗余信息不影响检索效率
- 覆盖面比精炼度重要——遗漏一条经验 = 可能重复踩坑

L1 JSON 中对 Agent 最有价值的三个字段：

```
issue_context  → 用于匹配"当前遇到的问题是否与某条经验相关"
trigger_patterns → 泛化后的触发条件，扩大匹配范围
prevention     → 直接可执行的预防动作
```

### 9.4 scope 分级与路由策略

v1 的 43 条经验中，按适用范围分类：

| scope | 占比 | 例子 | 谁需要 |
|-------|------|------|--------|
| `architecture` | ~35% | get_projects 权限盲区、stdio→HTTP 迁移、ProjectAccessService | 全团队 + Agent |
| `engineering` | ~20% | Bearer Token 格式、lockfile 同步、schema 与实现不同步 | 团队规范 + Agent |
| `environment` | ~45% | PowerShell `&&`、curl 别名、CRLF、heredoc 语法 | **仅特定环境的 Agent** |

**路由规则**：

```python
def should_apply_experience(exp: EngineeringExperience, current_env: dict) -> bool:
    """判断一条经验在当前环境下是否适用"""

    # architecture 和 engineering 级别：始终适用
    if exp.scope in (ExperienceScope.architecture, ExperienceScope.engineering):
        return True

    # environment 级别：检查环境条件是否匹配
    if exp.scope == ExperienceScope.environment and exp.environment_conditions:
        cond = exp.environment_conditions
        if cond.os and cond.os != current_env.get("os"):
            return False
        if cond.shell and cond.shell != current_env.get("shell"):
            return False
        if cond.ide and cond.ide != current_env.get("ide"):
            return False
        return True

    return True  # 无条件时默认适用
```

**示例**：

```json
{
  "issue_context": "PowerShell 中 curl 是 Invoke-WebRequest 的别名，-s 参数无效",
  "scope": "environment",
  "environment_conditions": {
    "os": "windows",
    "shell": "powershell"
  },
  "trigger_patterns": [
    "在 Windows 终端执行 curl 命令报参数绑定错误",
    "curl -s 或 --connect-timeout 在 PowerShell 中不工作",
    "PowerShell 中 HTTP 请求行为与预期不一致"
  ],
  "prevention": "使用 curl.exe 显式调用系统 curl，或在脚本顶部加 Remove-Item Alias:curl"
}
```

当 Agent 检测到当前环境是 macOS + zsh 时，这条经验会被自动跳过，不会产生误导。

### 9.5 蒸馏输出的三种消费路径

```
                    L1 JSON (43 条全量)
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
    Agent 个人记忆    团队知识库      L2 叙事重建
    (全量, 按 scope   (仅 architecture  (按主题聚合
     + 环境条件过滤)   + engineering)    生成 Markdown)
          │              │              │
     机器消费         规范文档        人阅读
     trigger_patterns  prevention      investigation_journey
     按需检索          写进 wiki       分享/onboarding
```

## 10. 后续扩展

1. **回写标记** — 蒸馏完成后将 `distilled=1` 写回原始记录，避免重复处理
2. **向量索引** — 对蒸馏经验做 embedding，支持语义检索（"MCP 鉴权相关的经验"）
3. **知识图谱注入** — 将蒸馏经验作为 Graphiti episode 写入，与现有知识图谱关联
4. **质量反馈环** — 团队成员对蒸馏经验标注"有用/无用"，迭代优化 System Prompt
5. **多项目聚合** — 支持从多个 Shadow-Folk 项目（tank, dpar, shadowfolk 等）统一蒸馏
6. **环境自动检测** — Agent 启动时自动采集 OS/Shell/IDE 信息，作为 scope 路由的输入
7. **语义去重** — 对 `root_cause` 做 embedding，cosine similarity > 0.85 的视为重复，解决 v1 中 Bearer Token 问题出现 3 条、CRLF 出现 4 条的重复问题
8. **Layer 3：编辑与取舍** — 在 L1+L2 全量提取后，增加一层"编辑判断"：从全量经验中选出 Top N 最值得分享的，生成带主次的精选版本（弥补管线缺少编辑判断力的短板）

## 附录：文档关系说明

| 文档 | 定位 | 内容 |
|------|------|------|
| **本文档** (`memory-distillation-design.md`) | 需求设计文档 | 定义蒸馏管线"该怎么做"：架构、数据模型、ETL 实现、消费者路由 |
| `evaluation/distillation-v1-evaluation.md` | 实际运行评估 | 记录 v1"做得怎么样"：运行数据、成功/失败分析、与手动总结对比、改进建议 |

评估文档发现的问题应回流到本文档，驱动 v2 迭代。本次更新（scope 分级、trigger_patterns、消费者路由）即来自 v1 评估的反馈。
