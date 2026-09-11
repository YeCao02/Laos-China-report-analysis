from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ArticleRecord:
    record_id: str
    source_code: str
    language: str
    title_original: str
    published_at: str | None
    date_precision: str = "indexed_timestamp"
    source_article_id: str | None = None
    story_id: str | None = None
    excerpt_original: str | None = None
    body_original: str | None = None
    body_method: str = "none"
    china_note_zh: str | None = None
    topic_labels: list[str] = field(default_factory=list)
    content_origin: str = "unknown"
    matched_queries: list[str] = field(default_factory=list)
    original_url: str | None = None
    archive_url: str | None = None
    search_url: str | None = None
    body_file: str | None = None
    raw_file: str | None = None
    evidence_grade: str = "C2"
    retrieval_tier: str = "T4_ARCHIVE_DISCOVERY"
    content_sha256: str | None = None
    ocr_confidence: float | None = None
    retrieved_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def year_month(self) -> str | None:
        return self.published_at[:7] if self.published_at and len(self.published_at) >= 7 else None


EVIDENCE_GRADES = {"A1", "A2", "B1", "B2", "C1", "C2"}
RETRIEVAL_TIERS = {
    "T1_DIRECT_CHINA",
    "T2_BILATERAL_VARIANT",
    "T3_ENTITY_TOPIC",
    "T4_ARCHIVE_DISCOVERY",
}

