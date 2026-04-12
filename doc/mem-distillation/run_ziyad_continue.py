"""继续 ziyad 蒸馏 — 从第 43 批跑到第 75 批，然后执行 L2"""
import json
import time as _time
from pathlib import Path

from extract import extract_all_from_csv
from models import NarrativeBundle
from narrative import (
    group_experiences_by_theme,
    rebuild_narrative_via_venus,
    render_narrative_markdown,
)
from transform import distill_via_venus, load_experiences
from venus_client import logger

MAX_BATCH = 75
BATCH_SIZE = 30
START_BATCH = 43
MIN_CONFIDENCE = 0.5

L1_OUTPUT = "output/dpar-ziyad-experiences.json"
L2_OUTPUT = "output/dpar-ziyad-narratives.md"

run_start = _time.monotonic()
total_api_calls = 0

summaries_csv = "dpar-export/ziyad_session_summaries.csv"
observations_csv = "dpar-export/ziyad_observations.csv"

summaries, observations = extract_all_from_csv(summaries_csv, observations_csv)
all_records = summaries + observations
total_batches = min(MAX_BATCH, -(-len(all_records) // BATCH_SIZE))

logger.info(
    f"═══ 继续蒸馏 ziyad ═══ 从批次 {START_BATCH}/{total_batches} 开始, "
    f"共 {len(all_records)} 条记录"
)

total_added = 0
l1_start = _time.monotonic()

for batch_idx in range(START_BATCH - 1, total_batches):
    i = batch_idx * BATCH_SIZE
    batch = all_records[i : i + BATCH_SIZE]
    if not batch:
        break
    batch_num = batch_idx + 1
    batch_summaries = [r for r in batch if "request" in r]
    batch_observations = [r for r in batch if "text" in r]

    logger.info(
        f"[L1] 批次 {batch_num}/{total_batches}: {len(batch)} 条记录 "
        f"({len(batch_summaries)} summaries + "
        f"{len(batch_observations)} observations)"
    )

    try:
        result = distill_via_venus(
            batch_summaries, batch_observations, "全量数据"
        )
        total_api_calls += 1

        skip_msg = (
            f", 跳过原因: {result.skipped_reason}"
            if result.skipped_reason
            else ""
        )
        logger.info(
            f"[L1] 批次 {batch_num}/{total_batches} 完成: "
            f"蒸馏出 {len(result.experiences)} 条经验{skip_msg}"
        )

        added = load_experiences(result, L1_OUTPUT, MIN_CONFIDENCE)
        total_added += added
    except Exception as e:
        total_api_calls += 1
        logger.error(f"[L1] 批次 {batch_num}/{total_batches} 失败，跳过: {e}")

l1_elapsed = _time.monotonic() - l1_start
logger.info(
    f"[Layer 1 完成] 批次 {START_BATCH}-{total_batches} | "
    f"新增 {total_added} 条经验 | 总耗时 {l1_elapsed:.1f}s"
)

# Layer 2
l2_start = _time.monotonic()
logger.info("[Layer 2] 开始叙事重建")

all_experiences = json.loads(Path(L1_OUTPUT).read_text(encoding="utf-8"))
theme_groups = group_experiences_by_theme(all_experiences, observations)
group_summary = ", ".join(
    f"{k}({len(v['experiences'])})" for k, v in theme_groups.items()
)
logger.info(f"[L2] 聚合为 {len(theme_groups)} 个主题组: {group_summary}")

all_narratives = []
methodology_parts = []

for theme_key, group in theme_groups.items():
    if not group["experiences"]:
        continue
    exps = group["experiences"]
    n_obs = len(group.get("raw_observations", []))

    if len(exps) > 25:
        logger.info(
            f"[L2] 主题 {theme_key} 有 {len(exps)} 条经验，"
            f"取 Top 25 高置信度经验"
        )
        exps = sorted(exps, key=lambda x: -x.get("confidence", 0))[:25]
        group = {
            "experiences": exps,
            "raw_observations": group.get("raw_observations", [])[:10],
        }
        n_obs = len(group["raw_observations"])

    logger.info(
        f"[L2] 重建主题: {theme_key} "
        f"({len(exps)} 条经验, {n_obs} 条原始观测)"
    )

    try:
        bundle = rebuild_narrative_via_venus(theme_key, group)
        total_api_calls += 1
        all_narratives.extend(bundle.narratives)
        logger.info(
            f"[L2] 主题 {theme_key} 完成: "
            f"生成 {len(bundle.narratives)} 条叙事"
        )
        if bundle.methodology_summary:
            methodology_parts.append(bundle.methodology_summary)
    except Exception as e:
        total_api_calls += 1
        logger.error(f"[L2] 主题 {theme_key} 失败，跳过: {e}")

final_bundle = NarrativeBundle(
    narratives=all_narratives,
    methodology_summary=(
        "\n\n".join(methodology_parts) if methodology_parts else None
    ),
)
render_narrative_markdown(final_bundle, L2_OUTPUT)

l2_elapsed = _time.monotonic() - l2_start
total_elapsed = _time.monotonic() - run_start
logger.info(
    f"[Layer 2 完成] {len(theme_groups)} 主题 | "
    f"{len(all_narratives)} 条叙事 | 总耗时 {l2_elapsed:.1f}s"
)
logger.info(
    f"═══ ziyad 蒸馏完成 ═══ 总耗时 {total_elapsed:.1f}s | "
    f"API 调用 {total_api_calls} 次"
)
logger.info(
    f"输出: {L1_OUTPUT} + {L2_OUTPUT} ({len(all_narratives)} 条叙事)"
)
