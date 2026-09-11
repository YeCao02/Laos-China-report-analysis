from __future__ import annotations

import unittest

from laos_china_corpus.archives.commoncrawl import parse_index_response
from laos_china_corpus.archives.commoncrawl_pasaxon_round5 import (
    EXCLUDED_INDEXES,
    choose_indexes,
    exact_dated_candidates,
    prioritize,
)


class CommonCrawlPasaxonRound5Tests(unittest.TestCase):
    def test_index_selection_is_real_year_bounded_and_excludes_completed(self):
        rows = [
            {"id": f"CC-MAIN-{year}-{week}"}
            for year in range(2014, 2020) for week in (20, 40, 51)
        ]
        selected = choose_indexes(rows, query_slots=9)
        self.assertEqual(len(selected), 9)
        self.assertFalse(set(selected) & EXCLUDED_INDEXES)
        self.assertEqual(sum(index.startswith("CC-MAIN-2014-") for index in selected), 2)
        self.assertEqual(sum(index.startswith("CC-MAIN-2019-") for index in selected), 1)

    def test_only_exact_url_day_candidates_are_promoted(self):
        records = parse_index_response([
            {"url": "http://pasaxon.org.la/hotnews/14-05-17/h1.html", "timestamp": "20170515000000", "filename": "a.warc.gz", "offset": 0, "length": 10},
            {"url": "http://pasaxon.org.la/econo/2019/11/e1.html", "timestamp": "20191201000000", "filename": "b.warc.gz", "offset": 0, "length": 10},
            {"url": "http://pasaxon.org.la/pasaxon-detail.php?p_id=1", "timestamp": "20191201000000", "filename": "c.warc.gz", "offset": 0, "length": 10},
        ])
        rows = exact_dated_candidates(records, source_index="CC-MAIN-2017-40")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"], "2017-05-14")

    def test_gap_month_and_unique_url_are_prioritized(self):
        rows = [
            {"canonical_url": "pasaxon.org.la/a", "year_month": "2018-04", "published_at": "2018-04-02", "slot": 1, "timestamp": "1"},
            {"canonical_url": "pasaxon.org.la/a", "year_month": "2018-04", "published_at": "2018-04-02", "slot": 1, "timestamp": "2"},
            {"canonical_url": "pasaxon.org.la/b", "year_month": "2017-05", "published_at": "2017-05-02", "slot": 1, "timestamp": "1"},
        ]
        queue = prioritize(rows, gaps={"2018-04"}, existing=set())
        self.assertEqual([row["canonical_url"] for row in queue], ["pasaxon.org.la/a", "pasaxon.org.la/b"])
        self.assertEqual(queue[0]["timestamp"], "2")


if __name__ == "__main__":
    unittest.main()
