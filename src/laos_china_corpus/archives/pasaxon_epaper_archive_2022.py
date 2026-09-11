"""Recover a bounded 2022 Pasaxon e-paper catalogue from saved CC indexes.

This module deliberately keeps newspaper *issues* outside the canonical
article table.  A cover thumbnail is discovery evidence, not article body
text.  Only a recoverable PDF (or a sufficiently detailed page image) may be
sent to the OCR/import pipeline later.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .commoncrawl import extract_archive_http_payload, parse_index_response
from .commoncrawl_pasaxon_round5 import PoliteHTTP, atomic_bytes, read_ndjson, utc_now, write_ndjson


STAGING_NAME = "pasaxon_epaper_archive_2022"
DEFAULT_INDEX_STAGINGS = (
    "commoncrawl_pasaxon_2021_2022",
    "commoncrawl_pasaxon_2021_2022_early",
)
_ISSUE_RE = re.compile(r"(?P<number>\d{1,2}\.\d{3})\s*\((?P<date>\d{2}\.\d{2}\.\d{4})\)")


@dataclass(frozen=True, slots=True)
class EpaperIssue:
    p_id: int
    issue_number: str
    published_at: str
    detail_url: str
    cover_url: str | None
    pdf_url: str | None = None


def _iso_date(value: str) -> str:
    return datetime.strptime(value, "%d.%m.%Y").date().isoformat()


def _p_id(url: str) -> int | None:
    raw = parse_qs(urlparse(url).query).get("p_id", [""])[0]
    return int(raw) if str(raw).isdigit() else None


def parse_issue_listing(html: str | bytes, page_url: str) -> list[EpaperIssue]:
    """Parse dated issue cards from a historical ``showlistpdf.php`` page."""

    soup = BeautifulSoup(html, "html.parser")
    issues: list[EpaperIssue] = []
    seen: set[int] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href"))
        if "pdf-detail.php" not in href:
            continue
        match = _ISSUE_RE.search(anchor.get_text(" ", strip=True))
        p_id = _p_id(urljoin(page_url, href))
        if not match or p_id is None or p_id in seen:
            continue
        cover: str | None = None
        node = anchor
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            image = node.find("img", src=True)
            if image and "ppdf/" in str(image.get("src")):
                cover = urljoin(page_url, str(image.get("src")))
                break
        issues.append(EpaperIssue(
            p_id=p_id,
            issue_number=match.group("number"),
            published_at=_iso_date(match.group("date")),
            detail_url=urljoin(page_url, href),
            cover_url=cover,
        ))
        seen.add(p_id)
    return issues


def parse_issue_detail(html: str | bytes, page_url: str) -> EpaperIssue:
    """Parse one historical ``pdf-detail.php`` page and its download target."""

    soup = BeautifulSoup(html, "html.parser")
    match = _ISSUE_RE.search(soup.get_text(" ", strip=True))
    p_id = _p_id(page_url)
    if not match or p_id is None:
        raise ValueError("detail page lacks a verifiable p_id and issue date")
    cover = next((
        urljoin(page_url, str(img.get("src")))
        for img in soup.find_all("img", src=True)
        if "ppdf/" in str(img.get("src"))
    ), None)
    pdf = next((
        urljoin(page_url, str(anchor.get("href")))
        for anchor in soup.find_all("a", href=True)
        if "/pdfs/" in urljoin(page_url, str(anchor.get("href")))
    ), None)
    return EpaperIssue(
        p_id=p_id,
        issue_number=match.group("number"),
        published_at=_iso_date(match.group("date")),
        detail_url=page_url,
        cover_url=cover,
        pdf_url=pdf,
    )


def _saved_rows(root: Path, source_stagings: tuple[str, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for staging in source_stagings:
        index_dir = root / "data/staging/archive_ocr" / staging / "indexes"
        for path in sorted(index_dir.glob("CC*.ndjson")):
            for record in parse_index_response(path.read_bytes()):
                lowered = urlparse(record.url).path.casefold()
                if lowered.endswith("/showlistpdf.php") or lowered.endswith("/pdf-detail.php"):
                    rows.append({
                        "source_index": path.stem,
                        "original_url": record.url,
                        "capture_timestamp": record.timestamp,
                        "filename": record.filename,
                        "offset": record.offset,
                        "length": record.length,
                        "warc_url": record.warc_url,
                        "range_header": record.range_header,
                    })
    return rows


def raw_wayback_url(replay_url: str) -> str:
    """Request archived bytes without the Wayback toolbar wrapper."""

    return re.sub(r"(/web/\d+)(?:id_)?/", r"\1id_/", replay_url, count=1)


def probe_pdf_archives(
    *, root: Path, interval: float = 1.0, max_requests: int = 24,
    staging_name: str = STAGING_NAME,
) -> dict[str, object]:
    """Check exact PDF URLs in Wayback and download only genuine PDF bytes."""

    staging = root / "data/staging/archive_ocr" / staging_name
    issues = read_ndjson(staging / "issues.ndjson")
    prior = read_ndjson(staging / "pdf_availability.ndjson")
    terminal = {str(row.get("pdf_url")) for row in prior if row.get("pdf_url")}
    http = PoliteHTTP(interval=interval)
    ledger = list(prior)
    recovered = 0
    for issue in issues:
        pdf_url = str(issue.get("pdf_url") or "")
        if not pdf_url or pdf_url in terminal or http.requests.total() >= max_requests:
            continue
        published = str(issue["published_at"]).replace("-", "")
        api = "https://archive.org/wayback/available?" + urlencode({
            "url": pdf_url, "timestamp": published,
        })
        row = {
            "p_id": issue["p_id"], "published_at": issue["published_at"],
            "issue_number": issue["issue_number"], "pdf_url": pdf_url,
            "availability_url": api, "checked_at": utc_now(),
        }
        try:
            payload = http.get(api, kind="availability")
            parsed = json.loads(payload)
            closest = parsed.get("archived_snapshots", {}).get("closest", {})
            if not closest.get("available") or str(closest.get("status")) != "200":
                row["status"] = "not_archived"
            elif http.requests.total() >= max_requests:
                row.update({"status": "available_not_downloaded", "archive_url": closest.get("url")})
            else:
                archive_url = raw_wayback_url(str(closest["url"]))
                binary = http.get(archive_url, kind="download")
                if not binary.startswith(b"%PDF"):
                    raise ValueError("archive replay is not a PDF")
                digest = hashlib.sha256(binary).hexdigest()
                pdf_path = staging / "pdf" / f"{issue['published_at']}_{digest}.pdf"
                atomic_bytes(pdf_path, binary)
                row.update({
                    "status": "pdf_recovered", "archive_url": archive_url,
                    "local_file": str(pdf_path.relative_to(root)),
                    "sha256": digest, "bytes": len(binary),
                })
                recovered += 1
        except Exception as exc:
            row.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        ledger.append(row)
        terminal.add(pdf_url)
        write_ndjson(staging / "pdf_availability.ndjson", ledger)
        time.sleep(0)
    status_counts = Counter(str(row.get("status")) for row in ledger)
    result = {
        "generated_at": utc_now(),
        "exact_pdf_urls": sum(bool(row.get("pdf_url")) for row in issues),
        "availability_rows": len(ledger),
        "requests_this_run": http.requests.total(),
        "request_limit": max_requests,
        "status_counts": dict(sorted(status_counts.items())),
        "pdfs_recovered_this_run": recovered,
        "canonical_database_written": False,
        "next_step": "local OCR only for pdf_recovered rows",
    }
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pdf_archive_probe"] = result
    atomic_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return result


def recover_catalog(
    *, root: Path, max_range_requests: int = 40, interval: float = 1.0,
    source_stagings: tuple[str, ...] = DEFAULT_INDEX_STAGINGS,
    staging_name: str = STAGING_NAME,
) -> dict[str, object]:
    """Replay saved list/detail captures and build a non-canonical issue catalogue."""

    staging = root / "data/staging/archive_ocr" / staging_name
    queue = sorted(_saved_rows(root, source_stagings), key=lambda row: (
        0 if "showlistpdf.php" in str(row["original_url"]) else 1,
        str(row["capture_timestamp"]), str(row["original_url"]),
    ))
    # One capture per URL per index is enough for this bounded recovery.
    unique: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for row in queue:
        key = (str(row["source_index"]), str(row["original_url"]))
        if key not in seen:
            unique.append(row)
            seen.add(key)
    http = PoliteHTTP(interval=interval)
    prior_issues = read_ndjson(staging / "issues.ndjson")
    issues: dict[int, dict[str, object]] = {int(row["p_id"]): row for row in prior_issues}
    ledger: list[dict[str, object]] = read_ndjson(staging / "screened.ndjson")
    failures: list[dict[str, object]] = read_ndjson(staging / "failures.ndjson")
    terminal = {
        (str(row.get("source_index")), str(row.get("original_url")), str(row.get("capture_timestamp")))
        for row in (*ledger, *failures)
    }
    for row in unique:
        identity = (str(row["source_index"]), str(row["original_url"]), str(row["capture_timestamp"]))
        if identity in terminal:
            continue
        if http.requests["range"] >= max_range_requests:
            break
        try:
            member = http.get(str(row["warc_url"]), kind="range", range_header=str(row["range_header"]))
            if len(member) != int(row["length"]):
                raise ValueError("range length mismatch")
            payload = extract_archive_http_payload(member)
            digest = hashlib.sha256(member).hexdigest()
            raw_path = staging / "raw" / f"{digest}.warc.gz"
            atomic_bytes(raw_path, member)
            url = str(row["original_url"])
            parsed = (
                parse_issue_listing(payload, url)
                if "showlistpdf.php" in url
                else [parse_issue_detail(payload, url)]
            )
            for issue in parsed:
                if not issue.published_at.startswith("2022-"):
                    continue
                current = issues.get(issue.p_id, {})
                merged = {**current, **{k: v for k, v in asdict(issue).items() if v is not None}}
                merged.update({
                    "source_index": row["source_index"],
                    "capture_timestamp": row["capture_timestamp"],
                    "archive_warc_url": row["warc_url"],
                    "raw_file": str(raw_path.relative_to(root)),
                })
                issues[issue.p_id] = merged
            ledger.append({**row, "status": "parsed", "issue_rows": len(parsed), "raw_file": str(raw_path.relative_to(root))})
        except Exception as exc:
            failures.append({**row, "status": "failed", "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now()})
    issue_rows = sorted(issues.values(), key=lambda row: (str(row["published_at"]), int(row["p_id"])))
    write_ndjson(staging / "issues.ndjson", issue_rows)
    write_ndjson(staging / "screened.ndjson", ledger)
    write_ndjson(staging / "failures.ndjson", failures)
    months = Counter(str(row["published_at"])[:7] for row in issue_rows)
    manifest = {
        "generated_at": utc_now(),
        "network_scope": ["data.commoncrawl.org"],
        "source_stagings": list(source_stagings),
        "saved_capture_rows": len(unique),
        "range_requests": http.requests["range"],
        "range_request_limit": max_range_requests,
        "parsed_capture_rows": len(ledger),
        "range_requests_this_run": http.requests["range"],
        "failure_rows": len(failures),
        "unique_2022_issues": len(issue_rows),
        "issues_by_month": dict(sorted(months.items())),
        "issues_with_cover_url": sum(bool(row.get("cover_url")) for row in issue_rows),
        "issues_with_pdf_url": sum(bool(row.get("pdf_url")) for row in issue_rows),
        "canonical_database_written": False,
        "cover_is_not_article_body": True,
        "next_step": "probe archive availability for PDFs first; OCR covers only as discovery evidence",
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-range-requests", type=int, default=40)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--staging-name", default=STAGING_NAME)
    parser.add_argument("--mode", choices=("catalog", "probe-pdfs"), default="catalog")
    parser.add_argument("--max-requests", type=int, default=24)
    args = parser.parse_args(argv)
    if args.mode == "probe-pdfs":
        result = probe_pdf_archives(
            root=args.root.resolve(), interval=args.interval,
            max_requests=args.max_requests, staging_name=args.staging_name,
        )
    else:
        result = recover_catalog(
            root=args.root.resolve(), max_range_requests=args.max_range_requests,
            interval=args.interval, staging_name=args.staging_name,
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
