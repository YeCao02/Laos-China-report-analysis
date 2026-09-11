from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse

from .pasaxon_round2 import ArchiveBlocked, PoliteFetcher, _china_hits
from .pasaxon_round3 import AtomicJsonLedger, _atomic_write, _is_challenge
from .pasaxon_round4 import FAILURE_STATUSES, _coverage, clean_modern_article_segment, url_key
from .pasaxon_round5 import _database_state, read_queue, utc_now
from .wayback import parse_direct_pasaxon_article


STAGING_NAME = "pasaxon_round7_wayback"
SOURCE_QUEUE = "pasaxon_round6_wayback"
PRIOR_STAGING = (
    "pasaxon_round4_wayback", "pasaxon_round5_wayback", "pasaxon_round6_wayback",
)
TERMINAL = {
    "china_match", "china_match_non_gap", "not_china", "duplicate_match",
    "existing_record", "parse_failure", "date_out_of_scope",
    "date_conflict", "false_positive",
}
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
HTTP_CIRCUIT_BREAKER_THRESHOLD = 3


def eligible_queue(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        parsed = urlparse(str(row["original_url"]))
        if parsed.path.lower().endswith("pasaxon-detail.php") or "p_id=" in parsed.query.lower():
            continue
        exact = row.get("exact_url_date")
        if not exact or len(str(exact)) != 10:
            continue
        if urlparse(str(row["archive_url"])).hostname != "web.archive.org":
            raise ValueError("Round7 archive URL is outside web.archive.org")
        result.append(row)
    return result


def _prior_state(root: Path, db_urls: set[str]) -> tuple[dict[str, int], set[str], set[str]]:
    counts: dict[str, int] = defaultdict(int)
    urls: set[str] = set()
    hashes: set[str] = set()
    for name in PRIOR_STAGING:
        base = root / "data/staging" / name
        for directory in (base / "records", base / "supplemental_records"):
            for path in directory.glob("*.json"):
                row = json.loads(path.read_text(encoding="utf-8"))
                key = url_key(str(row["original_url"]))
                urls.add(key)
                hashes.add(str(row["content_sha256"]))
                if directory.name == "records" and key not in db_urls:
                    counts[str(row["published_at"])[:7]] += 1
    return dict(counts), urls, hashes


def _write_record(
    root: Path, staging: Path, item: dict[str, object], payload: bytes,
    title: str, body: str, published_at: str, hits: list[str], supplemental: bool,
) -> dict[str, object]:
    raw_sha = hashlib.sha256(payload).hexdigest()
    body_bytes = body.encode("utf-8")
    body_sha = hashlib.sha256(body_bytes).hexdigest()
    month = published_at[:7]
    raw_path = staging / "raw" / month / f"{raw_sha}.html.gz"
    body_path = staging / "body" / month / f"{body_sha}.txt"
    directory = "supplemental_records" if supplemental else "records"
    record_path = staging / directory / f"{hashlib.sha256(str(item['url_key']).encode()).hexdigest()}.json"
    _atomic_write(raw_path, gzip.compress(payload, mtime=0))
    _atomic_write(body_path, body_bytes)
    row: dict[str, object] = {
        "source_code": "pasaxon_archive", "language": "lo",
        "title_original": title, "published_at": published_at,
        "date_precision": "url_day", "date_evidence": item["exact_url_date"],
        "body_original": body, "body_method": "wayback_direct_pasaxon_html",
        "matched_queries": hits, "original_url": item["original_url"],
        "archive_url": item["archive_url"],
        "raw_file": raw_path.relative_to(root).as_posix(),
        "body_file": body_path.relative_to(root).as_posix(),
        "evidence_grade": "B1", "retrieval_tier": "T1_DIRECT_CHINA",
        "raw_sha256": raw_sha, "content_sha256": body_sha,
        "archive_capture_timestamp": item["archive_capture_timestamp"],
        "retrieved_at": utc_now(), "parser": "old_pasaxon_direct_wayback_v7_dated_clean",
        "round7_target_month": item["target_month"],
        "round7_source_path": item["source_path"],
        "supplemental_non_gap": supplemental,
    }
    _atomic_write(record_path, (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    return row


def _outputs(staging: Path, ledger: AtomicJsonLedger) -> tuple[int, int, int]:
    def load(directory: str) -> list[dict[str, object]]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((staging / directory).glob("*.json"))]
    records, supplemental = load("records"), load("supplemental_records")
    for name, rows in (("records.ndjson", records), ("supplemental_records.ndjson", supplemental)):
        _atomic_write(staging / name, "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in rows).encode())
    failures = [x for x in ledger.rows.values() if str(x.get("status")) in FAILURE_STATUSES or str(x.get("status")) == "retryable_http_error"]
    _atomic_write(staging / "failures.ndjson", "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in failures).encode())
    return len(records), len(supplemental), len(failures)


def collect(
    *, root: Path, max_replays: int = 90, target_per_month: int = 2,
    interval: float = 1.0, fetcher: PoliteFetcher | None = None,
    queue_file: Path | None = None,
) -> dict[str, object]:
    staging = root / "data/staging" / STAGING_NAME
    staging.mkdir(parents=True, exist_ok=True)
    ledger = AtomicJsonLedger(staging / "screened.ndjson", key="url_key")
    queue_file = queue_file or root / "data/staging" / SOURCE_QUEUE / "pending_queue.ndjson"
    source_rows = read_queue(queue_file)
    queue = eligible_queue(source_rows)
    coverage = _coverage(root)
    db_counts, db_urls, db_hashes = _database_state(root)
    prior_counts, prior_urls, prior_hashes = _prior_state(root, db_urls)
    achieved = {
        month: max(coverage.get(month, 0), db_counts.get(month, 0)) + prior_counts.get(month, 0)
        for month in set(coverage) | set(db_counts) | set(prior_counts)
    }
    known_urls, known_hashes = db_urls | prior_urls, db_hashes | prior_hashes
    for directory in (staging / "records", staging / "supplemental_records"):
        for path in directory.glob("*.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            known_urls.add(url_key(str(row["original_url"])))
            known_hashes.add(str(row["content_sha256"]))
            if directory.name == "records":
                month = str(row["published_at"])[:7]
                achieved[month] = achieved.get(month, 0) + 1
    replay_total = sum(1 for x in ledger.rows.values() if x.get("status") != "existing_record")
    replay_start = replay_total
    statuses: dict[str, int] = defaultdict(int)
    matches: dict[str, int] = defaultdict(int)
    stopped_reason: str | None = None
    network = fetcher or PoliteFetcher(interval=max(1.0, interval))
    consecutive_retryable_http_errors = 0
    for item in queue:
        if replay_total >= max_replays:
            stopped_reason = f"article_replay_cap_reached:{max_replays}"
            break
        key, target_month = str(item["url_key"]), str(item["target_month"])
        prior = ledger.get(key)
        if prior and str(prior.get("status")) in TERMINAL:
            continue
        if achieved.get(target_month, 0) >= target_per_month:
            statuses["skipped_quota_met"] += 1
            continue
        base: dict[str, object] = {
            "url_key": key, "target_month": target_month, "source_path": item["source_path"],
            "original_url": item["original_url"], "archive_url": item["archive_url"],
            "archive_capture_timestamp": item["archive_capture_timestamp"],
            "exact_url_date": item["exact_url_date"], "screened_at": utc_now(),
            "status": None, "matched_terms": [], "error": None,
        }
        if key in known_urls:
            ledger.put({**base, "status": "existing_record"})
            statuses["existing_record"] += 1
            continue
        if urlparse(str(item["archive_url"])).hostname != "web.archive.org":
            raise ValueError("Round7 attempted an out-of-scope network URL")
        replay_total += 1
        try:
            payload = network.fetch(str(item["archive_url"]))
            consecutive_retryable_http_errors = 0
            if _is_challenge(payload):
                stopped_reason = f"challenge page detected:{item['archive_url']}"
                ledger.put({**base, "status": "challenge", "error": stopped_reason})
                statuses["challenge"] += 1
                break
            article = parse_direct_pasaxon_article(payload, str(item["original_url"]))
            if article.published_date != str(item["exact_url_date"]):
                ledger.put({**base, "status": "date_conflict", "published_at": article.published_date})
                statuses["date_conflict"] += 1
                continue
            title, body = clean_modern_article_segment(article.title, article.body)
            hits = _china_hits(title, body)
            if not hits:
                ledger.put({**base, "status": "not_china", "published_at": article.published_date})
                statuses["not_china"] += 1
                continue
            body_sha = hashlib.sha256(body.encode()).hexdigest()
            if body_sha in known_hashes:
                ledger.put({**base, "status": "duplicate_match", "published_at": article.published_date,
                            "matched_terms": hits, "body_sha256": body_sha})
                statuses["duplicate_match"] += 1
                continue
            month = article.published_date[:7]
            supplemental = achieved.get(month, 0) >= target_per_month
            row = _write_record(root, staging, item, payload, title, body, article.published_date, hits, supplemental)
            status = "china_match_non_gap" if supplemental else "china_match"
            ledger.put({**base, "status": status, "published_at": article.published_date,
                        "matched_terms": hits, "raw_sha256": row["raw_sha256"],
                        "body_sha256": row["content_sha256"], "raw_file": row["raw_file"],
                        "body_file": row["body_file"]})
            known_urls.add(key); known_hashes.add(str(row["content_sha256"]))
            if not supplemental:
                achieved[month] = achieved.get(month, 0) + 1
                matches[month] += 1
            statuses[status] += 1
        except ArchiveBlocked as exc:
            stopped_reason = str(exc)
            ledger.put({**base, "status": "blocked", "error": stopped_reason})
            statuses["blocked"] += 1
            break
        except HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_CODES:
                consecutive_retryable_http_errors += 1
                ledger.put({**base, "status": "retryable_http_error", "http_status": exc.code,
                            "error": f"HTTPError:{exc}"})
                statuses["retryable_http_error"] += 1
                if consecutive_retryable_http_errors >= HTTP_CIRCUIT_BREAKER_THRESHOLD:
                    stopped_reason = f"retryable_http_circuit_breaker:{exc.code}:{consecutive_retryable_http_errors}"
                    break
            else:
                consecutive_retryable_http_errors = 0
                ledger.put({**base, "status": "http_error", "http_status": exc.code,
                            "error": f"HTTPError:{exc}"})
                statuses["http_error"] += 1
        except Exception as exc:
            ledger.put({**base, "status": "parse_failure", "error": f"{type(exc).__name__}:{exc}"})
            statuses["parse_failure"] += 1
    records, supplemental, failures = _outputs(staging, ledger)
    totals: dict[str, int] = defaultdict(int)
    for row in ledger.rows.values(): totals[str(row.get("status"))] += 1
    pending = [x for x in queue if not ((ledger.get(str(x["url_key"])) and str(ledger.get(str(x["url_key"])).get("status")) in TERMINAL) or achieved.get(str(x["target_month"]), 0) >= target_per_month)]
    _atomic_write(staging / "pending_queue.ndjson", "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in pending).encode())
    summary: dict[str, object] = {
        "generated_at": utc_now(), "run_status": "stopped" if stopped_reason else "queue_exhausted",
        "database_mode": "read_only", "canonical_database_modified": False,
        "network_scope": "web.archive.org exact-date non-PHP replay URLs only",
        "php_urls_excluded": len(source_rows) - len(queue), "eligible_queue_records": len(queue),
        "network_concurrency": 1, "minimum_request_interval_seconds": max(1.0, interval),
        "article_replay_cap": max_replays, "article_replays_before_run": replay_start,
        "article_replays_this_run": replay_total - replay_start, "article_replays_total": replay_total,
        "status_counts_this_run": dict(statuses), "status_counts_total": dict(sorted(totals.items())),
        "new_matches_by_month": dict(sorted(matches.items())), "achieved_counts": dict(sorted(achieved.items())),
        "importable_records": records, "supplemental_records": supplemental, "failures": failures,
        "pending_queue_records": len(pending), "stopped_reason": stopped_reason,
    }
    _atomic_write(staging / "manifest.json", (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    return summary


def verify(root: Path) -> dict[str, object]:
    staging = root / "data/staging" / STAGING_NAME
    ledger = AtomicJsonLedger(staging / "screened.ndjson", key="url_key")
    failures: list[str] = []
    counts: dict[str, int] = {}
    for directory, status in (("records", "china_match"), ("supplemental_records", "china_match_non_gap")):
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((staging / directory).glob("*.json"))]
        counts[directory] = len(rows)
        for row in rows:
            key = url_key(str(row["original_url"])); audit = ledger.get(key)
            if not audit or audit.get("status") != status: failures.append(f"ledger:{key}")
            if urlparse(str(row["original_url"])).path.lower().endswith("pasaxon-detail.php"): failures.append(f"php:{key}")
            try:
                raw = gzip.decompress((root / str(row["raw_file"])).read_bytes())
                if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]: failures.append(f"raw_hash:{key}")
                body = (root / str(row["body_file"])).read_bytes()
                if hashlib.sha256(body).hexdigest() != row["content_sha256"]: failures.append(f"body_hash:{key}")
                if body.decode() != row["body_original"]: failures.append(f"body_text:{key}")
            except Exception as exc: failures.append(f"file:{key}:{type(exc).__name__}")
    result = {"generated_at": utc_now(), "ledger_records": len(ledger.rows), **counts,
              "verification_failures": failures, "all_verified": not failures}
    _atomic_write(staging / "verification.json", (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    return result


def reconcile_current_database(root: Path, target_per_month: int = 2) -> dict[str, object]:
    """Freeze an import list against the current canonical DB without writing it."""

    staging = root / "data/staging" / STAGING_NAME
    ledger = AtomicJsonLedger(staging / "screened.ndjson", key="url_key")
    db_counts, db_urls, db_hashes = _database_state(root)
    coverage = _coverage(root)
    kept: dict[str, int] = defaultdict(int)
    moved_duplicate = 0
    moved_quota = 0
    for path in sorted(
        (staging / "records").glob("*.json"),
        key=lambda p: (
            json.loads(p.read_text(encoding="utf-8"))["published_at"],
            json.loads(p.read_text(encoding="utf-8"))["original_url"],
        ),
    ):
        row = json.loads(path.read_text(encoding="utf-8"))
        key = url_key(str(row["original_url"]))
        month = str(row["published_at"])[:7]
        audit = ledger.get(key) or {"url_key": key}
        if key in db_urls or str(row["content_sha256"]) in db_hashes:
            destination = staging / "duplicate_records" / path.name
            row["preimport_disposition"] = "already_in_current_database"
            _atomic_write(destination, (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
            path.unlink()
            ledger.put({**audit, "status": "existing_record", "error": "current DB URL/content duplicate"})
            moved_duplicate += 1
            continue
        base_count = max(db_counts.get(month, 0), coverage.get(month, 0))
        if base_count + kept[month] >= target_per_month:
            destination = staging / "supplemental_records" / path.name
            row["preimport_disposition"] = "current_month_quota_met"
            row["supplemental_non_gap"] = True
            _atomic_write(destination, (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
            path.unlink()
            ledger.put({**audit, "status": "china_match_non_gap", "error": "current DB month quota met"})
            moved_quota += 1
            continue
        kept[month] += 1
    records, supplemental, failures = _outputs(staging, ledger)
    duplicate_total = len(list((staging / "duplicate_records").glob("*.json")))
    result: dict[str, object] = {
        "generated_at": utc_now(), "database_mode": "read_only",
        "canonical_database_modified": False,
        "moved_database_duplicates": duplicate_total,
        "moved_database_duplicates_this_run": moved_duplicate,
        "moved_quota_met_this_run": moved_quota, "importable_records": records,
        "supplemental_records": supplemental, "failures": failures,
        "kept_by_month": dict(sorted(kept.items())),
    }
    _atomic_write(staging / "preimport_reconciliation.json", (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    manifest_path = staging / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        status_counts: dict[str, int] = defaultdict(int)
        for row in ledger.rows.values():
            status_counts[str(row.get("status"))] += 1
        manifest.update({
            "preimport_reconciliation": result,
            "importable_records": records,
            "supplemental_records": supplemental,
            "status_counts_total": dict(sorted(status_counts.items())),
            "network_stopped": True,
        })
        _atomic_write(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Pasaxon Wayback round 7 exact-date non-PHP replay")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-replays", type=int, default=90)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--reconcile-only", action="store_true")
    args = parser.parse_args()
    result = (
        verify(args.root) if args.verify_only
        else reconcile_current_database(args.root) if args.reconcile_only
        else collect(root=args.root, max_replays=args.max_replays, interval=args.interval)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
