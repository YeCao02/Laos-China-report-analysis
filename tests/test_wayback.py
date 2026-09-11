from __future__ import annotations

from pathlib import Path
import unittest

from laos_china_corpus.archives.wayback import (
    WaybackCapture,
    build_cdx_query,
    is_china_related,
    is_lao_china_title,
    old_kpl_url_parts,
    parse_cdx,
    parse_direct_pasaxon_article,
    parse_old_kpl_article,
    parse_old_pasaxon_article,
    parse_old_pasaxon_home,
    pasaxon_url_identity,
    prioritized_captures,
)


FIXTURES = Path(__file__).parent / "fixtures"
URL = "http://kpl.net.la/english/news/newsrecord/2012/April/02.4.2012/edn1.htm"


class WaybackTests(unittest.TestCase):
    def test_modern_pasaxon_url_identities(self) -> None:
        self.assertEqual(
            pasaxon_url_identity("http://www.pasaxon.org.la/hotnews/2018/11/07/h2.html"),
            ("2018-11-07", "2018-11", 2),
        )
        self.assertEqual(
            pasaxon_url_identity("http://www.pasaxon.org.la/cooperation/2018/10/co3.html"),
            (None, "2018-10", 3),
        )
        self.assertEqual(
            pasaxon_url_identity("http://www.pasaxon.org.la/pasaxon-detail.php?p_id=749&act=leader-detail"),
            (None, None, 749),
        )

    def test_cdx_endpoint_and_parse(self) -> None:
        query = build_cdx_query(2012)
        self.assertIn("web.archive.org/cdx/search/cdx", query)
        self.assertIn("newsrecord%2F2012%2F%2A", query)
        rows = '[["timestamp","original","mimetype","digest"],["20140327190302","%s","text/html","ABC"]]' % URL
        capture = parse_cdx(rows)[0]
        self.assertEqual(capture.timestamp, "20140327190302")
        self.assertEqual(capture.replay_url, f"https://web.archive.org/web/20140327190302id_/{URL}")

    def test_old_url_date_and_article(self) -> None:
        self.assertEqual(old_kpl_url_parts(URL), ("2012-04-02", 1))
        article = parse_old_kpl_article((FIXTURES / "kpl_old_2012.html").read_bytes(), URL)
        self.assertEqual(article.title, "President receives Yunnan governor")
        self.assertEqual(article.published_date, "2012-04-02")
        self.assertIn("Laos and China", article.body)
        related, hits = is_china_related(article)
        self.assertTrue(related)
        self.assertIn("china", hits)
        self.assertIn("yunnan", hits)

    def test_prioritization_interleaves_dates(self) -> None:
        urls = [
            URL,
            URL.replace("edn1", "edn2"),
            URL.replace("02.4.2012", "03.4.2012"),
        ]
        captures = [WaybackCapture(f"2014032719030{i}", url) for i, url in enumerate(urls)]
        ordered = prioritized_captures(captures)["2012-04"]
        self.assertIn("02.4.2012/edn1", ordered[0].original_url)
        self.assertIn("03.4.2012/edn1", ordered[1].original_url)
        self.assertIn("02.4.2012/edn2", ordered[2].original_url)

    def test_old_pasaxon_home_and_article(self) -> None:
        home = (FIXTURES / "pasaxon_old_home_2012.html").read_bytes()
        links = parse_old_pasaxon_home(home, "http://www.pasaxon.org.la/")
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].published_date, "2012-01-04")
        related, hits = is_lao_china_title(links[0].title)
        self.assertTrue(related)
        self.assertIn("ລາວ-ຈີນ", hits)
        article = (FIXTURES / "pasaxon_old_article_2012.html").read_bytes()
        published, body, slot = parse_old_pasaxon_article(
            article, links[0].original_url, listing_title=links[0].title
        )
        self.assertEqual(published, "2012-01-04")
        self.assertEqual(slot, 1)
        self.assertIn("ມິດຕະພາບ", body)
        modern = (
            '<a href="hotnews/08-11-17/h2.html">'
            'ປະທານຕ້ອນຮັບຄະນະຜູ້ແທນຈີນ ເຜີຍແຜ່ ວັນທີ 08 ພະຈິກ 2017</a>'
        )
        modern_link = parse_old_pasaxon_home(modern, "http://www.pasaxon.org.la/")[0]
        self.assertEqual(modern_link.published_date, "2017-11-08")
        self.assertEqual(modern_link.slot, 2)
        php = (
            '<a href="pasaxon-detail.php?p_id=1941&act=leader-detail">'
            'ສຳພາດສະຖານີໂທລະພາບສູນກາງຈີນ</a>'
        )
        php_link = parse_old_pasaxon_home(php, "http://www.pasaxon.org.la/")[0]
        self.assertIsNone(php_link.published_date)
        self.assertEqual(php_link.slot, 1941)
        php_article = (
            '<h1>ສຳພາດສະຖານີໂທລະພາບສູນກາງຈີນ</h1>'
            '<div>12/08/2020 09:05:25</div><p>' + ('ເນື້ອໃນຂ່າວລາວຈີນ ' * 8) + '</p>'
        )
        published, _, slot = parse_old_pasaxon_article(
            php_article, php_link.original_url, listing_title=php_link.title
        )
        self.assertEqual(published, "2020-08-12")
        self.assertEqual(slot, 1941)

    def test_direct_pasaxon_articles_tree(self) -> None:
        html = (
            '<html><body><p><b>ລາວ-ຈີນ ຮ່ວມມື</b></p>'
            '<p>ສອງປະເທດສືບຕໍ່ພັດທະນາການພົວພັນ '
            'ແລະ ການຮ່ວມມືຮອບດ້ານໃຫ້ເຂັ້ມແຂງຂຶ້ນ.</p></body></html>'
        ).encode("utf-8")
        parsed = parse_direct_pasaxon_article(
            html, "http://www.pasaxon.org.la/articles/1-11-12/2.htm"
        )
        self.assertEqual(parsed.published_date, "2012-11-01")
        self.assertEqual(parsed.slot, 2)
        self.assertIn("ຈີນ", parsed.title)


    def test_direct_pasaxon_recovers_bold_lao_headline(self) -> None:
        html = (
            '<html><head><title>\ufffd\ufffd</title></head><body>'
            '<p class="BasicParagraph"><b><span>\u0e97\u0ec8\u0eb2\u0e99\u0e99\u0eb2\u0e8d\u0ebb\u0e81 <br>'
            '\u0e95\u0ec9\u0ead\u0e99\u0eae\u0eb1\u0e9a\u0e97\u0eb9\u0e94\u0e88\u0eb5\u0e99</span></b></p>'
            '<p>\u0ec3\u0e99\u0ea7\u0eb1\u0e99\u0e97\u0eb5 5 \u0ec0\u0ea1\u0eaa\u0eb2 2013, '
            + ('\u0eaa\u0e9b \u0e88\u0eb5\u0e99 \u0ec1\u0ea5\u0eb0 \u0eaa\u0e9b\u0e9b \u0ea5\u0eb2\u0ea7 \u0eae\u0ec8\u0ea7\u0ea1\u0ea1\u0eb7. ' * 6)
            + '</p></body></html>'
        ).encode("utf-8")
        parsed = parse_direct_pasaxon_article(
            html, "http://www.pasaxon.org.la/conten/5-4-13/1.htm"
        )
        self.assertEqual(parsed.title, "\u0e97\u0ec8\u0eb2\u0e99\u0e99\u0eb2\u0e8d\u0ebb\u0e81 \u0e95\u0ec9\u0ead\u0e99\u0eae\u0eb1\u0e9a\u0e97\u0eb9\u0e94\u0e88\u0eb5\u0e99")
        self.assertTrue(parsed.body.startswith("\u0ec3\u0e99\u0ea7\u0eb1\u0e99\u0e97\u0eb5"))


if __name__ == "__main__":
    unittest.main()
