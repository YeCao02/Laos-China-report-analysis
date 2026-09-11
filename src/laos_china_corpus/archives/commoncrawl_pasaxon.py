"""Bounded Pasaxon recovery from Common Crawl ARC/WARC range records."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .commoncrawl import CommonCrawlRecord, extract_archive_http_payload, fetch_archive_payload
from .wayback import is_lao_china_title, old_pasaxon_url_parts, parse_direct_pasaxon_article


def _append(path: Path, row: dict[str, object]) -> None:
    """Atomically replace a small NDJSON ledger after appending one row."""

    path.parent.mkdir(parents=True, exist_ok=True)
    prior = path.read_bytes() if path.exists() else b""
    line = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(prior)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_ndjson(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_ndjson(path: Path, rows: list[dict[str, object]]) -> None:
    payload = b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for row in rows
    )
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


def _repair_title(title: str, body: str) -> str:
    clean = title.strip()
    lao_count = sum("\u0e80" <= char <= "\u0eff" for char in clean)
    # The old Word-generated pages frequently declare windows-1252 while the
    # title bytes were decoded as CJK mojibake.  A replacement-character check
    # alone does not detect that form of corruption.
    if not clean or clean.count("\ufffd") / max(len(clean), 1) > 0.2 or lao_count < 3:
        return next((line.strip() for line in body.splitlines() if line.strip()), title)
    return clean


def _entity_lao_html(payload: bytes) -> bytes:
    """Word HTML uses ASCII Lao numeric entities despite a false windows-1252 header."""

    text = payload.decode("latin1", errors="replace")
    if "&#" not in text:
        return payload
    return html.unescape(text).encode("utf-8")


def _canonical_pasaxon_url(url: str) -> str:
    """Normalize host aliases solely for deduplication, retaining the source URL."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path).replace("//", "/").rstrip("/").casefold()
    if path.endswith("pasaxon-detail.php"):
        query = parse_qs(parsed.query)
        php_id = query.get("p_id", [""])[0]
        action = query.get("act", [""])[0].casefold()
        return f"{host}{path}?p_id={php_id}&act={action}"
    return f"{host}{path}"


def _is_php_article(url: str) -> bool:
    parsed = urlparse(url)
    php_id = parse_qs(parsed.query).get("p_id", [None])[0]
    return parsed.path.casefold().endswith("pasaxon-detail.php") and bool(
        php_id and str(php_id).isdigit()
    )


def _china_related(text: str) -> tuple[bool, list[str]]:
    """Screen article text with direct Lao and English China terms."""

    related, hits = is_lao_china_title(text)
    folded = text.casefold()
    english_terms = (
        "china", "chinese", "lao-china", "china-laos", "laos-china",
        "xi jinping", "beijing", "yunnan", "guangxi", "kunming",
    )
    for term in english_terms:
        if term in folded and term not in hits:
            hits.append(term)
    return related or bool(hits), hits


def _is_contaminated_template(title: str, body: str) -> bool:
    """Detect the compromised generic template seen in late 2020 captures."""

    folded = f"{title}\n{body}".casefold()
    spam_markers = ("agen togel", "daftar sbobet", "bertaruh online", "lapak online")
    return "toggle navigation" in folded and sum(marker in folded for marker in spam_markers) >= 2


def record_from_candidate(row: dict[str, object], *, source_index: str | None = None) -> CommonCrawlRecord:
    """Restore one CommonCrawlRecord from a saved HistoricalEvidenceCandidate."""

    warc = row.get("warc")
    if not isinstance(warc, dict):
        raise ValueError("saved Common Crawl candidate has no warc locator")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    timestamp = str(warc.get("capture_timestamp") or row.get("capture_timestamp") or "")
    extra = {
        key: value for key, value in metadata.items()
        if key not in {"status", "mime", "digest", "urlkey", "languages"}
    }
    if source_index:
        extra["source_index"] = source_index
    return CommonCrawlRecord(
        url=str(row.get("original_url") or ""),
        timestamp=timestamp,
        filename=str(warc.get("filename") or ""),
        offset=int(warc.get("offset")),
        length=int(warc.get("length")),
        status=int(metadata["status"]) if metadata.get("status") not in (None, "") else None,
        mime=str(metadata["mime"]) if metadata.get("mime") is not None else None,
        digest=str(metadata["digest"]) if metadata.get("digest") is not None else None,
        urlkey=str(metadata["urlkey"]) if metadata.get("urlkey") is not None else None,
        languages=str(metadata["languages"]) if metadata.get("languages") is not None else None,
        extra=extra,
    )


def load_candidate_file(path: Path) -> tuple[str, list[CommonCrawlRecord]]:
    """Load a previously saved discovery file without calling an index API."""

    match = re.search(r"(CC-MAIN-\d{4}-\d+)", path.name)
    source_index = match.group(1) if match else path.stem
    rows = _read_ndjson(path)
    return source_index, [record_from_candidate(row, source_index=source_index) for row in rows]


def prioritize_records(
    records: list[CommonCrawlRecord], *, limit: int, allow_page_date: bool = False,
) -> tuple[list[CommonCrawlRecord], dict[str, int]]:
    """Deduplicate dated articles and round-robin months before the scan cap."""

    by_month: dict[str, list[tuple[str, int, str, CommonCrawlRecord]]] = {}
    unsupported = 0
    duplicates = 0
    seen: set[str] = set()
    for record in records:
        parts = old_pasaxon_url_parts(record.url)
        if not parts and not (allow_page_date and _is_php_article(record.url)):
            unsupported += 1
            continue
        if parts:
            published, slot = parts
            group = published[:7]
        else:
            parsed = urlparse(record.url)
            slot = int(parse_qs(parsed.query)["p_id"][0])
            published = "page-date-pending"
            group = "page-date-pending"
        canonical = _canonical_pasaxon_url(record.url)
        if canonical in seen:
            duplicates += 1
            continue
        seen.add(canonical)
        by_month.setdefault(group, []).append((published, slot, canonical, record))
    for items in by_month.values():
        items.sort(key=lambda item: (item[0], item[1], item[2]))
    selected: list[CommonCrawlRecord] = []
    depth = 0
    while len(selected) < limit:
        added = False
        for month in sorted(by_month):
            if depth < len(by_month[month]):
                selected.append(by_month[month][depth][3])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        depth += 1
    return selected, {
        "input_rows": len(records), "dated_unique": len(seen),
        "unsupported_url": unsupported, "duplicate_url": duplicates,
        "selected_for_scan": len(selected),
    }


def collect_from_records(
    *,
    root: Path,
    records: list[CommonCrawlRecord],
    staging_name: str = "commoncrawl_pasaxon_2012",
    interval: float = 1.0,
    max_candidates: int | None = None,
    max_matches_per_month: int = 2,
    source_index: str | None = None,
    allow_page_date: bool = False,
    fetcher=fetch_archive_payload,
) -> dict[str, object]:
    """Screen dated old-site records once, with an atomic per-URL ledger."""

    staging = root / "data" / "staging" / "archive_ocr" / staging_name
    raw_dir = staging / "raw"
    records_path = staging / "records.ndjson"
    ledger_path = staging / "screened.ndjson"
    failures_path = staging / "failures.ndjson"
    ledger_rows = _read_ndjson(ledger_path)
    screened = {
        str(row.get("canonical_url") or _canonical_pasaxon_url(str(row["url"])))
        for row in ledger_rows
        if row.get("status") in {
            "china_match", "not_china", "quota_match_excluded", "unverifiable_date",
            "contaminated_template",
        }
    }
    matched_by_month = Counter(
        str(row["month"]) for row in ledger_rows if row.get("status") == "china_match"
    )
    conn = sqlite3.connect(f"file:{(root / 'data/corpus.sqlite3').resolve().as_posix()}?mode=ro", uri=True)
    try:
        existing_urls = {
            _canonical_pasaxon_url(str(row[0]))
            for row in conn.execute("SELECT original_url FROM articles WHERE original_url IS NOT NULL")
        }
    finally:
        conn.close()
    stats: Counter[str] = Counter()
    by_month: Counter[str] = Counter()
    last_request = 0.0
    for record in records:
        if max_candidates is not None and stats["range_requests"] >= max_candidates:
            stats["scan_cap_reached"] += 1
            break
        parts = old_pasaxon_url_parts(record.url)
        page_date_pending = not parts and allow_page_date and _is_php_article(record.url)
        if not parts and not page_date_pending:
            stats["unsupported_url"] += 1
            continue
        published = parts[0] if parts else None
        month = published[:7] if published else "page-date-pending"
        canonical_url = _canonical_pasaxon_url(record.url)
        if published and matched_by_month[month] >= max_matches_per_month:
            stats["month_quota_skipped"] += 1
            continue
        if canonical_url in screened or canonical_url in existing_urls:
            stats["skipped"] += 1
            continue
        delay = interval - (time.monotonic() - last_request)
        if delay > 0:
            time.sleep(delay)
        stats["range_requests"] += 1
        raw_path: Path | None = None
        raw_sha: str | None = None
        try:
            raw_member, body_payload = fetcher(record)
            last_request = time.monotonic()
            raw_sha = hashlib.sha256(raw_member).hexdigest()
            payload_sha = hashlib.sha256(body_payload).hexdigest()
            member_kind = "warc" if ".warc" in record.filename.casefold() else "arc"
            raw_path = raw_dir / month / f"{raw_sha}.{member_kind}.gz"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw_member)
            article = parse_direct_pasaxon_article(_entity_lao_html(body_payload), record.url)
            if _is_contaminated_template(article.title, article.body):
                raise ValueError("archived page is a contaminated generic SEO template")
            month = article.published_date[:7]
            related, hits = _china_related(f"{article.title}\n{article.body}")
            quota_full = related and matched_by_month[month] >= max_matches_per_month
            status = "quota_match_excluded" if quota_full else ("china_match" if related else "not_china")
            if related:
                if quota_full:
                    stats["month_quota_excluded_after_fetch"] += 1
                    _append(ledger_path, {
                        "url": record.url, "canonical_url": canonical_url, "month": month,
                        "status": status, "hits": hits, "raw_file": str(raw_path.relative_to(root)),
                        "raw_sha256": raw_sha, "payload_sha256": payload_sha,
                        "warc": record.warc_locator(),
                        "source_index": source_index or record.extra.get("source_index"),
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    })
                    continue
                content_sha = hashlib.sha256(article.body.encode("utf-8")).hexdigest()
                _append(records_path, {
                    "source_code": "pasaxon_archive", "language": "lo",
                    "title_original": _repair_title(article.title, article.body),
                    "published_at": article.published_date,
                    "date_precision": "page_day" if page_date_pending else "url_day",
                    "body_original": article.body, "body_method": "commoncrawl_arc_pasaxon_html",
                    "matched_queries": hits, "original_url": record.url,
                    "archive_url": record.warc_url, "evidence_grade": "B1",
                    "retrieval_tier": "T1_DIRECT_CHINA", "content_sha256": content_sha,
                    "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha, "raw_member_type": member_kind,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "capture_timestamp": record.timestamp, "warc": record.warc_locator(),
                    "parser": "commoncrawl_pasaxon_arc_v2", "old_pasaxon_slot": article.slot,
                    "source_index": source_index or record.extra.get("source_index"),
                })
                by_month[month] += 1
                matched_by_month[month] += 1
                stats["matches"] += 1
            else:
                stats["not_china"] += 1
            _append(ledger_path, {
                "url": record.url, "canonical_url": canonical_url, "month": month,
                "status": status, "hits": hits,
                "raw_file": str(raw_path.relative_to(root)), "raw_sha256": raw_sha,
                "payload_sha256": payload_sha, "warc": record.warc_locator(),
                "source_index": source_index or record.extra.get("source_index"),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            last_request = time.monotonic()
            stats["failures"] += 1
            error = f"{type(exc).__name__}: {exc}"
            failure_status = (
                "unverifiable_date"
                if page_date_pending and "verifiable page date" in str(exc)
                else (
                    "contaminated_template"
                    if "contaminated generic SEO template" in str(exc)
                    else "failed"
                )
            )
            failure = {
                "url": record.url, "canonical_url": canonical_url, "month": month,
                "source_index": source_index or record.extra.get("source_index"),
                "error": error,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            if raw_path is not None and raw_path.exists():
                failure["raw_file"] = str(raw_path.relative_to(root))
                failure["raw_sha256"] = raw_sha
                failure["warc"] = record.warc_locator()
            _append(failures_path, failure)
            _append(ledger_path, {**failure, "status": failure_status})
    result = {
        "screened_new": stats["matches"] + stats["not_china"] + stats["failures"],
        "range_requests": stats["range_requests"], "matches": stats["matches"],
        "matches_by_month": dict(sorted(by_month.items())),
        "not_china": stats["not_china"], "failures": stats["failures"],
        "skipped": stats["skipped"], "unsupported_url": stats["unsupported_url"],
        "month_quota_skipped": stats["month_quota_skipped"],
        "month_quota_excluded_after_fetch": stats["month_quota_excluded_after_fetch"],
        "scan_cap_reached": bool(stats["scan_cap_reached"]),
        "source_index": source_index, "output": str(staging),
    }
    (staging / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def quarantine_contaminated_staging(*, root: Path, staging_name: str) -> dict[str, int]:
    """Revalidate saved PHP members and atomically remove template false positives."""

    staging = root / "data" / "staging" / "archive_ocr" / staging_name
    records_path = staging / "records.ndjson"
    ledger_path = staging / "screened.ndjson"
    failures_path = staging / "failures.ndjson"
    records = _read_ndjson(records_path)
    ledger = _read_ndjson(ledger_path)
    by_url = {str(row["original_url"]): row for row in records}
    contaminated: dict[str, dict[str, object]] = {}
    for row in ledger:
        if row.get("source_index") != "CC-MAIN-2020-50":
            continue
        record_row = by_url.get(str(row.get("url")), {})
        raw_file = row.get("raw_file") or record_row.get("raw_file")
        if not raw_file:
            continue
        raw_path = root / str(raw_file)
        try:
            payload = extract_archive_http_payload(raw_path.read_bytes())
            article = parse_direct_pasaxon_article(_entity_lao_html(payload), str(row["url"]))
        except Exception:
            continue
        if _is_contaminated_template(article.title, article.body):
            contaminated[str(row["url"])] = {
                "raw_file": str(raw_file),
                "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            }
    if not contaminated:
        return {"quarantined": 0, "records_removed": 0}
    kept_records = [row for row in records if str(row.get("original_url")) not in contaminated]
    updated_ledger: list[dict[str, object]] = []
    existing_failures = _read_ndjson(failures_path)
    failed_urls = {str(row.get("url")) for row in existing_failures if row.get("status") == "contaminated_template"}
    now = datetime.now(timezone.utc).isoformat()
    for row in ledger:
        url = str(row.get("url"))
        if url in contaminated:
            prior_status = row.get("status")
            row = {
                **row, **contaminated[url], "prior_status": prior_status,
                "status": "contaminated_template",
                "error": "Archived response is a compromised generic SEO template; navigation keyword hit is not article evidence.",
                "revalidated_at": now,
            }
            if url not in failed_urls:
                existing_failures.append({
                    "url": url, "canonical_url": row.get("canonical_url"),
                    "month": row.get("month"), "source_index": row.get("source_index"),
                    "status": "contaminated_template", "raw_file": row.get("raw_file"),
                    "raw_sha256": row.get("raw_sha256"), "checked_at": now,
                    "error": row["error"],
                })
        updated_ledger.append(row)
    _write_ndjson(records_path, kept_records)
    _write_ndjson(ledger_path, updated_ledger)
    _write_ndjson(failures_path, existing_failures)
    return {
        "quarantined": len(contaminated),
        "records_removed": len(records) - len(kept_records),
    }


def enrich_ledger_raw_evidence(*, root: Path, staging_name: str) -> dict[str, int]:
    """Link legacy ledger rows to already-saved WARC members by target URI."""

    staging = root / "data" / "staging" / "archive_ocr" / staging_name
    ledger_path = staging / "screened.ndjson"
    rows = _read_ndjson(ledger_path)
    raw_by_url: dict[str, dict[str, object]] = {}
    for path in (staging / "raw").rglob("*.gz"):
        compressed = path.read_bytes()
        try:
            member = gzip.decompress(compressed)
        except (OSError, EOFError):
            continue
        target_match = re.search(br"(?im)^WARC-Target-URI:\s*(\S+)\s*$", member[:8192])
        if target_match:
            target = target_match.group(1).decode("utf-8", errors="replace").rstrip("\r")
        else:
            first_line = member.split(b"\n", 1)[0].strip()
            target = first_line.split(b" ", 1)[0].decode("utf-8", errors="replace")
        if not target.startswith(("http://", "https://")):
            continue
        try:
            payload = extract_archive_http_payload(compressed)
        except ValueError:
            continue
        raw_by_url[_canonical_pasaxon_url(target)] = {
            "raw_file": str(path.relative_to(root)),
            "raw_sha256": hashlib.sha256(compressed).hexdigest(),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
        }
    updated = 0
    missing = 0
    enriched: list[dict[str, object]] = []
    for row in rows:
        evidence = raw_by_url.get(str(row.get("canonical_url")))
        if evidence:
            missing_fields = {key: value for key, value in evidence.items() if not row.get(key)}
            if missing_fields:
                row = {**row, **missing_fields}
                updated += 1
        else:
            missing += 1
        enriched.append(row)
    _write_ndjson(ledger_path, enriched)
    return {"ledger_rows": len(rows), "updated": updated, "missing": missing}


def build_audit_manifest(
    *, root: Path, staging_name: str, index_files: list[Path],
    run_start_ledger_rows: int, request_limit: int,
    completion: dict[str, str], stop_reason: str,
) -> dict[str, object]:
    """Build a no-network manifest with byte-level evidence audits."""

    staging = root / "data" / "staging" / "archive_ocr" / staging_name
    ledger = _read_ndjson(staging / "screened.ndjson")
    records = _read_ndjson(staging / "records.ndjson")
    failures = _read_ndjson(staging / "failures.ndjson")
    raw_audit = Counter()
    audit_errors: list[dict[str, str]] = []
    for row in ledger:
        raw_file = row.get("raw_file")
        if not raw_file:
            audit_errors.append({"url": str(row.get("url")), "error": "missing raw_file locator"})
            continue
        path = root / str(raw_file)
        if not path.is_file():
            audit_errors.append({"url": str(row.get("url")), "error": "raw file absent"})
            continue
        raw_audit["raw_exists"] += 1
        compressed = path.read_bytes()
        if hashlib.sha256(compressed).hexdigest() == row.get("raw_sha256"):
            raw_audit["raw_sha_matches"] += 1
        try:
            payload = extract_archive_http_payload(compressed)
        except ValueError as exc:
            audit_errors.append({"url": str(row.get("url")), "error": str(exc)})
            continue
        raw_audit["member_extracts"] += 1
        if hashlib.sha256(payload).hexdigest() == row.get("payload_sha256"):
            raw_audit["payload_sha_matches"] += 1
    record_audit = Counter()
    for row in records:
        if hashlib.sha256(str(row["body_original"]).encode("utf-8")).hexdigest() == row.get("content_sha256"):
            record_audit["body_sha_matches"] += 1
        path = root / str(row["raw_file"])
        if path.is_file() and path.stat().st_size == int(row["warc"]["length"]):
            record_audit["range_length_matches"] += 1
    input_rows: list[dict[str, object]] = []
    for path in index_files:
        source_index, candidates = load_candidate_file(path)
        _, selection = prioritize_records(
            candidates, limit=120, allow_page_date=source_index.startswith("CC-MAIN-2020-")
        )
        input_rows.append({
            "source_index": source_index, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **selection,
        })
    per_index: dict[str, object] = {}
    for source_index in sorted({str(row.get("source_index")) for row in ledger}):
        index_ledger = [row for row in ledger if row.get("source_index") == source_index]
        index_records = [row for row in records if row.get("source_index") == source_index]
        per_index[source_index] = {
            "completion": completion.get(source_index, "not_started"),
            "range_requests_total": len(index_ledger),
            "status_counts": dict(sorted(Counter(str(row["status"]) for row in index_ledger).items())),
            "importable_records": len(index_records),
            "importable_months": dict(sorted(Counter(str(row["published_at"])[:7] for row in index_records).items())),
        }
    database_months: set[str] = set()
    database = root / "data" / "corpus.sqlite3"
    if database.exists():
        conn = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
        try:
            database_months = {
                str(row[0]) for row in conn.execute(
                    "SELECT DISTINCT substr(published_at,1,7) FROM articles "
                    "WHERE upper(source_code) LIKE 'PASAXON%' AND length(coalesce(body_original,''))>0"
                )
            }
        finally:
            conn.close()
    importable_by_month = Counter(str(row["published_at"])[:7] for row in records)
    manifest: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "staging_name": staging_name,
        "network_scope": ["data.commoncrawl.org"],
        "canonical_database_written": False,
        "single_threaded": True, "interval_seconds": 1.0,
        "request_limit_this_run": request_limit,
        "range_requests_this_run": len(ledger) - run_start_ledger_rows,
        "range_requests_total_staging": len(ledger),
        "stop_reason": stop_reason,
        "date_policy": {
            "dated_paths": "URL day",
            "pasaxon_detail_php": "p_id is not chronological and cannot imply publication date; require a valid DD/MM/YYYY printed in page content",
            "php_url_date_inference_reliable": False,
        },
        "quality_gate": {
            "contaminated_template_rows": sum(row.get("status") == "contaminated_template" for row in ledger),
            "contaminated_records_importable": 0,
        },
        "inputs": input_rows, "per_index": per_index,
        "ledger_rows": len(ledger), "failure_rows": len(failures),
        "importable_records": len(records),
        "importable_by_month": dict(sorted(importable_by_month.items())),
        "months_new_to_current_database": sorted(set(importable_by_month) - database_months),
        "raw_audit": {**dict(raw_audit), "expected": len(ledger), "errors": audit_errors},
        "record_audit": {**dict(record_audit), "expected": len(records)},
    }
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    manifest_path = staging / "manifest.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def collect_from_index_files(
    *, root: Path, index_files: list[Path],
    staging_name: str = "commoncrawl_pasaxon_2017_2019",
    max_per_index: int = 120, max_matches_per_month: int = 2,
    max_total_requests: int | None = None,
    interval: float = 1.0, fetcher=fetch_archive_payload,
) -> dict[str, object]:
    """Run isolated, bounded collection over saved discovery files."""

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", staging_name):
        raise ValueError("staging_name must be a plain directory name")
    staging = root / "data" / "staging" / "archive_ocr" / staging_name
    per_index: dict[str, dict[str, object]] = {}
    inputs: list[dict[str, object]] = []
    requests_used = 0
    for path in index_files:
        if max_total_requests is not None and requests_used >= max_total_requests:
            break
        source_index, records = load_candidate_file(path)
        allow_page_date = source_index.startswith("CC-MAIN-2020-")
        selected, selection = prioritize_records(
            records, limit=max_per_index, allow_page_date=allow_page_date
        )
        request_cap = max_per_index
        if max_total_requests is not None:
            request_cap = min(request_cap, max_total_requests - requests_used)
        result = collect_from_records(
            root=root, records=selected, staging_name=staging_name,
            interval=interval, max_candidates=request_cap,
            max_matches_per_month=max_matches_per_month,
            source_index=source_index, allow_page_date=allow_page_date, fetcher=fetcher,
        )
        requests_used += int(result["range_requests"])
        per_index[source_index] = {**selection, **result}
        inputs.append({
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            **selection,
        })
    all_records = _read_ndjson(staging / "records.ndjson")
    matches_by_month = Counter(str(row["published_at"])[:7] for row in all_records)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "staging_name": staging_name, "data_host": "data.commoncrawl.org",
        "max_per_index": max_per_index,
        "max_total_requests": max_total_requests,
        "range_requests_this_run": requests_used,
        "max_matches_per_month": max_matches_per_month,
        "interval_seconds": interval, "inputs": inputs, "per_index": per_index,
        "importable_records": len(all_records),
        "importable_by_month": dict(sorted(matches_by_month.items())),
    }
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--staging-name", default="commoncrawl_pasaxon_2017_2019")
    parser.add_argument("--max-per-index", type=int, default=120)
    parser.add_argument("--max-total-requests", type=int)
    parser.add_argument("--max-per-month", type=int, default=2)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    result = collect_from_index_files(
        root=args.root.resolve(), index_files=[path.resolve() for path in args.input],
        staging_name=args.staging_name, max_per_index=args.max_per_index,
        max_total_requests=args.max_total_requests,
        max_matches_per_month=args.max_per_month, interval=args.interval,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
