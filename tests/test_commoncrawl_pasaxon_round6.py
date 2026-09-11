from __future__ import annotations

import unittest

from laos_china_corpus.archives.commoncrawl_pasaxon_round6 import (
    prioritize_queue,
    strict_china_related,
    valid_queue_rows,
)


class CommonCrawlPasaxonRound6Tests(unittest.TestCase):
    def test_indochina_alone_is_not_china(self):
        self.assertFalse(strict_china_related("Indochina ອິນໂດຈີນ")[0])
        self.assertTrue(strict_china_related("ລາວ-ຈີນ")[0])

    def test_only_exact_url_date_survives(self):
        rows = [
            {"original_url": "http://pasaxon.org.la/hotnews/18-05-17/h1.html", "published_at": "2017-05-18"},
            {"original_url": "http://pasaxon.org.la/pasaxon-detail.php?p_id=1", "published_at": "2017-05-18"},
            {"original_url": "http://pasaxon.org.la/hotnews/18-05-17/h2.html", "published_at": "2017-05-19"},
        ]
        valid, rejected = valid_queue_rows(rows)
        self.assertEqual(len(valid), 1)
        self.assertEqual(rejected, 2)

    def test_prioritizes_zero_count_and_excludes_quota_met(self):
        rows = [
            {"canonical_url": "a", "year_month": "2017-01", "published_at": "2017-01-02", "slot": 1},
            {"canonical_url": "b", "year_month": "2017-02", "published_at": "2017-02-02", "slot": 1},
            {"canonical_url": "c", "year_month": "2017-03", "published_at": "2017-03-02", "slot": 1},
        ]
        queue = prioritize_queue(rows, current_counts={"2017-01": 1, "2017-02": 0, "2017-03": 2})
        self.assertEqual([row["canonical_url"] for row in queue], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
