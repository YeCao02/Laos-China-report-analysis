import unittest

from laos_china_corpus.archives.commoncrawl_pasaxon_epaper_pdfs import publication_date_from_pdf_url


class CommonCrawlPasaxonEpaperPdfTests(unittest.TestCase):
    def test_random_prefix_is_not_part_of_issue_day(self) -> None:
        self.assertEqual(publication_date_from_pdf_url("http://x/pdfs/950520-01-2022_compressed.pdf"), "2022-01-20")
        self.assertEqual(publication_date_from_pdf_url("https://x/pdfs/247722-9-2022.pdf"), "2022-09-22")

    def test_invalid_or_out_of_scope_date_is_rejected(self) -> None:
        self.assertIsNone(publication_date_from_pdf_url("http://x/pdfs/news.pdf"))
        self.assertIsNone(publication_date_from_pdf_url("http://x/pdfs/111531-02-2022.pdf"))


if __name__ == "__main__":
    unittest.main()
