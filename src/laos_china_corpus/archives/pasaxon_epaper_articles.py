"""Create article-level staging records from verified Pasaxon e-paper regions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from .commoncrawl_pasaxon_round5 import atomic_bytes, read_ndjson, utc_now, write_ndjson
from .pasaxon_epaper_native_text import keyword_contexts


ARTICLE_SPECS = (
    {
        "published_at": "2022-01-20", "page_num": 6,
        "column_ranges": ((80, 128), (128, 167)), "y_range": (5, 32),
        "title_original": "ຍອດປະລິມານເສດຖະກິດຈີນ ບັນລຸເຖິງ 110.000 ຕື້ຢວນ",
        "china_note_zh": "CRI供稿称，中国2021年经济总量达到约110万亿元，并讨论人均GDP、发展基础和高质量发展。",
        "topic_labels": ["china_economy"], "content_origin": "cri",
        "manual_review": "title visually verified on official issue page; native body is complete",
    },
    {
        "published_at": "2022-01-28", "page_num": 3,
        "column_ranges": ((4, 45), (45, 87), (87, 128)), "y_range": (64, 112),
        "title_original": "ສະຖານທູດຈີນ ປະຈໍາລາວ ມອບເຄື່ອງຊ່ວຍເຫຼືອສະຫະພັນແມ່ຍິງ ກະຊວງການຕ່າງປະເທດ",
        "china_note_zh": "中国驻老挝使馆向老挝外交部妇联捐赠防疫和学习用品，总值584,216.80元（约10.52亿基普）。",
        "topic_labels": ["bilateral", "china_in_laos", "aid_health"],
        "content_origin": "local_original",
        "manual_review": "title and three-column article boundary visually verified on official issue page",
    },
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _keep_item(text: str) -> bool:
    """Reject obvious legacy-font keyboard debris without rewriting Lao text."""

    if any("\u0e80" <= char <= "\u0eff" for char in text):
        return True
    if re.search(r"\b(?:CRI|GDP|UNICEF|Jiang|Zaidong|Beate|Das|tel)\b", text, re.I):
        return True
    return bool(re.fullmatch(r"[\d\s.,()/%:+-]+", text))


def extract_column_region(
    page: dict[str, object], *, column_ranges: tuple[tuple[float, float], ...],
    y_range: tuple[float, float],
) -> str:
    """Read a newspaper article in column order from LiteParse text items."""

    columns: list[str] = []
    items = list(page.get("text_items") or [])
    for x_min, x_max in column_ranges:
        selected = [
            item for item in items
            if x_min <= float(item.get("x") or 0) < x_max
            and y_range[0] <= float(item.get("y") or 0) <= y_range[1]
            and str(item.get("text") or "").strip()
        ]
        selected.sort(key=lambda item: (float(item.get("y") or 0), float(item.get("x") or 0)))
        lines = []
        for item in selected:
            value = re.sub(r"[ \t]+", " ", str(item.get("text") or "").strip())
            if _keep_item(value):
                lines.append(value)
        columns.append("\n".join(lines))
    return "\n\n".join(value for value in columns if value).strip() + "\n"


def stage_verified_articles(
    *, root: Path, staging_name: str = "pasaxon_epaper_archive_2022",
) -> dict[str, object]:
    """Generate deterministic, hash-linked article records; never write the database."""

    staging = root / "data/staging/archive_ocr" / staging_name
    issue_by_date = {str(row["published_at"]): row for row in read_ndjson(staging / "issues.ndjson")}
    native_by_date: dict[str, tuple[Path, dict[str, object]]] = {}
    for path in sorted((staging / "native_json").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        native_by_date[str(payload["published_at"])] = (path, payload)

    rows: list[dict[str, object]] = []
    for spec in ARTICLE_SPECS:
        published = str(spec["published_at"])
        issue = issue_by_date[published]
        native_path, native = native_by_date[published]
        pdf_path = root / str(native["pdf_file"])
        pdf_bytes = pdf_path.read_bytes()
        if not pdf_bytes.startswith(b"%PDF"):
            raise ValueError(f"not a PDF: {pdf_path}")
        if _sha256(pdf_bytes) != str(native["pdf_sha256"]):
            raise ValueError(f"PDF SHA-256 mismatch: {pdf_path}")
        page = next(
            value for value in list(native["pages"])
            if int(value["page_num"]) == int(spec["page_num"])
        )
        body = extract_column_region(
            page, column_ranges=tuple(spec["column_ranges"]), y_range=tuple(spec["y_range"])
        )
        hits = keyword_contexts(body)
        if len(body) < 500 or not hits:
            raise ValueError(f"article region failed body/China gate: {published} p{spec['page_num']}")
        identity = f"{native['pdf_sha256']}:{spec['page_num']}:{spec['title_original']}"
        suffix = _sha256(identity.encode("utf-8"))[:16]
        body_path = staging / "article_text" / f"PASAXON-EPAPER-{suffix}.txt"
        atomic_bytes(body_path, body.encode("utf-8"))
        body_sha = _sha256(body.encode("utf-8"))
        native_bytes = native_path.read_bytes()
        rows.append({
            "record_id": f"PASAXON-EPAPER-LO-{suffix}",
            "story_id": f"PASAXON-EPAPER-STORY-{suffix}",
            "source_code": "pasaxon_archive", "language": "lo",
            "source_article_id": f"epaper-{published}-p{spec['page_num']}",
            "published_at": published, "date_precision": "official_issue_day",
            "title_original": spec["title_original"], "body_original": body,
            "body_method": "liteparse_native_pdf_region", "content_sha256": body_sha,
            "china_note_zh": spec["china_note_zh"], "topic_labels": spec["topic_labels"],
            "content_origin": spec["content_origin"],
            "matched_queries": sorted({str(hit["keyword"]) for hit in hits}),
            "original_url": issue["detail_url"], "archive_url": native["archive_url"],
            "body_file": str(body_path.relative_to(root)),
            "raw_file": str(pdf_path.relative_to(root)), "raw_sha256": native["pdf_sha256"],
            "native_json_file": str(native_path.relative_to(root)),
            "native_json_sha256": _sha256(native_bytes), "retrieved_at": utc_now(),
            "evidence_grade": "B1", "retrieval_tier": "T1_DIRECT_CHINA",
            "metadata": {
                "issue_number": issue["issue_number"], "p_id": issue["p_id"],
                "pdf_url": native["pdf_url"], "pdf_sha256": native["pdf_sha256"],
                "page_num": spec["page_num"], "column_ranges": spec["column_ranges"],
                "y_range": spec["y_range"], "parser": native["parser"], "ocr_used": False,
                "manual_review": spec["manual_review"],
            },
        })
    output = staging / "article_records.ndjson"
    write_ndjson(output, rows)
    summary = {
        "generated_at": utc_now(), "articles": len(rows),
        "dates": [row["published_at"] for row in rows],
        "body_characters": sum(len(str(row["body_original"])) for row in rows),
        "output": str(output.relative_to(root)), "canonical_database_written": False,
    }
    atomic_bytes(
        staging / "article_staging_summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--staging-name", default="pasaxon_epaper_archive_2022")
    args = parser.parse_args(argv)
    print(json.dumps(stage_verified_articles(root=args.root.resolve(), staging_name=args.staging_name), ensure_ascii=False))


if __name__ == "__main__":
    main()
