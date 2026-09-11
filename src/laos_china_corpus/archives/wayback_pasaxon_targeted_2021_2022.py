"""Targeted Wayback recovery for bodyless Pasaxon 2021--2022 A2 leads."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

from .commoncrawl_pasaxon_round5 import atomic_bytes, read_ndjson, utc_now, write_ndjson
from .commoncrawl_pasaxon_round6 import strict_china_related
from .pasaxon_php_clean import parse_clean_php_article
from .pasaxon_round2 import ArchiveBlocked, PoliteFetcher
from .wayback import WaybackCapture, build_exact_cdx_query, parse_cdx


STAGING_NAME = "wayback_pasaxon_targeted_2021_2022"


def load_targets(root: Path) -> list[dict[str, object]]:
    database = root / "data/corpus.sqlite3"
    conn = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(
            "SELECT record_id,source_article_id AS p_id,published_at,title_original,original_url "
            "FROM articles WHERE source_code='pasaxon_archive' AND evidence_grade='A2' "
            "AND body_original IS NULL AND substr(published_at,1,4) IN ('2021','2022') "
            "ORDER BY published_at,record_id"
        )]
    finally:
        conn.close()


def _capture_rank(capture: WaybackCapture, published: str) -> tuple[int, int, str]:
    capture_day = datetime.strptime(capture.timestamp[:8], "%Y%m%d").date()
    published_day = date.fromisoformat(published)
    delta = (capture_day - published_day).days
    return ((0 if delta >= 0 else 1), abs(delta), capture.timestamp)


def choose_capture(captures: list[WaybackCapture], published: str, rank: int = 1) -> WaybackCapture | None:
    ordered = sorted(captures, key=lambda item: _capture_rank(item, published))
    return ordered[rank - 1] if len(ordered) >= rank else None


def _challenge(payload: bytes) -> bool:
    lower = payload[:20000].decode("utf-8", errors="ignore").casefold()
    return "captcha" in lower or "verify you are human" in lower or "access denied" in lower


def discover(
    *, root: Path, max_index_requests: int = 10, interval: float = 1.0,
    staging_name: str = STAGING_NAME,
) -> dict[str, object]:
    """Query exact CDX records with a resumable, three-error circuit breaker."""

    staging = root / "data/staging/archive_ocr" / staging_name
    targets = load_targets(root)
    ledger_path = staging / "queries.ndjson"
    candidates_path = staging / "candidates.ndjson"
    ledger = read_ndjson(ledger_path)
    done = {str(row.get("record_id")) for row in ledger if row.get("status") in {"complete", "no_capture"}}
    candidates = read_ndjson(candidates_path)
    by_record = {str(row["record_id"]): row for row in candidates}
    fetcher = PoliteFetcher(interval=interval, timeout=60)
    requests = 0
    consecutive_errors = 0
    stop_reason = "queue_exhausted"
    pending: list[dict[str, object]] = []

    for position, target in enumerate(targets):
        record_id = str(target["record_id"])
        if record_id in done:
            continue
        if requests >= max_index_requests:
            pending.extend({**row, "queue_status": "index_budget_exhausted"} for row in targets[position:])
            stop_reason = "index_budget_exhausted"
            break
        query = build_exact_cdx_query(str(target["original_url"]), int(str(target["published_at"])[:4]))
        requests += 1
        try:
            payload = fetcher.fetch(query)
            if _challenge(payload):
                raise ArchiveBlocked("Wayback CDX challenge page")
            payload_sha = hashlib.sha256(payload).hexdigest()
            response_path = staging / "cdx" / f"{target['p_id']}-{payload_sha}.json"
            atomic_bytes(response_path, payload)
            captures = parse_cdx(payload)
            chosen = choose_capture(captures, str(target["published_at"]))
            status = "complete" if chosen else "no_capture"
            ledger.append({
                **target, "status": status, "query_url": query,
                "response_file": str(response_path.relative_to(root)),
                "response_sha256": payload_sha, "capture_count": len(captures),
                "checked_at": utc_now(),
            })
            if chosen:
                by_record[record_id] = {
                    **target, "capture_timestamp": chosen.timestamp,
                    "capture_original_url": chosen.original_url,
                    "capture_digest": chosen.digest, "capture_mimetype": chosen.mimetype,
                    "archive_url": f"https://web.archive.org/web/{chosen.timestamp}id_/{chosen.original_url}",
                    "cdx_file": str(response_path.relative_to(root)), "cdx_sha256": payload_sha,
                }
            consecutive_errors = 0
            done.add(record_id)
        except (ArchiveBlocked, HTTPError, URLError, TimeoutError, ValueError) as exc:
            ledger.append({
                **target, "status": "failed", "query_url": query,
                "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now(),
            })
            consecutive_errors += 1
            if isinstance(exc, ArchiveBlocked) or consecutive_errors >= 3:
                pending.extend({**row, "queue_status": "archive_error_circuit_breaker"} for row in targets[position + 1:])
                stop_reason = "archive_error_circuit_breaker"
                break
        write_ndjson(ledger_path, ledger)
        write_ndjson(candidates_path, sorted(by_record.values(), key=lambda row: str(row["published_at"])))

    write_ndjson(ledger_path, ledger)
    write_ndjson(candidates_path, sorted(by_record.values(), key=lambda row: str(row["published_at"])))
    write_ndjson(staging / "continuation_queue.ndjson", pending)
    manifest = {
        "generated_at": utc_now(), "mode": "discover", "network_scope": ["web.archive.org"],
        "target_rows": len(targets), "index_requests": requests,
        "index_request_limit": max_index_requests,
        "status_counts": dict(sorted(Counter(str(row.get("status")) for row in ledger).items())),
        "candidate_rows": len(by_record), "continuation_rows": len(pending),
        "stopped_reason": stop_reason, "capture_date_used_as_publication_date": False,
        "canonical_database_written": False,
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def replay(
    *, root: Path, max_replay_requests: int = 10, interval: float = 1.0,
    staging_name: str = STAGING_NAME,
) -> dict[str, object]:
    """Fetch chosen snapshots and create verified upgrade records."""

    staging = root / "data/staging/archive_ocr" / staging_name
    candidates = read_ndjson(staging / "candidates.ndjson")
    ledger_path = staging / "screened.ndjson"
    records_path = staging / "records.ndjson"
    failures_path = staging / "replay_failures.ndjson"
    ledger = read_ndjson(ledger_path)
    records = read_ndjson(records_path)
    failures = read_ndjson(failures_path)
    terminal = {str(row.get("record_id")) for row in ledger if row.get("status") in {"china_match", "not_china", "invalid_page"}}
    records_by_id = {str(row["record_id"]): row for row in records}
    fetcher = PoliteFetcher(interval=interval, timeout=60)
    requests = 0
    consecutive_errors = 0
    pending: list[dict[str, object]] = []
    stop_reason = "queue_exhausted"

    for position, row in enumerate(candidates):
        record_id = str(row["record_id"])
        if record_id in terminal:
            continue
        if requests >= max_replay_requests:
            pending.extend({**item, "queue_status": "replay_budget_exhausted"} for item in candidates[position:])
            stop_reason = "replay_budget_exhausted"
            break
        requests += 1
        try:
            payload = fetcher.fetch(str(row["archive_url"]))
            if _challenge(payload):
                raise ArchiveBlocked("Wayback replay challenge page")
            raw_sha = hashlib.sha256(payload).hexdigest()
            raw_path = staging / "raw" / str(row["published_at"])[:7] / f"{raw_sha}.html.gz"
            atomic_bytes(raw_path, gzip.compress(payload))
            article = parse_clean_php_article(
                payload, expected_title=str(row["title_original"]),
                expected_date=str(row["published_at"]),
            )
            related, hits = strict_china_related(f"{article.title}\n{article.body}")
            if not related:
                status = "not_china"
            else:
                status = "china_match"
                content_sha = hashlib.sha256(article.body.encode("utf-8")).hexdigest()
                body_path = staging / "text" / article.published_date[:7] / f"{content_sha}.txt"
                atomic_bytes(body_path, article.body.encode("utf-8"))
                records_by_id[record_id] = {
                    "record_id": record_id, "source_article_id": row["p_id"],
                    "source_code": "pasaxon_archive", "language": "lo",
                    "title_original": article.title, "published_at": article.published_date,
                    "date_precision": "page_day_verified_against_listing",
                    "body_original": article.body,
                    "body_method": "wayback_pasaxon_php_html_hidden_injection_removed",
                    "matched_queries": hits, "original_url": row["original_url"],
                    "archive_url": row["archive_url"], "evidence_grade": "B1",
                    "retrieval_tier": "T1_DIRECT_CHINA", "content_sha256": content_sha,
                    "body_file": str(body_path.relative_to(root)),
                    "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                    "retrieved_at": utc_now(), "archive_capture_timestamp": row["capture_timestamp"],
                    "archive_digest": row.get("capture_digest"),
                    "metadata": {
                        "archive_provider": "Wayback", "parser": "pasaxon_php_clean_v1",
                        "cleaning": {"removed_injection_nodes": article.removed_injection_nodes,
                                     "listing_title_verified": True, "listing_date_verified": True},
                        "cdx_file": row["cdx_file"], "cdx_sha256": row["cdx_sha256"],
                    },
                }
            ledger.append({
                **row, "status": status, "raw_file": str(raw_path.relative_to(root)),
                "raw_sha256": raw_sha, "checked_at": utc_now(),
            })
            terminal.add(record_id)
            consecutive_errors = 0
        except (ArchiveBlocked, HTTPError, URLError, TimeoutError, ValueError) as exc:
            failure = {**row, "status": "failed", "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now()}
            failures.append(failure)
            ledger.append(failure)
            consecutive_errors += 1
            if isinstance(exc, ArchiveBlocked) or consecutive_errors >= 3:
                pending.extend({**item, "queue_status": "archive_error_circuit_breaker"} for item in candidates[position + 1:])
                stop_reason = "archive_error_circuit_breaker"
                break
        write_ndjson(ledger_path, ledger)
        write_ndjson(records_path, sorted(records_by_id.values(), key=lambda item: str(item["published_at"])))
        write_ndjson(failures_path, failures)

    write_ndjson(staging / "replay_continuation_queue.ndjson", pending)
    records = sorted(records_by_id.values(), key=lambda item: str(item["published_at"]))
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["replay"] = {
        "generated_at": utc_now(), "network_scope": ["web.archive.org"],
        "replay_requests": requests, "replay_request_limit": max_replay_requests,
        "screened_rows": len(ledger), "importable_records": len(records),
        "records_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in records).items())),
        "failure_rows": len(failures), "continuation_rows": len(pending),
        "stopped_reason": stop_reason, "canonical_database_written": False,
    }
    atomic_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest["replay"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("discover", "replay"), required=True)
    parser.add_argument("--staging-name", default=STAGING_NAME)
    parser.add_argument("--max-requests", type=int, default=10)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    function = discover if args.mode == "discover" else replay
    keyword = "max_index_requests" if args.mode == "discover" else "max_replay_requests"
    result = function(root=args.root.resolve(), staging_name=args.staging_name,
                      interval=args.interval, **{keyword: args.max_requests})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
