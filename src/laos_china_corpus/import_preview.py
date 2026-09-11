from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from .adapters.kpl import normalize_text, parse_kpl_timestamp
from .db import upsert_article
from .models import ArticleRecord


_HEADING_RE = re.compile(
    r"^###\s+(KPL-(LAO|EN)-(\d+))\uff5c"
    r"(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(?::\d{2})?)\uff5c(.*?)\s*$",
    re.MULTILINE,
)
_FIELD_RE = re.compile(r"^-\s*([^\uff1a\n]+)\uff1a(.*)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class PreviewImportStats:
    parsed: int
    unique: int
    lao: int
    english: int


def _content_origin(label: str) -> str:
    if "中国媒体" in label or "新华" in label:
        return "xinhua"
    if "其他媒体" in label or "供稿" in label or "转载" in label:
        return "syndicated"
    if "本地原创" in label:
        return "local_original"
    return "unknown"


def _evidence_grade(label: str) -> str:
    match = re.search(r"(?<![A-Z0-9])(A1|A2|B1|B2|C1|C2)(?![A-Z0-9])", label)
    return match.group(1) if match else "C2"


def _matched_query(search_url: str | None) -> list[str]:
    if not search_url:
        return []
    query = parse_qs(urlparse(search_url).query).get("search", [])
    return [normalize_text(value) for value in query if normalize_text(value)]


def _fields(block: str) -> dict[str, str]:
    return {
        normalize_text(match.group(1)): normalize_text(match.group(2))
        for match in _FIELD_RE.finditer(block)
    }


def parse_preview_markdown(text: str) -> list[ArticleRecord]:
    matches = list(_HEADING_RE.finditer(text))
    records: list[ArticleRecord] = []
    for index, match in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        values = _fields(text[match.end():block_end])
        record_id, marker, numeric_id, timestamp_raw, title = match.groups()
        language = "lo" if marker == "LAO" else "en"
        published_at, _ = parse_kpl_timestamp(timestamp_raw)
        scope = values.get("范围初筛", "")
        origin_label = values.get("稿源线索", "")
        evidence_label = values.get("证据", "")
        original_url = values.get("原文") or None
        search_url = values.get("检索页") or None
        topic_labels = ["bilateral"] if ("中老双边" in scope or "中国在老挝" in scope) else ["china_general"]
        records.append(
            ArticleRecord(
                record_id=record_id,
                source_code="kpl_lao" if language == "lo" else "kpl_english",
                source_article_id=str(int(numeric_id)),
                story_id=record_id,
                language=language,
                title_original=normalize_text(title),
                excerpt_original=values.get("摘录") or None,
                published_at=published_at,
                date_precision="indexed_timestamp",
                body_method="none",
                topic_labels=topic_labels,
                content_origin=_content_origin(origin_label),
                matched_queries=_matched_query(search_url),
                original_url=original_url,
                search_url=search_url,
                evidence_grade=_evidence_grade(evidence_label),
                retrieval_tier="T1_DIRECT_CHINA",
                metadata={
                    "scope": scope,
                    "origin_note": origin_label,
                    "evidence_note": evidence_label,
                    "search_indexed_at": published_at,
                    "search_indexed_at_raw": timestamp_raw,
                    "seed_source": "preview.md",
                    "parser_version": "kpl-preview-v1",
                },
            )
        )
    return records


def load_preview(path: Path) -> list[ArticleRecord]:
    return parse_preview_markdown(path.read_text(encoding="utf-8-sig"))


def preview_stats(records: Iterable[ArticleRecord]) -> PreviewImportStats:
    records = list(records)
    unique_ids = {record.record_id for record in records}
    return PreviewImportStats(
        parsed=len(records),
        unique=len(unique_ids),
        lao=sum(record.language == "lo" for record in records),
        english=sum(record.language == "en" for record in records),
    )


def import_preview(conn: sqlite3.Connection, path: Path) -> int:
    records = load_preview(path)
    stats = preview_stats(records)
    if stats.unique != stats.parsed:
        raise ValueError(
            f"preview contains duplicate record IDs: {stats.parsed} rows, {stats.unique} unique"
        )
    for record in records:
        upsert_article(conn, record)
    conn.commit()
    return stats.parsed
