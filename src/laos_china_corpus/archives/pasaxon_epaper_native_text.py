"""Extract and screen native text from recovered Pasaxon e-paper PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .commoncrawl_pasaxon_round5 import atomic_bytes, read_ndjson, utc_now, write_ndjson


DIRECT_PATTERNS = (
    "ສປ ຈີນ", "ສປຈີນ", "ຈີນ", "ລາວ-ຈີນ", "ຈີນ-ລາວ",
    "China", "Chinese", "Lao-China", "Laos-China", "China-Laos",
)


def keyword_contexts(text: str, *, radius: int = 240) -> list[dict[str, object]]:
    """Return deduplicated direct-China keyword contexts."""

    hits: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for keyword in DIRECT_PATTERNS:
        flags = re.IGNORECASE if keyword[0].isascii() else 0
        for match in re.finditer(re.escape(keyword), text, flags):
            key = (keyword.casefold(), match.start())
            if key in seen:
                continue
            seen.add(key)
            hits.append({
                "keyword": keyword,
                "start": match.start(),
                "end": match.end(),
                "context": text[max(0, match.start() - radius):match.end() + radius].strip(),
            })
    return sorted(hits, key=lambda row: int(row["start"]))


def _item_dict(item: object) -> dict[str, object]:
    return {
        key: getattr(item, key, None)
        for key in ("text", "x", "y", "width", "height", "font_name", "font_size", "confidence")
    }


def extract_native_text(*, root: Path, staging_name: str = "pasaxon_epaper_archive_2022") -> dict[str, object]:
    """Parse all recovered PDFs locally; OCR is explicitly disabled."""

    try:
        from liteparse import LiteParse
    except ImportError as exc:
        raise RuntimeError("LiteParse 2.0.0 is required on PYTHONPATH") from exc
    staging = root / "data/staging/archive_ocr" / staging_name
    availability = read_ndjson(staging / "pdf_availability.ndjson")
    prior = read_ndjson(staging / "native_text_manifest.ndjson")
    done = {str(row.get("sha256")) for row in prior if row.get("sha256")}
    ledger = list(prior)
    parser = LiteParse(ocr_enabled=False, output_format="json", quiet=True)
    for source in availability:
        if source.get("status") != "pdf_recovered" or str(source.get("sha256")) in done:
            continue
        pdf_path = root / str(source["local_file"])
        result = parser.parse(str(pdf_path))
        digest = str(source["sha256"])
        pages: list[dict[str, object]] = []
        all_hits: list[dict[str, object]] = []
        for page in result.pages:
            page_text = str(page.text or "")
            page_hits = keyword_contexts(page_text)
            for hit in page_hits:
                hit["page_num"] = page.page_num
            all_hits.extend(page_hits)
            pages.append({
                "page_num": page.page_num,
                "width": page.width,
                "height": page.height,
                "text": page_text,
                "text_items": [_item_dict(item) for item in page.text_items],
                "china_keyword_hits": page_hits,
            })
        text = str(result.text or "")
        text_path = staging / "native_text" / f"{source['published_at']}_{digest}.txt"
        json_path = staging / "native_json" / f"{source['published_at']}_{digest}.json"
        atomic_bytes(text_path, text.encode("utf-8"))
        payload = {
            "source": "PASAXON", "published_at": source["published_at"],
            "issue_number": source["issue_number"], "pdf_url": source["pdf_url"],
            "archive_url": source["archive_url"], "pdf_file": source["local_file"],
            "pdf_sha256": digest, "parser": "liteparse_2.0.0_native_pdf_text",
            "ocr_used": False, "text": text, "pages": pages,
            "china_keyword_hits": all_hits,
        }
        atomic_bytes(json_path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        ledger.append({
            "published_at": source["published_at"], "issue_number": source["issue_number"],
            "sha256": digest, "pdf_file": source["local_file"],
            "text_file": str(text_path.relative_to(root)), "json_file": str(json_path.relative_to(root)),
            "pages": len(pages), "characters": len(text),
            "china_keyword_hits": len(all_hits),
            "hit_pages": sorted({int(row["page_num"]) for row in all_hits}),
            "parser": "liteparse_2.0.0_native_pdf_text", "ocr_used": False,
            "extracted_at": utc_now(),
        })
        write_ndjson(staging / "native_text_manifest.ndjson", ledger)
    status = {
        "generated_at": utc_now(), "recovered_pdfs": len(ledger),
        "total_pages": sum(int(row["pages"]) for row in ledger),
        "total_characters": sum(int(row["characters"]) for row in ledger),
        "issues_with_china_hits": sum(int(row["china_keyword_hits"]) > 0 for row in ledger),
        "china_keyword_hits": sum(int(row["china_keyword_hits"]) for row in ledger),
        "hit_pages": dict(sorted(Counter(
            f"{row['published_at']}:p{page}" for row in ledger for page in row["hit_pages"]
        ).items())),
        "ocr_used": False, "canonical_database_written": False,
        "next_step": "segment and verify hit-page articles before canonical import",
    }
    atomic_bytes(staging / "native_text_summary.json", json.dumps(status, ensure_ascii=False, indent=2).encode("utf-8"))
    return status


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--staging-name", default="pasaxon_epaper_archive_2022")
    args = parser.parse_args(argv)
    print(json.dumps(extract_native_text(root=args.root.resolve(), staging_name=args.staging_name), ensure_ascii=False))


if __name__ == "__main__":
    main()
