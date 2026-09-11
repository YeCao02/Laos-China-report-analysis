"""Discover and screen unqueried 2014--2019 Pasaxon Common Crawl indexes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.error import HTTPError

from .commoncrawl import build_index_query, extract_archive_http_payload, parse_index_response
from .commoncrawl_pasaxon import _canonical_pasaxon_url, _entity_lao_html, _is_contaminated_template
from .commoncrawl_pasaxon_round5 import (
    COLLINFO_URL,
    PoliteHTTP,
    atomic_bytes,
    category_key,
    load_coverage,
    load_existing_urls,
    read_ndjson,
    utc_now,
    write_ndjson,
)
from .commoncrawl_pasaxon_round6 import strict_china_related
from .wayback import parse_direct_pasaxon_article, pasaxon_url_identity


STAGING_NAME = "commoncrawl_pasaxon_round7"
PRIOR_STAGING = ("commoncrawl_pasaxon_round5", "commoncrawl_pasaxon_round6", STAGING_NAME)
BASE_EXCLUDED = {
    "CC-MAIN-2017-51", "CC-MAIN-2018-51", "CC-MAIN-2019-51", "CC-MAIN-2020-50",
}


def queried_indexes(
    root: Path, *, prior_staging: tuple[str, ...] = PRIOR_STAGING,
    base_excluded: set[str] | None = None,
) -> set[str]:
    excluded = set(BASE_EXCLUDED if base_excluded is None else base_excluded)
    for name in prior_staging:
        path = root / "data/staging/archive_ocr" / name / "manifest.json"
        if not path.is_file():
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        excluded.update(str(value) for value in manifest.get("selected_indexes", []) if value)
        excluded.update(str(value) for value in manifest.get("excluded_indexes", []) if value)
    return excluded


def choose_unqueried_indexes(
    collinfo: list[dict[str, object]], *, excluded: set[str], slots: int,
    allowed_years: tuple[int, ...] = tuple(range(2014, 2020)),
) -> list[str]:
    """Choose up to two unqueried indexes/year, balanced across years."""

    by_year: dict[int, list[str]] = defaultdict(list)
    for row in collinfo:
        index = str(row.get("id") or "")
        # Common Crawl's legacy 2012 collection is named ``CC-MAIN-2012``
        # without a week suffix.  Treat it as week zero so a bounded historical
        # pass can query it without inferring any article date from the capture.
        match = re.fullmatch(r"CC-MAIN-(20\d{2})(?:-(\d+))?", index)
        if not match or index in excluded:
            continue
        year = int(match.group(1))
        if year in allowed_years:
            by_year[year].append(index)
    for values in by_year.values():
        values.sort(
            key=lambda item: int(item.rsplit("-", 1)[1])
            if re.fullmatch(r"CC-MAIN-20\d{2}-\d+", item) else 0,
            reverse=True,
        )
    chosen: list[str] = []
    depth = 0
    while len(chosen) < slots and any(depth < len(by_year[year]) for year in allowed_years):
        for year in allowed_years:
            if depth < len(by_year[year]) and len(chosen) < slots:
                chosen.append(by_year[year][depth])
        depth += 1
    return chosen


def dated_candidates_for_years(
    records: list[object], *, source_index: str, allowed_years: tuple[int, ...],
    non_php_only: bool = False,
) -> list[dict[str, object]]:
    """Keep captures whose original URL independently supplies a full date."""

    candidates: list[dict[str, object]] = []
    for record in records:
        url = str(record.url)
        lowered = url.casefold()
        if non_php_only and (".php" in lowered or "?" in url):
            continue
        identity = pasaxon_url_identity(url)
        if not identity or not identity[0]:
            continue
        published, _, slot = identity
        if int(published[:4]) not in allowed_years:
            continue
        candidates.append({
            "source_index": source_index, "published_at": published,
            "year_month": published[:7], "slot": slot, "original_url": url,
            "canonical_url": _canonical_pasaxon_url(url),
            "timestamp": record.timestamp, "filename": record.filename,
            "offset": record.offset, "length": record.length,
            "warc_url": record.warc_url, "range_header": record.range_header,
            "digest": record.digest,
        })
    return candidates


def prioritize_candidates(
    rows: list[dict[str, object]], *, counts: dict[str, int], existing: set[str],
) -> list[dict[str, object]]:
    best: dict[str, dict[str, object]] = {}
    for row in rows:
        canonical = str(row["canonical_url"])
        month = str(row["year_month"])
        if canonical in existing or counts.get(month, 0) >= 2:
            continue
        old = best.get(canonical)
        if old is None or str(row["timestamp"]) > str(old["timestamp"]):
            best[canonical] = row
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in best.values():
        groups[str(row["year_month"])].append(row)
    for group in groups.values():
        group.sort(key=lambda row: (str(row["published_at"]), int(row["slot"]), str(row["canonical_url"])))
    months = sorted(groups, key=lambda month: (counts.get(month, 0), month))
    queue: list[dict[str, object]] = []
    depth = 0
    while any(depth < len(groups[month]) for month in months):
        for month in months:
            if depth < len(groups[month]):
                queue.append(groups[month][depth])
        depth += 1
    return queue


def run(
    *, root: Path, max_index_requests: int = 10, max_range_requests: int = 90,
    interval: float = 1.0, staging_name: str = STAGING_NAME,
    allowed_years: tuple[int, ...] = tuple(range(2014, 2020)),
    prior_staging: tuple[str, ...] = PRIOR_STAGING,
    base_excluded: set[str] | None = None, non_php_only: bool = False,
) -> dict[str, object]:
    staging = root / "data/staging/archive_ocr" / staging_name
    index_dir, raw_dir, text_dir = staging / "indexes", staging / "raw", staging / "text"
    ledger_path, records_path = staging / "screened.ndjson", staging / "records.ndjson"
    failures_path, pending_path = staging / "failures.ndjson", staging / "continuation_queue.ndjson"
    http = PoliteHTTP(interval=interval)
    ledger, records, failures = read_ndjson(ledger_path), read_ndjson(records_path), read_ndjson(failures_path)
    excluded = queried_indexes(root, prior_staging=prior_staging, base_excluded=base_excluded)

    collinfo_payload = http.get(COLLINFO_URL, kind="index")
    atomic_bytes(index_dir / "collinfo.json", collinfo_payload)
    indexes = choose_unqueried_indexes(
        json.loads(collinfo_payload), excluded=excluded, slots=max_index_requests - 1,
        allowed_years=allowed_years,
    )
    candidates: list[dict[str, object]] = []
    index_audit: list[dict[str, object]] = []
    for index in indexes:
        query = build_index_query(
            index, "pasaxon.org.la", filters=("status:200", "mime:text/html"),
            collapse="urlkey", match_type="domain", page=0, page_size=10000,
        )
        try:
            payload = http.get(query, kind="index")
            output = index_dir / f"{index}.ndjson"; atomic_bytes(output, payload)
            parsed = parse_index_response(payload)
            dated = dated_candidates_for_years(
                parsed, source_index=index, allowed_years=allowed_years,
                non_php_only=non_php_only,
            )
            candidates.extend(dated)
            index_audit.append({
                "index": index, "status": "complete", "query_url": query,
                "response_file": str(output.relative_to(root)),
                "response_sha256": hashlib.sha256(payload).hexdigest(),
                "rows": len(parsed), "exact_dated_candidates": len(dated),
            })
        except Exception as exc:
            failure = {"stage": "index", "source_index": index, "url": query, "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now()}
            failures.append(failure)
            index_audit.append({"index": index, "status": "failed", "query_url": query, "error": str(exc)})
            write_ndjson(failures_path, failures)

    counts, _ = load_coverage(root)
    existing = load_existing_urls(root)
    for prior in prior_staging:
        for row in read_ndjson(root / "data/staging/archive_ocr" / prior / "screened.ndjson"):
            if row.get("canonical_url"):
                existing.add(str(row["canonical_url"]))
    queue = prioritize_candidates(candidates, counts=counts, existing=existing)
    matched = Counter(str(row["published_at"])[:7] for row in records)
    terminal = {str(row.get("canonical_url")) for row in ledger if row.get("canonical_url")}
    pending: list[dict[str, object]] = []
    blocked_categories: set[str] = set()
    stopped_reason = "queue_exhausted"

    for position, row in enumerate(queue):
        month, canonical = str(row["year_month"]), str(row["canonical_url"])
        category = category_key(str(row["original_url"]))
        if canonical in terminal:
            continue
        if counts.get(month, 0) + matched[month] >= 2:
            pending.append({**row, "queue_status": "month_quota_met"}); continue
        if category in blocked_categories:
            pending.append({**row, "queue_status": "blocked_contaminated_template_path"}); continue
        if http.requests["range"] >= max_range_requests:
            pending.extend({**item, "queue_status": "range_budget_exhausted"} for item in queue[position:])
            stopped_reason = "range_budget_exhausted"; break
        raw_member: bytes | None = None
        try:
            raw_member = http.get(str(row["warc_url"]), kind="range", range_header=str(row["range_header"]))
            if len(raw_member) != int(row["length"]):
                raise ValueError(f"range length mismatch: expected {row['length']}, got {len(raw_member)}")
            payload = extract_archive_http_payload(raw_member)
            raw_sha, payload_sha = hashlib.sha256(raw_member).hexdigest(), hashlib.sha256(payload).hexdigest()
            kind = "warc" if ".warc" in str(row["filename"]).casefold() else "arc"
            raw_path = raw_dir / month / f"{raw_sha}.{kind}.gz"; atomic_bytes(raw_path, raw_member)
            article = parse_direct_pasaxon_article(_entity_lao_html(payload), str(row["original_url"]))
            if _is_contaminated_template(article.title, article.body):
                blocked_categories.add(category)
                failure = {**row, "stage": "range", "status": "contaminated_template", "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha, "payload_sha256": payload_sha, "error": "compromised generic SEO template", "checked_at": utc_now()}
                failures.append(failure); ledger.append(failure)
            else:
                related, hits = strict_china_related(f"{article.title}\n{article.body}")
                body_bytes = article.body.encode("utf-8"); body_sha = hashlib.sha256(body_bytes).hexdigest()
                text_path = text_dir / month / f"{body_sha}.txt"; atomic_bytes(text_path, body_bytes)
                ledger_row = {**row, "status": "china_match" if related else "not_china", "hits": hits, "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha, "payload_sha256": payload_sha, "body_file": str(text_path.relative_to(root)), "content_sha256": body_sha, "checked_at": utc_now()}
                ledger.append(ledger_row)
                if related:
                    records.append({
                        "source_code": "pasaxon_archive", "language": "lo", "title_original": article.title,
                        "published_at": article.published_date, "date_precision": "url_day", "body_original": article.body,
                        "body_method": "commoncrawl_warc_pasaxon_html", "matched_queries": hits,
                        "original_url": row["original_url"], "archive_url": row["warc_url"], "evidence_grade": "B1",
                        "retrieval_tier": "T1_DIRECT_CHINA", "content_sha256": body_sha,
                        "body_file": str(text_path.relative_to(root)), "raw_file": str(raw_path.relative_to(root)),
                        "raw_sha256": raw_sha, "payload_sha256": payload_sha, "retrieved_at": utc_now(),
                        "capture_timestamp": row["timestamp"], "source_index": row["source_index"],
                        "warc": {key: row[key] for key in ("warc_url", "filename", "offset", "length", "range_header")},
                        "parser": f"{staging_name}_v1",
                    }); matched[month] += 1
            write_ndjson(ledger_path, ledger); write_ndjson(records_path, records); write_ndjson(failures_path, failures)
        except Exception as exc:
            failure = {**row, "stage": "range", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now()}
            if raw_member is not None:
                failure["raw_sha256"] = hashlib.sha256(raw_member).hexdigest()
            failures.append(failure); ledger.append(failure)
            write_ndjson(failures_path, failures); write_ndjson(ledger_path, ledger)
            if isinstance(exc, HTTPError) and exc.code in {401, 403, 429}:
                pending.extend({**item, "queue_status": f"stopped_http_{exc.code}"} for item in queue[position + 1:])
                stopped_reason = f"http_{exc.code}"; break

    write_ndjson(pending_path, pending); write_ndjson(failures_path, failures)
    audit = Counter()
    for row in ledger:
        if not row.get("raw_file"): continue
        path = root / str(row["raw_file"])
        if not path.is_file(): continue
        raw = path.read_bytes(); audit["raw_exists"] += 1
        audit["raw_sha_matches"] += hashlib.sha256(raw).hexdigest() == row.get("raw_sha256")
        try:
            payload = extract_archive_http_payload(raw); audit["payload_extracts"] += 1
            audit["payload_sha_matches"] += hashlib.sha256(payload).hexdigest() == row.get("payload_sha256")
        except ValueError: pass
        if row.get("body_file"):
            body = root / str(row["body_file"]); audit["body_exists"] += body.is_file()
            if body.is_file(): audit["body_sha_matches"] += hashlib.sha256(body.read_bytes()).hexdigest() == row.get("content_sha256")
    manifest = {
        "generated_at": utc_now(), "network_scope": ["index.commoncrawl.org", "data.commoncrawl.org"],
        "canonical_database_written": False, "single_threaded": True, "interval_seconds": interval,
        "allowed_years": list(allowed_years), "non_php_only": non_php_only,
        "excluded_indexes": sorted(excluded), "selected_indexes": indexes,
        "index_requests": http.requests["index"], "index_request_limit": max_index_requests,
        "range_requests": http.requests["range"], "range_request_limit": max_range_requests,
        "index_audit": index_audit, "exact_dated_candidates": len(candidates), "prioritized_rows": len(queue),
        "screened_rows": len(ledger), "importable_records": len(records),
        "importable_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in records).items())),
        "failure_rows": len(failures), "continuation_rows": len(pending),
        "blocked_template_categories": sorted(blocked_categories), "stopped_reason": stopped_reason,
        "hash_audit": dict(audit),
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
    print(json.dumps(run(root=args.root.resolve(), max_index_requests=args.max_index_requests, max_range_requests=args.max_range_requests, interval=args.interval), ensure_ascii=False))


if __name__ == "__main__":
    main()
