"""DPAR 蒸馏入口 — 从 SQLite DB 导出 CSV 并运行蒸馏管线"""
import csv
import sqlite3
import sys
from pathlib import Path

from pipeline import run_distillation
from venus_client import logger


def export_db_to_csv(db_path: str, output_dir: str, prefix: str):
    """从 DPAR SQLite DB 导出 session_summaries 和 observations 为 CSV"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    for table in ["session_summaries", "observations"]:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY created_at_epoch").fetchall()
        if not rows:
            continue
        csv_path = out / f"{prefix}_{table}.csv"
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        logger.info(f"[Export] {db_path} -> {csv_path} ({len(rows)} rows)")

    conn.close()


def main():
    dpar_dir = Path(__file__).parent.parent / "SourceMem" / "dpar-mem"
    export_dir = Path(__file__).parent / "dpar-export"

    hughesli_db = dpar_dir / "dpar-hughesli.db"
    ziyad_db = dpar_dir / "dpar-ziyad.db"

    logger.info("═══ DPAR 数据导出 ═══")
    export_db_to_csv(str(hughesli_db), str(export_dir), "hughesli")
    export_db_to_csv(str(ziyad_db), str(export_dir), "ziyad")

    for person in ["hughesli", "ziyad"]:
        summaries_csv = str(export_dir / f"{person}_session_summaries.csv")
        observations_csv = str(export_dir / f"{person}_observations.csv")

        if not Path(summaries_csv).exists():
            logger.warning(f"[Skip] {summaries_csv} not found")
            continue

        logger.info(f"\n═══ 开始蒸馏: {person} ═══")
        run_distillation(
            summaries_csv=summaries_csv,
            observations_csv=observations_csv,
            hours=0,
            l1_output=f"output/dpar-{person}-experiences.json",
            l2_output=f"output/dpar-{person}-narratives.md",
            batch_size=30,
            min_confidence=0.5,
        )


if __name__ == "__main__":
    main()
