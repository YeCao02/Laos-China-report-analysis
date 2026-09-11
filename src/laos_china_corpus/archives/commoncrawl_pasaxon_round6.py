"""Resume Round5 Pasaxon candidates using only Common Crawl Range requests."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.error import HTTPError

from .commoncrawl import extract_archive_http_payload
from .commoncrawl_pasaxon import (
    _canonical_pasaxon_url,
    _china_related,
    _entity_lao_html,
    _is_contaminated_template,
)
from .commoncrawl_pasaxon_round5 import (
    PoliteHTTP,
    atomic_bytes,
    category_key,
    load_coverage,
    read_ndjson,
    utc_now,
    write_ndjson,
)
from .wayback import parse_direct_pasaxon_article, pasaxon_url_identity


STAGING_NAME = "commoncrawl_pasaxon_round6"
ROUND5_NAME = "commoncrawl_pasaxon_round5"


def strict_china_related(text: str) -> tuple[bool, list[str]]:
    """Screen China mentions after masking common Indochina false positives."""

    cleaned = text
    for pattern in (
        "indochina", "indo-china", "indo china",
        "ອິນໂດຈີນ", "ອິນໂດ-ຈີນ", "ອິນໂດ ຈີນ",
        "ອິນດູຈີນ", "ອິນດູ-ຈີນ", "ອິນດູ ຈີນ",
    ):
        cleaned = cleaned.replace(pattern, " ").replace(pattern.title(), " ")
    return _china_related(cleaned)


def valid_queue_rows(rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    """Retain only candidates whose URL independently supplies a full date."""

    valid: list[dict[str, object]] = []
    rejected = 0
    seen: set[str] = set()
    for row in rows:
        identity = pasaxon_url_identity(str(row.get("original_url") or ""))
        canonical = _canonical_pasaxon_url(str(row.get("original_url") or ""))
        if not identity or not identity[0] or identity[0] != row.get("published_at") or canonical in seen:
            rejected += 1
            continue
        seen.add(canonical)
        valid.append({**row, "canonical_url": canonical, "year_month": identity[0][:7]})
    return valid, rejected


def prioritize_queue(
    rows: list[dict[str, object]], *, current_counts: dict[str, int],
) -> list[dict[str, object]]:
    """Round-robin only months below two, least-covered months first."""

    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        month = str(row["year_month"])
        if current_counts.get(month, 0) < 2:
            groups[month].append(row)
    for group in groups.values():
        group.sort(key=lambda row: (
            str(row["published_at"]), int(row.get("slot") or 0), str(row["canonical_url"])
        ))
    months = sorted(groups, key=lambda month: (current_counts.get(month, 0), month))
    output: list[dict[str, object]] = []
    depth = 0
    while any(depth < len(groups[month]) for month in months):
        for month in months:
            if depth < len(groups[month]):
                output.append(groups[month][depth])
        depth += 1
    return output


def run(*, root: Path, max_range_requests: int = 90, interval: float = 1.0) -> dict[str, object]:
    source = root / "data/staging/archive_ocr" / ROUND5_NAME
    staging = root / "data/staging/archive_ocr" / STAGING_NAME
    raw_dir, text_dir = staging / "raw", staging / "text"
    ledger_path, records_path = staging / "screened.ndjson", staging / "records.ndjson"
    failures_path, pending_path = staging / "failures.ndjson", staging / "pending.ndjson"
    source_rows = read_ndjson(source / "continuation_queue.ndjson")
    queue_rows, rejected_dates = valid_queue_rows(source_rows)
    # The canonical coverage export is regenerated after each imported round,
    # so it already includes any Round5 records accepted by the parent run.
    # Adding the staging records again would double count them.
    current_counts, _ = load_coverage(root)
    queue = prioritize_queue(queue_rows, current_counts=current_counts)

    http = PoliteHTTP(interval=interval)
    ledger = read_ndjson(ledger_path)
    records = read_ndjson(records_path)
    failures = read_ndjson(failures_path)
    terminal = {str(row.get("canonical_url")) for row in ledger if row.get("canonical_url")}
    matched = Counter(str(row["published_at"])[:7] for row in records)
    blocked_categories: set[str] = set()
    pending: list[dict[str, object]] = []
    stopped_reason = "queue_exhausted"

    for position, row in enumerate(queue):
        month = str(row["year_month"])
        canonical = str(row["canonical_url"])
        category = category_key(str(row["original_url"]))
        if canonical in terminal:
            continue
        if current_counts.get(month, 0) + matched[month] >= 2:
            pending.append({**row, "queue_status": "month_quota_met"})
            continue
        if category in blocked_categories:
            pending.append({**row, "queue_status": "blocked_contaminated_template_path"})
            continue
        if http.requests["range"] >= max_range_requests:
            pending.extend({**item, "queue_status": "range_budget_exhausted"} for item in queue[position:])
            stopped_reason = "range_budget_exhausted"
            break

        raw_member: bytes | None = None
        try:
            raw_member = http.get(str(row["warc_url"]), kind="range", range_header=str(row["range_header"]))
            if len(raw_member) != int(row["length"]):
                raise ValueError(f"range length mismatch: expected {row['length']}, got {len(raw_member)}")
            payload = extract_archive_http_payload(raw_member)
            raw_sha = hashlib.sha256(raw_member).hexdigest()
            payload_sha = hashlib.sha256(payload).hexdigest()
            member_kind = "warc" if ".warc" in str(row["filename"]).casefold() else "arc"
            raw_path = raw_dir / month / f"{raw_sha}.{member_kind}.gz"
            atomic_bytes(raw_path, raw_member)
            article = parse_direct_pasaxon_article(_entity_lao_html(payload), str(row["original_url"]))
            if _is_contaminated_template(article.title, article.body):
                blocked_categories.add(category)
                failure = {
                    **row, "stage": "range", "status": "contaminated_template",
                    "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha, "error": "compromised generic SEO template",
                    "checked_at": utc_now(),
                }
                failures.append(failure); ledger.append(failure)
            else:
                related, hits = strict_china_related(f"{article.title}\n{article.body}")
                body_bytes = article.body.encode("utf-8")
                body_sha = hashlib.sha256(body_bytes).hexdigest()
                text_path = text_dir / month / f"{body_sha}.txt"
                atomic_bytes(text_path, body_bytes)
                status = "china_match" if related else "not_china"
                ledger_row = {
                    **row, "status": status, "hits": hits,
                    "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha, "body_file": str(text_path.relative_to(root)),
                    "content_sha256": body_sha, "checked_at": utc_now(),
                }
                ledger.append(ledger_row)
                if related:
                    records.append({
                        "source_code": "pasaxon_archive", "language": "lo",
                        "title_original": article.title, "published_at": article.published_date,
                        "date_precision": "url_day", "body_original": article.body,
                        "body_method": "commoncrawl_warc_pasaxon_html", "matched_queries": hits,
                        "original_url": row["original_url"], "archive_url": row["warc_url"],
                        "evidence_grade": "B1", "retrieval_tier": "T1_DIRECT_CHINA",
                        "content_sha256": body_sha, "body_file": str(text_path.relative_to(root)),
                        "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                        "payload_sha256": payload_sha, "retrieved_at": utc_now(),
                        "capture_timestamp": row["timestamp"], "source_index": row["source_index"],
                        "warc": {key: row[key] for key in ("warc_url", "filename", "offset", "length", "range_header")},
                        "parser": "commoncrawl_pasaxon_round6_v1",
                    })
                    matched[month] += 1
            write_ndjson(ledger_path, ledger); write_ndjson(records_path, records)
            write_ndjson(failures_path, failures)
        except Exception as exc:
            failure = {
                **row, "stage": "range", "status": "failed",
                "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now(),
            }
            if raw_member is not None:
                failure["raw_sha256"] = hashlib.sha256(raw_member).hexdigest()
            failures.append(failure); ledger.append(failure)
            write_ndjson(failures_path, failures); write_ndjson(ledger_path, ledger)
            if isinstance(exc, HTTPError) and exc.code in {401, 403, 429}:
                pending.extend({**item, "queue_status": f"stopped_http_{exc.code}"} for item in queue[position + 1:])
                stopped_reason = f"http_{exc.code}"
                break

    write_ndjson(pending_path, pending)
    write_ndjson(failures_path, failures)
    if stopped_reason == "queue_exhausted" and pending and all(
        row.get("queue_status") == "month_quota_met" for row in pending
    ):
        stopped_reason = "all_target_months_quota_met"
    audit = Counter()
    for row in ledger:
        if not row.get("raw_file"):
            continue
        raw_path = root / str(row["raw_file"])
        if not raw_path.is_file():
            continue
        raw = raw_path.read_bytes(); audit["raw_exists"] += 1
        audit["raw_sha_matches"] += hashlib.sha256(raw).hexdigest() == row.get("raw_sha256")
        try:
            payload = extract_archive_http_payload(raw); audit["payload_extracts"] += 1
            audit["payload_sha_matches"] += hashlib.sha256(payload).hexdigest() == row.get("payload_sha256")
        except ValueError:
            pass
        if row.get("body_file"):
            body_path = root / str(row["body_file"])
            audit["body_exists"] += body_path.is_file()
            if body_path.is_file():
                audit["body_sha_matches"] += hashlib.sha256(body_path.read_bytes()).hexdigest() == row.get("content_sha256")
    manifest = {
        "generated_at": utc_now(), "network_scope": ["data.commoncrawl.org"],
        "source_queue": str((source / "continuation_queue.ndjson").relative_to(root)),
        "canonical_database_written": False, "single_threaded": True,
        "interval_seconds": interval, "range_requests": http.requests["range"],
        "range_request_limit": max_range_requests, "source_rows": len(source_rows),
        "valid_dated_rows": len(queue_rows), "rejected_unverifiable_dates": rejected_dates,
        "prioritized_rows": len(queue), "screened_rows": len(ledger),
        "importable_records": len(records),
        "importable_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in records).items())),
        "failure_rows": len(failures), "pending_rows": len(pending),
        "blocked_template_categories": sorted(blocked_categories),
        "stopped_reason": stopped_reason, "hash_audit": dict(audit),
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-range-requests", type=int, default=90)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    print(json.dumps(run(root=args.root.resolve(), max_range_requests=args.max_range_requests, interval=args.interval), ensure_ascii=False))


if __name__ == "__main__":
    main()
