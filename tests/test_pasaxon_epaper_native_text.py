import unittest

from laos_china_corpus.archives.pasaxon_epaper_articles import extract_column_region
from laos_china_corpus.archives.pasaxon_epaper_native_text import keyword_contexts


class PasaxonEpaperNativeTextTests(unittest.TestCase):
    def test_direct_lao_and_english_hits_keep_context(self) -> None:
        hits = keyword_contexts("abc ລາວ-ຈີນ xyz China-Laos end", radius=4)
        self.assertTrue(any(row["keyword"] == "ລາວ-ຈີນ" for row in hits))
        self.assertTrue(any(row["keyword"] == "China-Laos" for row in hits))
        self.assertTrue(all(row["context"] for row in hits))

    def test_non_china_text_has_no_hits(self) -> None:
        self.assertEqual(keyword_contexts("ຂ່າວປະຈໍາວັນ"), [])

    def test_article_region_reads_columns_and_excludes_neighbour(self) -> None:
        page = {"text_items": [
            {"x": 4, "y": 2, "text": "ກ ຈີນ"},
            {"x": 4, "y": 3, "text": "ຂ"},
            {"x": 45, "y": 2, "text": "ຄ"},
            {"x": 128, "y": 2, "text": "ບົດຂ້າງຄຽງ"},
            {"x": 4, "y": 20, "text": "ນອກແນວຕັ້ງ"},
            {"x": 4, "y": 4, "text": "5dly> 'm"},
        ]}
        text = extract_column_region(
            page, column_ranges=((4, 45), (45, 87)), y_range=(2, 10)
        )
        self.assertEqual(text, "ກ ຈີນ\nຂ\n\nຄ\n")


if __name__ == "__main__":
    unittest.main()
