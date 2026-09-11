from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from .pasaxon_round2 import ArchiveBlocked, PoliteFetcher, _china_hits
from .pasaxon_round3 import AtomicJsonLedger, _atomic_write, _is_challenge, canonical_url
from .wayback import (
    WaybackCapture,
    load_cdx,
    parse_direct_pasaxon_article,
    pasaxon_url_identity,
)


STAGING_NAME = "pasaxon_round4_wayback"
TERMINAL = {
    "china_match",
    "not_china",
    "duplicate_match",
    "existing_record",
    "parse_failure",
    "http_error",
    "date_out_of_scope",
    "china_match_non_gap",
}
FAILURE_STATUSES = {"parse_failure", "http_error", "blocked", "challenge"}
SOURCE_PRIORITY = {
    "cooperation": 0,
    "pasaxon-detail.php": 0,
    "worldnews": 1,
    "hotnews": 2,
    "economic": 3,
    "econo": 3,
    "index": 4,
    "jaengkarn": 5,
}
ACT_PRIORITY = {
    "cooperation-detail": 0,
    "forigner-detail": 1,
    "investment-detail": 2,
    "leader-detail": 3,
    "economic-detail": 4,
    "politic1-detail": 5,
    "politic-detail": 6,
    "pasaxon-detail": 7,
}


@dataclass(frozen=True, slots=True)
class Candidate:
    capture: WaybackCapture
    url_key: str
    target_month: str
    source_path: str
    exact_url_date: str | None
    slot: int
    priority: tuple[int, int, str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def url_key(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    pid = query.get("p_id", [None])[0]
    if parsed.path.lower().endswith("pasaxon-detail.php") and pid and str(pid).isdigit():
        return f"php:{int(str(pid))}"
    return parsed.path.lower().rstrip("/")


def source_path(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path.lower().endswith("pasaxon-detail.php"):
        return "pasaxon-detail.php"
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0].lower() if parts else ""


def php_target_month(pid: int) -> str | None:
    """Target only the three still-missing 2020 windows using ID/date anchors."""

    if 253 <= pid <= 594:
        return "2020-01"
    if 1810 <= pid <= 2040:
        return "2020-08"
    if 2041 <= pid <= 2250:
        return "2020-09"
    return None


def _coverage(root: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    with (root / "data/catalog/monthly_coverage.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if row["source"] != "PASAXON" or not ("2014-01" <= row["year_month"] <= "2020-12"):
                continue
            result[row["year_month"]] = int(row["sample_story_count"])
    return result


def _existing_urls(root: Path) -> set[str]:
    database = root / "data/corpus.sqlite3"
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {
            url_key(str(row[0]))
            for row in conn.execute(
                "SELECT original_url FROM articles WHERE source_code='pasaxon_archive' "
                "AND original_url IS NOT NULL"
            )
        }
    finally:
        conn.close()


def _act_rank(url: str) -> int:
    act = parse_qs(urlparse(url).query).get("act", [""])[0].lower()
    return ACT_PRIORITY.get(act, 20)


def load_candidates(
    *,
    root: Path,
    index_files: Iterable[Path] | None = None,
) -> tuple[dict[str, list[Candidate]], dict[str, int]]:
    staging = root / "data/staging" / STAGING_NAME
    paths = list(index_files or sorted((staging / "cdx").glob("*.json")))
    selected: dict[str, Candidate] = {}
    index_counts: dict[str, int] = {}
    for path in paths:
        captures = load_cdx(path)
        index_counts[path.relative_to(root).as_posix()] = len(captures)
        for capture in captures:
            identity = pasaxon_url_identity(capture.original_url)
            if not identity:
                continue
            exact_date, month_hint, slot = identity
            category = source_path(capture.original_url)
            if category == "pasaxon-detail.php":
                target = php_target_month(slot)
                if not target:
                    continue
                # IDs are only approximate chronology, but ordering by ID is
                # markedly safer than grouping by section: the exact month is
                # still accepted solely from the printed page date.
                rank = (SOURCE_PRIORITY[category], slot, capture.original_url)
            else:
                target = month_hint
                if not target or not ("2014-01" <= target <= "2020-12"):
                    continue
                rank = (SOURCE_PRIORITY.get(category, 20), slot, capture.original_url)
            key = url_key(capture.original_url)
            candidate = Candidate(capture, key, target, category, exact_date, slot, rank)
            prior = selected.get(key)
            if prior is None or candidate.priority < prior.priority:
                selected[key] = candidate
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in selected.values():
        grouped[candidate.target_month].append(candidate)
    for month in grouped:
        grouped[month].sort(key=lambda item: item.priority)
    return dict(grouped), index_counts


def priority_months(grouped: dict[str, list[Candidate]], coverage: dict[str, int]) -> list[str]:
    preferred = [
        "2015-08", "2017-10",
        "2020-01", "2020-08", "2020-09",
        "2019-05", "2019-06", "2019-07", "2019-08",
        "2017-02", "2017-03", "2017-04",
        "2018-01", "2018-02", "2018-04", "2018-05", "2018-06",
        "2018-07", "2018-08", "2018-09", "2018-10",
        "2019-01", "2019-02", "2019-03", "2019-04",
    ]
    rest = sorted(
        month for month in grouped
        if coverage.get(month, 0) < 2 and month not in preferred
    )
    return [month for month in preferred if month in grouped and coverage.get(month, 0) < 2] + rest


def _write_record(
    *,
    root: Path,
    staging: Path,
    candidate: Candidate,
    payload: bytes,
    title: str,
    body: str,
    published: str,
    hits: list[str],
    retrieved_at: str,
) -> dict[str, object]:
    title, body = clean_modern_article_segment(title, body)
    raw_sha = hashlib.sha256(payload).hexdigest()
    body_bytes = body.encode("utf-8")
    body_sha = hashlib.sha256(body_bytes).hexdigest()
    month = published[:7]
    raw_path = staging / "raw" / month / f"{raw_sha}.html.gz"
    body_path = staging / "body" / month / f"{body_sha}.txt"
    record_path = staging / "records" / f"{hashlib.sha256(candidate.url_key.encode()).hexdigest()}.json"
    _atomic_write(raw_path, gzip.compress(payload, mtime=0))
    _atomic_write(body_path, body_bytes)
    date_method = "url_day" if candidate.exact_url_date else "page_day"
    row: dict[str, object] = {
        "source_code": "pasaxon_archive",
        "language": "lo",
        "title_original": title,
        "published_at": published,
        "date_precision": date_method,
        "date_evidence": candidate.exact_url_date or "printed_page_date",
        "body_original": body,
        "body_method": "wayback_direct_pasaxon_html",
        "matched_queries": hits,
        "original_url": candidate.capture.original_url,
        "archive_url": candidate.capture.replay_url,
        "raw_file": raw_path.relative_to(root).as_posix(),
        "body_file": body_path.relative_to(root).as_posix(),
        "evidence_grade": "B1",
        "retrieval_tier": "T1_DIRECT_CHINA",
        "raw_sha256": raw_sha,
        "content_sha256": body_sha,
        "archive_capture_timestamp": candidate.capture.timestamp,
        "archive_digest": candidate.capture.digest,
        "old_pasaxon_slot": candidate.slot,
        "retrieved_at": retrieved_at,
        "parser": "old_pasaxon_direct_wayback_v4",
        "round4_target_month": candidate.target_month,
        "round4_source_path": candidate.source_path,
    }
    _atomic_write(
        record_path,
        (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return row


def clean_modern_article_segment(title: str, body: str) -> tuple[str, str]:
    """Remove site navigation when a modern template exposes only a site title."""

    title_compact = re.sub(r"[\s\u200b]+", "", title)
    generic = title_compact.startswith("\u0ea5\u0eb2\u0e8d\u0e81\u0eb2\u0e99\u0e82\u0ec8\u0eb2\u0ea7") or (
        ("\u0eab\u0e99\u0eb1\u0e87\u0eaa\u0eb7\u0e9e\u0eb4\u0ea1" in title_compact or "\u0edc\u0eb1\u0e87\u0eaa\u0eb7\u0e9e\u0eb4\u0ea1" in title_compact)
        and len(title) < 60
    )
    if not generic:
        popular_marker = "\u0e82\u0ec8\u0eb2\u0ea7\u0e97\u0eb5\u0ec8\u0ec4\u0e94\u0ec9\u0eae\u0eb1\u0e9a\u0e84\u0ea7\u0eb2\u0ea1\u0e99\u0eb4\u0e8d\u0ebb\u0ea1"
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        boundary = next(
            (index for index, line in enumerate(lines)
             if popular_marker in re.sub(r"[\s\u200b]+", "", line)),
            len(lines),
        )
        trimmed = "\n\n".join(lines[:boundary])
        if boundary < len(lines) and len(trimmed) >= 60:
            return title, trimmed
        return title, body
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    china_index = next(
        (index for index, line in enumerate(lines) if _china_hits(line, "")),
        None,
    )
    if china_index is None:
        return title, body
    lead = lines[china_index]
    if len(lead) <= 200 and not lead.startswith(("\u0ec3\u0e99\u0ea7\u0eb1\u0e99\u0e97\u0eb5", "\u0ea7\u0eb1\u0e99\u0e97\u0eb5")):
        title_index = china_index
    elif china_index:
        title_index = china_index - 1
    else:
        return title, body
    recovered = lines[title_index]
    article_lines = lines[title_index + 1:]
    popular_marker = "\u0e82\u0ec8\u0eb2\u0ea7\u0e97\u0eb5\u0ec8\u0ec4\u0e94\u0ec9\u0eae\u0eb1\u0e9a\u0e84\u0ea7\u0eb2\u0ea1\u0e99\u0eb4\u0e8d\u0ebb\u0ea1"
    boundary = next(
        (
            index for index, line in enumerate(article_lines)
            if popular_marker in re.sub(r"[\s\u200b]+", "", line)
        ),
        len(article_lines),
    )
    article_body = "\n\n".join(article_lines[:boundary])
    return (recovered, article_body) if len(article_body) >= 60 else (title, body)


def refresh_importable_records(root: Path, staging: Path, ledger: AtomicJsonLedger) -> int:
    refreshed = 0
    for path in sorted((staging / "records").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        title, body = clean_modern_article_segment(
            str(row["title_original"]), str(row["body_original"])
        )
        if title == row["title_original"] and body == row["body_original"]:
            continue
        body_bytes = body.encode("utf-8")
        body_sha = hashlib.sha256(body_bytes).hexdigest()
        body_path = staging / "body" / str(row["published_at"])[:7] / f"{body_sha}.txt"
        _atomic_write(body_path, body_bytes)
        row.update(
            {
                "title_original": title,
                "body_original": body,
                "body_file": body_path.relative_to(root).as_posix(),
                "content_sha256": body_sha,
                "parser": "old_pasaxon_direct_wayback_v4_clean_segment",
            }
        )
        _atomic_write(
            path,
            (json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        key = url_key(str(row["original_url"]))
        audit = ledger.get(key)
        if audit:
            ledger.put(
                {
                    **audit,
                    "body_sha256": body_sha,
                    "body_file": row["body_file"],
                }
            )
        refreshed += 1
    return refreshed


def _rebuild_outputs(staging: Path, ledger: AtomicJsonLedger) -> tuple[int, int]:
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((staging / "records").glob("*.json"))
    ]
    _atomic_write(
        staging / "records.ndjson",
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records).encode("utf-8"),
    )
    failures = [
        row for row in ledger.rows.values() if str(row.get("status")) in FAILURE_STATUSES
    ]
    _atomic_write(
        staging / "failures.ndjson",
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in failures).encode("utf-8"),
    )
    return len(records), len(failures)


def reconcile_non_gap_records(
    staging: Path, ledger: AtomicJsonLedger, coverage: dict[str, int]
) -> int:
    """Keep useful extra captures, but exclude already-met months from import."""

    moved = 0
    supplemental = staging / "supplemental_records"
    supplemental.mkdir(parents=True, exist_ok=True)
    for path in sorted((staging / "records").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        month = str(row["published_at"])[:7]
        if coverage.get(month, 0) < 2:
            continue
        destination = supplemental / path.name
        os.replace(path, destination)
        key = url_key(str(row["original_url"]))
        audit = ledger.get(key)
        if audit:
            ledger.put(
                {
                    **audit,
                    "status": "china_match_non_gap",
                    "error": "actual page month already met the two-story minimum",
                }
            )
        moved += 1
    return moved


def collect(
    *,
    root: Path,
    max_replays: int = 120,
    target_per_month: int = 2,
    interval: float = 1.0,
    fetcher: PoliteFetcher | None = None,
    index_files: Iterable[Path] | None = None,
) -> dict[str, object]:
    staging = root / "data/staging" / STAGING_NAME
    staging.mkdir(parents=True, exist_ok=True)
    ledger = AtomicJsonLedger(staging / "screened.ndjson", key="url_key")
    coverage = _coverage(root)
    reconciled_non_gap = reconcile_non_gap_records(staging, ledger, coverage)
    existing = _existing_urls(root)
    grouped, index_counts = load_candidates(root=root, index_files=index_files)
    months = priority_months(grouped, coverage)
    network = fetcher or PoliteFetcher(interval=interval)

    staged: dict[str, int] = defaultdict(int)
    story_keys: set[str] = set()
    for path in (staging / "records").glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        staged[str(row["published_at"])[:7]] += 1
        story_keys.add(f"{row['published_at']}|{row['content_sha256']}")

    replay_total = sum(
        1 for row in ledger.rows.values() if row.get("status") != "existing_record"
    )
    replay_start = replay_total
    new_matches: dict[str, int] = defaultdict(int)
    statuses: dict[str, int] = defaultdict(int)
    stopped_reason: str | None = None
    for target_month in months:
        if stopped_reason or replay_total >= max_replays:
            break
        if coverage.get(target_month, 0) + staged.get(target_month, 0) >= target_per_month:
            continue
        for candidate in grouped[target_month]:
            if coverage.get(target_month, 0) + staged.get(target_month, 0) >= target_per_month:
                break
            if replay_total >= max_replays:
                stopped_reason = f"article_replay_cap_reached:{max_replays}"
                break
            prior = ledger.get(candidate.url_key)
            if prior and str(prior.get("status")) in TERMINAL:
                continue
            screened_at = utc_now()
            base: dict[str, object] = {
                "url_key": candidate.url_key,
                "original_url": candidate.capture.original_url,
                "archive_url": candidate.capture.replay_url,
                "archive_capture_timestamp": candidate.capture.timestamp,
                "target_month": target_month,
                "source_path": candidate.source_path,
                "screened_at": screened_at,
                "status": None,
                "matched_terms": [],
                "error": None,
            }
            if candidate.url_key in existing:
                ledger.put({**base, "status": "existing_record"})
                statuses["existing_record"] += 1
                continue
            replay_total += 1
            try:
                payload = network.fetch(candidate.capture.replay_url)
                if _is_challenge(payload):
                    stopped_reason = f"challenge page detected:{candidate.capture.replay_url}"
                    ledger.put({**base, "status": "challenge", "error": stopped_reason})
                    statuses["challenge"] += 1
                    break
                article = parse_direct_pasaxon_article(payload, candidate.capture.original_url)
                published_month = article.published_date[:7]
                if not ("2014-01" <= published_month <= "2020-12"):
                    ledger.put(
                        {
                            **base,
                            "status": "date_out_of_scope",
                            "published_at": article.published_date,
                        }
                    )
                    statuses["date_out_of_scope"] += 1
                    continue
                hits = _china_hits(article.title, article.body)
                if not hits:
                    ledger.put(
                        {
                            **base,
                            "status": "not_china",
                            "published_at": article.published_date,
                        }
                    )
                    statuses["not_china"] += 1
                    continue
                if coverage.get(published_month, 0) + staged.get(published_month, 0) >= target_per_month:
                    ledger.put(
                        {
                            **base,
                            "status": "china_match_non_gap",
                            "published_at": article.published_date,
                            "matched_terms": hits,
                            "error": "actual page month already met the two-story minimum",
                        }
                    )
                    statuses["china_match_non_gap"] += 1
                    continue
                body_sha = hashlib.sha256(article.body.encode("utf-8")).hexdigest()
                story_key = f"{article.published_date}|{body_sha}"
                if story_key in story_keys:
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
                row = _write_record(
                    root=root,
                    staging=staging,
                    candidate=candidate,
                    payload=payload,
                    title=article.title,
                    body=article.body,
                    published=article.published_date,
                    hits=hits,
                    retrieved_at=screened_at,
                )
                ledger.put(
                    {
                        **base,
                        "status": "china_match",
                        "published_at": article.published_date,
                        "matched_terms": hits,
                        "raw_sha256": row["raw_sha256"],
                        "body_sha256": row["content_sha256"],
                        "raw_file": row["raw_file"],
                        "body_file": row["body_file"],
                    }
                )
                story_keys.add(story_key)
                staged[published_month] += 1
                new_matches[published_month] += 1
                statuses["china_match"] += 1
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

    record_count, failure_count = _rebuild_outputs(staging, ledger)
    total_status: dict[str, int] = defaultdict(int)
    for row in ledger.rows.values():
        total_status[str(row.get("status"))] += 1
    summary: dict[str, object] = {
        "generated_at": utc_now(),
        "run_status": "stopped" if stopped_reason else "completed_available_priority_work",
        "database_mode": "read_only",
        "canonical_database_modified": False,
        "network_concurrency": 1,
        "minimum_request_interval_seconds": max(1.0, interval),
        "article_replay_cap": max_replays,
        "article_replays_before_run": replay_start,
        "article_replays_this_run": replay_total - replay_start,
        "article_replays_total": replay_total,
        "index_counts": index_counts,
        "priority_months": months,
        "status_counts_this_run": dict(statuses),
        "status_counts_total": dict(sorted(total_status.items())),
        "new_matches_by_month": dict(sorted(new_matches.items())),
        "staged_matches_by_month": dict(sorted(staged.items())),
        "importable_records": record_count,
        "failures": failure_count,
        "reconciled_non_gap_records": reconciled_non_gap,
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
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((staging / "records").glob("*.json"))
    ]
    for row in records:
        key = url_key(str(row["original_url"]))
        audit = ledger.get(key)
        if not audit or audit.get("status") != "china_match":
            failures.append(f"ledger:{key}")
        raw_path = root / str(row["raw_file"])
        body_path = root / str(row["body_file"])
        try:
            raw = gzip.decompress(raw_path.read_bytes())
            if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
                failures.append(f"raw_hash:{key}")
        except Exception as exc:
            failures.append(f"raw:{key}:{type(exc).__name__}")
        try:
            body = body_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != row["content_sha256"]:
                failures.append(f"body_hash:{key}")
            if body.decode("utf-8") != row["body_original"]:
                failures.append(f"body_text:{key}")
        except Exception as exc:
            failures.append(f"body:{key}:{type(exc).__name__}")
        if not re.fullmatch(r"20(?:1[4-9]|20)-\d{2}-\d{2}", str(row["published_at"])):
            failures.append(f"date:{key}")
    result = {
        "generated_at": utc_now(),
        "records": len(records),
        "ledger_records": len(ledger.rows),
        "verification_failures": failures,
        "all_verified": not failures,
    }
    _atomic_write(
        staging / "verification.json",
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return result


def finalize(
    root: Path,
    *,
    article_replay_cap: int = 120,
    stopped_reason: str = "stopped_on_parent_request_after_current_request",
) -> dict[str, object]:
    """Close an interrupted bounded run without making any network request."""

    staging = root / "data/staging" / STAGING_NAME
    ledger = AtomicJsonLedger(staging / "screened.ndjson", key="url_key")
    coverage = _coverage(root)
    reconcile_non_gap_records(staging, ledger, coverage)
    refreshed_records = refresh_importable_records(root, staging, ledger)
    grouped, index_counts = load_candidates(root=root)
    months = priority_months(grouped, coverage)
    record_count, failure_count = _rebuild_outputs(staging, ledger)
    status_counts: dict[str, int] = defaultdict(int)
    matches: dict[str, int] = defaultdict(int)
    for row in ledger.rows.values():
        status = str(row.get("status"))
        status_counts[status] += 1
        if status == "china_match":
            matches[str(row.get("published_at"))[:7]] += 1
    pending: list[dict[str, object]] = []
    for month in months:
        for candidate in grouped[month]:
            prior = ledger.get(candidate.url_key)
            if prior and str(prior.get("status")) in TERMINAL:
                continue
            pending.append(
                {
                    "target_month": month,
                    "url_key": candidate.url_key,
                    "source_path": candidate.source_path,
                    "original_url": candidate.capture.original_url,
                    "archive_url": candidate.capture.replay_url,
                    "archive_capture_timestamp": candidate.capture.timestamp,
                    "exact_url_date": candidate.exact_url_date,
                }
            )
    _atomic_write(
        staging / "pending_queue.ndjson",
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in pending).encode("utf-8"),
    )
    check = verify(root)
    article_replays = sum(
        1 for row in ledger.rows.values() if row.get("status") != "existing_record"
    )
    manifest: dict[str, object] = {
        "generated_at": utc_now(),
        "run_status": "stopped_cleanly_with_resumable_queue",
        "database_mode": "read_only",
        "canonical_database_modified": False,
        "network_stopped": True,
        "network_concurrency": 1,
        "minimum_request_interval_seconds": 1.0,
        "article_replay_cap": article_replay_cap,
        "article_replays_total": article_replays,
        "remaining_replay_budget": max(0, article_replay_cap - article_replays),
        "index_counts": index_counts,
        "candidate_months": len(grouped),
        "priority_gap_months": months,
        "ledger_records": len(ledger.rows),
        "status_counts": dict(sorted(status_counts.items())),
        "valid_matches_by_month": dict(sorted(matches.items())),
        "importable_records": record_count,
        "supplemental_non_gap_records": len(list((staging / "supplemental_records").glob("*.json"))),
        "failures": failure_count,
        "pending_queue_records": len(pending),
        "verification": check,
        "refreshed_article_segments": refreshed_records,
        "stopped_reason": stopped_reason,
    }
    _atomic_write(
        staging / "manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    lines = [
        "# Pasaxon Round 4 Wayback audit",
        "",
        f"- Article replay cap: {article_replay_cap}; completed: {article_replays}; remaining: {max(0, article_replay_cap - article_replays)}.",
        f"- Ledger: {len(ledger.rows)} URLs; importable gap-filling records: {record_count}; supplemental non-gap captures: {manifest['supplemental_non_gap_records']}.",
        f"- Valid new matches by month: {json.dumps(dict(sorted(matches.items())), ensure_ascii=False)}.",
        f"- Failures: {failure_count}; verification: {'PASS' if check['all_verified'] else 'FAIL'}.",
        f"- Pending resumable queue: {len(pending)} URLs.",
        f"- Stop reason: `{stopped_reason}`.",
    ]
    _atomic_write(staging / "round4_audit.md", ("\n".join(lines) + "\n").encode("utf-8"))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded Pasaxon Wayback backfill round 4")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-replays", type=int, default=120)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if args.finalize:
        result = finalize(args.root, article_replay_cap=args.max_replays)
    elif args.verify_only:
        result = verify(args.root)
    else:
        result = collect(root=args.root, max_replays=args.max_replays, interval=args.interval)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
