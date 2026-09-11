"""Bounded Common Crawl discovery for the Pasaxon 2021--2022 site gap.

The capture year is used only to choose Common Crawl indexes.  It is never
mapped to a publication date.  Opaque PHP/current-site article URLs remain
discovery candidates until a downloaded page supplies its own complete date.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .commoncrawl import build_index_query, extract_archive_http_payload, parse_index_response
from .commoncrawl_pasaxon import (
    _canonical_pasaxon_url,
    _entity_lao_html,
    _is_contaminated_template,
    _repair_title,
)
from .commoncrawl_pasaxon_round5 import (
    COLLINFO_URL,
    PoliteHTTP,
    atomic_bytes,
    read_ndjson,
    utc_now,
    write_ndjson,
)
from .commoncrawl_pasaxon_round6 import strict_china_related
from .commoncrawl_pasaxon_round7 import choose_unqueried_indexes
from .wayback import parse_direct_pasaxon_article, pasaxon_url_identity


STAGING_NAME = "commoncrawl_pasaxon_2021_2022"
ALLOWED_YEARS = (2021, 2022)
ACTION_PRIORITY = {
    "cooperation-detail": 0,
    "leader-detail": 1,
    "investment-detail": 2,
    "economic-detail": 3,
    "forigner-detail": 4,
    "politic-detail": 5,
    "politic1-detail": 5,
    "pasaxon-detail": 6,
}


def article_url_kind(url: str) -> str | None:
    """Classify a URL that is worth body-level date/content verification."""

    identity = pasaxon_url_identity(url)
    if identity and identity[0] and int(identity[0][:4]) in ALLOWED_YEARS:
        return "exact_dated"
    parsed = urlparse(url)
    path = parsed.path.casefold().rstrip("/")
    query = parse_qs(parsed.query)
    if path.endswith("pasaxon-detail.php") and str(query.get("p_id", [""])[0]).isdigit():
        return "php_detail_page_date_required"
    if re.search(r"-\d+\.html$", path) and not path.startswith(("/search/", "/tags/")):
        return "slug_article_page_date_required"
    return None


def candidate_rows(records: list[object], *, source_index: str) -> list[dict[str, object]]:
    """Deduplicate article-shaped captures while preserving WARC locators."""

    best: dict[str, dict[str, object]] = {}
    for record in records:
        kind = article_url_kind(str(record.url))
        if not kind:
            continue
        canonical = _canonical_pasaxon_url(str(record.url))
        row = {
            "source_index": source_index,
            "url_kind": kind,
            "original_url": str(record.url),
            "canonical_url": canonical,
            "capture_timestamp": str(record.timestamp),
            "filename": str(record.filename),
            "offset": int(record.offset),
            "length": int(record.length),
            "warc_url": str(record.warc_url),
            "range_header": str(record.range_header),
            "digest": record.digest,
        }
        prior = best.get(canonical)
        if prior is None or row["capture_timestamp"] > prior["capture_timestamp"]:
            best[canonical] = row
    return sorted(best.values(), key=lambda row: (str(row["url_kind"]), str(row["canonical_url"])))


def _php_identity(url: str) -> tuple[int, str] | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    raw_id = str(query.get("p_id", [""])[0])
    if not parsed.path.casefold().endswith("pasaxon-detail.php") or not raw_id.isdigit():
        return None
    return int(raw_id), str(query.get("act", [""])[0]).casefold()


def build_replay_queue(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Prefer China-rich sections and interleave capture months.

    Capture month controls request ordering only.  It is never persisted as a
    publication date; the downloaded page must provide that date itself.
    """

    best_by_id: dict[str, dict[str, object]] = {}
    for row in rows:
        php = _php_identity(str(row["original_url"]))
        identity = f"php:{php[0]}" if php else str(row["canonical_url"])
        action = php[1] if php else ""
        rank = ACTION_PRIORITY.get(action, 20)
        prior = best_by_id.get(identity)
        prior_php = _php_identity(str(prior["original_url"])) if prior else None
        prior_rank = ACTION_PRIORITY.get(prior_php[1], 20) if prior_php else 20
        if prior is None or (rank, -int(str(row["capture_timestamp"]))) < (
            prior_rank, -int(str(prior["capture_timestamp"])),
        ):
            best_by_id[identity] = row
    groups: dict[str, list[dict[str, object]]] = {}
    for row in best_by_id.values():
        month = str(row["capture_timestamp"])[:6]
        groups.setdefault(month, []).append(row)
    for group in groups.values():
        group.sort(key=lambda row: (
            ACTION_PRIORITY.get((_php_identity(str(row["original_url"])) or (0, ""))[1], 20),
            (_php_identity(str(row["original_url"])) or (0, ""))[0],
            str(row["canonical_url"]),
        ))
    queue: list[dict[str, object]] = []
    depth = 0
    months = sorted(groups)
    while any(depth < len(groups[month]) for month in months):
        for month in months:
            if depth < len(groups[month]):
                queue.append(groups[month][depth])
        depth += 1
    return queue


def replay(
    *, root: Path, max_range_requests: int = 120, interval: float = 1.0,
    staging_name: str = STAGING_NAME,
) -> dict[str, object]:
    """Replay a bounded candidate queue and keep only page-dated China bodies."""

    staging = root / "data/staging/archive_ocr" / staging_name
    candidates = read_ndjson(staging / "candidates.ndjson")
    queue = build_replay_queue(candidates)
    ledger_path = staging / "screened.ndjson"
    records_path = staging / "records.ndjson"
    failures_path = staging / "replay_failures.ndjson"
    pending_path = staging / "continuation_queue.ndjson"
    ledger = read_ndjson(ledger_path)
    records = read_ndjson(records_path)
    failures = read_ndjson(failures_path)
    # A manually interrupted pre-circuit-breaker run recorded template
    # contamination as a generic failure.  Normalize it offline on resume.
    for row in (*ledger, *failures):
        if "contaminated generic SEO template" in str(row.get("error")):
            row["status"] = "contaminated_template"
    write_ndjson(ledger_path, ledger)
    write_ndjson(failures_path, failures)
    terminal_ids = {
        str(row.get("candidate_identity")) for row in ledger if row.get("candidate_identity")
    }
    matched = Counter(str(row.get("published_at", ""))[:7] for row in records)
    http = PoliteHTTP(interval=interval)
    pending: list[dict[str, object]] = []
    stop_reason = "queue_exhausted"
    consecutive_contaminated = 0
    contaminated_indexes: set[str] = set()

    for position, row in enumerate(queue):
        php = _php_identity(str(row["original_url"]))
        identity = f"php:{php[0]}" if php else str(row["canonical_url"])
        if identity in terminal_ids:
            continue
        if http.requests["range"] >= max_range_requests:
            pending.extend({**item, "queue_status": "range_budget_exhausted"} for item in queue[position:])
            stop_reason = "range_budget_exhausted"
            break
        raw_path: Path | None = None
        raw_sha: str | None = None
        payload_sha: str | None = None
        try:
            raw_member = http.get(
                str(row["warc_url"]), kind="range", range_header=str(row["range_header"]),
            )
            if len(raw_member) != int(row["length"]):
                raise ValueError(f"range length mismatch: expected {row['length']}, got {len(raw_member)}")
            payload = extract_archive_http_payload(raw_member)
            raw_sha = hashlib.sha256(raw_member).hexdigest()
            payload_sha = hashlib.sha256(payload).hexdigest()
            member_kind = "warc" if ".warc" in str(row["filename"]).casefold() else "arc"
            raw_path = staging / "raw" / str(row["capture_timestamp"])[:6] / f"{raw_sha}.{member_kind}.gz"
            atomic_bytes(raw_path, raw_member)
            article = parse_direct_pasaxon_article(_entity_lao_html(payload), str(row["original_url"]))
            if _is_contaminated_template(article.title, article.body):
                raise ValueError("archived page is a contaminated generic SEO template")
            consecutive_contaminated = 0
            contaminated_indexes.clear()
            published = article.published_date
            if int(published[:4]) not in ALLOWED_YEARS:
                ledger.append({
                    **row, "candidate_identity": identity, "status": "out_of_scope_page_date",
                    "published_at": published, "raw_file": str(raw_path.relative_to(root)),
                    "raw_sha256": raw_sha, "payload_sha256": payload_sha, "checked_at": utc_now(),
                })
                terminal_ids.add(identity)
                write_ndjson(ledger_path, ledger)
                continue
            related, hits = strict_china_related(f"{article.title}\n{article.body}")
            month = published[:7]
            status = "china_match" if related else "not_china"
            if related and matched[month] >= 2:
                status = "quota_match_excluded"
            content_sha = hashlib.sha256(article.body.encode("utf-8")).hexdigest()
            body_path = staging / "text" / month / f"{content_sha}.txt"
            atomic_bytes(body_path, article.body.encode("utf-8"))
            ledger_row = {
                **row, "candidate_identity": identity, "status": status,
                "published_at": published, "hits": hits,
                "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                "payload_sha256": payload_sha, "body_file": str(body_path.relative_to(root)),
                "content_sha256": content_sha, "checked_at": utc_now(),
            }
            ledger.append(ledger_row)
            terminal_ids.add(identity)
            if status == "china_match":
                records.append({
                    "source_code": "pasaxon_archive", "language": "lo",
                    "title_original": _repair_title(article.title, article.body),
                    "published_at": published, "date_precision": "page_day",
                    "body_original": article.body,
                    "body_method": "commoncrawl_warc_pasaxon_php_html",
                    "matched_queries": hits, "original_url": row["original_url"],
                    "archive_url": row["warc_url"], "evidence_grade": "B1",
                    "retrieval_tier": "T1_DIRECT_CHINA", "content_sha256": content_sha,
                    "body_file": str(body_path.relative_to(root)),
                    "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha, "retrieved_at": utc_now(),
                    "capture_timestamp": row["capture_timestamp"], "source_index": row["source_index"],
                    "warc": {key: row[key] for key in (
                        "warc_url", "filename", "offset", "length", "range_header",
                    )},
                    "parser": "commoncrawl_pasaxon_2021_2022_v1",
                })
                matched[month] += 1
            write_ndjson(ledger_path, ledger)
            write_ndjson(records_path, records)
        except Exception as exc:
            contaminated = "contaminated generic SEO template" in str(exc)
            failure = {
                **row, "candidate_identity": identity, "stage": "range",
                "status": "contaminated_template" if contaminated else "failed",
                "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now(),
            }
            if raw_path is not None:
                failure.update({
                    "raw_file": str(raw_path.relative_to(root)),
                    "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha,
                })
            failures.append(failure)
            ledger.append(failure)
            terminal_ids.add(identity)
            write_ndjson(failures_path, failures)
            write_ndjson(ledger_path, ledger)
            if contaminated:
                consecutive_contaminated += 1
                contaminated_indexes.add(str(row["source_index"]))
                if consecutive_contaminated >= 5 and len(contaminated_indexes) >= 3:
                    pending.extend({**item, "queue_status": "contaminated_template_circuit_breaker"} for item in queue[position + 1:])
                    stop_reason = "contaminated_template_circuit_breaker"
                    break
            else:
                consecutive_contaminated = 0
                contaminated_indexes.clear()

    write_ndjson(pending_path, pending)
    audit = Counter()
    for row in ledger:
        raw_file = row.get("raw_file")
        if not raw_file:
            continue
        path = root / str(raw_file)
        if path.is_file():
            raw = path.read_bytes()
            audit["raw_exists"] += 1
            audit["raw_sha_matches"] += hashlib.sha256(raw).hexdigest() == row.get("raw_sha256")
            try:
                payload = extract_archive_http_payload(raw)
                audit["payload_extracts"] += 1
                audit["payload_sha_matches"] += hashlib.sha256(payload).hexdigest() == row.get("payload_sha256")
            except ValueError:
                pass
        body_file = row.get("body_file")
        if body_file:
            body_path = root / str(body_file)
            audit["body_exists"] += body_path.is_file()
            if body_path.is_file():
                audit["body_sha_matches"] += hashlib.sha256(body_path.read_bytes()).hexdigest() == row.get("content_sha256")
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["replay"] = {
        "generated_at": utc_now(), "network_scope": ["data.commoncrawl.org"],
        "range_requests": http.requests["range"], "range_request_limit": max_range_requests,
        "queue_rows": len(queue), "screened_rows": len(ledger),
        "importable_records": len(records),
        "importable_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in records).items())),
        "status_counts": dict(sorted(Counter(str(row.get("status")) for row in ledger).items())),
        "failure_rows": len(failures), "continuation_rows": len(pending),
        "stopped_reason": stop_reason, "hash_audit": dict(audit),
        "capture_date_used_as_publication_date": False,
    }
    atomic_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest["replay"]


def discover(
    *, root: Path, max_index_requests: int = 10, interval: float = 1.0,
    staging_name: str = STAGING_NAME, excluded_indexes: set[str] | None = None,
) -> dict[str, object]:
    """Query balanced 2021/2022 indexes and persist an offline replay queue."""

    if max_index_requests < 2:
        raise ValueError("max_index_requests must include collinfo plus at least one index")
    staging = root / "data/staging/archive_ocr" / staging_name
    index_dir = staging / "indexes"
    http = PoliteHTTP(interval=interval)
    collinfo_payload = http.get(COLLINFO_URL, kind="index")
    atomic_bytes(index_dir / "collinfo.json", collinfo_payload)
    indexes = choose_unqueried_indexes(
        json.loads(collinfo_payload), excluded=set(excluded_indexes or ()), slots=max_index_requests - 1,
        allowed_years=ALLOWED_YEARS,
    )
    all_candidates: dict[str, dict[str, object]] = {}
    index_audit: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for index in indexes:
        query = build_index_query(
            index, "pasaxon.org.la", filters=("status:200", "mime:text/html"),
            collapse="urlkey", match_type="domain", page=0, page_size=10000,
        )
        try:
            payload = http.get(query, kind="index")
            output = index_dir / f"{index}.ndjson"
            atomic_bytes(output, payload)
            parsed = parse_index_response(payload)
            rows = candidate_rows(parsed, source_index=index)
            for row in rows:
                canonical = str(row["canonical_url"])
                prior = all_candidates.get(canonical)
                if prior is None or str(row["capture_timestamp"]) > str(prior["capture_timestamp"]):
                    all_candidates[canonical] = row
            index_audit.append({
                "index": index, "status": "complete", "query_url": query,
                "response_file": str(output.relative_to(root)),
                "response_sha256": hashlib.sha256(payload).hexdigest(),
                "rows": len(parsed), "article_candidates": len(rows),
                "candidate_kinds": dict(sorted(Counter(str(row["url_kind"]) for row in rows).items())),
            })
        except Exception as exc:
            failure = {
                "stage": "index", "source_index": index, "url": query,
                "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now(),
            }
            failures.append(failure)
            index_audit.append({"index": index, "status": "failed", "query_url": query, "error": str(exc)})
    rows = sorted(all_candidates.values(), key=lambda row: (
        str(row["url_kind"]), str(row["capture_timestamp"]), str(row["canonical_url"]),
    ))
    write_ndjson(staging / "candidates.ndjson", rows)
    write_ndjson(staging / "failures.ndjson", failures)
    manifest = {
        "generated_at": utc_now(),
        "network_scope": ["index.commoncrawl.org"],
        "canonical_database_written": False,
        "capture_year_is_not_publication_date": True,
        "single_threaded": True,
        "interval_seconds": interval,
        "allowed_index_years": list(ALLOWED_YEARS),
        "selected_indexes": indexes,
        "index_requests": http.requests["index"],
        "index_request_limit": max_index_requests,
        "index_audit": index_audit,
        "unique_article_candidates": len(rows),
        "candidate_kinds": dict(sorted(Counter(str(row["url_kind"]) for row in rows).items())),
        "failure_rows": len(failures),
        "next_step": "body replay must verify a complete page date in 2021 or 2022 before import",
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def rebuild_candidates_from_saved_indexes(
    *, root: Path, source_staging: str = STAGING_NAME,
    target_staging: str = f"{STAGING_NAME}_earliest", prefer: str = "earliest",
) -> dict[str, object]:
    """Create a fresh replay queue from saved indexes without network access."""

    if prefer not in {"earliest", "latest"}:
        raise ValueError("prefer must be earliest or latest")
    source = root / "data/staging/archive_ocr" / source_staging
    target = root / "data/staging/archive_ocr" / target_staging
    source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    merged: dict[str, dict[str, object]] = {}
    index_audit: list[dict[str, object]] = []
    for item in source_manifest.get("index_audit", []):
        if item.get("status") != "complete" or not item.get("response_file"):
            continue
        path = root / str(item["response_file"])
        payload = path.read_bytes()
        rows = candidate_rows(parse_index_response(payload), source_index=str(item["index"]))
        for row in rows:
            canonical = str(row["canonical_url"])
            prior = merged.get(canonical)
            if prior is None or (
                prefer == "earliest" and str(row["capture_timestamp"]) < str(prior["capture_timestamp"])
            ) or (
                prefer == "latest" and str(row["capture_timestamp"]) > str(prior["capture_timestamp"])
            ):
                merged[canonical] = row
        index_audit.append({
            "index": item["index"], "source_file": str(path.relative_to(root)),
            "source_sha256": hashlib.sha256(payload).hexdigest(), "article_candidates": len(rows),
        })
    rows = sorted(merged.values(), key=lambda row: (
        str(row["url_kind"]), str(row["capture_timestamp"]), str(row["canonical_url"]),
    ))
    write_ndjson(target / "candidates.ndjson", rows)
    write_ndjson(target / "failures.ndjson", [])
    manifest = {
        "generated_at": utc_now(), "network_scope": [], "offline_rebuild": True,
        "source_staging": source_staging, "capture_preference": prefer,
        "canonical_database_written": False, "capture_year_is_not_publication_date": True,
        "selected_indexes": [row["index"] for row in index_audit], "index_audit": index_audit,
        "unique_article_candidates": len(rows),
        "candidate_kinds": dict(sorted(Counter(str(row["url_kind"]) for row in rows).items())),
    }
    atomic_bytes(target / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-index-requests", type=int, default=10)
    parser.add_argument("--max-range-requests", type=int, default=120)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--mode", choices=("discover", "replay", "rebuild-earliest"), default="discover")
    parser.add_argument("--staging-name", default=STAGING_NAME)
    parser.add_argument("--source-staging", default=STAGING_NAME)
    parser.add_argument("--exclude-indexes-from")
    args = parser.parse_args(argv)
    excluded: set[str] = set()
    if args.exclude_indexes_from:
        prior = args.root.resolve() / "data/staging/archive_ocr" / args.exclude_indexes_from / "manifest.json"
        excluded.update(json.loads(prior.read_text(encoding="utf-8")).get("selected_indexes", []))
    if args.mode == "discover":
        result = discover(
            root=args.root.resolve(), max_index_requests=args.max_index_requests, interval=args.interval,
            staging_name=args.staging_name, excluded_indexes=excluded,
        )
    elif args.mode == "rebuild-earliest":
        result = rebuild_candidates_from_saved_indexes(
            root=args.root.resolve(), source_staging=args.source_staging,
            target_staging=args.staging_name, prefer="earliest",
        )
    else:
        result = replay(
            root=args.root.resolve(), max_range_requests=args.max_range_requests,
            interval=args.interval, staging_name=args.staging_name,
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
