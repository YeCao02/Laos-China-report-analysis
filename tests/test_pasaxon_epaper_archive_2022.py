import unittest

from laos_china_corpus.archives.pasaxon_epaper_archive_2022 import (
    parse_issue_detail,
    parse_issue_listing,
    raw_wayback_url,
)


class PasaxonEpaperArchiveTests(unittest.TestCase):
    def test_wayback_replay_is_forced_to_raw_bytes(self) -> None:
        self.assertEqual(
            raw_wayback_url("https://web.archive.org/web/20220922101815/http://example.test/a.pdf"),
            "https://web.archive.org/web/20220922101815id_/http://example.test/a.pdf",
        )

    def test_listing_extracts_issue_date_id_and_cover(self) -> None:
        html = """
        <div class='card'><img src='ppdf/cover.jpg'>
        <a href='pdf-detail.php?p_id=642&act=pdf-detail'>14.059(30.09.2022)</a></div>
        """
        issue = parse_issue_listing(html, "http://pasaxon.org.la/showlistpdf.php?page=1")[0]
        self.assertEqual(issue.p_id, 642)
        self.assertEqual(issue.published_at, "2022-09-30")
        self.assertEqual(issue.cover_url, "http://pasaxon.org.la/ppdf/cover.jpg")

    def test_detail_extracts_pdf_without_promoting_upload_date(self) -> None:
        html = """
        <img src='ppdf/cover.jpg'><h4>ສະບັບທີ: 14.053(22.09.2022)</h4>
        <a href='pdfs/247722-9-2022.pdf'>download</a>
        """
        issue = parse_issue_detail(
            html, "https://pasaxon.org.la/pdf-detail.php?p_id=636&act=pdf-detail"
        )
        self.assertEqual(issue.p_id, 636)
        self.assertEqual(issue.published_at, "2022-09-22")
        self.assertEqual(issue.pdf_url, "https://pasaxon.org.la/pdfs/247722-9-2022.pdf")


if __name__ == "__main__":
    unittest.main()
