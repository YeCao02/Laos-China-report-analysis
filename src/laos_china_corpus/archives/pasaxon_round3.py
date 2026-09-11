from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit

from .pasaxon_round2 import ArchiveBlocked, PoliteFetcher, _china_hits, _priority_groups
from .wayback import (
    WaybackCapture,
    build_pasaxon_path_cdx_query,
    load_cdx,
    old_pasaxon_url_parts,
    parse_cdx,
    parse_direct_pasaxon_article,
    save_cdx,
)


DEFAULT_PRIORITY_MONTHS = (
    "2012-03",
    "2012-04",
    "2012-11",
    "2012-12",
    "2013-04",
    "2013-05",
    "2013-06",
    "2013-07",
)

TERMINAL_STATUSES = {
    "china_match",
    "not_china",
    "parse_failure",
    "http_error",
    "existing_record",
    "not_china_false_positive",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url: str) -> str:
    """Canonicalise only transport-default details; preserve the old path identity."""

    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    port = parts.port
    netloc = hostname
    if port and not ((parts.scheme.lower() == "http" and port == 80) or
                     (parts.scheme.lower() == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AtomicJsonLedger:
    """A small NDJSON ledger whose complete file is atomically replaced per URL.

    The collection is deliberately single-threaded.  Replacing the complete
    snapshot after each screened URL guarantees that an interrupted run sees
    either the old valid ledger or the new valid ledger, never a partial line.
    """

    def __init__(self, path: Path, *, key: str = "canonical_url") -> None:
        self.path = path
        self.key = key
        self.rows: dict[str, dict[str, object]] = {}
        if path.exists():
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if key not in row:
                    raise ValueError(f"{path}:{line_number} lacks {key}")
                self.rows[str(row[key])] = row

    def get(self, value: str) -> dict[str, object] | None:
        return self.rows.get(value)

    def put(self, row: dict[str, object]) -> None:
        value = str(row[self.key])
        self.rows[value] = dict(row)
        data = "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in sorted(self.rows.values(), key=lambda item: str(item[self.key]))
        ).encode("utf-8")
        _atomic_write(self.path, data)


def _read_existing(database: Path) -> tuple[dict[str, int], set[str]]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        counts = {
            str(month): int(count)
            for month, count in conn.execute(
                "SELECT substr(published_at,1,7),count(*) FROM articles "
                "WHERE source_code='pasaxon_archive' AND published_at BETWEEN "
                "'2012-01-01' AND '2020-12-31' GROUP BY 1"
            )
        }
        urls = {
            canonical_url(str(row[0]))
            for row in conn.execute(
                "SELECT original_url FROM articles WHERE source_code='pasaxon_archive' "
                "AND original_url IS NOT NULL"
            )
        }
        return counts, urls
    finally:
        conn.close()


def _is_challenge(payload: bytes) -> bool:
    sample = payload[:250_000].lower()
    signals = (
        b"captcha",
        b"verify you are human",
        b"cf-chl-captcha",
        b"g-recaptcha",
    )
    return any(signal in sample for signal in signals)


def discover_path_index(
    *,
    path_prefix: str,
    output: Path,
    year_from: int = 2012,
    year_to: int = 2020,
    fetcher: PoliteFetcher | None = None,
) -> int:
    """Fetch one path-bounded CDX index and atomically publish the result."""

    network = fetcher or PoliteFetcher(interval=1.0)
    payload = network.fetch(
        build_pasaxon_path_cdx_query(
            path_prefix, year_from=year_from, year_to=year_to
        )
    )
    if _is_challenge(payload):
        raise ArchiveBlocked("challenge page detected while fetching the CDX index")
    captures = parse_cdx(payload)
    # save_cdx itself writes directly, so stage it in the same directory and
    # atomically promote the fully serialised index.
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        save_cdx(captures, temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(captures)


def _write_match(
    *,
    root: Path,
    staging: Path,
    capture: WaybackCapture,
    month: str,
    payload: bytes,
    title: str,
    body: str,
    published_at: str,
    slot: int,
    hits: list[str],
    screened_at: str,
) -> dict[str, object]:
    raw_sha = hashlib.sha256(payload).hexdigest()
    body_bytes = body.encode("utf-8")
    body_sha = hashlib.sha256(body_bytes).hexdigest()
    raw_path = staging / "raw" / month / f"{raw_sha}.html.gz"
    body_path = staging / "body" / month / f"{body_sha}.txt"
    record_path = staging / "records" / f"{hashlib.sha256(canonical_url(capture.original_url).encode()).hexdigest()}.json"
    compressed = gzip.compress(payload, mtime=0)
    _atomic_write(raw_path, compressed)
    _atomic_write(body_path, body_bytes)
    row: dict[str, object] = {
        "source_code": "pasaxon_archive",
        "language": "lo",
        "title_original": title,
        "published_at": published_at,
        "date_precision": "url_day",
        "body_original": body,
        "body_method": "wayback_direct_pasaxon_html",
        "matched_queries": hits,
        "original_url": capture.original_url,
        "archive_url": capture.replay_url,
        "raw_file": raw_path.relative_to(root).as_posix(),
        "body_file": body_path.relative_to(root).as_posix(),
        "evidence_grade": "B1",
        "retrieval_tier": "T1_DIRECT_CHINA",
        "raw_sha256": raw_sha,
        "content_sha256": body_sha,
        "archive_capture_timestamp": capture.timestamp,
        "archive_digest": capture.digest,
        "old_pasaxon_slot": slot,
        "retrieved_at": screened_at,
        "parser": "old_pasaxon_direct_wayback_v3",
    }
    _atomic_write(
        record_path,
        (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return row


def _rebuild_records(staging: Path) -> int:
    rows: list[dict[str, object]] = []
    for path in sorted((staging / "records").glob("*.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    data = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    _atomic_write(staging / "records.ndjson", data)
    return len(rows)


def _refresh_staged_matches(root: Path, staging: Path, ledger: AtomicJsonLedger) -> int:
    """Reparse saved payloads so parser improvements are resumably propagated."""

    refreshed = 0
    for record_path in sorted((staging / "records").glob("*.json")):
        old = json.loads(record_path.read_text(encoding="utf-8"))
        raw_path = root / str(old["raw_file"])
        payload = gzip.decompress(raw_path.read_bytes())
        article = parse_direct_pasaxon_article(payload, str(old["original_url"]))
        hits = _china_hits(article.title, article.body)
        if not hits:
            key = canonical_url(str(old["original_url"]))
            audit = ledger.get(key)
            rejected_path = staging / "rejected" / record_path.name
            rejected = {
                **old,
                "rejection_reason": "isolated Lao substring was a personal name, not China",
                "rejected_at": utc_now(),
            }
            _atomic_write(
                rejected_path,
                (json.dumps(rejected, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            record_path.unlink()
            if audit:
                ledger.put(
                    {
                        **audit,
                        "status": "not_china_false_positive",
                        "matched_terms": [],
                        "error": "isolated Lao substring was a personal name, not China",
                    }
                )
            refreshed += 1
            continue
        capture = WaybackCapture(
            timestamp=str(old["archive_capture_timestamp"]),
            original_url=str(old["original_url"]),
            digest=str(old.get("archive_digest") or "") or None,
            mimetype="text/html",
        )
        row = _write_match(
            root=root,
            staging=staging,
            capture=capture,
            month=str(old["published_at"])[:7],
            payload=payload,
            title=article.title,
            body=article.body,
            published_at=article.published_date,
            slot=article.slot,
            hits=hits,
            screened_at=str(old["retrieved_at"]),
        )
        key = canonical_url(str(old["original_url"]))
        audit = ledger.get(key)
        if audit:
            ledger.put(
                {
                    **audit,
                    "matched_terms": hits,
                    "raw_sha256": row["raw_sha256"],
                    "body_sha256": row["content_sha256"],
                    "raw_file": row["raw_file"],
                    "body_file": row["body_file"],
                }
            )
        refreshed += 1
    return refreshed


def collect(
    *,
    root: Path,
    index_file: Path,
    months: Iterable[str] = DEFAULT_PRIORITY_MONTHS,
    target_per_month: int = 2,
    interval: float = 1.0,
    fetcher: PoliteFetcher | None = None,
    database: Path | None = None,
) -> dict[str, object]:
    """Screen an existing CDX index without mutating the canonical database."""

    staging = root / "data" / "staging" / "pasaxon_round3"
    staging.mkdir(parents=True, exist_ok=True)
    ledger = AtomicJsonLedger(staging / "screened.ndjson")
    database = database or root / "data" / "corpus.sqlite3"
    existing_counts, existing_urls = _read_existing(database)
    captures = load_cdx(index_file)
    groups = _priority_groups(captures)
    requested = [month for month in months if month in groups]
    network = fetcher or PoliteFetcher(interval=interval)

    staged_matches: dict[str, int] = defaultdict(int)
    for row in ledger.rows.values():
        if row.get("status") == "china_match":
            staged_matches[str(row["month"])] += 1

    attempted_this_run: dict[str, int] = defaultdict(int)
    status_this_run: dict[str, int] = defaultdict(int)
    matches_this_run: dict[str, int] = defaultdict(int)
    stopped_reason: str | None = None
    replayed = 0

    for month in requested:
        if stopped_reason:
            break
        need = target_per_month - existing_counts.get(month, 0) - staged_matches.get(month, 0)
        if need <= 0:
            continue
        for capture in groups[month]:
            if matches_this_run[month] >= need:
                break
            key = canonical_url(capture.original_url)
            prior = ledger.get(key)
            if prior and str(prior.get("status")) in TERMINAL_STATUSES:
                continue
            screened_at = utc_now()
            base_row: dict[str, object] = {
                "canonical_url": key,
                "original_url": capture.original_url,
                "archive_url": capture.replay_url,
                "archive_capture_timestamp": capture.timestamp,
                "month": month,
                "screened_at": screened_at,
                "matched_terms": [],
                "error": None,
            }
            if key in existing_urls:
                ledger.put({**base_row, "status": "existing_record"})
                status_this_run["existing_record"] += 1
                continue
            attempted_this_run[month] += 1
            replayed += 1
            try:
                payload = network.fetch(capture.replay_url)
                if _is_challenge(payload):
                    stopped_reason = f"challenge page detected: {capture.replay_url}"
                    ledger.put({**base_row, "status": "challenge", "error": stopped_reason})
                    status_this_run["challenge"] += 1
                    break
                article = parse_direct_pasaxon_article(payload, capture.original_url)
                hits = _china_hits(article.title, article.body)
                if not hits:
                    ledger.put({**base_row, "status": "not_china"})
                    status_this_run["not_china"] += 1
                    continue
                record = _write_match(
                    root=root,
                    staging=staging,
                    capture=capture,
                    month=month,
                    payload=payload,
                    title=article.title,
                    body=article.body,
                    published_at=article.published_date,
                    slot=article.slot,
                    hits=hits,
                    screened_at=screened_at,
                )
                ledger.put(
                    {
                        **base_row,
                        "status": "china_match",
                        "matched_terms": hits,
                        "raw_sha256": record["raw_sha256"],
                        "body_sha256": record["content_sha256"],
                        "raw_file": record["raw_file"],
                        "body_file": record["body_file"],
                    }
                )
                status_this_run["china_match"] += 1
                matches_this_run[month] += 1
                staged_matches[month] += 1
            except ArchiveBlocked as exc:
                stopped_reason = str(exc)
                ledger.put({**base_row, "status": "blocked", "error": stopped_reason})
                status_this_run["blocked"] += 1
                break
            except HTTPError as exc:
                # PoliteFetcher promotes 403/429 to ArchiveBlocked. Other HTTP
                # statuses are terminal for this immutable capture URL.
                ledger.put(
                    {
                        **base_row,
                        "status": "http_error",
                        "http_status": exc.code,
                        "error": f"HTTPError: {exc}",
                    }
                )
                status_this_run["http_error"] += 1
            except Exception as exc:
                ledger.put(
                    {
                        **base_row,
                        "status": "parse_failure",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                status_this_run["parse_failure"] += 1

    refreshed_records = _refresh_staged_matches(root, staging, ledger)
    record_count = _rebuild_records(staging)
    terminal_counts: dict[str, int] = defaultdict(int)
    screened_by_month: dict[str, int] = defaultdict(int)
    matches_by_month: dict[str, int] = defaultdict(int)
    for row in ledger.rows.values():
        status = str(row.get("status"))
        terminal_counts[status] += 1
        month = str(row.get("month"))
        screened_by_month[month] += 1
        if status == "china_match":
            matches_by_month[month] += 1

    summary: dict[str, object] = {
        "generated_at": utc_now(),
        "database_mode": "read_only",
        "network_concurrency": 1,
        "minimum_request_interval_seconds": max(1.0, interval),
        "source_index": index_file.relative_to(root).as_posix(),
        "source_index_records": len(captures),
        "requested_months": requested,
        "target_china_articles_per_month": target_per_month,
        "network_replays_this_run": replayed,
        "attempted_by_month_this_run": dict(attempted_this_run),
        "status_counts_this_run": dict(status_this_run),
        "new_matches_by_month_this_run": dict(matches_this_run),
        "ledger_records_total": len(ledger.rows),
        "screened_by_month_total": dict(sorted(screened_by_month.items())),
        "status_counts_total": dict(sorted(terminal_counts.items())),
        "matches_by_month_total": dict(sorted(matches_by_month.items())),
        "importable_records_total": record_count,
        "reparsed_importable_records": refreshed_records,
        "existing_database_counts": {
            month: existing_counts.get(month, 0) for month in requested
        },
        "stopped_reason": stopped_reason,
    }
    _atomic_write(
        staging / "manifest.json",
        (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return summary


def write_audit(
    root: Path,
    *,
    index_files: Iterable[Path] | None = None,
) -> dict[str, object]:
    """Verify all staged evidence and write a human/machine-readable audit."""

    staging = root / "data" / "staging" / "pasaxon_round3"
    ledger = AtomicJsonLedger(staging / "screened.ndjson")
    if index_files is None:
        index_files = (
            root / "data/staging/pasaxon_round2/cdx/pasaxon_conten_2012_2020.json",
            staging / "cdx/pasaxon_articles_2012_2020.json",
        )
    indexed: dict[str, set[str]] = defaultdict(set)
    index_counts: dict[str, int] = {}
    for path in index_files:
        captures = load_cdx(path)
        index_counts[path.relative_to(root).as_posix()] = len(captures)
        for capture in captures:
            parts = old_pasaxon_url_parts(capture.original_url)
            if parts:
                indexed[parts[0][:7]].add(canonical_url(capture.original_url))

    hash_failures: list[str] = []
    titles_without_lao: list[str] = []
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((staging / "records").glob("*.json"))
    ]
    record_urls: set[str] = set()
    for record in records:
        url = canonical_url(str(record["original_url"]))
        record_urls.add(url)
        raw_path = root / str(record["raw_file"])
        body_path = root / str(record["body_file"])
        try:
            raw_payload = gzip.decompress(raw_path.read_bytes())
            if hashlib.sha256(raw_payload).hexdigest() != record["raw_sha256"]:
                hash_failures.append(f"raw:{url}")
        except Exception as exc:
            hash_failures.append(f"raw:{url}:{type(exc).__name__}")
        try:
            body_payload = body_path.read_bytes()
            if hashlib.sha256(body_payload).hexdigest() != record["content_sha256"]:
                hash_failures.append(f"body:{url}")
            if body_payload.decode("utf-8") != record["body_original"]:
                hash_failures.append(f"body_text:{url}")
        except Exception as exc:
            hash_failures.append(f"body:{url}:{type(exc).__name__}")
        if len(re.findall(r"[\u0e80-\u0eff]", str(record["title_original"]))) < 3:
            titles_without_lao.append(url)
        audit_row = ledger.get(url)
        if not audit_row or audit_row.get("status") != "china_match":
            hash_failures.append(f"ledger_link:{url}")
        elif (
            audit_row.get("raw_sha256") != record["raw_sha256"]
            or audit_row.get("body_sha256") != record["content_sha256"]
        ):
            hash_failures.append(f"ledger_hash:{url}")

    status_counts: dict[str, int] = defaultdict(int)
    screened: dict[str, int] = defaultdict(int)
    matches: dict[str, int] = defaultdict(int)
    for row in ledger.rows.values():
        month = str(row["month"])
        status = str(row["status"])
        screened[month] += 1
        status_counts[status] += 1
        if status == "china_match":
            matches[month] += 1
            if str(row["canonical_url"]) not in record_urls:
                hash_failures.append(f"missing_record:{row['canonical_url']}")

    month_rows = []
    for month in sorted(screened):
        indexed_count = len(indexed.get(month, set()))
        month_rows.append(
            {
                "month": month,
                "indexed_unique_urls": indexed_count,
                "screened_urls": screened[month],
                "valid_china_matches": matches[month],
                "remaining_unreviewed": max(0, indexed_count - screened[month]),
                "screening_complete_or_quota_met": (
                    screened[month] >= indexed_count or matches[month] >= 2
                ),
            }
        )

    failure_statuses = {
        key: value
        for key, value in status_counts.items()
        if key in {"parse_failure", "http_error", "blocked", "challenge"}
    }
    audit: dict[str, object] = {
        "generated_at": utc_now(),
        "database_mode": "read_only",
        "network_concurrency": 1,
        "minimum_request_interval_seconds": 1.0,
        "index_counts": index_counts,
        "ledger_records": len(ledger.rows),
        "article_replays": len(ledger.rows) - status_counts.get("existing_record", 0),
        "status_counts": dict(sorted(status_counts.items())),
        "importable_records": len(records),
        "rejected_false_positives": status_counts.get("not_china_false_positive", 0),
        "failure_statuses": failure_statuses,
        "hash_failures": hash_failures,
        "titles_without_lao": titles_without_lao,
        "all_hashes_verified": not hash_failures,
        "all_titles_recovered": not titles_without_lao,
        "months": month_rows,
    }
    _atomic_write(
        staging / "round3_audit.json",
        (json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    final_manifest = {
        "run_status": "completed_bounded_replay",
        **audit,
        "network_stopped": True,
        "canonical_database_modified": False,
        "screening_policy": "all indexed URLs until month exhausted or two valid matches",
        "audit_files": [
            "data/staging/pasaxon_round3/round3_audit.json",
            "data/staging/pasaxon_round3/round3_audit.md",
        ],
    }
    _atomic_write(
        staging / "manifest.json",
        (json.dumps(final_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    lines = [
        "# Pasaxon Round 3 historical replay audit",
        "",
        f"- CDX indexes: `conten/*` {index_counts.get('data/staging/pasaxon_round2/cdx/pasaxon_conten_2012_2020.json', 0)} records; `articles/*` {index_counts.get('data/staging/pasaxon_round3/cdx/pasaxon_articles_2012_2020.json', 0)} records.",
        f"- Screened ledger: {len(ledger.rows)} URLs; {len(ledger.rows) - status_counts.get('existing_record', 0)} article replays.",
        f"- Valid China-related full texts: {len(records)}; false-positive personal-name substrings rejected: {status_counts.get('not_china_false_positive', 0)}.",
        f"- Failures/blocks: {sum(failure_statuses.values())}; hash verification: {'PASS' if not hash_failures else 'FAIL'}; title recovery: {'PASS' if not titles_without_lao else 'FAIL'}.",
        "",
        "| Month | Indexed unique URLs | Screened | Valid matches | Remaining | Stop condition |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in month_rows:
        condition = "exhausted or quota met" if row["screening_complete_or_quota_met"] else "incomplete"
        lines.append(
            f"| {row['month']} | {row['indexed_unique_urls']} | {row['screened_urls']} | "
            f"{row['valid_china_matches']} | {row['remaining_unreviewed']} | {condition} |"
        )
    _atomic_write(staging / "round3_audit.md", ("\n".join(lines) + "\n").encode("utf-8"))
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable Pasaxon Wayback screening round 3")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--index-file", type=Path, required=True)
    parser.add_argument(
        "--discover-prefix",
        help="First refresh --index-file using one path-bounded CDX query",
    )
    parser.add_argument("--month", action="append")
    parser.add_argument("--target-per-month", type=int, default=2)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if args.discover_prefix:
        discovered = discover_path_index(
            path_prefix=args.discover_prefix,
            output=args.index_file,
        )
        print(json.dumps({"discovered_index_records": discovered}, ensure_ascii=False))
    result = collect(
        root=args.root,
        index_file=args.index_file,
        months=args.month or DEFAULT_PRIORITY_MONTHS,
        target_per_month=args.target_per_month,
        interval=args.interval,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
