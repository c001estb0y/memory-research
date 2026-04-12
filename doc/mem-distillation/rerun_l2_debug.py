"""单独重试 ziyad 的 debugging 主题，增加 JSON escape 修复"""
import json
import re
from pathlib import Path

from models import NarrativeBundle
from narrative import group_experiences_by_theme, NARRATIVE_SYSTEM_PROMPT, NARRATIVE_SCHEMA_HINT
from narrative import _strip_markdown_json, _try_repair_json
from venus_client import VENUS_MODEL, call_venus_api, logger

MAX_EXPS = 6
MAX_OBS = 3
FIELD_LIMIT = 200
TIMEOUT = 300


def _truncate(s: str, limit: int = FIELD_LIMIT) -> str:
    if not s:
        return ""
    return s[:limit] + ("..." if len(s) > limit else "")


def _fix_json_escapes(text: str) -> str:
    """Fix invalid escape sequences in JSON strings."""
    def fix_match(m):
        s = m.group(0)
        fixed = re.sub(r'(?<!\\)\\(?!["\\/bfnrtu])', r'\\\\', s)
        return fixed
    return re.sub(r'"(?:[^"\\]|\\.)*"', fix_match, text, flags=re.DOTALL)


def build_slim_prompt(theme_key, exps, obs):
    parts = [f"## 主题：{theme_key}\n", "### 结构化经验\n"]
    for i, exp in enumerate(exps, 1):
        parts.append(f"**经验{i}**")
        parts.append(f"问题: {_truncate(exp.get('issue_context', ''))}")
        parts.append(f"根因: {_truncate(exp.get('root_cause', ''))}")
        parts.append(f"方案: {_truncate(exp.get('solution', ''))}")
        parts.append(f"组件: {', '.join(exp.get('related_components', [])[:5])}")
        parts.append("")
    if obs:
        parts.append("### 原始观测\n")
        for o in obs:
            parts.append(f"[{o.get('type', '')}] {o.get('title', '')}")
            if o.get("text"):
                parts.append(_truncate(o["text"], 200))
            parts.append("")
    parts.append("请基于以上信息重建经验叙事。重点还原排查过程中的失败尝试和思维转折。")
    parts.append("注意：输出纯 JSON，不要在字符串值中使用未转义的反斜杠。")
    return "\n".join(parts)


def render_narrative_block(n):
    lines = [
        f"## {n.title}", "",
        "### 问题描述", n.problem_description, "",
        "### 局限环境", n.environment, "",
        "### 项目", n.project, "",
        "### 处理时间", n.timeline, "",
        "### 解决过程", n.investigation_journey, "",
        "### 解决结果", n.resolution, "",
        "### 经验提炼",
    ]
    for t in n.takeaways:
        lines.append(f"- {t}")
    lines.extend(["", f"**方法论标签**: {' / '.join(n.methodology_tags)}", "", "---", ""])
    return "\n".join(lines)


def main():
    l1_path = "output/dpar-ziyad-experiences.json"
    obs_csv = "dpar-export/ziyad_observations.csv"
    output_md = "output/dpar-ziyad-narratives.md"

    all_exps = json.loads(Path(l1_path).read_text(encoding="utf-8"))

    import csv
    with open(obs_csv, "r", encoding="utf-8") as f:
        observations = list(csv.DictReader(f))

    theme_groups = group_experiences_by_theme(all_exps, observations)
    group = theme_groups.get("debugging")
    if not group:
        logger.error("未找到 debugging 主题")
        return

    exps = sorted(group["experiences"], key=lambda x: -x.get("confidence", 0))[:MAX_EXPS]
    obs = group.get("raw_observations", [])[:MAX_OBS]

    logger.info(f"重试 ziyad debugging: {len(exps)} exps, {len(obs)} obs")

    prompt = build_slim_prompt("debugging", exps, obs)
    payload = {
        "model": VENUS_MODEL,
        "messages": [
            {"role": "system", "content": NARRATIVE_SYSTEM_PROMPT + NARRATIVE_SCHEMA_HINT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 4096,
        "_timeout": TIMEOUT,
    }

    result = call_venus_api(payload)
    content = result["choices"][0]["message"]["content"]
    content = _strip_markdown_json(content)
    content = _fix_json_escapes(content)
    content = _try_repair_json(content)

    try:
        bundle = NarrativeBundle.model_validate_json(content)
    except Exception as e:
        logger.error(f"验证再次失败: {e}")
        logger.error(f"内容前 500 字符: {content[:500]}")
        return

    logger.info(f"成功: {len(bundle.narratives)} 条叙事")

    new_blocks = [render_narrative_block(n) for n in bundle.narratives]

    p = Path(output_md)
    existing = p.read_text(encoding="utf-8")

    if "## 方法论总结" in existing:
        idx = existing.index("## 方法论总结")
        final = existing[:idx] + "\n".join(new_blocks) + existing[idx:]
    else:
        final = existing.rstrip() + "\n\n" + "\n".join(new_blocks)

    p.write_text(final, encoding="utf-8")
    logger.info(f"追加 {len(new_blocks)} 条叙事到 {output_md}")


if __name__ == "__main__":
    main()
