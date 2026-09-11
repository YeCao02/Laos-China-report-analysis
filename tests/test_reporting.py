from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.config import ProjectPaths
from laos_china_corpus.db import connect, upsert_article
from laos_china_corpus.models import ArticleRecord
from laos_china_corpus.reporting import build_status_report_artifact


class ReportingTests(unittest.TestCase):
    def test_report_artifact_has_auditable_chart_and_all_historical_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.ensure()
            conn = connect(paths.database)
            upsert_article(conn, ArticleRecord(
                record_id="KPL-1", source_code="KPL-EN", language="en",
                source_article_id="1", story_id="KPL-STORY-1", title_original="China",
                published_at="2012-01-20", body_original="body", evidence_grade="B1",
                retrieval_tier="T1_DIRECT_CHINA",
            ))
            upsert_article(conn, ArticleRecord(
                record_id="KPL-2", source_code="kpl_archive", language="lo",
                source_article_id="2", story_id="KPL-STORY-2", title_original="ຈີນ",
                published_at="2012-01-21", body_original="body", evidence_grade="B1",
                retrieval_tier="T1_DIRECT_CHINA",
            ))
            conn.commit()
            out = build_status_report_artifact(conn, paths)
            artifact = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(artifact["surface"], "report")
            self.assertEqual(len(artifact["snapshot"]["datasets"]["yearly"]), 30)
            self.assertEqual(len(artifact["snapshot"]["datasets"]["historical_coverage"]), 2)
            self.assertTrue(any(block["type"] == "chart" for block in artifact["manifest"]["blocks"]))
            self.assertEqual(artifact["manifest"]["charts"][0]["sourceId"], "corpus_sqlite")
            kpl_2012 = next(row for row in artifact["snapshot"]["datasets"]["yearly"]
                            if row["source"] == "KPL" and row["year"] == "2012")
            self.assertEqual(kpl_2012["body_months"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
