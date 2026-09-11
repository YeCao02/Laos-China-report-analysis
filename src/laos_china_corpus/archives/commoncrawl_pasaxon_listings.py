"""Recover dated Pasaxon article leads from archived 2021--2022 listing pages.

Listing pages are discovery evidence, not article-body evidence.  They may retain
the original Pasaxon title, URL and publication timestamp even when a later
capture of the corresponding PHP detail page has been replaced by a polluted
template.  Consequently this module writes an A2 discovery queue and never
imports its output as full-text articles.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from .commoncrawl import extract_archive_http_payload, parse_index_response
from .commoncrawl_pasaxon import _canonical_pasaxon_url, _entity_lao_html
from .commoncrawl_pasaxon_2021_2022 import ALLOWED_YEARS, _php_identity
from .commoncrawl_pasaxon_round5 import PoliteHTTP, atomic_bytes, read_ndjson, utc_now, write_ndjson
from .commoncrawl_pasaxon_round6 import strict_china_related


STAGING_NAME = "commoncrawl_pasaxon_2021_2022_listings"

# Pages are ordered by expected relevance.  Notice/detail/PDF/CV pages are not
# listings and are deliberately excluded.
LISTING_PRIORITY = {
    "/showlistcooperation.php": 0,
    "/showlisteconomic.php": 1,
    "/showlistpolitic.php": 2,
    "/showlistforigner.php": 3,
    "/showlistfinance.php": 4,
    "/showlistagriculture.php": 5,
    "/showlisttechnology.php": 6,
    "/index.php": 7,
}

ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*['\"](?P<href>[^'\"]*pasaxon-detail\.php\?[^'\"]+)['\"][^>]*>"
    r"(?P<title>.*?)</a>(?P<after>.{0,600})",
    re.IGNORECASE | re.DOTALL,
)
DATE_RE = re.compile(
    r"(?:(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>20\d{2})|"
    r"(?P<y2>20\d{2})-(?P<m2>\d{1,2})-(?P<d2>\d{1,2}))"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
)
TAG_RE = re.compile(r"<[^>]+>")


def listing_kind(url: str) -> str | None:
    path = urlparse(url).path.casefold().rstrip("/") or "/index.php"
    return path if path in LISTING_PRIORITY else None


def _text(fragment: str) -> str:
    return " ".join(html.unescape(TAG_RE.sub(" ", fragment)).replace("\u200b", " ").split())


def _date(match: re.Match[str]) -> str:
    if match.group("y"):
        year, month, day = int(match.group("y")), int(match.group("m")), int(match.group("d"))
    else:
        year, month, day = int(match.group("y2")), int(match.group("m2")), int(match.group("d2"))
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_listing_refs(payload: bytes | str, listing_url: str) -> list[dict[str, object]]:
    """Extract article URL/title/date triples whose title is explicitly China-related."""

    source = (
        _entity_lao_html(payload).decode("utf-8", errors="replace")
        if isinstance(payload, bytes) else payload
    )
    found: dict[str, dict[str, object]] = {}
    for match in ANCHOR_RE.finditer(source):
        href = html.unescape(match.group("href"))
        article_url = urljoin(listing_url, href)
        identity = _php_identity(article_url)
        if not identity:
            continue
        date_match = DATE_RE.search(_text(match.group("after")))
        if not date_match:
            continue
        published = _date(date_match)
        if int(published[:4]) not in ALLOWED_YEARS:
            continue
        title = _text(match.group("title"))
        related, hits = strict_china_related(title)
        if not related:
            continue
        key = f"php:{identity[0]}"
        found[key] = {
            "candidate_identity": key,
            "p_id": identity[0],
            "action": identity[1],
            "title_original": title,
            "published_at": published,
            "original_url": article_url,
            "matched_queries": hits,
            "listing_url": listing_url,
        }
    return sorted(found.values(), key=lambda row: (str(row["published_at"]), int(row["p_id"])))


def build_listing_queue(
    index_files: list[Path], *, root: Path, only_kinds: set[str] | None = None,
    capture_years: set[int] | None = None,
) -> list[dict[str, object]]:
    """Build an offline, relevance-ordered queue from previously saved indexes."""

    rows: list[dict[str, object]] = []
    for path in index_files:
        index = path.stem
        for record in parse_index_response(path.read_bytes()):
            kind = listing_kind(record.url)
            if not kind:
                continue
            if only_kinds and kind not in only_kinds:
                continue
            if capture_years and int(str(record.timestamp)[:4]) not in capture_years:
                continue
            rows.append({
                "source_index": index,
                "listing_kind": kind,
                "listing_url": record.url,
                "capture_timestamp": record.timestamp,
                "filename": record.filename,
                "offset": record.offset,
                "length": record.length,
                "warc_url": record.warc_url,
                "range_header": record.range_header,
                "index_file": str(path.relative_to(root)),
            })
    # One capture per listing kind and index is sufficient.  Interleave indexes
    # within each high-value section so the request budget spans both years.
    best: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (str(row["source_index"]), str(row["listing_kind"]))
        prior = best.get(key)
        if prior is None or str(row["capture_timestamp"]) > str(prior["capture_timestamp"]):
            best[key] = row
    return sorted(best.values(), key=lambda row: (
        LISTING_PRIORITY[str(row["listing_kind"])],
        str(row["capture_timestamp"])[:4],
        str(row["source_index"]),
    ))


def discover_from_saved_indexes(
    *, root: Path, source_stagings: list[str], max_range_requests: int = 80,
    interval: float = 1.0, staging_name: str = STAGING_NAME,
    only_kinds: set[str] | None = None, capture_years: set[int] | None = None,
) -> dict[str, object]:
    """Replay listing pages and persist A2 dated-title discoveries."""

    staging = root / "data/staging/archive_ocr" / staging_name
    index_files: list[Path] = []
    for name in source_stagings:
        index_files.extend(sorted((root / "data/staging/archive_ocr" / name / "indexes").glob("CC*.ndjson")))
    queue = build_listing_queue(
        index_files, root=root, only_kinds=only_kinds, capture_years=capture_years,
    )
    ledger_path = staging / "screened.ndjson"
    discovery_path = staging / "discoveries.ndjson"
    failure_path = staging / "failures.ndjson"
    ledger = read_ndjson(ledger_path)
    failures = read_ndjson(failure_path)
    discoveries = read_ndjson(discovery_path)
    done = {(str(row.get("source_index")), str(row.get("listing_kind"))) for row in ledger}
    by_id = {str(row["candidate_identity"]): row for row in discoveries}
    http = PoliteHTTP(interval=interval)
    pending: list[dict[str, object]] = []

    for position, row in enumerate(queue):
        key = (str(row["source_index"]), str(row["listing_kind"]))
        if key in done:
            continue
        if http.requests["range"] >= max_range_requests:
            pending.extend({**item, "queue_status": "range_budget_exhausted"} for item in queue[position:])
            break
        raw_path: Path | None = None
        raw_sha: str | None = None
        payload_sha: str | None = None
        try:
            raw = http.get(str(row["warc_url"]), kind="range", range_header=str(row["range_header"]))
            if len(raw) != int(row["length"]):
                raise ValueError(f"range length mismatch: expected {row['length']}, got {len(raw)}")
            payload = extract_archive_http_payload(raw)
            raw_sha = hashlib.sha256(raw).hexdigest()
            payload_sha = hashlib.sha256(payload).hexdigest()
            raw_path = staging / "raw" / str(row["capture_timestamp"])[:6] / f"{raw_sha}.warc.gz"
            atomic_bytes(raw_path, raw)
            refs = parse_listing_refs(payload, str(row["listing_url"]))
            for ref in refs:
                enriched = {
                    **ref,
                    "evidence_grade": "A2",
                    "evidence_scope": "listing_title_date_url_only_no_article_body",
                    "archive_url": row["warc_url"],
                    "source_index": row["source_index"],
                    "capture_timestamp": row["capture_timestamp"],
                    "listing_kind": row["listing_kind"],
                    "raw_file": str(raw_path.relative_to(root)),
                    "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha,
                    "retrieved_at": utc_now(),
                }
                prior = by_id.get(str(ref["candidate_identity"]))
                if prior is None or str(enriched["capture_timestamp"]) < str(prior["capture_timestamp"]):
                    by_id[str(ref["candidate_identity"])] = enriched
            ledger.append({
                **row,
                "status": "screened",
                "china_refs": len(refs),
                "raw_file": str(raw_path.relative_to(root)),
                "raw_sha256": raw_sha,
                "payload_sha256": payload_sha,
                "checked_at": utc_now(),
            })
        except Exception as exc:
            failure = {
                **row,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "checked_at": utc_now(),
            }
            if raw_path is not None:
                failure.update({
                    "raw_file": str(raw_path.relative_to(root)),
                    "raw_sha256": raw_sha,
                    "payload_sha256": payload_sha,
                })
            ledger.append(failure)
            failures.append(failure)
        done.add(key)
        write_ndjson(ledger_path, ledger)
        write_ndjson(discovery_path, sorted(by_id.values(), key=lambda item: (
            str(item["published_at"]), int(item["p_id"]),
        )))
        write_ndjson(failure_path, failures)

    write_ndjson(staging / "continuation_queue.ndjson", pending)
    discoveries = sorted(by_id.values(), key=lambda item: (str(item["published_at"]), int(item["p_id"])))
    manifest = {
        "generated_at": utc_now(),
        "network_scope": ["data.commoncrawl.org"],
        "source_stagings": source_stagings,
        "only_kinds": sorted(only_kinds or ()),
        "capture_years": sorted(capture_years or ()),
        "saved_index_files": len(index_files),
        "queue_rows": len(queue),
        "range_requests": http.requests["range"],
        "range_request_limit": max_range_requests,
        "screened_rows": len(ledger),
        "failure_rows": len(failures),
        "continuation_rows": len(pending),
        "unique_china_discoveries": len(discoveries),
        "discoveries_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in discoveries).items())),
        "evidence_grade": "A2",
        "article_bodies_recovered": 0,
        "canonical_database_written": False,
        "capture_date_used_as_publication_date": False,
        "next_step": "target only discovered p_id values for body recovery",
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def reprocess_saved_raw(*, root: Path, staging_name: str = STAGING_NAME) -> dict[str, object]:
    """Repair parser-only failures from locally preserved WARC members."""

    staging = root / "data/staging/archive_ocr" / staging_name
    ledger_path = staging / "screened.ndjson"
    discovery_path = staging / "discoveries.ndjson"
    failure_path = staging / "failures.ndjson"
    ledger = read_ndjson(ledger_path)
    discoveries = read_ndjson(discovery_path)
    by_id = {str(row["candidate_identity"]): row for row in discoveries}
    recovered = 0
    unrecovered: list[dict[str, object]] = []
    hash_audit = Counter()

    for row in ledger:
        if row.get("status") != "failed" or "string pattern on a bytes-like object" not in str(row.get("error")):
            if row.get("status") == "failed":
                unrecovered.append(row)
            continue
        raw_file = row.get("raw_file")
        if not raw_file:
            unrecovered.append(row)
            continue
        try:
            raw_path = root / str(raw_file)
            raw = raw_path.read_bytes()
            hash_audit["raw_exists"] += 1
            if hashlib.sha256(raw).hexdigest() != row.get("raw_sha256"):
                raise ValueError("raw SHA-256 mismatch")
            hash_audit["raw_sha_matches"] += 1
            payload = extract_archive_http_payload(raw)
            if hashlib.sha256(payload).hexdigest() != row.get("payload_sha256"):
                raise ValueError("payload SHA-256 mismatch")
            hash_audit["payload_sha_matches"] += 1
            refs = parse_listing_refs(payload, str(row["listing_url"]))
            for ref in refs:
                enriched = {
                    **ref,
                    "evidence_grade": "A2",
                    "evidence_scope": "listing_title_date_url_only_no_article_body",
                    "archive_url": row["warc_url"],
                    "source_index": row["source_index"],
                    "capture_timestamp": row["capture_timestamp"],
                    "listing_kind": row["listing_kind"],
                    "raw_file": row["raw_file"],
                    "raw_sha256": row["raw_sha256"],
                    "payload_sha256": row["payload_sha256"],
                    "retrieved_at": utc_now(),
                }
                prior = by_id.get(str(ref["candidate_identity"]))
                if prior is None or str(enriched["capture_timestamp"]) < str(prior["capture_timestamp"]):
                    by_id[str(ref["candidate_identity"])] = enriched
            row["prior_error"] = row.pop("error")
            row["status"] = "screened_offline_reprocess"
            row["china_refs"] = len(refs)
            row["reprocessed_at"] = utc_now()
            recovered += 1
        except Exception as exc:
            row["reprocess_error"] = f"{type(exc).__name__}: {exc}"
            unrecovered.append(row)

    discoveries = sorted(by_id.values(), key=lambda item: (str(item["published_at"]), int(item["p_id"])))
    write_ndjson(ledger_path, ledger)
    write_ndjson(discovery_path, discoveries)
    write_ndjson(failure_path, unrecovered)
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["offline_reprocess"] = {
        "generated_at": utc_now(),
        "network_requests": 0,
        "recovered_parser_failures": recovered,
        "unrecovered_failures": len(unrecovered),
        "unique_china_discoveries": len(discoveries),
        "discoveries_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in discoveries).items())),
        "hash_audit": dict(hash_audit),
        "capture_date_used_as_publication_date": False,
    }
    manifest["failure_rows"] = len(unrecovered)
    manifest["unique_china_discoveries"] = len(discoveries)
    manifest["discoveries_by_month"] = manifest["offline_reprocess"]["discoveries_by_month"]
    atomic_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest["offline_reprocess"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--source-staging", action="append", required=True)
    parser.add_argument("--staging-name", default=STAGING_NAME)
    parser.add_argument("--max-range-requests", type=int, default=80)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--mode", choices=("replay", "reprocess-saved"), default="replay")
    parser.add_argument("--only-kind", action="append")
    parser.add_argument("--capture-year", action="append", type=int)
    args = parser.parse_args(argv)
    if args.mode == "reprocess-saved":
        result = reprocess_saved_raw(root=args.root.resolve(), staging_name=args.staging_name)
    else:
        result = discover_from_saved_indexes(
            root=args.root.resolve(), source_stagings=args.source_staging,
            max_range_requests=args.max_range_requests, interval=args.interval,
            staging_name=args.staging_name,
            only_kinds=set(args.only_kind or ()), capture_years=set(args.capture_year or ()),
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
