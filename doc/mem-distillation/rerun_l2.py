"""补跑 L2 叙事 — 逐主题小批量重试，增大 timeout，精简 prompt"""
import json
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


def build_slim_prompt(theme_key: str, exps: list, obs: list) -> str:
    parts = [f"## 主题：{theme_key}\n"]
    parts.append("### 结构化经验\n")
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

    parts.append("请基于以上信息重建经验叙事，还原排查过程中的失败尝试和思维转折。")
    return "\n".join(parts)


def call_l2(theme_key: str, exps: list, obs: list) -> NarrativeBundle:
    prompt = build_slim_prompt(theme_key, exps, obs)
    payload = {
        "model": VENUS_MODEL,
        "messages": [
            {"role": "system", "content": NARRATIVE_SYSTEM_PROMPT + NARRATIVE_SCHEMA_HINT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.5,
        "max_tokens": 4096,
        "_timeout": TIMEOUT,
    }
    result = call_venus_api(payload)
    content = result["choices"][0]["message"]["content"]
    content = _strip_markdown_json(content)
    content = _try_repair_json(content)
    return NarrativeBundle.model_validate_json(content)


def load_existing_theme_titles(md_path: str) -> set:
    p = Path(md_path)
    if not p.exists():
        return set()
    titles = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("## ") and "方法论总结" not in line:
            titles.add(line[3:].strip())
    return titles


def render_narrative_block(n) -> str:
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


def append_narratives_to_md(md_path: str, new_blocks: list, methodology: str | None = None):
    p = Path(md_path)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""

    if "## 方法论总结" in existing:
        idx = existing.index("## 方法论总结")
        before = existing[:idx]
        after_section = existing[idx:]
    else:
        before = existing.rstrip() + "\n\n"
        after_section = ""

    new_content = "\n".join(new_blocks)

    if methodology and after_section:
        after_section = f"## 方法论总结\n\n{methodology}\n"
    elif methodology:
        after_section = f"\n## 方法论总结\n\n{methodology}\n"

    p.write_text(before + new_content + after_section, encoding="utf-8")


def run_l2_for_person(person: str, l1_path: str, obs_csv: str, output_md: str, skip_themes: set):
    logger.info(f"\n{'='*60}")
    logger.info(f"L2 补跑: {person}")

    all_exps = json.loads(Path(l1_path).read_text(encoding="utf-8"))
    logger.info(f"  L1 经验: {len(all_exps)}")

    observations = []
    if Path(obs_csv).exists():
        import csv
        with open(obs_csv, "r", encoding="utf-8") as f:
            observations = list(csv.DictReader(f))

    theme_groups = group_experiences_by_theme(all_exps, observations)

    new_blocks = []
    methodology_parts = []
    success = 0
    fail = 0

    for theme_key, group in theme_groups.items():
        if theme_key in skip_themes:
            logger.info(f"  [跳过] {theme_key}")
            continue
        if not group["experiences"]:
            continue

        exps = sorted(group["experiences"], key=lambda x: -x.get("confidence", 0))[:MAX_EXPS]
        obs = group.get("raw_observations", [])[:MAX_OBS]

        logger.info(f"  [L2] {theme_key}: {len(exps)} exps, {len(obs)} obs")

        try:
            bundle = call_l2(theme_key, exps, obs)
            for n in bundle.narratives:
                new_blocks.append(render_narrative_block(n))
            if bundle.methodology_summary:
                methodology_parts.append(bundle.methodology_summary)
            logger.info(f"  [L2] {theme_key} 成功: {len(bundle.narratives)} 条叙事")
            success += 1
        except Exception as e:
            logger.error(f"  [L2] {theme_key} 失败: {e}")
            fail += 1

    if new_blocks:
        methodology = "\n\n".join(methodology_parts) if methodology_parts else None
        append_narratives_to_md(output_md, new_blocks, methodology)
        logger.info(f"  追加 {len(new_blocks)} 条叙事到 {output_md}")

    logger.info(f"  完成: 成功={success}, 失败={fail}")


def main():
    run_l2_for_person(
        "hughesli",
        "output/dpar-hughesli-experiences.json",
        "dpar-export/hughesli_observations.csv",
        "output/dpar-hughesli-narratives.md",
        skip_themes={"cross_platform"},
    )

    run_l2_for_person(
        "ziyad",
        "output/dpar-ziyad-experiences.json",
        "dpar-export/ziyad_observations.csv",
        "output/dpar-ziyad-narratives.md",
        skip_themes={"deployment"},
    )


if __name__ == "__main__":
    main()
