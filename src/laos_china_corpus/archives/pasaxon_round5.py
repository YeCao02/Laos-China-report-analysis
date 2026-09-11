from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse

from .pasaxon_round2 import ArchiveBlocked, PoliteFetcher, _china_hits
from .pasaxon_round3 import AtomicJsonLedger, _atomic_write, _is_challenge
from .pasaxon_round4 import (
    FAILURE_STATUSES,
    _coverage,
    clean_modern_article_segment,
    url_key,
)
from .wayback import parse_direct_pasaxon_article


STAGING_NAME = "pasaxon_round5_wayback"
ROUND4 = "pasaxon_round4_wayback"
TERMINAL = {
    "china_match",
    "china_match_non_gap",
    "not_china",
    "false_positive",
    "duplicate_match",
    "existing_record",
    "parse_failure",
    "http_error",
    "date_out_of_scope",
    "skipped_quota_met",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_queue(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        required = {
            "url_key", "target_month", "original_url", "archive_url",
            "archive_capture_timestamp", "source_path",
        }
        if not required.issubset(row):
            raise ValueError(f"{path}:{line_number} lacks required queue fields")
        if urlparse(str(row["archive_url"])).hostname != "web.archive.org":
            raise ValueError(f"{path}:{line_number} archive URL is outside web.archive.org")
        rows.append(row)
    return rows


def _database_state(root: Path) -> tuple[dict[str, int], set[str], set[str]]:
    database = root / "data/corpus.sqlite3"
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        counts = {
            str(month): int(count)
            for month, count in conn.execute(
                "SELECT substr(published_at,1,7),count(*) FROM articles "
                "WHERE source_code='pasaxon_archive' AND published_at BETWEEN "
                "'2014-01-01' AND '2020-12-31' GROUP BY 1"
            )
        }
        urls = {
            url_key(str(row[0]))
            for row in conn.execute(
                "SELECT original_url FROM articles WHERE source_code='pasaxon_archive' "
                "AND original_url IS NOT NULL"
            )
        }
        hashes = {
            str(row[0])
            for row in conn.execute(
                "SELECT content_sha256 FROM articles WHERE source_code='pasaxon_archive' "
                "AND content_sha256 IS NOT NULL"
            )
        }
        return counts, urls, hashes
    finally:
        conn.close()


def prior_staged_state(
    root: Path, existing_urls: set[str] | None = None
) -> tuple[dict[str, int], set[str], set[str]]:
    counts: dict[str, int] = defaultdict(int)
    urls: set[str] = set()
    hashes: set[str] = set()
    round4 = root / "data/staging" / ROUND4
    for directory in (round4 / "records", round4 / "supplemental_records"):
        for path in directory.glob("*.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            key = url_key(str(row["original_url"]))
            urls.add(key)
            hashes.add(str(row["content_sha256"]))
            if directory.name == "records" and key not in (existing_urls or set()):
                counts[str(row["published_at"])[:7]] += 1
    return dict(counts), urls, hashes


def _write_evidence(
    *,
    root: Path,
    staging: Path,
    queue_row: dict[str, object],
    payload: bytes,
    title: str,
    body: str,
    published_at: str,
    hits: list[str],
    retrieved_at: str,
    supplemental: bool,
) -> dict[str, object]:
    title, body = clean_modern_article_segment(title, body)
    raw_sha = hashlib.sha256(payload).hexdigest()
    body_bytes = body.encode("utf-8")
    body_sha = hashlib.sha256(body_bytes).hexdigest()
    month = published_at[:7]
    raw_path = staging / "raw" / month / f"{raw_sha}.html.gz"
    body_path = staging / "body" / month / f"{body_sha}.txt"
    record_dir = "supplemental_records" if supplemental else "records"
    record_path = staging / record_dir / f"{hashlib.sha256(str(queue_row['url_key']).encode()).hexdigest()}.json"
    _atomic_write(raw_path, gzip.compress(payload, mtime=0))
    _atomic_write(body_path, body_bytes)
    exact_url_date = queue_row.get("exact_url_date")
    row: dict[str, object] = {
        "source_code": "pasaxon_archive",
        "language": "lo",
        "title_original": title,
        "published_at": published_at,
        "date_precision": "url_day" if exact_url_date else "page_day",
        "date_evidence": exact_url_date or "printed_page_date",
        "body_original": body,
        "body_method": "wayback_direct_pasaxon_html",
        "matched_queries": hits,
        "original_url": queue_row["original_url"],
        "archive_url": queue_row["archive_url"],
        "raw_file": raw_path.relative_to(root).as_posix(),
        "body_file": body_path.relative_to(root).as_posix(),
        "evidence_grade": "B1",
        "retrieval_tier": "T1_DIRECT_CHINA",
        "raw_sha256": raw_sha,
        "content_sha256": body_sha,
        "archive_capture_timestamp": queue_row["archive_capture_timestamp"],
        "retrieved_at": retrieved_at,
        "parser": "old_pasaxon_direct_wayback_v5_clean_segment",
        "round5_target_month": queue_row["target_month"],
        "round5_source_path": queue_row["source_path"],
        "supplemental_non_gap": supplemental,
    }
    _atomic_write(
        record_path,
        (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return row


def _rebuild(staging: Path, ledger: AtomicJsonLedger) -> tuple[int, int, int]:
    def rows(directory: str) -> list[dict[str, object]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((staging / directory).glob("*.json"))
        ]
    importable = rows("records")
    supplemental = rows("supplemental_records")
    _atomic_write(
        staging / "records.ndjson",
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in importable).encode("utf-8"),
    )
    _atomic_write(
        staging / "supplemental_records.ndjson",
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in supplemental).encode("utf-8"),
    )
    failures = [
        row for row in ledger.rows.values() if str(row.get("status")) in FAILURE_STATUSES
    ]
    _atomic_write(
        staging / "failures.ndjson",
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in failures).encode("utf-8"),
    )
    return len(importable), len(supplemental), len(failures)


def refresh_saved_records(root: Path, staging: Path, ledger: AtomicJsonLedger) -> dict[str, int]:
    """Re-segment saved PHP pages and quarantine matches caused only by navigation."""

    result = {"refreshed": 0, "quarantined": 0}
    for directory in (staging / "records", staging / "supplemental_records"):
        for path in sorted(directory.glob("*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            title, body = clean_modern_article_segment(
                str(row["title_original"]), str(row["body_original"])
            )
            hits = _china_hits(title, body)
            key = url_key(str(row["original_url"]))
            audit = ledger.get(key) or {}
            if not hits:
                row["rejection_reason"] = "no_china_in_clean_article_segment"
                rejected = staging / "rejected_records" / path.name
                _atomic_write(
                    rejected,
                    (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
                path.unlink()
                ledger.put({
                    **audit,
                    "url_key": key,
                    "status": "false_positive",
                    "matched_terms": [],
                    "error": "Indochina/navigation substring after clean article segmentation",
                })
                result["quarantined"] += 1
                continue
            body_bytes = body.encode("utf-8")
            body_sha = hashlib.sha256(body_bytes).hexdigest()
            body_path = staging / "body" / str(row["published_at"])[:7] / f"{body_sha}.txt"
            _atomic_write(body_path, body_bytes)
            if title != row["title_original"] or body_sha != row["content_sha256"]:
                result["refreshed"] += 1
            row.update({
                "title_original": title,
                "body_original": body,
                "body_file": body_path.relative_to(root).as_posix(),
                "content_sha256": body_sha,
                "matched_queries": hits,
                "parser": "old_pasaxon_direct_wayback_v5_clean_segment",
            })
            _atomic_write(
                path,
                (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            ledger.put({
                **audit,
                "url_key": key,
                "matched_terms": hits,
                "body_sha256": body_sha,
                "body_file": row["body_file"],
            })
    return result


def collect(
    *,
    root: Path,
    max_replays: int = 93,
    target_per_month: int = 2,
    interval: float = 1.0,
    fetcher: PoliteFetcher | None = None,
    queue_file: Path | None = None,
) -> dict[str, object]:
    staging = root / "data/staging" / STAGING_NAME
    staging.mkdir(parents=True, exist_ok=True)
    ledger = AtomicJsonLedger(staging / "screened.ndjson", key="url_key")
    refresh_result = refresh_saved_records(root, staging, ledger)
    queue_file = queue_file or root / "data/staging" / ROUND4 / "pending_queue.ndjson"
    queue = read_queue(queue_file)
    coverage_csv = _coverage(root)
    db_counts, db_urls, db_hashes = _database_state(root)
    prior_counts, prior_urls, prior_hashes = prior_staged_state(root, db_urls)
    achieved: dict[str, int] = {
        month: max(coverage_csv.get(month, 0), db_counts.get(month, 0)) + prior_counts.get(month, 0)
        for month in set(coverage_csv) | set(db_counts) | set(prior_counts)
    }
    known_urls = db_urls | prior_urls
    known_hashes = db_hashes | prior_hashes
    for directory in (staging / "records", staging / "supplemental_records"):
        for path in directory.glob("*.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            known_urls.add(url_key(str(row["original_url"])))
            known_hashes.add(str(row["content_sha256"]))
            if directory.name == "records":
                month = str(row["published_at"])[:7]
                achieved[month] = achieved.get(month, 0) + 1

    network = fetcher or PoliteFetcher(interval=interval)
    replay_total = sum(
        1 for row in ledger.rows.values()
        if row.get("status") not in {"existing_record", "skipped_quota_met"}
    )
    replay_start = replay_total
    statuses: dict[str, int] = defaultdict(int)
    new_matches: dict[str, int] = defaultdict(int)
    stopped_reason: str | None = None
    for item in queue:
        if replay_total >= max_replays:
            stopped_reason = f"article_replay_cap_reached:{max_replays}"
            break
        key = str(item["url_key"])
        prior = ledger.get(key)
        if prior and str(prior.get("status")) in TERMINAL:
            continue
        target_month = str(item["target_month"])
        base: dict[str, object] = {
            "url_key": key,
            "target_month": target_month,
            "source_path": item["source_path"],
            "original_url": item["original_url"],
            "archive_url": item["archive_url"],
            "archive_capture_timestamp": item["archive_capture_timestamp"],
            "screened_at": utc_now(),
            "status": None,
            "matched_terms": [],
            "error": None,
        }
        if achieved.get(target_month, 0) >= target_per_month:
            statuses["skipped_quota_met"] += 1
            continue
        if key in known_urls:
            ledger.put({**base, "status": "existing_record"})
            statuses["existing_record"] += 1
            continue
        replay_total += 1
        try:
            payload = network.fetch(str(item["archive_url"]))
            if _is_challenge(payload):
                stopped_reason = f"challenge page detected:{item['archive_url']}"
                ledger.put({**base, "status": "challenge", "error": stopped_reason})
                statuses["challenge"] += 1
                break
            article = parse_direct_pasaxon_article(payload, str(item["original_url"]))
            month = article.published_date[:7]
            if not ("2014-01" <= month <= "2020-12"):
                ledger.put({**base, "status": "date_out_of_scope", "published_at": article.published_date})
                statuses["date_out_of_scope"] += 1
                continue
            hits = _china_hits(article.title, article.body)
            if not hits:
                ledger.put({**base, "status": "not_china", "published_at": article.published_date})
                statuses["not_china"] += 1
                continue
            title, body = clean_modern_article_segment(article.title, article.body)
            body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if body_sha in known_hashes:
                ledger.put(
                    {
                        **base,
                        "status": "duplicate_match",
                        "published_at": article.published_date,
                        "matched_terms": hits,
                        "body_sha256": body_sha,
                    }
                )
                statuses["duplicate_match"] += 1
                continue
            supplemental = achieved.get(month, 0) >= target_per_month
            row = _write_evidence(
                root=root,
                staging=staging,
                queue_row=item,
                payload=payload,
                title=title,
                body=body,
                published_at=article.published_date,
                hits=hits,
                retrieved_at=str(base["screened_at"]),
                supplemental=supplemental,
            )
            status = "china_match_non_gap" if supplemental else "china_match"
            ledger.put(
                {
                    **base,
                    "status": status,
                    "published_at": article.published_date,
                    "matched_terms": hits,
                    "raw_sha256": row["raw_sha256"],
                    "body_sha256": row["content_sha256"],
                    "raw_file": row["raw_file"],
                    "body_file": row["body_file"],
                }
            )
            known_urls.add(key)
            known_hashes.add(str(row["content_sha256"]))
            if not supplemental:
                achieved[month] = achieved.get(month, 0) + 1
                new_matches[month] += 1
            statuses[status] += 1
        except ArchiveBlocked as exc:
            stopped_reason = str(exc)
            ledger.put({**base, "status": "blocked", "error": stopped_reason})
            statuses["blocked"] += 1
            break
        except HTTPError as exc:
            ledger.put(
                {
                    **base,
                    "status": "http_error",
                    "http_status": exc.code,
                    "error": f"HTTPError:{exc}",
                }
            )
            statuses["http_error"] += 1
        except Exception as exc:
            ledger.put(
                {
                    **base,
                    "status": "parse_failure",
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            statuses["parse_failure"] += 1

    importable, supplemental, failures = _rebuild(staging, ledger)
    total_status: dict[str, int] = defaultdict(int)
    for row in ledger.rows.values():
        total_status[str(row.get("status"))] += 1
    pending = [
        item for item in queue
        if not (
            (ledger.get(str(item["url_key"])) and
             str(ledger.get(str(item["url_key"])).get("status")) in TERMINAL)
            or achieved.get(str(item["target_month"]), 0) >= target_per_month
        )
    ]
    _atomic_write(
        staging / "pending_queue.ndjson",
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in pending).encode("utf-8"),
    )
    summary: dict[str, object] = {
        "generated_at": utc_now(),
        "run_status": "stopped" if stopped_reason else "queue_exhausted",
        "database_mode": "read_only",
        "canonical_database_modified": False,
        "network_scope": "web.archive.org replay URLs only",
        "network_concurrency": 1,
        "minimum_request_interval_seconds": max(1.0, interval),
        "source_queue": queue_file.relative_to(root).as_posix(),
        "source_queue_records": len(queue),
        "article_replay_cap": max_replays,
        "article_replays_before_run": replay_start,
        "article_replays_this_run": replay_total - replay_start,
        "article_replays_total": replay_total,
        "status_counts_this_run": dict(statuses),
        "status_counts_total": dict(sorted(total_status.items())),
        "new_matches_by_month": dict(sorted(new_matches.items())),
        "achieved_counts": dict(sorted(achieved.items())),
        "importable_records": importable,
        "supplemental_records": supplemental,
        "failures": failures,
        "pending_queue_records": len(pending),
        "offline_refresh": refresh_result,
        "stopped_reason": stopped_reason,
    }
    _atomic_write(
        staging / "manifest.json",
        (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return summary


def verify(root: Path) -> dict[str, object]:
    staging = root / "data/staging" / STAGING_NAME
    ledger = AtomicJsonLedger(staging / "screened.ndjson", key="url_key")
    failures: list[str] = []
    counts: dict[str, int] = {}
    for directory in ("records", "supplemental_records"):
        rows = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((staging / directory).glob("*.json"))
        ]
        counts[directory] = len(rows)
        expected_status = "china_match" if directory == "records" else "china_match_non_gap"
        for row in rows:
            key = url_key(str(row["original_url"]))
            audit = ledger.get(key)
            if not audit or audit.get("status") != expected_status:
                failures.append(f"ledger:{key}")
            try:
                raw = gzip.decompress((root / str(row["raw_file"])).read_bytes())
                if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
                    failures.append(f"raw_hash:{key}")
            except Exception as exc:
                failures.append(f"raw:{key}:{type(exc).__name__}")
            try:
                body = (root / str(row["body_file"])).read_bytes()
                if hashlib.sha256(body).hexdigest() != row["content_sha256"]:
                    failures.append(f"body_hash:{key}")
                if body.decode("utf-8") != row["body_original"]:
                    failures.append(f"body_text:{key}")
            except Exception as exc:
                failures.append(f"body:{key}:{type(exc).__name__}")
    result = {
        "generated_at": utc_now(),
        "ledger_records": len(ledger.rows),
        **counts,
        "verification_failures": failures,
        "all_verified": not failures,
    }
    _atomic_write(
        staging / "verification.json",
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Pasaxon Wayback round 5 queue continuation")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-replays", type=int, default=93)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = verify(args.root) if args.verify_only else collect(
        root=args.root, max_replays=args.max_replays, interval=args.interval
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
