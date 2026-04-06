"""Transform Layer 2 — 叙事重建 + Load Layer 2"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from pydantic import ValidationError

from models import NarrativeBundle, NarrativeExperience
from venus_client import VENUS_MODEL, call_venus_api, logger


# ── 叙事重建 System Prompt ──

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


def group_experiences_by_theme(
    experiences: List[dict],
    observations: List[dict],
) -> Dict[str, dict]:
    theme_groups: Dict[str, dict] = defaultdict(
        lambda: {"experiences": [], "raw_observations": []}
    )

    for exp in experiences:
        theme_key = exp.get("experience_type", "unknown")
        theme_groups[theme_key]["experiences"].append(exp)

    for obs in observations:
        concepts = obs.get("concepts", "")
        matched = False
        for _theme_key, group in theme_groups.items():
            for exp in group["experiences"]:
                components = set(
                    c.lower() for c in exp.get("related_components", [])
                )
                obs_concepts = (
                    set(c.strip().lower() for c in concepts.split(","))
                    if concepts
                    else set()
                )
                if components & obs_concepts:
                    group["raw_observations"].append(obs)
                    matched = True
                    break
            if matched:
                break

    return dict(theme_groups)


def build_narrative_prompt(theme_key: str, group: dict) -> str:
    parts = [f"## 主题：{theme_key}\n"]

    parts.append("### 结构化经验（Layer 1 蒸馏结果）\n")
    for i, exp in enumerate(group["experiences"], 1):
        parts.append(f"#### 经验 {i}")
        parts.append(f"- **问题**: {exp.get('issue_context', '')}")
        parts.append(f"- **根因**: {exp.get('root_cause', '')}")
        parts.append(f"- **方案**: {exp.get('solution', '')}")
        parts.append(f"- **原理**: {exp.get('rationale', '')}")
        parts.append(f"- **预防**: {exp.get('prevention', '')}")
        parts.append(
            f"- **组件**: {', '.join(exp.get('related_components', []))}"
        )
        parts.append("")

    if group.get("raw_observations"):
        parts.append("### 原始观测记录（包含排查过程的细节）\n")
        for obs in group["raw_observations"][:15]:
            parts.append(
                f"**[{obs.get('type', '')}] {obs.get('title', '')}**"
            )
            if obs.get("text"):
                parts.append(obs["text"][:400])
            if obs.get("facts"):
                parts.append(f"facts: {obs['facts'][:300]}")
            parts.append("")

    parts.append(
        "请基于以上结构化经验和原始观测记录，重建完整的经验叙事。"
        "重点还原排查过程中的失败尝试、思维转折和噪音识别。"
    )
    return "\n".join(parts)


def _build_narrative_schema() -> dict:
    schema = NarrativeBundle.model_json_schema()
    _strip_titles(schema)
    return schema


def _strip_titles(obj):
    if isinstance(obj, dict):
        obj.pop("title", None)
        for v in obj.values():
            _strip_titles(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_titles(item)


def _strip_markdown_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _try_repair_json(text: str) -> str:
    import json as _json
    text = text.rstrip()
    try:
        _json.loads(text)
        return text
    except _json.JSONDecodeError:
        pass

    last_brace = text.rfind("}")
    if last_brace > 0:
        candidate = text[:last_brace + 1]
        open_brackets = candidate.count("[") - candidate.count("]")
        open_braces = candidate.count("{") - candidate.count("}")
        suffix = "]" * open_brackets + "}" * open_braces
        candidate += suffix
        try:
            _json.loads(candidate)
            logger.warning("[JSON Repair] 成功修复截断的 JSON")
            return candidate
        except _json.JSONDecodeError:
            pass

    open_brackets = text.count("[") - text.count("]")
    open_braces = text.count("{") - text.count("}")
    if open_brackets > 0 or open_braces > 0:
        text += "]" * max(0, open_brackets) + "}" * max(0, open_braces)
        try:
            _json.loads(text)
            logger.warning("[JSON Repair] 暴力补全括号成功")
            return text
        except _json.JSONDecodeError:
            pass

    return text


NARRATIVE_SCHEMA_HINT = """
## JSON 输出格式要求（严格遵守）
直接输出 JSON，不要用 markdown 代码块包裹。字段定义如下：
{
  "narratives": [
    {
      "title": "string — 经验标题",
      "problem_description": "string — 问题描述",
      "environment": "string — 技术栈、部署方式、工具链",
      "project": "string — 所属项目",
      "timeline": "string — 处理时间和耗时",
      "investigation_journey": "string — 完整排查过程，包含失败尝试",
      "resolution": "string — 最终解决方案",
      "takeaways": ["string — 可复用的经验提炼，以动词开头"],
      "methodology_tags": ["string — 方法论标签"]
    }
  ],
  "methodology_summary": "string|null — 多条经验的方法论总结"
}
"""


def rebuild_narrative_via_venus(
    theme_key: str,
    group: dict,
) -> NarrativeBundle:
    user_prompt = build_narrative_prompt(theme_key, group)

    payload = {
        "model": VENUS_MODEL,
        "messages": [
            {
                "role": "system",
                "content": NARRATIVE_SYSTEM_PROMPT + NARRATIVE_SCHEMA_HINT,
            },
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.5,
        "max_tokens": 8192,
    }

    result_json = call_venus_api(payload)
    content = result_json["choices"][0]["message"]["content"]
    content = _strip_markdown_json(content)
    content = _try_repair_json(content)

    try:
        return NarrativeBundle.model_validate_json(content)
    except ValidationError as e:
        logger.error(f"[Pydantic] L2 校验失败: {e}")
        logger.error(f"[Pydantic] LLM 原始输出: {content[:500]}")
        raise RuntimeError(f"Narrative validation failed: {e}")


# ── Load Layer 2 — Markdown 渲染 ──

def render_narrative_markdown(
    bundle: NarrativeBundle,
    output_path: str,
) -> str:
    lines = ["# 工程经验叙事", ""]
    lines.append(
        f"> 自动生成于 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}，"
        f"基于 Shadow-Folk 记忆数据蒸馏重建。"
    )
    lines.append("")

    for narrative in bundle.narratives:
        lines.append(f"## {narrative.title}")
        lines.append("")
        lines.append("### 问题描述")
        lines.append(narrative.problem_description)
        lines.append("")
        lines.append("### 局限环境")
        lines.append(narrative.environment)
        lines.append("")
        lines.append("### 项目")
        lines.append(narrative.project)
        lines.append("")
        lines.append("### 处理时间")
        lines.append(narrative.timeline)
        lines.append("")
        lines.append("### 解决过程")
        lines.append(narrative.investigation_journey)
        lines.append("")
        lines.append("### 解决结果")
        lines.append(narrative.resolution)
        lines.append("")
        lines.append("### 经验提炼")
        for t in narrative.takeaways:
            lines.append(f"- {t}")
        lines.append("")
        lines.append(
            f"**方法论标签**: {' / '.join(narrative.methodology_tags)}"
        )
        lines.append("")
        lines.append("---")
        lines.append("")

    if bundle.methodology_summary:
        lines.append("## 方法论总结")
        lines.append("")
        lines.append(bundle.methodology_summary)
        lines.append("")

    content = "\n".join(lines)
    Path(output_path).write_text(content, encoding="utf-8")
    logger.info(f"[L2 Load] Markdown 写入: {output_path}")
    return content
