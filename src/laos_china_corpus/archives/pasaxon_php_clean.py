"""Conservatively recover genuine Pasaxon PHP articles with hidden SEO injection."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from bs4 import BeautifulSoup

from .commoncrawl import extract_archive_http_payload
from .commoncrawl_pasaxon import _entity_lao_html, _is_contaminated_template
from .commoncrawl_pasaxon_round5 import atomic_bytes, read_ndjson, utc_now, write_ndjson
from .commoncrawl_pasaxon_round6 import strict_china_related


PAGE_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?(?!\d)")
INJECTION_MARKERS = ("agen togel", "agen slot", "lapak online", "bertaruh online")


@dataclass(frozen=True)
class CleanPasaxonArticle:
    title: str
    body: str
    published_date: str
    removed_injection_nodes: int


def _compact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w\u0e80-\u0eff]+", "", normalized.replace("\u200b", "").replace("\u00ad", ""))


def titles_agree(actual: str, expected: str) -> bool:
    left, right = _compact(actual), _compact(expected)
    if not left or not right:
        return False
    return left in right or right in left or SequenceMatcher(None, left, right).ratio() >= 0.82


def parse_clean_php_article(
    payload: bytes | str, *, expected_title: str, expected_date: str,
) -> CleanPasaxonArticle:
    """Remove only explicit hidden injection nodes, then verify article structure."""

    if isinstance(payload, bytes):
        source = _entity_lao_html(payload).decode("utf-8", errors="replace")
    else:
        source = payload
    soup = BeautifulSoup(source, "html.parser")
    injected = []
    for node in soup.select(".cok"):
        injected.append(node)
        node.decompose()
    for node in soup.find_all(["script", "style", "nav"]):
        node.decompose()

    candidate = None
    for heading in soup.find_all("h4"):
        title = " ".join(heading.get_text(" ", strip=True).split())
        if len(re.findall(r"[\u0e80-\u0eff]", title)) < 3:
            continue
        small = heading.find_next("small")
        date_match = PAGE_DATE.search(small.get_text(" ", strip=True) if small else "")
        if date_match and titles_agree(title, expected_title):
            candidate = (heading, title, date_match)
            break
    if candidate is None:
        raise ValueError("no verified Pasaxon article heading/date pair")
    heading, title, date_match = candidate
    published = f"{int(date_match.group(3)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(1)):02d}"
    if published != expected_date:
        raise ValueError(f"page date {published} conflicts with listing date {expected_date}")

    container = heading.find_next(class_="text-justify")
    if container is None:
        raise ValueError("verified heading has no Pasaxon text-justify body container")
    lines: list[str] = []
    for raw in container.get_text("\n", strip=True).splitlines():
        line = " ".join(raw.split())
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    while lines and lines[-1].startswith(("ລິຂະສິດ", "Copyright")):
        lines.pop()
    body = "\n\n".join(lines)
    if len(body) < 120 or len(re.findall(r"[\u0e80-\u0eff]", body)) < 60:
        raise ValueError("cleaned Pasaxon body is too short")
    lower = body.casefold()
    if any(marker in lower for marker in INJECTION_MARKERS) or _is_contaminated_template(title, body):
        raise ValueError("injection remains inside verified article container")
    return CleanPasaxonArticle(title, body, published, len(injected))


def recover_staging(*, root: Path, staging_name: str) -> dict[str, object]:
    """Reprocess preserved contaminated rows offline and create importable records."""

    staging = root / "data/staging/archive_ocr" / staging_name
    ledger_path = staging / "screened.ndjson"
    records_path = staging / "records.ndjson"
    failures_path = staging / "clean_failures.ndjson"
    ledger = read_ndjson(ledger_path)
    existing_records = read_ndjson(records_path)
    records_by_id = {str(row.get("candidate_identity")): row for row in existing_records}
    failures: list[dict[str, object]] = []
    recovered = 0
    audit = Counter()

    for row in ledger:
        if row.get("status") not in {"contaminated_template", "china_match_cleaned"}:
            continue
        identity = str(row.get("candidate_identity"))
        if identity in records_by_id:
            continue
        try:
            raw_path = root / str(row["raw_file"])
            raw = raw_path.read_bytes()
            audit["raw_exists"] += 1
            if hashlib.sha256(raw).hexdigest() != row.get("raw_sha256"):
                raise ValueError("raw SHA-256 mismatch")
            audit["raw_sha_matches"] += 1
            payload = extract_archive_http_payload(raw)
            if hashlib.sha256(payload).hexdigest() != row.get("payload_sha256"):
                raise ValueError("payload SHA-256 mismatch")
            audit["payload_sha_matches"] += 1
            article = parse_clean_php_article(
                payload,
                expected_title=str(row["listing_title_original"]),
                expected_date=str(row["listing_published_at"]),
            )
            related, hits = strict_china_related(f"{article.title}\n{article.body}")
            if not related:
                raise ValueError("cleaned verified article is not China-related")
            content_sha = hashlib.sha256(article.body.encode("utf-8")).hexdigest()
            body_path = staging / "text" / article.published_date[:7] / f"{content_sha}.txt"
            atomic_bytes(body_path, article.body.encode("utf-8"))
            record = {
                "candidate_identity": identity,
                "source_code": "pasaxon_archive",
                "language": "lo",
                "title_original": article.title,
                "published_at": article.published_date,
                "date_precision": "page_day_verified_against_listing",
                "body_original": article.body,
                "body_method": "commoncrawl_pasaxon_php_html_hidden_injection_removed",
                "matched_queries": hits,
                "original_url": row["original_url"],
                "archive_url": row["warc_url"],
                "evidence_grade": "B1",
                "retrieval_tier": "T1_DIRECT_CHINA",
                "content_sha256": content_sha,
                "body_file": str(body_path.relative_to(root)),
                "raw_file": row["raw_file"],
                "raw_sha256": row["raw_sha256"],
                "payload_sha256": row["payload_sha256"],
                "retrieved_at": utc_now(),
                "capture_timestamp": row["capture_timestamp"],
                "source_index": row["source_index"],
                "cleaning": {
                    "removed_injection_nodes": article.removed_injection_nodes,
                    "listing_title_verified": True,
                    "listing_date_verified": True,
                    "body_container": "class=text-justify",
                },
                "parser": "pasaxon_php_clean_v1",
            }
            records_by_id[identity] = record
            row["status"] = "china_match_cleaned"
            row["body_file"] = record["body_file"]
            row["content_sha256"] = content_sha
            row["cleaned_at"] = utc_now()
            recovered += 1
        except Exception as exc:
            failures.append({
                "candidate_identity": identity,
                "raw_file": row.get("raw_file"),
                "error": f"{type(exc).__name__}: {exc}",
                "checked_at": utc_now(),
            })

    records = sorted(records_by_id.values(), key=lambda row: (str(row["published_at"]), str(row["candidate_identity"])))
    write_ndjson(ledger_path, ledger)
    write_ndjson(records_path, records)
    write_ndjson(failures_path, failures)
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["clean_recovery"] = {
        "generated_at": utc_now(),
        "network_requests": 0,
        "recovered_this_run": recovered,
        "importable_records": len(records),
        "records_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in records).items())),
        "clean_failures": len(failures),
        "hash_audit": dict(audit),
        "canonical_database_written": False,
    }
    atomic_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest["clean_recovery"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--staging-name", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(recover_staging(root=args.root.resolve(), staging_name=args.staging_name), ensure_ascii=False))


if __name__ == "__main__":
    main()
