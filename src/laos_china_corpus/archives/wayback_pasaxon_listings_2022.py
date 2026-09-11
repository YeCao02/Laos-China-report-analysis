"""Bounded Wayback discovery across five high-value Pasaxon 2022 listing pages."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError

from .commoncrawl_pasaxon_listings import parse_listing_refs
from .commoncrawl_pasaxon_round5 import atomic_bytes, read_ndjson, utc_now, write_ndjson
from .pasaxon_round2 import ArchiveBlocked, PoliteFetcher
from .wayback import build_exact_cdx_query, parse_cdx


STAGING_NAME = "wayback_pasaxon_listings_2022"
BASE = "http://www.pasaxon.org.la"
LISTINGS = (
    "/showlistcooperation.php",
    "/showlisteconomic.php",
    "/showlistpolitic.php",
    "/showlistforigner.php",
    "/index.php",
)


def discover(
    *, root: Path, interval: float = 1.0, staging_name: str = STAGING_NAME,
    listings: tuple[str, ...] = LISTINGS,
) -> dict[str, object]:
    staging = root / "data/staging/archive_ocr" / staging_name
    fetcher = PoliteFetcher(interval=interval, timeout=60)
    candidates: list[dict[str, object]] = []
    ledger: list[dict[str, object]] = []
    consecutive_errors = 0
    stop_reason = "queue_exhausted"
    for path in listings:
        url = BASE + path
        query = build_exact_cdx_query(url, 2022)
        try:
            payload = fetcher.fetch(query)
            lower = payload[:20000].decode("utf-8", errors="ignore").casefold()
            if "captcha" in lower or "verify you are human" in lower:
                raise ArchiveBlocked("Wayback CDX challenge page")
            sha = hashlib.sha256(payload).hexdigest()
            output = staging / "cdx" / f"{path.strip('/').replace('.php','')}-{sha}.json"
            atomic_bytes(output, payload)
            captures = parse_cdx(payload)
            # Keep one capture per listing/capture month; capture month is only
            # a replay partition and never an article publication date.
            by_month = {}
            for capture in sorted(captures, key=lambda item: item.timestamp):
                by_month.setdefault(capture.timestamp[:6], capture)
            for capture in by_month.values():
                candidates.append({
                    "listing_kind": path, "listing_url": url,
                    "capture_timestamp": capture.timestamp,
                    "capture_original_url": capture.original_url,
                    "capture_digest": capture.digest,
                    "archive_url": f"https://web.archive.org/web/{capture.timestamp}id_/{capture.original_url}",
                    "cdx_file": str(output.relative_to(root)), "cdx_sha256": sha,
                })
            ledger.append({
                "listing_kind": path, "status": "complete", "query_url": query,
                "capture_count": len(captures), "monthly_capture_count": len(by_month),
                "response_file": str(output.relative_to(root)), "response_sha256": sha,
                "checked_at": utc_now(),
            })
            consecutive_errors = 0
        except (ArchiveBlocked, HTTPError, URLError, TimeoutError, ValueError) as exc:
            ledger.append({
                "listing_kind": path, "status": "failed", "query_url": query,
                "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now(),
            })
            consecutive_errors += 1
            if isinstance(exc, ArchiveBlocked) or consecutive_errors >= 3:
                stop_reason = "archive_error_circuit_breaker"
                break
    candidates.sort(key=lambda row: (str(row["capture_timestamp"])[:6], listings.index(str(row["listing_kind"]))))
    write_ndjson(staging / "queries.ndjson", ledger)
    write_ndjson(staging / "candidates.ndjson", candidates)
    manifest = {
        "generated_at": utc_now(), "mode": "discover", "network_scope": ["web.archive.org"],
        "listing_queries": len(ledger), "status_counts": dict(Counter(str(row["status"]) for row in ledger)),
        "candidate_rows": len(candidates), "candidate_capture_months": sorted({str(row["capture_timestamp"])[:6] for row in candidates}),
        "stopped_reason": stop_reason, "capture_date_used_as_publication_date": False,
        "canonical_database_written": False,
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def replay(
    *, root: Path, max_requests: int = 40, interval: float = 1.0,
    staging_name: str = STAGING_NAME,
) -> dict[str, object]:
    staging = root / "data/staging/archive_ocr" / staging_name
    candidates = read_ndjson(staging / "candidates.ndjson")
    ledger_path = staging / "screened.ndjson"
    discovery_path = staging / "discoveries.ndjson"
    failure_path = staging / "failures.ndjson"
    ledger = read_ndjson(ledger_path)
    discoveries = read_ndjson(discovery_path)
    failures = read_ndjson(failure_path)
    done = {(str(row.get("listing_kind")), str(row.get("capture_timestamp"))) for row in ledger if row.get("status") == "screened"}
    by_id = {str(row["candidate_identity"]): row for row in discoveries}
    fetcher = PoliteFetcher(interval=interval, timeout=60)
    requests = 0
    consecutive_errors = 0
    pending: list[dict[str, object]] = []
    stop_reason = "queue_exhausted"
    for position, row in enumerate(candidates):
        key = (str(row["listing_kind"]), str(row["capture_timestamp"]))
        if key in done:
            continue
        if requests >= max_requests:
            pending.extend({**item, "queue_status": "replay_budget_exhausted"} for item in candidates[position:])
            stop_reason = "replay_budget_exhausted"
            break
        requests += 1
        try:
            payload = fetcher.fetch(str(row["archive_url"]))
            sha = hashlib.sha256(payload).hexdigest()
            raw_path = staging / "raw" / str(row["capture_timestamp"])[:6] / f"{sha}.html.gz"
            atomic_bytes(raw_path, gzip.compress(payload))
            refs = parse_listing_refs(payload, str(row["listing_url"]))
            for ref in refs:
                enriched = {
                    **ref, "evidence_grade": "A2",
                    "evidence_scope": "wayback_listing_title_date_url_only_no_article_body",
                    "archive_url": row["archive_url"], "listing_kind": row["listing_kind"],
                    "capture_timestamp": row["capture_timestamp"],
                    "raw_file": str(raw_path.relative_to(root)), "raw_sha256": sha,
                    "payload_sha256": sha, "retrieved_at": utc_now(),
                    "archive_provider": "Wayback",
                }
                prior = by_id.get(str(ref["candidate_identity"]))
                if prior is None or str(enriched["capture_timestamp"]) < str(prior["capture_timestamp"]):
                    by_id[str(ref["candidate_identity"])] = enriched
            ledger.append({**row, "status": "screened", "china_refs": len(refs),
                           "raw_file": str(raw_path.relative_to(root)), "raw_sha256": sha,
                           "checked_at": utc_now()})
            done.add(key)
            consecutive_errors = 0
        except (ArchiveBlocked, HTTPError, URLError, TimeoutError, ValueError) as exc:
            failure = {**row, "status": "failed", "error": f"{type(exc).__name__}: {exc}", "checked_at": utc_now()}
            ledger.append(failure)
            failures.append(failure)
            consecutive_errors += 1
            if isinstance(exc, ArchiveBlocked) or consecutive_errors >= 3:
                pending.extend({**item, "queue_status": "archive_error_circuit_breaker"} for item in candidates[position + 1:])
                stop_reason = "archive_error_circuit_breaker"
                break
        write_ndjson(ledger_path, ledger)
        write_ndjson(discovery_path, sorted(by_id.values(), key=lambda item: (str(item["published_at"]), int(item["p_id"]))))
        write_ndjson(failure_path, failures)
    write_ndjson(staging / "continuation_queue.ndjson", pending)
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["replay"] = {
        "generated_at": utc_now(), "network_scope": ["web.archive.org"],
        "requests": requests, "request_limit": max_requests, "screened_rows": len(ledger),
        "unique_china_discoveries": len(by_id),
        "discoveries_by_month": dict(sorted(Counter(str(row["published_at"])[:7] for row in by_id.values()).items())),
        "failure_rows": len(failures), "continuation_rows": len(pending), "stopped_reason": stop_reason,
        "canonical_database_written": False, "capture_date_used_as_publication_date": False,
    }
    atomic_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest["replay"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("discover", "replay"), required=True)
    parser.add_argument("--staging-name", default=STAGING_NAME)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--listing-kind", action="append")
    args = parser.parse_args(argv)
    result = discover(root=args.root.resolve(), interval=args.interval, staging_name=args.staging_name,
                      listings=tuple(args.listing_kind or LISTINGS)) if args.mode == "discover" else replay(
        root=args.root.resolve(), interval=args.interval, staging_name=args.staging_name, max_requests=args.max_requests,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
