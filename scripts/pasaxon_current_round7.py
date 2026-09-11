from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from laos_china_corpus.adapters.pasaxon import (  # noqa: E402
    PasaxonParseError,
    build_tag_url,
    parse_article,
    parse_search_results,
)
from pasaxon_current_round6 import (  # noqa: E402
    BoundedFetcher,
    StopNetwork,
    decode,
    direct_hits,
    in_window,
    sha256,
    utcnow,
    write_json,
    write_ndjson,
)


ROUND6 = ROOT / "data" / "staging" / "pasaxon_current_round6"
DEFAULT_OUT = ROOT / "data" / "staging" / "pasaxon_current_round7"
ROUND6_LAO_REQUESTED = {40, 50, 60, 70, 80, 90}


def read_ndjson(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def save_article(out: Path, row: dict[str, object], record, raw_sha: str, raw_file: str) -> dict[str, object]:  # noqa: ANN001
    data = asdict(record)
    text = record.body_original or ""
    body_sha = sha256(text.encode("utf-8"))
    records_dir = out / "articles"
    records_dir.mkdir(parents=True, exist_ok=True)
    body_path = records_dir / f"{record.record_id}.md"
    body_path.write_text(
        f"# {record.title_original}\n\n- Published: {record.published_at}\n- URL: {record.original_url}\n\n{text}\n",
        encoding="utf-8",
    )
    data.update({
        "raw_file": raw_file,
        "body_file": body_path.relative_to(ROOT).as_posix(),
        "raw_sha256": raw_sha,
        "body_sha256": body_sha,
        "listing_raw_file": row["listing_raw_file"],
        "listing_page": row["page"],
        "listing_query": row["query"],
        "china_direct_hits": direct_hits(record.title_original + "\n" + text),
        "round": "pasaxon_current_round7",
    })
    (records_dir / f"{record.record_id}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return data


def page_plan() -> list[tuple[str, int]]:
    lao = [("ຈີນ", page) for page in range(40, 100) if page not in ROUND6_LAO_REQUESTED]
    english = [("China", page) for page in range(40, 100)]
    return lao + english


def run(out: Path, cap: int, delay: float) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    fetcher = BoundedFetcher(out, cap, delay)
    round6_records = read_ndjson(ROUND6 / "records.ndjson")
    baseline = Counter(str(row.get("published_at") or "")[:7] for row in round6_records)
    baseline_urls = {str(row.get("original_url")) for row in round6_records}
    new_counts: Counter[str] = Counter()
    records: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    seen_urls = set(baseline_urls)
    pages_completed: list[dict[str, object]] = []

    try:
        for query, page in page_plan():
            if fetcher.count >= cap:
                break
            listing_url = build_tag_url(query, page)
            try:
                payload, final_url, listing_raw = fetcher.fetch(
                    listing_url, f"round7:tag:{query}:{page}"
                )
                listing = parse_search_results(decode(payload), final_url, query)
                page_candidates: list[dict[str, object]] = []
                for discovery in listing.discoveries:
                    row = {
                        "url": discovery.url,
                        "source_article_id": discovery.source_article_id,
                        "title_original": discovery.title_original,
                        "listing_published_at": discovery.published_at,
                        "query": query,
                        "page": page,
                        "listing_url": final_url,
                        "listing_raw_file": listing_raw,
                        "status": "discovered",
                    }
                    candidates.append(row)
                    if discovery.url not in seen_urls:
                        page_candidates.append(row)
                pages_completed.append({
                    "query": query, "page": page, "url": final_url,
                    "discoveries": len(listing.discoveries),
                    "new_urls": len(page_candidates), "listing_raw_file": listing_raw,
                })
                # One representative detail per page makes date calibration systematic
                # across the full historical range while retaining a body budget.
                if not page_candidates or fetcher.count >= cap:
                    continue
                row = page_candidates[0]
                article_url = str(row["url"])
                seen_urls.add(article_url)
                try:
                    body_payload, article_final, raw_file = fetcher.fetch(
                        article_url, f"round7:article:{query}:page{page}"
                    )
                    record = parse_article(
                        decode(body_payload), article_final,
                        matched_queries=[query], search_url=final_url,
                        retrieved_at=utcnow(),
                    )
                    if not in_window(record.published_at):
                        raise ValueError(f"article date outside round window: {record.published_at}")
                    month = str(record.published_at)[:7]
                    if baseline[month] + new_counts[month] >= 2:
                        raise ValueError(f"month already at combined cap 2: {month}")
                    text = record.title_original + "\n" + (record.body_original or "")
                    if not direct_hits(text):
                        raise ValueError("no direct China term in full article")
                    if not record.body_original or len(record.body_original.strip()) < 100:
                        raise ValueError("body too short for full-text inclusion")
                    saved = save_article(out, row, record, sha256(body_payload), raw_file)
                    saved["baseline_month_count"] = baseline[month]
                    saved["combined_month_count_after"] = baseline[month] + new_counts[month] + 1
                    records.append(saved)
                    new_counts[month] += 1
                    write_ndjson(out / "records.ndjson", records)
                    row["status"] = "included"
                    row["article_published_at"] = record.published_at
                except StopNetwork:
                    raise
                except (PasaxonParseError, ValueError, HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
                    row["status"] = "excluded_after_detail"
                    row["detail_error"] = f"{type(exc).__name__}: {exc}"
                    fetcher.failures.append({
                        "url": article_url, "purpose": "round7_article_validation",
                        "query": query, "page": page, "at": utcnow(),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    fetcher._flush()
            except StopNetwork:
                raise
            except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, ValueError) as exc:
                fetcher.failures.append({
                    "url": listing_url, "purpose": "round7_listing",
                    "query": query, "page": page, "at": utcnow(),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                fetcher._flush()
    except StopNetwork:
        pass
    finally:
        write_ndjson(out / "candidates.ndjson", candidates)
        write_ndjson(out / "records.ndjson", records)
        write_ndjson(out / "pages_completed.ndjson", pages_completed)
        fetcher._flush()

    combined = Counter(baseline)
    combined.update(new_counts)
    coverage = {
        f"{year}-{month:02d}": {
            "round6_baseline": baseline[f"{year}-{month:02d}"],
            "round7_added": new_counts[f"{year}-{month:02d}"],
            "combined": combined[f"{year}-{month:02d}"],
        }
        for year in range(2023, 2026) for month in range(1, 13)
    }
    audit = {
        "round": "pasaxon_current_round7",
        "completed_at": utcnow(),
        "network_stopped": True,
        "network_stop_reason": fetcher.stop_reason or "bounded page plan completed; no further requests scheduled",
        "allowed_host": "pasaxon.org.la",
        "forbidden_sources_used": [],
        "request_cap": cap,
        "request_count": fetcher.count,
        "minimum_delay_seconds": fetcher.delay,
        "page_range": "40-99 only",
        "page_100_or_above_requested": False,
        "round6_lao_pages_skipped": sorted(ROUND6_LAO_REQUESTED),
        "pages_completed": len(pages_completed),
        "pages_by_query": dict(Counter(str(row["query"]) for row in pages_completed)),
        "candidate_rows": len(candidates),
        "unique_candidate_urls": len({str(row["url"]) for row in candidates}),
        "valid_fulltext_records": len(records),
        "failures_or_exclusions": len(fetcher.failures),
        "coverage": coverage,
        "months_improved": sorted(month for month, count in new_counts.items() if count),
        "sqlite_written": False,
    }
    write_json(out / "audit.json", audit)
    (out / "README.md").write_text(
        "# Pasaxon current-site Round7\n\n"
        f"- Requests: {fetcher.count}/{cap}, single threaded, minimum {fetcher.delay:.2f}s between requests.\n"
        "- Host scope: only `pasaxon.org.la`; page 100 and above forbidden.\n"
        f"- Listing pages completed: {len(pages_completed)}; valid new full texts: {len(records)}.\n"
        f"- Months improved over Round6: {', '.join(audit['months_improved']) or 'none'}.\n"
        f"- Stop reason: {audit['network_stop_reason']}.\n\n"
        "Round6 records are used only as a read-only monthly-cap baseline. Listing snippets are discovery "
        "evidence and never count as articles. Each included record passed official-host, article timestamp, "
        "full-body, direct-China-term, combined monthly cap, and raw/body SHA-256 checks. No SQLite was written.\n",
        encoding="utf-8",
    )
    return audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--request-cap", type=int, default=120)
    parser.add_argument("--delay", type=float, default=1.05)
    args = parser.parse_args()
    if not 1 <= args.request_cap <= 120:
        parser.error("--request-cap must be 1..120")
    audit = run(args.out.resolve(), args.request_cap, args.delay)
    print(json.dumps(audit, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
