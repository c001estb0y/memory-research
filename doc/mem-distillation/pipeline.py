#!/usr/bin/env python3
"""记忆蒸馏管线主入口 — 两层完整流程"""

from __future__ import annotations

import argparse
import json
import time as _time
from pathlib import Path

from extract import extract_all_from_csv, extract_from_csv
from models import NarrativeBundle
from narrative import (
    group_experiences_by_theme,
    rebuild_narrative_via_venus,
    render_narrative_markdown,
)
from transform import distill_via_venus, load_experiences
from venus_client import logger


def run_distillation(
    summaries_csv: str,
    observations_csv: str,
    hours: int = 0,
    l1_output: str = "output/distilled_experiences.json",
    l2_output: str = "output/experience_narratives.md",
    batch_size: int = 20,
    min_confidence: float = 0.5,
):
    run_start = _time.monotonic()
    total_api_calls = 0

    Path(l1_output).parent.mkdir(parents=True, exist_ok=True)
    Path(l2_output).parent.mkdir(parents=True, exist_ok=True)

    # ═══ Extract ═══
    if hours > 0:
        logger.info(f"══ 蒸馏开始 ══ 数据源: shadowfolk, 时间窗口: {hours}h")
        summaries, observations = extract_from_csv(
            summaries_csv, observations_csv, hours
        )
    else:
        logger.info("══ 蒸馏开始 ══ 数据源: shadowfolk, 全量数据")
        summaries, observations = extract_all_from_csv(
            summaries_csv, observations_csv
        )

    if not summaries and not observations:
        logger.info("[Skip] 无新数据，跳过蒸馏")
        return

    time_range = f"最近 {hours} 小时" if hours > 0 else "全量数据"

    # ═══ Layer 1: 结构化蒸馏 ═══
    l1_start = _time.monotonic()
    total_added = 0

    all_records = summaries + observations
    n_batches = -(-len(all_records) // batch_size)
    logger.info(
        f"[Layer 1] 开始结构化蒸馏, 共 {len(all_records)} 条记录, "
        f"分 {n_batches} 批"
    )

    for i in range(0, len(all_records), batch_size):
        batch = all_records[i : i + batch_size]
        batch_num = i // batch_size + 1
        batch_summaries = [r for r in batch if "request" in r]
        batch_observations = [r for r in batch if "text" in r]

        logger.info(
            f"[L1] 批次 {batch_num}/{n_batches}: {len(batch)} 条记录 "
            f"({len(batch_summaries)} summaries + "
            f"{len(batch_observations)} observations)"
        )

        try:
            result = distill_via_venus(
                batch_summaries, batch_observations, time_range
            )
            total_api_calls += 1

            skip_msg = (
                f", 跳过原因: {result.skipped_reason}"
                if result.skipped_reason
                else ""
            )
            logger.info(
                f"[L1] 批次 {batch_num}/{n_batches} 完成: "
                f"蒸馏出 {len(result.experiences)} 条经验{skip_msg}"
            )

            added = load_experiences(result, l1_output, min_confidence)
            total_added += added
        except Exception as e:
            total_api_calls += 1
            logger.error(
                f"[L1] 批次 {batch_num}/{n_batches} 失败，跳过: {e}"
            )

    l1_elapsed = _time.monotonic() - l1_start
    logger.info(
        f"[Layer 1 完成] {n_batches} 批 | 新增 {total_added} 条经验 | "
        f"总耗时 {l1_elapsed:.1f}s"
    )

    # ═══ Layer 2: 叙事重建 ═══
    l2_start = _time.monotonic()
    logger.info("[Layer 2] 开始叙事重建")

    all_experiences = json.loads(
        Path(l1_output).read_text(encoding="utf-8")
    )

    theme_groups = group_experiences_by_theme(all_experiences, observations)
    group_summary = ", ".join(
        f"{k}({len(v['experiences'])})" for k, v in theme_groups.items()
    )
    logger.info(
        f"[L2] 聚合为 {len(theme_groups)} 个主题组: {group_summary}"
    )

    all_narratives = []
    methodology_parts = []

    for theme_key, group in theme_groups.items():
        if not group["experiences"]:
            continue
        n_obs = len(group.get("raw_observations", []))
        logger.info(
            f"[L2] 重建主题: {theme_key} "
            f"({len(group['experiences'])} 条经验, {n_obs} 条原始观测)"
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
    render_narrative_markdown(final_bundle, l2_output)

    l2_elapsed = _time.monotonic() - l2_start
    logger.info(
        f"[Layer 2 完成] {len(theme_groups)} 主题 | "
        f"{len(all_narratives)} 条叙事 | 总耗时 {l2_elapsed:.1f}s"
    )

    # ═══ 全流程汇总 ═══
    total_elapsed = _time.monotonic() - run_start
    logger.info(
        f"══ 蒸馏完成 ══ 总耗时 {total_elapsed:.1f}s | "
        f"API 调用 {total_api_calls} 次"
    )
    logger.info(
        f"输出: {l1_output} ({total_added} 条) + "
        f"{l2_output} ({len(all_narratives)} 条叙事)"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Shadow-Folk 记忆蒸馏管线"
    )
    parser.add_argument(
        "--summaries",
        default="../SourceMem/shadowfolk_summaries_week.csv",
        help="summaries CSV 路径",
    )
    parser.add_argument(
        "--observations",
        default="../SourceMem/shadowfolk_observations_week.csv",
        help="observations CSV 路径",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=0,
        help="时间窗口（小时），0 表示全量",
    )
    parser.add_argument(
        "--l1-output",
        default="output/distilled_experiences.json",
        help="Layer 1 输出路径",
    )
    parser.add_argument(
        "--l2-output",
        default="output/experience_narratives.md",
        help="Layer 2 输出路径",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="每批记录数",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="最低置信度阈值",
    )
    args = parser.parse_args()

    run_distillation(
        summaries_csv=args.summaries,
        observations_csv=args.observations,
        hours=args.hours,
        l1_output=args.l1_output,
        l2_output=args.l2_output,
        batch_size=args.batch_size,
        min_confidence=args.min_confidence,
    )


if __name__ == "__main__":
    main()
