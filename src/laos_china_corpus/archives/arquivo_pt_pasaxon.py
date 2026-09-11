"""Recover exact-dated Pasaxon pages from the public Arquivo.pt web archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .commoncrawl_pasaxon import _canonical_pasaxon_url, _entity_lao_html, _is_contaminated_template
from .commoncrawl_pasaxon_round5 import (
    PoliteHTTP, atomic_bytes, load_coverage, load_existing_urls, read_ndjson,
    utc_now, write_ndjson,
)
from .commoncrawl_pasaxon_round6 import strict_china_related
from .pasaxon_round4 import clean_modern_article_segment
from .wayback import parse_direct_pasaxon_article, pasaxon_url_identity


STAGING_NAME = "arquivo_pt_pasaxon_round1"
CDX_URL = (
    "https://arquivo.pt/wayback/cdx?url=pasaxon.org.la&matchType=domain"
    "&from=2012&to=2020&filter==status:200&filter=~mime:text/html"
    "&output=json&limit=100000"
)


def clean_arquivo_article(title: str, body: str) -> tuple[str, str]:
    """Recover the headline and exclude navigation/footer text in Arquivo.pt captures."""

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    compact_title = re.sub(r"[\s\u200b]+", "", title)
    generic = (
        ("ໜັງພິມ" in compact_title or "ໜັງສືພິມ" in compact_title or "ຫນັງສືພິມ" in compact_title)
        and "ປະຊາຊົນ" in compact_title and len(title) < 60
    )
    report_marker = re.sub(r"[\s\u200b]+", "", "ບົດລາຍງານ")
    footer_marker = re.sub(r"[\s\u200b]+", "", "ສະພາແຫ່ງຊາດ")
    if generic:
        marker = next(
            (i for i, line in enumerate(lines) if re.sub(r"[\s\u200b]+", "", line) == report_marker),
            None,
        )
        if marker is not None and marker + 2 < len(lines):
            recovered = lines[marker + 1]
            article_lines = lines[marker + 2:]
            boundary = next(
                (i for i, line in enumerate(article_lines)
                 if re.sub(r"[\s\u200b]+", "", line) == footer_marker),
                len(article_lines),
            )
            article_body = "\n\n".join(article_lines[:boundary])
            if len(article_body) >= 60:
                return recovered, article_body
    cleaned_title, cleaned_body = clean_modern_article_segment(title, body)
    cleaned_lines = [line.strip() for line in cleaned_body.splitlines() if line.strip()]
    boundary = next(
        (i for i, line in enumerate(cleaned_lines)
         if re.sub(r"[\s\u200b]+", "", line) == footer_marker),
        len(cleaned_lines),
    )
    trimmed = "\n\n".join(cleaned_lines[:boundary])
    return cleaned_title, trimmed if len(trimmed) >= 60 else cleaned_body


def extract_heading(payload: bytes) -> str | None:
    """Extract the visible article H1/H2/H3, which is more reliable than the site title."""

    source = _entity_lao_html(payload).decode("utf-8", "replace")
    for match in re.finditer(r"<h[1-3]\b[^>]*>(.*?)</h[1-3]>", source, re.IGNORECASE | re.DOTALL):
        value = html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
        value = re.sub(r"[\s\u200b]+", " ", value).strip()
        if len(value) >= 5 and sum("\u0e80" <= char <= "\u0eff" for char in value) >= 3:
            return value
    return None


def _record_from_screened(row: dict[str, object], *, title: str, body: str, hits: list[str], root: Path) -> dict[str, object]:
    body_bytes = body.encode("utf-8")
    body_sha = hashlib.sha256(body_bytes).hexdigest()
    body_path = root / "data/staging/archive_ocr" / STAGING_NAME / "text" / str(row["year_month"]) / f"{body_sha}.txt"
    atomic_bytes(body_path, body_bytes)
    suffix = hashlib.sha256(str(row["canonical_url"]).encode("utf-8")).hexdigest()[:16]
    return {
        "record_id": f"PASAXON-ARQUIVO-LO-{suffix}",
        "story_id": f"PASAXON-ARQUIVO-STORY-{suffix}",
        "source_code": "pasaxon_archive", "language": "lo",
        "title_original": title, "published_at": row["published_at"],
        "date_precision": "url_day", "body_original": body,
        "body_method": "arquivo_pt_official_html", "matched_queries": hits,
        "original_url": row["original_url"], "archive_url": row["archive_url"],
        "evidence_grade": "B1", "retrieval_tier": "T1_DIRECT_CHINA",
        "content_sha256": body_sha, "body_file": str(body_path.relative_to(root)),
        "raw_file": row["raw_file"], "raw_sha256": row["raw_sha256"],
        "retrieved_at": row.get("checked_at") or utc_now(),
        "archive_capture_timestamp": row["timestamp"], "archive_digest": row.get("digest"),
        "old_pasaxon_slot": row["slot"], "parser": "arquivo_pt_pasaxon_round1_v2",
        "metadata": {"archive_provider": "Arquivo.pt", "cdx_url": CDX_URL},
    }


def refresh_saved_records(*, root: Path) -> dict[str, object]:
    """Offline re-segment every saved response and rebuild the import list."""

    staging = root / "data/staging/archive_ocr" / STAGING_NAME
    ledger_path, records_path = staging / "screened.ndjson", staging / "records.ndjson"
    ledger = read_ndjson(ledger_path)
    refreshed: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    for row in ledger:
        if not row.get("raw_file"):
            refreshed.append(row)
            continue
        payload = gzip.decompress((root / str(row["raw_file"])).read_bytes())
        article = parse_direct_pasaxon_article(_entity_lao_html(payload), str(row["original_url"]))
        title, body = clean_arquivo_article(article.title, article.body)
        title = extract_heading(payload) or title
        related, hits = strict_china_related(f"{title}\n{body}")
        body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        body_path = staging / "text" / str(row["year_month"]) / f"{body_sha}.txt"
        atomic_bytes(body_path, body.encode("utf-8"))
        updated = {
            **row, "status": "china_match" if related else "not_china",
            "hits": hits, "title_original": title,
            "body_file": str(body_path.relative_to(root)), "content_sha256": body_sha,
            "offline_refreshed_at": utc_now(),
        }
        refreshed.append(updated)
        if related:
            records.append(_record_from_screened(updated, title=title, body=body, hits=hits, root=root))
    write_ndjson(ledger_path, refreshed); write_ndjson(records_path, records)
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({
        "importable_records": len(records),
        "importable_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in records).items())),
        "offline_refresh": {"parser": "arquivo_pt_pasaxon_round1_v2", "rows": len(refreshed), "refreshed_at": utc_now()},
    })
    atomic_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"screened": len(refreshed), "importable": len(records), "by_month": manifest["importable_by_month"]}


def parse_cdx(payload: bytes) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for number, line in enumerate(payload.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not item.get("url") or not item.get("timestamp"):
            raise ValueError(f"Arquivo.pt CDX row {number} lacks url/timestamp")
        rows.append(item)
    return rows


def exact_dated_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate captures and retain only non-PHP URLs with an independent URL day."""

    best: dict[str, dict[str, object]] = {}
    for row in rows:
        url = str(row["url"])
        if ".php" in url.casefold() or "?" in url:
            continue
        identity = pasaxon_url_identity(url)
        if not identity or not identity[0] or not ("2012-01-01" <= identity[0] <= "2020-12-31"):
            continue
        published, _, slot = identity
        canonical = _canonical_pasaxon_url(url)
        candidate = {
            "published_at": published, "year_month": published[:7], "slot": slot,
            "original_url": url, "canonical_url": canonical,
            "timestamp": str(row["timestamp"]), "digest": row.get("digest"),
            "mime": row.get("mime"), "status": row.get("status"),
            "archive_url": f"https://arquivo.pt/noFrame/replay/{row['timestamp']}id_/{url}",
        }
        old = best.get(canonical)
        if old is None or str(candidate["timestamp"]) > str(old["timestamp"]):
            best[canonical] = candidate
    return sorted(best.values(), key=lambda row: (str(row["year_month"]), str(row["published_at"]), int(row["slot"])))


def prioritize(
    rows: list[dict[str, object]], *, counts: dict[str, int], existing: set[str],
    target: int = 4,
) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        month = str(row["year_month"])
        if str(row["canonical_url"]) not in existing and counts.get(month, 0) < target:
            groups[month].append(row)
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
    *, root: Path, max_replay_requests: int = 90, interval: float = 1.0,
    target_per_month: int = 4,
) -> dict[str, object]:
    staging = root / "data/staging/archive_ocr" / STAGING_NAME
    raw_dir, text_dir = staging / "raw", staging / "text"
    ledger_path, records_path = staging / "screened.ndjson", staging / "records.ndjson"
    failures_path, pending_path = staging / "failures.ndjson", staging / "continuation_queue.ndjson"
    http = PoliteHTTP(interval=interval)
    ledger, records, failures = read_ndjson(ledger_path), read_ndjson(records_path), read_ndjson(failures_path)

    index_payload = http.get(CDX_URL, kind="index")
    atomic_bytes(staging / "arquivo_pt_cdx.ndjson", index_payload)
    index_rows = parse_cdx(index_payload)
    dated = exact_dated_rows(index_rows)
    counts, _ = load_coverage(root)
    existing = load_existing_urls(root)
    terminal = {str(row.get("canonical_url")) for row in ledger if row.get("canonical_url")}
    queue = prioritize(dated, counts=counts, existing=existing | terminal, target=target_per_month)
    matched = Counter(str(row["published_at"])[:7] for row in records)
    pending: list[dict[str, object]] = []
    stopped_reason = "queue_exhausted"

    for position, row in enumerate(queue):
        month = str(row["year_month"])
        if counts.get(month, 0) + matched[month] >= target_per_month:
            pending.append({**row, "queue_status": "month_target_met"})
            continue
        if http.requests["range"] >= max_replay_requests:
            pending.extend({**item, "queue_status": "replay_budget_exhausted"} for item in queue[position:])
            stopped_reason = "replay_budget_exhausted"
            break
        try:
            payload = http.get(str(row["archive_url"]), kind="range")
            raw_sha = hashlib.sha256(payload).hexdigest()
            raw_path = raw_dir / month / f"{raw_sha}.html.gz"
            atomic_bytes(raw_path, gzip.compress(payload, mtime=0))
            article = parse_direct_pasaxon_article(_entity_lao_html(payload), str(row["original_url"]))
            title, body = clean_arquivo_article(article.title, article.body)
            title = extract_heading(payload) or title
            if _is_contaminated_template(title, body):
                raise ValueError("compromised generic SEO template")
            related, hits = strict_china_related(f"{title}\n{body}")
            body_bytes = body.encode("utf-8")
            body_sha = hashlib.sha256(body_bytes).hexdigest()
            body_path = text_dir / month / f"{body_sha}.txt"
            atomic_bytes(body_path, body_bytes)
            audit_row = {
                **row, "status": "china_match" if related else "not_china", "hits": hits,
                "title_original": title, "body_file": str(body_path.relative_to(root)),
                "content_sha256": body_sha, "raw_file": str(raw_path.relative_to(root)),
                "raw_sha256": raw_sha, "checked_at": utc_now(),
            }
            ledger.append(audit_row)
            if related:
                records.append(_record_from_screened(
                    audit_row, title=title, body=body, hits=hits, root=root,
                ))
                matched[month] += 1
            write_ndjson(ledger_path, ledger)
            write_ndjson(records_path, records)
        except Exception as exc:
            failure = {**row, "stage": "replay", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now()}
            failures.append(failure); ledger.append(failure)
            write_ndjson(failures_path, failures); write_ndjson(ledger_path, ledger)

    write_ndjson(records_path, records); write_ndjson(pending_path, pending); write_ndjson(failures_path, failures)
    audit = Counter()
    for row in ledger:
        raw_value = row.get("raw_file")
        if not raw_value:
            continue
        raw_path = root / str(raw_value)
        audit["raw_exists"] += raw_path.is_file()
        if raw_path.is_file():
            payload = gzip.decompress(raw_path.read_bytes())
            audit["raw_sha_matches"] += hashlib.sha256(payload).hexdigest() == row.get("raw_sha256")
        body_value = row.get("body_file")
        if body_value:
            body_path = root / str(body_value)
            audit["body_exists"] += body_path.is_file()
            if body_path.is_file():
                audit["body_sha_matches"] += hashlib.sha256(body_path.read_bytes()).hexdigest() == row.get("content_sha256")
    manifest = {
        "generated_at": utc_now(), "archive_provider": "Arquivo.pt",
        "network_scope": ["arquivo.pt"], "canonical_database_written": False,
        "single_threaded": True, "interval_seconds": interval,
        "index_requests": http.requests["index"], "replay_requests": http.requests["range"],
        "replay_request_limit": max_replay_requests, "target_per_month": target_per_month,
        "cdx_rows": len(index_rows), "unique_exact_dated_urls": len(dated),
        "prioritized_rows": len(queue), "screened_rows": len(ledger),
        "importable_records": len(records),
        "importable_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in records).items())),
        "failure_rows": len(failures), "continuation_rows": len(pending),
        "stopped_reason": stopped_reason, "hash_audit": dict(audit),
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-replay-requests", type=int, default=90)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--target-per-month", type=int, default=4)
    args = parser.parse_args(argv)
    print(json.dumps(run(
        root=args.root.resolve(), max_replay_requests=args.max_replay_requests,
        interval=args.interval, target_per_month=args.target_per_month,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
