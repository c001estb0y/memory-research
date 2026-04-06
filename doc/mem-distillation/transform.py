"""Transform Layer 1 — 结构化蒸馏 + Load Layer 1"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import List

from pydantic import ValidationError

from models import DistillationResult, EngineeringExperience
from venus_client import VENUS_MODEL, call_venus_api, logger


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
- confidence: 如果你不确定某条经验是否准确，降低置信度
- 如果整批记忆都是噪音，返回空 experiences 列表并填写 skipped_reason
"""


def build_distill_prompt(
    summaries: list,
    observations: list,
    time_range: str,
) -> str:
    parts = [f"## 时间范围\n{time_range}\n"]

    if summaries:
        parts.append("## Session Summaries\n")
        for s in summaries:
            parts.append(f"### Session {s.get('id', 'N/A')}")
            for key in [
                "request", "investigated", "learned", "completed", "next_steps"
            ]:
                val = s.get(key, "")
                if val:
                    parts.append(f"- **{key}**: {val}")
            if s.get("meta_intent"):
                parts.append(f"- **meta_intent**: {s['meta_intent']}")
            parts.append("")

    if observations:
        parts.append("## Observations\n")
        for o in observations:
            parts.append(
                f"### [{o.get('type', 'unknown')}] {o.get('title', 'N/A')}"
            )
            if o.get("text"):
                parts.append(o["text"][:500])
            if o.get("facts"):
                parts.append(f"- **facts**: {o['facts'][:300]}")
            if o.get("meta_intent"):
                parts.append(f"- **meta_intent**: {o['meta_intent']}")
            parts.append("")

    parts.append("请从上述记忆中蒸馏出有价值的工程经验。")
    return "\n".join(parts)


def _build_json_schema() -> dict:
    schema = DistillationResult.model_json_schema()
    _strip_titles(schema)
    return schema


def _strip_titles(obj):
    """递归移除 JSON Schema 中的 title 字段以兼容 strict mode"""
    if isinstance(obj, dict):
        obj.pop("title", None)
        for v in obj.values():
            _strip_titles(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_titles(item)


def _strip_markdown_json(text: str) -> str:
    """剥离 LLM 输出中的 markdown 代码块包裹"""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _try_repair_json(text: str) -> str:
    """尝试修复被截断的 JSON — 补全缺失的括号"""
    import re
    text = text.rstrip()
    # 尝试直接解析，如果成功直接返回
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # 策略1：如果在 experiences 数组中间截断，尝试截断到最后一个完整对象
    last_brace = text.rfind("}")
    if last_brace > 0:
        candidate = text[:last_brace + 1]
        # 计算未闭合的括号
        open_brackets = candidate.count("[") - candidate.count("]")
        open_braces = candidate.count("{") - candidate.count("}")
        suffix = "]" * open_brackets + "}" * open_braces
        candidate += suffix
        try:
            json.loads(candidate)
            logger.warning("[JSON Repair] 成功修复截断的 JSON")
            return candidate
        except json.JSONDecodeError:
            pass

    # 策略2：暴力补全括号
    open_brackets = text.count("[") - text.count("]")
    open_braces = text.count("{") - text.count("}")
    if open_brackets > 0 or open_braces > 0:
        text += "]" * max(0, open_brackets) + "}" * max(0, open_braces)
        try:
            json.loads(text)
            logger.warning("[JSON Repair] 暴力补全括号成功")
            return text
        except json.JSONDecodeError:
            pass

    return text  # 无法修复，返回原文


SCHEMA_HINT = """
## JSON 输出格式要求（严格遵守）
直接输出 JSON，不要用 markdown 代码块包裹。字段定义如下：
{
  "experiences": [
    {
      "issue_context": "string — 遇到了什么问题，包含具体错误信息",
      "root_cause": "string — 根因分析",
      "solution": "string — 最终修复步骤或方案",
      "rationale": "string — 为什么这么做，权衡取舍",
      "experience_type": "string — 必须是以下之一: debugging, architecture, deployment, configuration, cross_platform, workflow, tooling",
      "related_components": ["string — 关联的系统模块或技术栈"],
      "prevention": "string|null — 如何避免同类问题再次发生",
      "confidence": 0.0-1.0
    }
  ],
  "skipped_reason": "string|null — 如果全部为噪音，说明跳过原因"
}
"""


def distill_via_venus(
    summaries: list,
    observations: list,
    time_range: str,
) -> DistillationResult:
    user_prompt = build_distill_prompt(summaries, observations, time_range)

    payload = {
        "model": VENUS_MODEL,
        "messages": [
            {
                "role": "system",
                "content": DISTILL_SYSTEM_PROMPT + SCHEMA_HINT,
            },
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    result_json = call_venus_api(payload)
    content = result_json["choices"][0]["message"]["content"]
    content = _strip_markdown_json(content)
    content = _try_repair_json(content)

    try:
        return DistillationResult.model_validate_json(content)
    except ValidationError as e:
        logger.error(f"[Pydantic] 校验失败: {e}")
        logger.error(f"[Pydantic] LLM 原始输出: {content[:500]}")
        raise RuntimeError(f"LLM output validation failed: {e}")


# ── Load Layer 1 ──

def compute_experience_hash(exp: EngineeringExperience) -> str:
    raw = f"{exp.issue_context}|{exp.solution}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_experiences(
    result: DistillationResult,
    output_path: str = "distilled_experiences.json",
    min_confidence: float = 0.5,
) -> int:
    output = Path(output_path)

    existing: List[dict] = []
    existing_hashes: set = set()
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        existing_hashes = {e.get("_hash") for e in existing}

    added = 0
    skipped_low = 0
    skipped_dup = 0
    for exp in result.experiences:
        if exp.confidence < min_confidence:
            skipped_low += 1
            continue
        h = compute_experience_hash(exp)
        if h in existing_hashes:
            skipped_dup += 1
            continue
        entry = exp.model_dump()
        entry["_hash"] = h
        entry["_distilled_at"] = datetime.utcnow().isoformat() + "Z"
        existing.append(entry)
        existing_hashes.add(h)
        added += 1

    output.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.debug(
        f"[L1 Load] {len(result.experiences)} 条经验, "
        f"去重跳过 {skipped_dup}, 低置信跳过 {skipped_low}, 新增 {added}"
    )
    return added
