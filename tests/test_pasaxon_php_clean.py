from __future__ import annotations

import unittest

from laos_china_corpus.archives.pasaxon_php_clean import parse_clean_php_article, titles_agree


class PasaxonPhpCleanTests(unittest.TestCase):
    def test_removes_hidden_injection_but_keeps_verified_article(self):
        title = "ການນຳສູງສຸດລາວ-ຈີນ ກຳນົດ 2021 ເປັນປີມິດຕະພາບ"
        body = "ຈີນ ແລະ ລາວ " * 20
        page = f"""
        <p class="cok">Lapak online <a href="spam">agen togel</a></p>
        <div><h4>{title}</h4><small>ເວລາ: 17/02/2021 14:37:04</small>
        <p class="text-justify"><p>{body}</p></p></div>
        """
        article = parse_clean_php_article(
            page.encode("utf-8"), expected_title=title.replace(" ", "  "), expected_date="2021-02-17",
        )
        self.assertEqual(article.published_date, "2021-02-17")
        self.assertEqual(article.removed_injection_nodes, 1)
        self.assertNotIn("togel", article.body)
        self.assertIn("ຈີນ", article.body)

    def test_rejects_listing_title_or_date_conflict(self):
        title = "ລາວ-ຈີນ ຮ່ວມມື"
        page = f'<h4>{title}</h4><small>17/02/2021</small><p class="text-justify">{"ຈີນ ລາວ " * 30}</p>'
        with self.assertRaisesRegex(ValueError, "heading/date"):
            parse_clean_php_article(page, expected_title="ຫົວຂໍ້ອື່ນ", expected_date="2021-02-17")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            parse_clean_php_article(page, expected_title=title, expected_date="2021-02-18")

    def test_title_agreement_ignores_spacing_and_zero_width(self):
        self.assertTrue(titles_agree("ລາວ-ຈີນ", "ລາວ - \u200bຈີນ"))


if __name__ == "__main__":
    unittest.main()
