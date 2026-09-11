from __future__ import annotations

import json
import unittest

from laos_china_corpus.archives.arquivo_pt_pasaxon import (
    clean_arquivo_article, exact_dated_rows, extract_heading, parse_cdx, prioritize,
)


class ArquivoPtPasaxonTests(unittest.TestCase):
    def test_cdx_parser_and_latest_exact_day_capture(self):
        payload = "\n".join(json.dumps(row) for row in [
            {"url": "http://pasaxon.org.la/worldnews/2019/01/28/wo1.html", "timestamp": "20190101000000", "digest": "a"},
            {"url": "http://pasaxon.org.la/worldnews/2019/01/28/wo1.html", "timestamp": "20190701000000", "digest": "b"},
            {"url": "http://pasaxon.org.la/index.php?p_id=1", "timestamp": "20190701000000"},
        ]).encode()
        rows = exact_dated_rows(parse_cdx(payload))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"], "2019-01-28")
        self.assertEqual(rows[0]["digest"], "b")

    def test_prioritize_only_below_target_and_nonduplicate(self):
        rows = [
            {"canonical_url": "a", "year_month": "2018-01", "published_at": "2018-01-01", "slot": 1},
            {"canonical_url": "b", "year_month": "2018-02", "published_at": "2018-02-01", "slot": 1},
            {"canonical_url": "c", "year_month": "2018-03", "published_at": "2018-03-01", "slot": 1},
        ]
        result = prioritize(rows, counts={"2018-01": 0, "2018-02": 3, "2018-03": 4}, existing={"a"}, target=4)
        self.assertEqual([row["canonical_url"] for row in result], ["b"])

    def test_generic_site_title_is_recovered_and_footer_removed(self):
        title, body = clean_arquivo_article(
            "ໜັງພິມ ປະຊາຊົນ",
            "Toggle navigation\nບົດລາຍງານ\nຈີນ ແລະ ລາວ ຮ່ວມມື\n"
            + "ເນື້ອຫາຂ່າວທີ່ຍາວພຽງພໍ " * 8
            + "\nສະພາແຫ່ງຊາດ\nຂໍ້ຄວາມທ້າຍເວັບ",
        )
        self.assertEqual(title, "ຈີນ ແລະ ລາວ ຮ່ວມມື")
        self.assertNotIn("ສະພາແຫ່ງຊາດ", body)
        self.assertNotIn("ຂໍ້ຄວາມທ້າຍເວັບ", body)

    def test_heading_extraction_prefers_visible_lao_heading(self):
        payload = "<html><h2>ຫົວຂໍ້ ລາວ-ຈີນ <small>ຜູ້ຂຽນ</small></h2></html>".encode("utf-8")
        self.assertEqual(extract_heading(payload), "ຫົວຂໍ້ ລາວ-ຈີນ ຜູ້ຂຽນ")


if __name__ == "__main__":
    unittest.main()
