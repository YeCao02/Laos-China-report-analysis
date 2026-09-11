from __future__ import annotations

import unittest
from types import SimpleNamespace

from laos_china_corpus.archives.commoncrawl_pasaxon_round7 import (
    choose_unqueried_indexes,
    dated_candidates_for_years,
)


class CommonCrawlPasaxonRound8Tests(unittest.TestCase):
    def test_selects_only_2013_and_2020_and_excludes_contaminated_index(self):
        rows = [{"id": value} for value in (
            "CC-MAIN-2013-20", "CC-MAIN-2013-48", "CC-MAIN-2019-51",
            "CC-MAIN-2020-05", "CC-MAIN-2020-50",
        )]
        chosen = choose_unqueried_indexes(
            rows, excluded={"CC-MAIN-2020-50"}, slots=9, allowed_years=(2013, 2020),
        )
        self.assertEqual(chosen, ["CC-MAIN-2013-48", "CC-MAIN-2020-05", "CC-MAIN-2013-20"])

    def test_rejects_php_query_and_wrong_year_but_keeps_exact_date_html(self):
        def record(url: str):
            return SimpleNamespace(
                url=url, timestamp="20200101000000", filename="x.warc.gz",
                offset=1, length=2, warc_url="https://data.commoncrawl.org/x",
                range_header="bytes=1-2", digest="sha1:test",
            )
        rows = dated_candidates_for_years([
            record("http://pasaxon.org.la/articles/3-4-2013/1.html"),
            record("http://pasaxon.org.la/index.php?p_id=1"),
            record("http://pasaxon.org.la/articles/3-4-2019/2.html"),
        ], source_index="CC-MAIN-2020-05", allowed_years=(2013, 2020), non_php_only=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"], "2013-04-03")


if __name__ == "__main__":
    unittest.main()
