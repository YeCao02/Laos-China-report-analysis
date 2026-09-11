from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import ArticleRecord


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS articles (
  record_id TEXT PRIMARY KEY,
  source_code TEXT NOT NULL,
  source_article_id TEXT,
  story_id TEXT,
  language TEXT NOT NULL,
  title_original TEXT NOT NULL,
  excerpt_original TEXT,
  body_original TEXT,
  body_method TEXT NOT NULL DEFAULT 'none',
  published_at TEXT,
  date_precision TEXT NOT NULL,
  china_note_zh TEXT,
  topic_labels_json TEXT NOT NULL DEFAULT '[]',
  content_origin TEXT NOT NULL DEFAULT 'unknown',
  matched_queries_json TEXT NOT NULL DEFAULT '[]',
  original_url TEXT,
  archive_url TEXT,
  search_url TEXT,
  body_file TEXT,
  raw_file TEXT,
  evidence_grade TEXT NOT NULL,
  retrieval_tier TEXT NOT NULL,
  content_sha256 TEXT,
  ocr_confidence REAL,
  retrieved_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_code, language, source_article_id)
);
CREATE INDEX IF NOT EXISTS idx_articles_source_month ON articles(source_code, published_at);
CREATE INDEX IF NOT EXISTS idx_articles_story ON articles(story_id);

CREATE TABLE IF NOT EXISTS story_clusters (
  story_id TEXT PRIMARY KEY,
  source_code TEXT NOT NULL,
  representative_record_id TEXT NOT NULL,
  cluster_method TEXT NOT NULL,
  confidence REAL,
  manual_status TEXT NOT NULL DEFAULT 'unreviewed',
  FOREIGN KEY(representative_record_id) REFERENCES articles(record_id)
);

CREATE TABLE IF NOT EXISTS evidence_objects (
  evidence_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  evidence_grade TEXT NOT NULL,
  evidence_url TEXT,
  local_file TEXT,
  content_sha256 TEXT,
  observed_at TEXT,
  title_match INTEGER,
  date_match INTEGER,
  notes TEXT,
  FOREIGN KEY(record_id) REFERENCES articles(record_id)
);

CREATE TABLE IF NOT EXISTS crawl_partitions (
  partition_id TEXT PRIMARY KEY,
  source_code TEXT NOT NULL,
  query TEXT NOT NULL,
  date_from TEXT,
  date_to TEXT,
  page INTEGER,
  expected_hits INTEGER,
  parsed_rows INTEGER,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_checked_at TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS sample_memberships (
  sample_id TEXT NOT NULL,
  story_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  source_code TEXT NOT NULL,
  year_month TEXT NOT NULL,
  selection_rank INTEGER NOT NULL,
  score REAL NOT NULL,
  quota_reason TEXT NOT NULL,
  selected_at TEXT NOT NULL,
  PRIMARY KEY(sample_id, story_id),
  FOREIGN KEY(record_id) REFERENCES articles(record_id)
);

CREATE TABLE IF NOT EXISTS translation_queue (
  record_id TEXT PRIMARY KEY,
  story_id TEXT NOT NULL,
  source_language TEXT NOT NULL,
  body_sha256 TEXT,
  body_file TEXT,
  translation_status TEXT NOT NULL DEFAULT 'pending_provider_decision',
  provider TEXT,
  model TEXT,
  prompt_hash TEXT,
  queued_at TEXT NOT NULL,
  completed_at TEXT,
  FOREIGN KEY(record_id) REFERENCES articles(record_id)
);

CREATE TABLE IF NOT EXISTS run_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_article(conn: sqlite3.Connection, article: ArticleRecord) -> None:
    values = {
        **{name: getattr(article, name) for name in (
            "record_id", "source_code", "source_article_id", "story_id", "language",
            "title_original", "excerpt_original", "body_original", "body_method",
            "published_at", "date_precision", "china_note_zh", "content_origin",
            "original_url", "archive_url", "search_url", "body_file", "raw_file",
            "evidence_grade", "retrieval_tier", "content_sha256", "ocr_confidence",
            "retrieved_at",
        )},
        "topic_labels_json": json.dumps(article.topic_labels, ensure_ascii=False),
        "matched_queries_json": json.dumps(article.matched_queries, ensure_ascii=False),
        "metadata_json": json.dumps(article.metadata, ensure_ascii=False),
    }
    columns = list(values)
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{c}=excluded.{c}" for c in columns if c != "record_id")
    conn.execute(
        f"INSERT INTO articles ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(record_id) DO UPDATE SET {updates}",
        [values[c] for c in columns],
    )

