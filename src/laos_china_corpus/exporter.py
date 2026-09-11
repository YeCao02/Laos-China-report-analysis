from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

from .config import SNAPSHOT_END, SNAPSHOT_START, ProjectPaths

CSV_FIELDS = (
    "record_id", "source", "published_at", "title_original", "language",
    "china_note_zh", "topic_labels", "content_origin", "original_url",
    "archive_url", "body_file", "raw_file", "evidence_grade", "story_id",
    "selected_for_sample", "quota_reason",
)


def _source_group(source_code: str) -> str:
    source = source_code.upper()
    if source.startswith("KPL"):
        return "KPL"
    if source.startswith("PASAXON"):
        return "PASAXON"
    return source


def _article_dict(row: sqlite3.Row, selected: dict[str, str]) -> dict:
    result = dict(row)
    result["topic_labels"] = json.loads(result.pop("topic_labels_json") or "[]")
    result["matched_queries"] = json.loads(result.pop("matched_queries_json") or "[]")
    result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
    result["source"] = _source_group(result["source_code"])
    result["selected_for_sample"] = result["record_id"] in selected
    result["quota_reason"] = selected.get(result["record_id"])
    return result


def export_articles(conn: sqlite3.Connection, paths: ProjectPaths, *, per_record: bool = True) -> int:
    paths.ensure()
    selected = {
        row["record_id"]: row["quota_reason"]
        for row in conn.execute("SELECT record_id, quota_reason FROM sample_memberships")
    }
    rows = conn.execute("SELECT * FROM articles ORDER BY published_at, source_code, record_id").fetchall()
    jsonl_path = paths.catalog / "articles.jsonl"
    csv_path = paths.catalog / "articles.csv"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as jsonl, csv_path.open(
        "w", encoding="utf-8-sig", newline=""
    ) as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            item = _article_dict(row, selected)
            jsonl.write(json.dumps(item, ensure_ascii=False) + "\n")
            csv_item = {**item, "topic_labels": "|".join(item["topic_labels"])}
            writer.writerow(csv_item)
            if per_record:
                year_month = (item["published_at"] or "unknown")[:7]
                json_dir = paths.records / item["source"] / year_month
                text_dir = paths.text / item["source"] / year_month
                json_dir.mkdir(parents=True, exist_ok=True)
                text_dir.mkdir(parents=True, exist_ok=True)
                json_path = json_dir / f"{item['record_id']}.json"
                md_path = text_dir / f"{item['record_id']}.md"
                json_path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
                body = item["body_original"] or item["excerpt_original"] or ""
                md = (
                    f"# {item['title_original']}\n\n"
                    f"- record_id: `{item['record_id']}`\n"
                    f"- story_id: `{item['story_id'] or ''}`\n"
                    f"- 来源: {item['source']}\n"
                    f"- 语言: {item['language']}\n"
                    f"- 日期: {item['published_at'] or ''}（{item['date_precision']}）\n"
                    f"- 证据等级: {item['evidence_grade']}\n"
                    f"- 原文: {item['original_url'] or ''}\n"
                    f"- 存档: {item['archive_url'] or ''}\n\n"
                    f"## 涉华说明\n\n{item['china_note_zh'] or '待人工编写'}\n\n"
                    f"## 原文／OCR文本\n\n{body}\n"
                )
                md_path.write_text(md, encoding="utf-8")
                # Preserve a source-native TXT/OCR file when the importer supplied one.
                # The generated Markdown remains available under data/text, but replacing
                # body_file would break the body SHA-256 provenance chain.
                if item["body_original"] and not item["body_file"]:
                    conn.execute(
                        "UPDATE articles SET body_file=? WHERE record_id=?",
                        (str(md_path.relative_to(paths.root)), item["record_id"]),
                    )
    conn.commit()
    return len(rows)


def _months(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year += 1
            month = 1


def export_monthly_coverage(conn: sqlite3.Connection, paths: ProjectPaths) -> int:
    rows = conn.execute(
        "SELECT CASE WHEN upper(source_code) LIKE 'KPL%' THEN 'KPL' "
        "WHEN upper(source_code) LIKE 'PASAXON%' THEN 'PASAXON' ELSE upper(source_code) END source, "
        "substr(published_at,1,7) ym, story_id, evidence_grade, metadata_json, "
        "body_original,body_file "
        "FROM articles WHERE published_at IS NOT NULL AND story_id IS NOT NULL"
    ).fetchall()
    candidates: dict[tuple[str, str], set[str]] = {}
    eligible: dict[tuple[str, str], set[str]] = {}
    qualified_body: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (row["source"], row["ym"])
        candidates.setdefault(key, set()).add(row["story_id"])
        metadata = json.loads(row["metadata_json"] or "{}")
        if row["evidence_grade"] in {"A1", "A2", "B1", "B2"} or (
            row["evidence_grade"] == "C1" and metadata.get("second_corroboration")
        ):
            eligible.setdefault(key, set()).add(row["story_id"])
            if row["body_original"] or row["body_file"]:
                qualified_body.setdefault(key, set()).add(row["story_id"])
    samples = {
        (row["source_code"], row["year_month"]): row["n"]
        for row in conn.execute(
            "SELECT source_code,year_month,count(*) n FROM sample_memberships GROUP BY source_code,year_month"
        )
    }
    out = paths.catalog / "monthly_coverage.csv"
    fields = (
        "source", "year_month", "candidate_story_count", "eligible_story_count",
        "qualified_body_story_count",
        "sample_story_count", "coverage_status", "partial_period", "gap_reason",
    )
    count = 0
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for source in ("KPL", "PASAXON"):
            for ym in _months(SNAPSHOT_START, SNAPSHOT_END):
                n_candidates = len(candidates.get((source, ym), set()))
                n_eligible = len(eligible.get((source, ym), set()))
                n_qualified = len(qualified_body.get((source, ym), set()))
                n_sample = samples.get((source, ym), 0)
                if n_sample >= 4:
                    status = "target_met"
                elif n_sample >= 2:
                    status = "minimum_met"
                else:
                    status = "documented_gap"
                if n_candidates == 0:
                    reason = "no_discovered_records; historical_backfill_required"
                elif n_eligible == 0:
                    reason = "records_found_but_no_quota_eligible_evidence"
                elif n_qualified == 0:
                    reason = "eligible_records_found_but_no_verified_body"
                elif n_sample < 2:
                    reason = "fewer_than_minimum_qualified_body_stories"
                elif n_sample < 4:
                    reason = "fewer_than_target_eligible_unique_stories"
                else:
                    reason = ""
                writer.writerow({
                    "source": source,
                    "year_month": ym,
                    "candidate_story_count": n_candidates,
                    "eligible_story_count": n_eligible,
                    "qualified_body_story_count": n_qualified,
                    "sample_story_count": n_sample,
                    "coverage_status": status,
                    "partial_period": int(ym == SNAPSHOT_END.strftime("%Y-%m")),
                    "gap_reason": reason,
                })
                count += 1
    return count
