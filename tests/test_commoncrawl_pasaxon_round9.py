from __future__ import annotations

import unittest
from types import SimpleNamespace

from laos_china_corpus.archives.commoncrawl_pasaxon_round7 import (
    choose_unqueried_indexes,
    dated_candidates_for_years,
)


class CommonCrawlPasaxonRound9Tests(unittest.TestCase):
    @staticmethod
    def record(url: str) -> SimpleNamespace:
        return SimpleNamespace(
            url=url, timestamp="20200901000000", filename="x.warc.gz",
            offset=1, length=2, warc_url="https://data.commoncrawl.org/x",
            range_header="bytes=1-2", digest="sha1:test",
        )

    def test_only_remaining_2012_and_2020_indexes_are_selected(self):
        rows = [{"id": value} for value in (
            "CC-MAIN-2012", "CC-MAIN-2013-48", "CC-MAIN-2020-05",
            "CC-MAIN-2020-50", "CC-MAIN-2021-04",
        )]
        chosen = choose_unqueried_indexes(
            rows, excluded={"CC-MAIN-2020-50"}, slots=9,
            allowed_years=(2012, 2020),
        )
        self.assertEqual(chosen, ["CC-MAIN-2012", "CC-MAIN-2020-05"])

    def test_php_id_and_capture_time_never_supply_publication_date(self):
        rows = dated_candidates_for_years([
            self.record("http://pasaxon.org.la/index.php?p_id=20200817"),
            self.record("http://pasaxon.org.la/index.php/17-8-2020/12.html"),
            self.record("http://pasaxon.org.la/articles/17-8-2019/12.html"),
        ], source_index="CC-MAIN-2020-05", allowed_years=(2012, 2020))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"], "2020-08-17")


if __name__ == "__main__":
    unittest.main()
