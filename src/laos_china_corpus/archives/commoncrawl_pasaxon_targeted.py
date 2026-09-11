"""Build offline detail-page replay queues from verified Pasaxon listing leads."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .commoncrawl import parse_index_response
from .commoncrawl_pasaxon import _canonical_pasaxon_url
from .commoncrawl_pasaxon_2021_2022 import _php_identity
from .commoncrawl_pasaxon_round5 import atomic_bytes, read_ndjson, utc_now, write_ndjson


def _capture_distance(capture: str, published: str) -> tuple[int, int]:
    capture_day = datetime.strptime(capture[:8], "%Y%m%d").date()
    published_day = datetime.strptime(published, "%Y-%m-%d").date()
    delta = (capture_day - published_day).days
    # Prefer the closest capture on/after publication.  Pre-publication
    # captures cannot contain the article and are retained only as fallbacks.
    return (0, delta) if delta >= 0 else (1, abs(delta))


def build_target_queue(
    *, root: Path, listing_staging: str, source_stagings: list[str],
    target_staging: str, capture_rank: int = 1,
) -> dict[str, object]:
    """Select one ranked capture per verified listing p_id without network access."""

    if capture_rank < 1:
        raise ValueError("capture_rank must be >= 1")
    discoveries_path = root / "data/staging/archive_ocr" / listing_staging / "discoveries.ndjson"
    discoveries = read_ndjson(discoveries_path)
    targets = {int(row["p_id"]): row for row in discoveries}
    index_files: list[Path] = []
    for name in source_stagings:
        index_files.extend(sorted((root / "data/staging/archive_ocr" / name / "indexes").glob("CC*.ndjson")))

    available: dict[int, list[dict[str, object]]] = {p_id: [] for p_id in targets}
    for path in index_files:
        source_index = path.stem
        for record in parse_index_response(path.read_bytes()):
            identity = _php_identity(record.url)
            if not identity or identity[0] not in targets:
                continue
            available[identity[0]].append({
                "source_index": source_index,
                "url_kind": "php_detail_page_date_required",
                "original_url": record.url,
                "canonical_url": _canonical_pasaxon_url(record.url),
                "capture_timestamp": record.timestamp,
                "filename": record.filename,
                "offset": record.offset,
                "length": record.length,
                "warc_url": record.warc_url,
                "range_header": record.range_header,
                "digest": record.digest,
                "listing_published_at": targets[identity[0]]["published_at"],
                "listing_title_original": targets[identity[0]]["title_original"],
                "listing_evidence_file": str(discoveries_path.relative_to(root)),
                "listing_evidence_grade": "A2",
            })

    selected: list[dict[str, object]] = []
    unavailable: list[dict[str, object]] = []
    capture_counts: dict[int, int] = {}
    for p_id, lead in sorted(targets.items()):
        choices = sorted(available[p_id], key=lambda row: (
            _capture_distance(str(row["capture_timestamp"]), str(lead["published_at"])),
            str(row["source_index"]),
            str(row["original_url"]),
        ))
        # Deduplicate repeated URLs within the same index.
        unique: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for row in choices:
            key = (str(row["source_index"]), str(row["canonical_url"]))
            if key not in seen:
                unique.append(row)
                seen.add(key)
        capture_counts[p_id] = len(unique)
        if len(unique) >= capture_rank:
            selected.append(unique[capture_rank - 1])
        else:
            unavailable.append({
                "candidate_identity": f"php:{p_id}",
                "p_id": p_id,
                "published_at": lead["published_at"],
                "title_original": lead["title_original"],
                "available_capture_count": len(unique),
                "requested_capture_rank": capture_rank,
                "status": "no_ranked_detail_capture",
            })

    staging = root / "data/staging/archive_ocr" / target_staging
    write_ndjson(staging / "candidates.ndjson", selected)
    write_ndjson(staging / "unavailable.ndjson", unavailable)
    write_ndjson(staging / "failures.ndjson", [])
    manifest = {
        "generated_at": utc_now(),
        "offline_build": True,
        "network_scope": [],
        "listing_staging": listing_staging,
        "source_stagings": source_stagings,
        "saved_index_files": len(index_files),
        "target_leads": len(targets),
        "capture_rank": capture_rank,
        "selected_detail_candidates": len(selected),
        "unavailable_targets": len(unavailable),
        "available_capture_count_distribution": dict(sorted(Counter(capture_counts.values()).items())),
        "capture_date_used_as_publication_date": False,
        "canonical_database_written": False,
        "next_step": "run the bounded 2021_2022 replay against this staging directory",
    }
    atomic_bytes(staging / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--listing-staging", default="commoncrawl_pasaxon_2021_2022_listings")
    parser.add_argument("--source-staging", action="append", required=True)
    parser.add_argument("--target-staging", required=True)
    parser.add_argument("--capture-rank", type=int, default=1)
    args = parser.parse_args(argv)
    result = build_target_queue(
        root=args.root.resolve(), listing_staging=args.listing_staging,
        source_stagings=args.source_staging, target_staging=args.target_staging,
        capture_rank=args.capture_rank,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
