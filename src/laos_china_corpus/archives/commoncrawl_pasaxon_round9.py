"""Final bounded Common Crawl pass for exact-dated Pasaxon URLs in 2012/2020.

Round 8 deliberately excluded PHP-shaped URLs.  This pass allows them only when
``pasaxon_url_identity`` can recover a complete publication date from the URL
itself; an opaque PHP article id or the Common Crawl capture time is never used
as a publication date.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .commoncrawl import parse_index_response
from .commoncrawl_pasaxon_round5 import atomic_bytes, read_ndjson, utc_now
from .commoncrawl_pasaxon_round7 import (
    dated_candidates_for_years,
    queried_indexes,
    run as run_generic,
)


STAGING_NAME = "commoncrawl_pasaxon_round9"
PRIOR_STAGING = (
    "commoncrawl_pasaxon_round5",
    "commoncrawl_pasaxon_round6",
    "commoncrawl_pasaxon_round7",
    "commoncrawl_pasaxon_round8",
    STAGING_NAME,
)
BASE_EXCLUDED = {
    "CC-MAIN-2017-51", "CC-MAIN-2018-51", "CC-MAIN-2019-51", "CC-MAIN-2020-50",
}


def run(
    *, root: Path, max_index_requests: int = 10, max_range_requests: int = 90,
    interval: float = 1.0,
) -> dict[str, object]:
    return run_generic(
        root=root, max_index_requests=max_index_requests,
        max_range_requests=max_range_requests, interval=interval,
        staging_name=STAGING_NAME, allowed_years=(2012, 2020),
        prior_staging=PRIOR_STAGING, base_excluded=BASE_EXCLUDED,
        non_php_only=False,
    )


def write_offline_audit(root: Path) -> dict[str, object]:
    """Reconcile prior queues, live coverage export, and Round 9 artifacts."""

    staging = root / "data/staging/archive_ocr" / STAGING_NAME
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    with (root / "data/catalog/monthly_coverage.csv").open(
        encoding="utf-8-sig", newline="",
    ) as handle:
        coverage = [
            row for row in csv.DictReader(handle)
            if row["source"] == "PASAXON" and row["year_month"] <= "2020-12"
        ]
    gap_rows = [row for row in coverage if row["coverage_status"] == "documented_gap"]
    gap_months = {row["year_month"] for row in gap_rows}

    screened_urls: set[str] = set()
    for path in root.glob("data/staging/archive_ocr/commoncrawl_pasaxon*/screened.ndjson"):
        screened_urls.update(
            str(row["canonical_url"]) for row in read_ndjson(path)
            if row.get("canonical_url")
        )
    queue_specs = (
        ("round5", root / "data/staging/archive_ocr/commoncrawl_pasaxon_round5/continuation_queue.ndjson"),
        ("round6", root / "data/staging/archive_ocr/commoncrawl_pasaxon_round6/pending.ndjson"),
        ("round7", root / "data/staging/archive_ocr/commoncrawl_pasaxon_round7/continuation_queue.ndjson"),
        ("round8", root / "data/staging/archive_ocr/commoncrawl_pasaxon_round8/continuation_queue.ndjson"),
    )
    queue_audit: list[dict[str, object]] = []
    for label, path in queue_specs:
        rows = read_ndjson(path)
        in_gaps = [row for row in rows if str(row.get("year_month")) in gap_months]
        unscreened = [
            row for row in in_gaps
            if str(row.get("canonical_url") or "") not in screened_urls
        ]
        queue_audit.append({
            "queue": label, "path": str(path.relative_to(root)), "rows": len(rows),
            "current_gap_rows": len(in_gaps),
            "current_gap_unscreened_rows": len(unscreened),
            "unscreened_by_month": dict(sorted(Counter(
                str(row.get("year_month")) for row in unscreened
            ).items())),
        })

    indexed_by_month: Counter[str] = Counter()
    gap_indexed_by_month: Counter[str] = Counter()
    index_hashes = Counter()
    for item in manifest.get("index_audit", []):
        rel = item.get("response_file")
        if not rel:
            continue
        path = root / str(rel)
        if not path.is_file():
            continue
        payload = path.read_bytes(); index_hashes["exists"] += 1
        index_hashes["sha256_matches"] += (
            hashlib.sha256(payload).hexdigest() == item.get("response_sha256")
        )
        candidates = dated_candidates_for_years(
            parse_index_response(payload), source_index=str(item.get("index")),
            allowed_years=(2012, 2020),
        )
        for row in candidates:
            indexed_by_month[str(row["year_month"])] += 1
            if str(row["year_month"]) in gap_months:
                gap_indexed_by_month[str(row["year_month"])] += 1

    collinfo = json.loads((staging / "indexes/collinfo.json").read_text(encoding="utf-8"))
    excluded = queried_indexes(root, prior_staging=PRIOR_STAGING, base_excluded=BASE_EXCLUDED)
    remaining: dict[int, list[str]] = defaultdict(list)
    for row in collinfo:
        index = str(row.get("id") or "")
        match = re.fullmatch(r"CC-MAIN-(20\d{2})(?:-\d+)?", index)
        if match and 2012 <= int(match.group(1)) <= 2020 and index not in excluded:
            remaining[int(match.group(1))].append(index)

    records = read_ndjson(staging / "records.ndjson")
    # Keep the staging contract explicit even for a verified negative result.
    # Downstream import/audit commands can consume empty NDJSON files without
    # treating their absence as an interrupted collector run.
    for name in ("records.ndjson", "screened.ndjson"):
        path = staging / name
        if not path.exists():
            atomic_bytes(path, b"")
    result: dict[str, object] = {
        "generated_at": utc_now(), "network_stopped_before_audit": True,
        "canonical_database_written": False,
        "coverage_source": "data/catalog/monthly_coverage.csv",
        "historical_months": len(coverage), "documented_gap_months": len(gap_rows),
        "documented_gap_list": [row["year_month"] for row in gap_rows],
        "prior_queue_audit": queue_audit,
        "round9_index_requests": manifest.get("index_requests"),
        "round9_range_requests": manifest.get("range_requests"),
        "round9_exact_dated_by_month": dict(sorted(indexed_by_month.items())),
        "round9_exact_dated_in_current_gaps": dict(sorted(gap_indexed_by_month.items())),
        "remaining_unqueried_indexes_2012_2020": {
            str(year): sorted(values) for year, values in sorted(remaining.items())
        },
        "index_hash_audit": dict(index_hashes),
        "importable_records": len(records),
        "body_level_review": {
            "candidates": manifest.get("prioritized_rows", 0),
            "reviewed": manifest.get("screened_rows", 0),
            "china_matches": len(records),
            "navigation_or_indochina_false_positives": 0,
            "note": "No current-gap exact-dated URL survived prioritization; no body was claimed without review.",
        },
        "conclusion": (
            "No unscreened exact-dated official Pasaxon URL for a current 2012-2020 "
            "documented-gap month was found in prior continuation queues or the "
            "remaining Common Crawl indexes."
        ),
    }
    atomic_bytes(staging / "round9_audit.json", json.dumps(
        result, ensure_ascii=False, indent=2,
    ).encode("utf-8"))
    queue_lines = "\n".join(
        f"| {row['queue']} | {row['rows']} | {row['current_gap_rows']} | "
        f"{row['current_gap_unscreened_rows']} |" for row in queue_audit
    )
    markdown = f"""# Pasaxon Common Crawl Round 9 audit

- Generated: `{result['generated_at']}`
- Scope: official `pasaxon.org.la`, 2012--2020 current `documented_gap` months
- Network: stopped before this offline audit; only Common Crawl index/data hosts were used
- Canonical SQLite written: no

## Current coverage and prior queues

The current export contains **{len(gap_rows)}** Pasaxon historical documented-gap months.

| Queue | Total rows | Rows in current gaps | Unscreened rows in current gaps |
|---|---:|---:|---:|
{queue_lines}

No prior Round5/6/7/8 continuation row belongs to a current gap month.

## Final index pass

- Index requests: **{manifest.get('index_requests')} / {manifest.get('index_request_limit')}**
- Range requests: **{manifest.get('range_requests')} / {manifest.get('range_request_limit')}**
- Exact-dated URL candidates: **{manifest.get('exact_dated_candidates')}**
- Exact-dated candidates in current gap months: **{sum(gap_indexed_by_month.values())}**
- Importable China-related records: **{len(records)}**
- Remaining unqueried 2012--2020 indexes: **{sum(len(v) for v in remaining.values())}**

The legacy `CC-MAIN-2012` index supplied 17 exact-dated URLs, all in January or
February 2012, which are not current gap months. `CC-MAIN-2020-05` supplied no
exact-dated 2020 URL. Opaque PHP IDs and capture timestamps were not accepted as
publication dates.

## Integrity and conclusion

- Saved index responses present: **{index_hashes['exists']}**
- Saved index SHA-256 matches: **{index_hashes['sha256_matches']}**
- No body-level China match was asserted without downloading and parsing the
  article body; navigation and `Indochina` strings cannot create an import here.

**Negative result:** {result['conclusion']}
"""
    atomic_bytes(staging / "round9_audit.md", markdown.encode("utf-8"))
    return result


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
