from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from laos_china_corpus.archives.commoncrawl_pasaxon_listings import (
    build_listing_queue,
    listing_kind,
    parse_listing_refs,
)


class CommonCrawlPasaxonListingTests(unittest.TestCase):
    def test_listing_kind_excludes_detail_and_unrelated_pages(self):
        self.assertEqual(listing_kind("http://pasaxon.org.la/showlistcooperation.php"), "/showlistcooperation.php")
        self.assertEqual(listing_kind("http://pasaxon.org.la/"), "/index.php")
        self.assertIsNone(listing_kind("http://pasaxon.org.la/showlistnotice_detail.php?p_id=1"))
        self.assertIsNone(listing_kind("http://pasaxon.org.la/pasaxon-detail.php?p_id=1"))

    def test_extracts_only_dated_china_titles(self):
        page = """
        <a href="pasaxon-detail.php?p_id=3802&amp;act=cooperation-detail">
          ລາວ-ຈີນ ເປີດໂຄງການ 4x100
        </a><p>31/03/2021 08:33:42</p>
        <a href="pasaxon-detail.php?p_id=3803&amp;act=economic-detail">ຂ່າວລາວ</a>
        <p>2021-03-31 09:00:00</p>
        """
        rows = parse_listing_refs(page, "http://pasaxon.org.la/showlistcooperation.php")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_identity"], "php:3802")
        self.assertEqual(rows[0]["published_at"], "2021-03-31")
        self.assertIn("p_id=3802", rows[0]["original_url"])
        byte_rows = parse_listing_refs(page.encode("utf-8"), "http://pasaxon.org.la/showlistcooperation.php")
        self.assertEqual(byte_rows, rows)

    def test_rejects_capture_without_page_date_and_out_of_scope_date(self):
        no_date = '<a href="pasaxon-detail.php?p_id=1&act=cooperation-detail">ລາວ-ຈີນ</a>'
        old = '<a href="pasaxon-detail.php?p_id=2&act=cooperation-detail">ລາວ-ຈີນ</a><p>31/03/2020</p>'
        self.assertEqual(parse_listing_refs(no_date, "http://pasaxon.org.la/index.php"), [])
        self.assertEqual(parse_listing_refs(old, "http://pasaxon.org.la/index.php"), [])


if __name__ == "__main__":
    unittest.main()
