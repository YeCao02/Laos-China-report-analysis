from __future__ import annotations

import unittest
from types import SimpleNamespace

from laos_china_corpus.archives.commoncrawl_pasaxon_2021_2022 import (
    article_url_kind,
    build_replay_queue,
    candidate_rows,
)


class CommonCrawlPasaxon20212022Tests(unittest.TestCase):
    @staticmethod
    def record(url: str, timestamp: str = "20220101000000") -> SimpleNamespace:
        return SimpleNamespace(
            url=url, timestamp=timestamp, filename="x.warc.gz", offset=10, length=20,
            warc_url="https://data.commoncrawl.org/x", range_header="bytes=10-29", digest="sha1:x",
        )

    def test_article_shapes_require_page_date_when_url_has_no_date(self):
        self.assertEqual(
            article_url_kind("http://pasaxon.org.la/pasaxon-detail.php?p_id=3001&act=politic-detail"),
            "php_detail_page_date_required",
        )
        self.assertEqual(
            article_url_kind("https://pasaxon.org.la/lao-china-railway-1234.html"),
            "slug_article_page_date_required",
        )
        self.assertEqual(
            article_url_kind("http://pasaxon.org.la/articles/2021/12/03/2.html"),
            "exact_dated",
        )
        self.assertIsNone(article_url_kind("https://pasaxon.org.la/tags/china.html?page=2"))

    def test_latest_capture_per_canonical_article_is_retained(self):
        rows = candidate_rows([
            self.record("http://www.pasaxon.org.la/pasaxon-detail.php?p_id=3001&act=politic-detail", "20210101"),
            self.record("http://pasaxon.org.la:80/pasaxon-detail.php?p_id=3001&act=politic-detail", "20220101"),
        ], source_index="CC-MAIN-2022-05")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["capture_timestamp"], "20220101")

    def test_replay_queue_deduplicates_php_id_and_prefers_cooperation(self):
        rows = [
            {"original_url": "http://pasaxon.org.la/pasaxon-detail.php?p_id=5&act=politic-detail", "canonical_url": "p5-politic", "capture_timestamp": "20210101"},
            {"original_url": "http://pasaxon.org.la/pasaxon-detail.php?p_id=5&act=cooperation-detail", "canonical_url": "p5-coop", "capture_timestamp": "20210201"},
            {"original_url": "http://pasaxon.org.la/pasaxon-detail.php?p_id=6&act=economic-detail", "canonical_url": "p6", "capture_timestamp": "20220101"},
        ]
        queue = build_replay_queue(rows)
        self.assertEqual(len(queue), 2)
        self.assertIn("cooperation-detail", queue[0]["original_url"])


if __name__ == "__main__":
    unittest.main()
