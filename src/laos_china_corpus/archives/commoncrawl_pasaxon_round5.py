"""Bounded discovery and retrieval from alternate Common Crawl indexes."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .commoncrawl import (
    CommonCrawlRecord,
    build_index_query,
    extract_archive_http_payload,
    parse_index_response,
)
from .commoncrawl_pasaxon import (
    _canonical_pasaxon_url,
    _china_related,
    _entity_lao_html,
    _is_contaminated_template,
)
from .wayback import parse_direct_pasaxon_article, pasaxon_url_identity


COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
EXCLUDED_INDEXES = {
    "CC-MAIN-2017-51", "CC-MAIN-2018-51", "CC-MAIN-2019-51", "CC-MAIN-2020-50",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_bytes(path: Path, payload: bytes) -> None:
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


def read_ndjson(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_ndjson(path: Path, rows: list[dict[str, object]]) -> None:
    atomic_bytes(path, b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for row in rows
    ))


class PoliteHTTP:
    def __init__(self, *, interval: float = 1.0, timeout: float = 60.0) -> None:
        self.interval = interval
        self.timeout = timeout
        self.last_request = 0.0
        self.requests = Counter()

    def get(self, url: str, *, kind: str, range_header: str | None = None) -> bytes:
        delay = self.interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        headers = {
            "User-Agent": "laos-china-media-corpus/0.1 (+academic archive recovery)",
            "Accept-Encoding": "identity",
        }
        if range_header:
            headers["Range"] = range_header
        self.requests[kind] += 1
        try:
            with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
                return response.read()
        finally:
            self.last_request = time.monotonic()


def choose_indexes(collinfo: list[dict[str, object]], *, query_slots: int) -> list[str]:
    """Choose two indexes for 2014--2016 and one for 2017--2019 when possible."""

    by_year: dict[int, list[str]] = defaultdict(list)
    for row in collinfo:
        index = str(row.get("id") or "")
        match = re.fullmatch(r"CC-MAIN-(20\d{2})-(\d+)", index)
        if not match or index in EXCLUDED_INDEXES:
            continue
        year = int(match.group(1))
        if 2014 <= year <= 2019:
            by_year[year].append(index)
    for indexes in by_year.values():
        indexes.sort(key=lambda value: int(value.rsplit("-", 1)[1]), reverse=True)
    chosen: list[str] = []
    desired = {2014: 2, 2015: 2, 2016: 2, 2017: 1, 2018: 1, 2019: 1}
    for year in range(2014, 2020):
        chosen.extend(by_year[year][:desired[year]])
    return chosen[:query_slots]


def load_coverage(root: Path) -> tuple[dict[str, int], set[str]]:
    counts: dict[str, int] = {}
    gaps: set[str] = set()
    with (root / "data/catalog/monthly_coverage.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["source"] != "PASAXON":
                continue
            month = row["year_month"]
            counts[month] = int(row["sample_story_count"] or 0)
            if row["coverage_status"] == "documented_gap":
                gaps.add(month)
    return counts, gaps


def load_existing_urls(root: Path) -> set[str]:
    database = root / "data/corpus.sqlite3"
    conn = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        urls = {
            _canonical_pasaxon_url(str(row[0]))
            for row in conn.execute("SELECT original_url FROM articles WHERE original_url IS NOT NULL")
        }
    finally:
        conn.close()
    prior = root / "data/staging/archive_ocr/commoncrawl_pasaxon_2017_2019/screened.ndjson"
    urls.update(str(row.get("canonical_url")) for row in read_ndjson(prior) if row.get("canonical_url"))
    return urls


def exact_dated_candidates(
    records: list[CommonCrawlRecord], *, source_index: str,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for record in records:
        identity = pasaxon_url_identity(record.url)
        if not identity or not identity[0]:
            continue
        published, _, slot = identity
        if not ("2014-01-01" <= published <= "2019-12-31"):
            continue
        candidates.append({
            "source_index": source_index, "published_at": published,
            "year_month": published[:7], "slot": slot, "original_url": record.url,
            "canonical_url": _canonical_pasaxon_url(record.url),
            "timestamp": record.timestamp, "filename": record.filename,
            "offset": record.offset, "length": record.length,
            "warc_url": record.warc_url, "range_header": record.range_header,
            "digest": record.digest,
        })
    return candidates


def prioritize(
    rows: list[dict[str, object]], *, gaps: set[str], existing: set[str],
) -> list[dict[str, object]]:
    best: dict[str, dict[str, object]] = {}
    for row in rows:
        canonical = str(row["canonical_url"])
        if canonical in existing:
            continue
        prior = best.get(canonical)
        if prior is None or str(row["timestamp"]) > str(prior["timestamp"]):
            best[canonical] = row
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in best.values():
        groups[str(row["year_month"])].append(row)
    for month in groups:
        groups[month].sort(key=lambda row: (str(row["published_at"]), int(row["slot"]), str(row["canonical_url"])))
    months = sorted(groups, key=lambda month: (month not in gaps, month))
    queue: list[dict[str, object]] = []
    depth = 0
    while True:
        added = False
        for month in months:
            if depth < len(groups[month]):
                queue.append(groups[month][depth])
                added = True
        if not added:
            break
        depth += 1
    return queue


def category_key(url: str) -> str:
    parts = [part for part in urlparse(url).path.casefold().split("/") if part]
    return parts[0] if parts else "root"


def reparse_saved_records(*, root: Path) -> dict[str, object]:
    """Offline-only repair after a source parser improvement."""

    staging = root / "data/staging/archive_ocr/commoncrawl_pasaxon_round5"
    records_path, ledger_path = staging / "records.ndjson", staging / "screened.ndjson"
    rows, ledger = read_ndjson(records_path), read_ndjson(ledger_path)
    repaired: list[dict[str, object]] = []
    rejected: set[str] = set()
    for row in rows:
        raw_path = root / str(row["raw_file"])
        payload = extract_archive_http_payload(raw_path.read_bytes())
        article = parse_direct_pasaxon_article(_entity_lao_html(payload), str(row["original_url"]))
        related, hits = _china_related(f"{article.title}\n{article.body}")
        canonical = _canonical_pasaxon_url(str(row["original_url"]))
        if not related or _is_contaminated_template(article.title, article.body):
            rejected.add(canonical)
            continue
        body_sha = hashlib.sha256(article.body.encode("utf-8")).hexdigest()
        body_path = text_dir = staging / "text" / article.published_date[:7] / f"{body_sha}.txt"
        atomic_bytes(body_path, article.body.encode("utf-8"))
        repaired.append({
            **row, "title_original": article.title, "body_original": article.body,
            "matched_queries": hits, "content_sha256": body_sha,
            "body_file": str(body_path.relative_to(root)),
            "parser": "commoncrawl_pasaxon_round5_v2",
        })
    updated_ledger: list[dict[str, object]] = []
    repaired_by_url = {
        _canonical_pasaxon_url(str(row["original_url"])): row for row in repaired
    }
    for row in ledger:
        canonical = str(row.get("canonical_url"))
        if canonical in rejected and row.get("status") == "china_match":
            updated_ledger.append({
                **row, "prior_status": "china_match", "status": "not_china_after_body_reparse",
                "reparsed_at": utc_now(),
            })
        elif canonical in repaired_by_url:
            record = repaired_by_url[canonical]
            updated_ledger.append({
                **row, "hits": record["matched_queries"],
                "content_sha256": record["content_sha256"],
                "body_file": record["body_file"], "reparsed_at": utc_now(),
            })
        else:
            updated_ledger.append(row)
    write_ndjson(records_path, repaired)
    write_ndjson(ledger_path, updated_ledger)
    return {
        "input_records": len(rows), "repaired_records": len(repaired),
        "rejected_false_positives": len(rejected),
    }


def run(
    *, root: Path, max_index_requests: int = 10, max_range_requests: int = 90,
    interval: float = 1.0,
) -> dict[str, object]:
    staging = root / "data/staging/archive_ocr/commoncrawl_pasaxon_round5"
    index_dir, raw_dir, text_dir = staging / "indexes", staging / "raw", staging / "text"
    ledger_path, records_path = staging / "screened.ndjson", staging / "records.ndjson"
    failures_path, queue_path = staging / "failures.ndjson", staging / "continuation_queue.ndjson"
    http = PoliteHTTP(interval=interval)
    failures = read_ndjson(failures_path)
    ledger = read_ndjson(ledger_path)
    records_out = read_ndjson(records_path)
    try:
        collinfo_payload = http.get(COLLINFO_URL, kind="index")
        atomic_bytes(index_dir / "collinfo.json", collinfo_payload)
        collinfo = json.loads(collinfo_payload)
    except Exception as exc:
        raise RuntimeError(f"collinfo discovery failed: {exc}") from exc
    indexes = choose_indexes(collinfo, query_slots=max_index_requests - 1)
    all_candidates: list[dict[str, object]] = []
    index_audit: list[dict[str, object]] = []
    for index in indexes:
        query = build_index_query(
            index, "pasaxon.org.la", filters=("status:200", "mime:text/html"),
            collapse="urlkey", match_type="domain", page=0, page_size=10000,
        )
        output = index_dir / f"{index}.ndjson"
        try:
            payload = http.get(query, kind="index")
            atomic_bytes(output, payload)
            parsed = parse_index_response(payload)
            candidates = exact_dated_candidates(parsed, source_index=index)
            all_candidates.extend(candidates)
            index_audit.append({
                "index": index, "query_url": query, "response_file": str(output.relative_to(root)),
                "response_sha256": hashlib.sha256(payload).hexdigest(),
                "rows": len(parsed), "exact_dated_candidates": len(candidates), "status": "complete",
            })
        except Exception as exc:
            failures.append({
                "stage": "index", "source_index": index, "url": query,
                "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now(),
            })
            index_audit.append({"index": index, "query_url": query, "status": "failed", "error": str(exc)})
    counts, gaps = load_coverage(root)
    existing = load_existing_urls(root)
    existing.update(str(row.get("canonical_url")) for row in ledger if row.get("canonical_url"))
    queue = prioritize(all_candidates, gaps=gaps, existing=existing)
    matched = Counter(str(row["published_at"])[:7] for row in records_out)
    terminal = {str(row.get("canonical_url")) for row in ledger if row.get("canonical_url")}
    blocked_categories: set[str] = set()
    range_requests = 0
    continuation: list[dict[str, object]] = []
    for position, row in enumerate(queue):
        month = str(row["year_month"])
        canonical = str(row["canonical_url"])
        category = category_key(str(row["original_url"]))
        if canonical in terminal:
            continue
        if counts.get(month, 0) + matched[month] >= 2:
            continuation.append({**row, "queue_status": "month_quota_met"})
            continue
        if category in blocked_categories:
            continuation.append({**row, "queue_status": "blocked_contaminated_template_path"})
            continue
        if range_requests >= max_range_requests:
            continuation.extend({**item, "queue_status": "range_budget_exhausted"} for item in queue[position:])
            break
        range_requests += 1
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
                status = "contaminated_template"
                failure = {
                    **row, "stage": "range", "status": status,
                    "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha, "error": "compromised generic SEO template",
                    "checked_at": utc_now(),
                }
                failures.append(failure)
                ledger.append(failure)
                write_ndjson(failures_path, failures); write_ndjson(ledger_path, ledger)
                continue
            related, hits = _china_related(f"{article.title}\n{article.body}")
            body_sha = hashlib.sha256(article.body.encode("utf-8")).hexdigest()
            text_path = text_dir / month / f"{body_sha}.txt"
            atomic_bytes(text_path, article.body.encode("utf-8"))
            status = "china_match" if related else "not_china"
            ledger_row = {
                **row, "status": status, "hits": hits,
                "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                "payload_sha256": payload_sha, "body_file": str(text_path.relative_to(root)),
                "content_sha256": body_sha, "checked_at": utc_now(),
            }
            ledger.append(ledger_row)
            if related:
                record = {
                    "source_code": "pasaxon_archive", "language": "lo",
                    "title_original": article.title, "published_at": article.published_date,
                    "date_precision": "url_day", "body_original": article.body,
                    "body_method": "commoncrawl_warc_pasaxon_html",
                    "matched_queries": hits, "original_url": row["original_url"],
                    "archive_url": row["warc_url"], "evidence_grade": "B1",
                    "retrieval_tier": "T1_DIRECT_CHINA", "content_sha256": body_sha,
                    "body_file": str(text_path.relative_to(root)),
                    "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha, "retrieved_at": utc_now(),
                    "capture_timestamp": row["timestamp"], "source_index": row["source_index"],
                    "warc": {key: row[key] for key in ("warc_url", "filename", "offset", "length", "range_header")},
                    "parser": "commoncrawl_pasaxon_round5_v1",
                }
                records_out.append(record)
                matched[month] += 1
            write_ndjson(ledger_path, ledger); write_ndjson(records_path, records_out)
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
                continuation.extend({**item, "queue_status": f"stopped_http_{exc.code}"} for item in queue[position + 1:])
                break
    write_ndjson(queue_path, continuation)
    write_ndjson(failures_path, failures)
    # Offline evidence audit.
    raw_audit = Counter()
    for row in ledger:
        if not row.get("raw_file"):
            continue
        path = root / str(row["raw_file"])
        if path.is_file():
            raw_audit["exists"] += 1
            raw = path.read_bytes()
            raw_audit["raw_sha_matches"] += hashlib.sha256(raw).hexdigest() == row.get("raw_sha256")
            try:
                payload = extract_archive_http_payload(raw)
                raw_audit["extracts"] += 1
                raw_audit["payload_sha_matches"] += hashlib.sha256(payload).hexdigest() == row.get("payload_sha256")
            except ValueError:
                pass
    manifest = {
        "generated_at": utc_now(), "network_scope": ["index.commoncrawl.org", "data.commoncrawl.org"],
        "canonical_database_written": False, "excluded_indexes": sorted(EXCLUDED_INDEXES),
        "selected_indexes": indexes, "index_requests": http.requests["index"],
        "index_request_limit": max_index_requests, "range_requests": range_requests,
        "range_request_limit": max_range_requests, "single_threaded": True,
        "interval_seconds": interval, "index_audit": index_audit,
        "exact_dated_candidates": len(all_candidates), "prioritized_queue": len(queue),
        "screened_rows": len(ledger), "importable_records": len(records_out),
        "importable_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in records_out).items())),
        "failure_rows": len(failures), "blocked_template_categories": sorted(blocked_categories),
        "continuation_rows": len(continuation), "raw_audit": dict(raw_audit),
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-index-requests", type=int, default=10)
    parser.add_argument("--max-range-requests", type=int, default=90)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    print(json.dumps(run(
        root=args.root.resolve(), max_index_requests=args.max_index_requests,
        max_range_requests=args.max_range_requests, interval=args.interval,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
