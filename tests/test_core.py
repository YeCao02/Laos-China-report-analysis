from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.clustering import assign_story_ids
from laos_china_corpus.config import ProjectPaths
from laos_china_corpus.db import connect, upsert_article
from laos_china_corpus.exporter import export_monthly_coverage
from laos_china_corpus.inventory import export_source_year_inventory
from laos_china_corpus.models import ArticleRecord
from laos_china_corpus.normalize import canonicalize_url, normalize_text
from laos_china_corpus.sampling import select_monthly_sample


class CoreTests(unittest.TestCase):
    def test_normalize_lao_without_losing_visible_characters(self):
        self.assertEqual(normalize_text("ລາວ\u200b-ຈີນ   2026"), "ລາວ-ຈີນ 2026")

    def test_canonical_url_keeps_id(self):
        self.assertEqual(
            canonicalize_url("HTTPS://KPL.GOV.LA/detail.aspx?id=45&utm_source=x#frag"),
            "https://kpl.gov.la/detail.aspx?id=45",
        )

    def test_bilingual_kpl_versions_share_story_but_remain_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "db.sqlite")
            for language, record_id in (("lo", "KPL-LAO-000045"), ("en", "KPL-EN-000045")):
                upsert_article(conn, ArticleRecord(
                    record_id=record_id, source_code=f"KPL-{language.upper()}", language=language,
                    source_article_id="45", title_original=record_id,
                    published_at="2014-06-17T03:37", evidence_grade="A1",
                    retrieval_tier="T1_DIRECT_CHINA",
                ))
            conn.commit()
            assign_story_ids(conn)
            rows = conn.execute("SELECT record_id,story_id FROM articles ORDER BY record_id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["story_id"], rows[1]["story_id"])
            conn.close()

    def test_sample_counts_unique_story_and_requires_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "db.sqlite")
            for index in range(6):
                upsert_article(conn, ArticleRecord(
                    record_id=f"P-{index}", source_code="PASAXON", language="lo",
                    source_article_id=str(index), story_id=f"P-STORY-{index}",
                    title_original=f"ລາວ-ຈີນ {index}", published_at=f"2025-01-{index+1:02d}",
                    body_original="body", evidence_grade="A1", retrieval_tier="T1_DIRECT_CHINA",
                    content_origin="reprint" if index < 4 else "local_original",
                    metadata={"scope": "中老双边"},
                ))
            conn.commit()
            selected = select_monthly_sample(conn, target=4)
            self.assertEqual(selected, 4)
            syndicated = conn.execute(
                "SELECT count(*) FROM sample_memberships s JOIN articles a ON a.record_id=s.record_id "
                "WHERE a.content_origin='reprint'"
            ).fetchone()[0]
            self.assertLessEqual(syndicated, 2)
            conn.close()

    def test_coverage_has_352_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ProjectPaths(root)
            paths.ensure()
            conn = connect(paths.database)
            count = export_monthly_coverage(conn, paths)
            self.assertEqual(count, 352)
            conn.close()

    def test_inventory_normalizes_archive_source_and_counts_all_historical_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ProjectPaths(root)
            paths.ensure()
            conn = connect(paths.database)
            upsert_article(conn, ArticleRecord(
                record_id="PA-2012-1", source_code="pasaxon_archive", language="lo",
                source_article_id="1", story_id="PA-STORY-1", title_original="ຈີນ",
                published_at="2012-01-17", body_original="body", evidence_grade="B1",
                retrieval_tier="T1_DIRECT_CHINA",
            ))
            conn.execute(
                "INSERT INTO sample_memberships VALUES (?,?,?,?,?,?,?,?,?)",
                ("test", "PA-STORY-1", "PA-2012-1", "PASAXON", "2012-01", 1, 1.0,
                 "test", "2026-08-13T00:00:00Z"),
            )
            conn.commit()
            export_source_year_inventory(conn, paths)
            report = json.loads((paths.audit / "source_inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(report["families"][0]["family"], "PASAXON")
            historical = report["historical_coverage_2012_2020"]["PASAXON"]
            self.assertEqual(historical["documented_gap"], 108)
            conn.close()


if __name__ == "__main__":
    unittest.main()
