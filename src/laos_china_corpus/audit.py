from __future__ import annotations

import csv
import sqlite3
from datetime import date

from .config import SNAPSHOT_END, SNAPSHOT_START, ProjectPaths


def _months(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year += 1
            month = 1


def export_backfill_queue(conn: sqlite3.Connection, paths: ProjectPaths) -> int:
    discovered = {
        (row["source"], row["ym"]): row["n"]
        for row in conn.execute(
            "SELECT CASE WHEN upper(source_code) LIKE 'KPL%' THEN 'KPL' "
            "WHEN upper(source_code) LIKE 'PASAXON%' THEN 'PASAXON' ELSE upper(source_code) END source,"
            "substr(published_at,1,7) ym,count(DISTINCT story_id) n FROM articles "
            "WHERE published_at IS NOT NULL GROUP BY source,ym"
        )
    }
    attempts = {
        row["source_code"].upper(): row["n"]
        for row in conn.execute("SELECT source_code,count(*) n FROM crawl_partitions GROUP BY source_code")
    }
    path = paths.audit / "historical_backfill_queue.csv"
    fields = (
        "source", "year_month", "candidate_story_count", "priority", "attempted_entry_points",
        "next_entry_points", "status", "note",
    )
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for source in ("KPL", "PASAXON"):
            for ym in _months(SNAPSHOT_START, SNAPSHOT_END):
                candidates = discovered.get((source, ym), 0)
                historical = ym < "2021-01"
                writer.writerow({
                    "source": source,
                    "year_month": ym,
                    "candidate_story_count": candidates,
                    "priority": "P0" if historical and candidates < 2 else ("P1" if candidates < 4 else "P2"),
                    "attempted_entry_points": f"current_site_partitions={attempts.get(source, 0)}; seed_import={int(source == 'KPL')}",
                    "next_entry_points": (
                        "old_domain|Common_Crawl|LOC_catalog|epaper_PDF|official_reprint"
                        if historical else "current_search|tag|epaper|30_day_lookback"
                    ),
                    "status": "backfill_required" if candidates < 2 else ("enrichment_required" if candidates < 4 else "candidate_target_met"),
                    "note": "站内零命中不等于当月无报道；须保留入口级审计证据。",
                })
                count += 1
    failed = paths.audit / "failed_tasks.ndjson"
    failed.touch(exist_ok=True)
    return count
