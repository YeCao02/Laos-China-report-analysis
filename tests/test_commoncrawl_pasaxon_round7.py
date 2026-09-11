from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.archives.commoncrawl_pasaxon_round7 import (
    choose_unqueried_indexes,
    prioritize_candidates,
    queried_indexes,
)


class CommonCrawlPasaxonRound7Tests(unittest.TestCase):
    def test_current_round_manifest_is_excluded_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "data/staging/archive_ocr/commoncrawl_pasaxon_round7"
            staging.mkdir(parents=True)
            (staging / "manifest.json").write_text(json.dumps({
                "selected_indexes": ["CC-MAIN-2018-43"],
                "excluded_indexes": ["CC-MAIN-2017-47"],
            }), encoding="utf-8")
            excluded = queried_indexes(root)
            self.assertIn("CC-MAIN-2018-43", excluded)
            self.assertIn("CC-MAIN-2017-47", excluded)

    def test_selects_balanced_unqueried_indexes(self):
        rows = [{"id": f"CC-MAIN-{year}-{week}"} for year in range(2014, 2020) for week in (30, 40, 50)]
        chosen = choose_unqueried_indexes(rows, excluded={"CC-MAIN-2014-50", "CC-MAIN-2019-50"}, slots=9)
        self.assertEqual(len(chosen), 9)
        self.assertNotIn("CC-MAIN-2014-50", chosen)
        self.assertNotIn("CC-MAIN-2019-50", chosen)
        self.assertEqual(len(chosen), len(set(chosen)))

    def test_only_gap_months_and_new_urls_are_queued(self):
        rows = [
            {"canonical_url": "a", "year_month": "2018-01", "published_at": "2018-01-01", "slot": 1, "timestamp": "1"},
            {"canonical_url": "b", "year_month": "2018-02", "published_at": "2018-02-01", "slot": 1, "timestamp": "1"},
            {"canonical_url": "c", "year_month": "2018-03", "published_at": "2018-03-01", "slot": 1, "timestamp": "1"},
        ]
        result = prioritize_candidates(rows, counts={"2018-01": 0, "2018-02": 1, "2018-03": 2}, existing={"a"})
        self.assertEqual([row["canonical_url"] for row in result], ["b"])


if __name__ == "__main__":
    unittest.main()
