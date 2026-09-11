"""Bounded discovery and recovery of Pasaxon 2022 PDFs from Common Crawl."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse

from .commoncrawl import build_index_query, extract_archive_http_payload, parse_index_response
from .commoncrawl_pasaxon_round5 import PoliteHTTP, atomic_bytes, read_ndjson, utc_now, write_ndjson


STAGING_NAME = "commoncrawl_pasaxon_epaper_pdfs_2022"
INDEXES = (
    "CC-MAIN-2022-05", "CC-MAIN-2022-21", "CC-MAIN-2022-27",
    "CC-MAIN-2022-33", "CC-MAIN-2022-40", "CC-MAIN-2022-49",
)
HOST_PREFIXES = ("pasaxon.org.la/pdfs/", "www.pasaxon.org.la/pdfs/")
_DATE_RE = re.compile(r"(?P<day>\d{2})-(?P<month>\d{1,2})-(?P<year>2022)(?:\D|$)")


def publication_date_from_pdf_url(url: str) -> str | None:
    """Read the terminal DD-M-YYYY component; random filename prefix is ignored."""

    match = _DATE_RE.search(Path(urlparse(url).path).name)
    if not match:
        return None
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"])).isoformat()
    except ValueError:
        return None


def discover(*, root: Path, interval: float = 1.0, staging_name: str = STAGING_NAME) -> dict[str, object]:
    """Query only the known 2022 indexes and retain official-date-matched PDF rows."""

    staging = root / "data/staging/archive_ocr" / staging_name
    issue_rows = read_ndjson(root / "data/staging/archive_ocr/pasaxon_epaper_archive_2022/issues.ndjson")
    issue_by_date = {str(row["published_at"]): row for row in issue_rows}
    http = PoliteHTTP(interval=interval)
    candidates: dict[str, dict[str, object]] = {}
    audit: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for index in INDEXES:
        for prefix in HOST_PREFIXES:
            query = build_index_query(
                index, prefix, filters=("status:200",), collapse="urlkey",
                match_type="prefix", page=0, page_size=10000,
            )
            try:
                payload = http.get(query, kind="index")
                output = staging / "indexes" / f"{index}_{hashlib.sha256(prefix.encode()).hexdigest()[:8]}.ndjson"
                atomic_bytes(output, payload)
                parsed = parse_index_response(payload)
                matched = 0
                for record in parsed:
                    published = publication_date_from_pdf_url(record.url)
                    if not published or published not in issue_by_date:
                        continue
                    if ".pdf" not in urlparse(record.url).path.casefold():
                        continue
                    row = {
                        "source_index": index, "original_url": record.url,
                        "published_at": published, "issue": issue_by_date[published],
                        "capture_timestamp": record.timestamp, "mime": record.mime,
                        "filename": record.filename, "offset": record.offset,
                        "length": record.length, "warc_url": record.warc_url,
                        "range_header": record.range_header, "digest": record.digest,
                    }
                    prior = candidates.get(record.url)
                    if prior is None or str(row["capture_timestamp"]) < str(prior["capture_timestamp"]):
                        candidates[record.url] = row
                    matched += 1
                audit.append({
                    "index": index, "prefix": prefix, "query_url": query,
                    "response_file": str(output.relative_to(root)), "rows": len(parsed),
                    "official_date_matches": matched, "status": "complete",
                })
            except HTTPError as exc:
                if exc.code == 404:
                    # Common Crawl returns 404 with a JSON "No Captures found" body.
                    audit.append({
                        "index": index, "prefix": prefix, "query_url": query,
                        "rows": 0, "official_date_matches": 0,
                        "status": "complete_no_captures",
                    })
                    continue
                failures.append({
                    "index": index, "prefix": prefix, "query_url": query,
                    "status": "failed", "error": f"HTTPError {exc.code}: {exc.reason}",
                    "checked_at": utc_now(),
                })
            except Exception as exc:
                failures.append({
                    "index": index, "prefix": prefix, "query_url": query,
                    "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                    "checked_at": utc_now(),
                })
    rows = sorted(candidates.values(), key=lambda row: (str(row["published_at"]), str(row["original_url"])))
    write_ndjson(staging / "candidates.ndjson", rows)
    write_ndjson(staging / "failures.ndjson", failures)
    manifest = {
        "generated_at": utc_now(), "network_scope": ["index.commoncrawl.org"],
        "indexes": list(INDEXES), "host_prefixes": list(HOST_PREFIXES),
        "requests": http.requests.total(), "audit": audit,
        "candidate_pdf_urls": len(rows),
        "candidate_dates": dict(sorted(Counter(str(row["published_at"]) for row in rows).items())),
        "failures": len(failures), "capture_date_used_as_publication_date": False,
        "canonical_database_written": False,
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def recover(
    *, root: Path, interval: float = 1.0, max_range_requests: int = 24,
    staging_name: str = STAGING_NAME,
) -> dict[str, object]:
    """Download matched WARC members and retain payloads that are genuine PDFs."""

    staging = root / "data/staging/archive_ocr" / staging_name
    candidates = read_ndjson(staging / "candidates.ndjson")
    ledger = read_ndjson(staging / "recovery.ndjson")
    terminal = {str(row.get("original_url")) for row in ledger}
    http = PoliteHTTP(interval=interval)
    recovered_this_run = 0
    for row in candidates:
        if str(row["original_url"]) in terminal or http.requests["range"] >= max_range_requests:
            continue
        result = {**row, "checked_at": utc_now()}
        try:
            member = http.get(str(row["warc_url"]), kind="range", range_header=str(row["range_header"]))
            if len(member) != int(row["length"]):
                raise ValueError("range length mismatch")
            member_sha = hashlib.sha256(member).hexdigest()
            raw_path = staging / "raw" / f"{member_sha}.warc.gz"
            atomic_bytes(raw_path, member)
            payload = extract_archive_http_payload(member)
            if not payload.startswith(b"%PDF"):
                raise ValueError("archived payload is not a PDF")
            digest = hashlib.sha256(payload).hexdigest()
            pdf_path = staging / "pdf" / f"{row['published_at']}_{digest}.pdf"
            atomic_bytes(pdf_path, payload)
            result.update({
                "status": "pdf_recovered", "raw_file": str(raw_path.relative_to(root)),
                "raw_sha256": member_sha, "local_file": str(pdf_path.relative_to(root)),
                "sha256": digest, "bytes": len(payload),
            })
            recovered_this_run += 1
        except Exception as exc:
            result.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        ledger.append(result)
        terminal.add(str(row["original_url"]))
        write_ndjson(staging / "recovery.ndjson", ledger)
    summary = {
        "generated_at": utc_now(), "candidate_pdf_urls": len(candidates),
        "range_requests": http.requests["range"], "range_request_limit": max_range_requests,
        "status_counts": dict(sorted(Counter(str(row.get("status")) for row in ledger).items())),
        "pdfs_recovered_this_run": recovered_this_run,
        "recovered_dates": sorted({str(row["published_at"]) for row in ledger if row.get("status") == "pdf_recovered"}),
        "canonical_database_written": False,
    }
    atomic_bytes(staging / "recovery_summary.json", json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"))
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("discover", "recover"), default="discover")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-range-requests", type=int, default=24)
    parser.add_argument("--staging-name", default=STAGING_NAME)
    args = parser.parse_args(argv)
    function = discover if args.mode == "discover" else recover
    kwargs = {"root": args.root.resolve(), "interval": args.interval, "staging_name": args.staging_name}
    if args.mode == "recover":
        kwargs["max_range_requests"] = args.max_range_requests
    print(json.dumps(function(**kwargs), ensure_ascii=False))


if __name__ == "__main__":
    main()
