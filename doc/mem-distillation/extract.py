"""Extract 层 — 从 CSV / SQLite 拉取原始记忆数据"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta
from typing import List, Tuple

from venus_client import logger


def extract_from_csv(
    summaries_path: str,
    observations_path: str,
    hours: int = 24,
) -> Tuple[List[dict], List[dict]]:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    cutoff_epoch = int(cutoff.timestamp() * 1000)

    logger.info(
        f"[Extract] CSV 数据源: summaries={summaries_path}, "
        f"observations={observations_path}, 时间窗口={hours}h, "
        f"cutoff_epoch={cutoff_epoch}"
    )

    summaries: List[dict] = []
    with open(summaries_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            epoch = int(row.get("created_at_epoch", 0))
            if epoch >= cutoff_epoch:
                summaries.append(dict(row))

    observations: List[dict] = []
    with open(observations_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            epoch = int(row.get("created_at_epoch", 0))
            if epoch >= cutoff_epoch:
                observations.append(dict(row))

    logger.info(
        f"[Extract] 拉取完成: {len(summaries)} summaries, "
        f"{len(observations)} observations"
    )
    return summaries, observations


def extract_all_from_csv(
    summaries_path: str,
    observations_path: str,
) -> Tuple[List[dict], List[dict]]:
    """不做时间过滤，提取全量数据"""
    logger.info(
        f"[Extract] CSV 全量提取: summaries={summaries_path}, "
        f"observations={observations_path}"
    )

    summaries: List[dict] = []
    with open(summaries_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            summaries.append(dict(row))

    observations: List[dict] = []
    with open(observations_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            observations.append(dict(row))

    logger.info(
        f"[Extract] 全量拉取完成: {len(summaries)} summaries, "
        f"{len(observations)} observations"
    )
    return summaries, observations


def extract_from_sqlite(
    db_path: str,
    hours: int = 24,
) -> Tuple[List[dict], List[dict]]:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    summaries = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM memories WHERE content_type = 'session_summary' "
            "AND timestamp >= ? ORDER BY timestamp DESC",
            (cutoff.isoformat(),),
        ).fetchall()
    ]

    observations = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM memories WHERE content_type = 'observation' "
            "AND timestamp >= ? ORDER BY timestamp DESC",
            (cutoff.isoformat(),),
        ).fetchall()
    ]

    conn.close()
    logger.info(
        f"[Extract] SQLite 拉取完成: {len(summaries)} summaries, "
        f"{len(observations)} observations"
    )
    return summaries, observations
