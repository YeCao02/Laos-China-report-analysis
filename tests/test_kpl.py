from __future__ import annotations

import sqlite3
import unittest
from datetime import date
from pathlib import Path

from laos_china_corpus.adapters.kpl import (
    KPLParseError,
    build_search_url,
    normalize_text,
    parse_detail_page,
    parse_search_page,
    plan_search_partitions,
)
from laos_china_corpus.db import SCHEMA
from laos_china_corpus.import_preview import import_preview, load_preview, preview_stats


LAO_SEARCH = """
<!doctype html><html><body><script>var total = 12;</script>
<ul><li>
  <span><img alt="图片"></span>
  <h3><a href='detail.aspx?id=53'>ຄະນະ​ຜູ້​ແທນ​ລາວ ແລະ ສປ ຈີນ</a></h3>
  <p class='uk-text-small uk-text-muted'>24/06/2014 11:20</p>
  <p>ຂປລ. ຄະນະຜູ້ແທນລາວໄດ້ເຂົ້າຮ່ວມກອງປະຊຸມ.</p>
</li></ul></body></html>
"""

EN_DETAIL = """
<!doctype html><html><head>
<meta property="og:title" content="China &amp; Laos strengthen ties">
<meta name="Description" content="A concise summary.">
</head><body>
<h1 class="uk-hidden">Lao News Agency</h1>
<article class="post-entry">
  <div class="post-top-entry"><h1>China &amp; Laos strengthen ties</h1>
    <span class="post-time uk-text-muted">22/07/2014 09:16</span></div>
  <div class="post-ct-entry">
    <div class="post-summary"><span>KPL</span> A concise summary.</div>
    <p>China and Laos agreed to expand cooperation.</p>
    <figure><img alt="Officials at the meeting"></figure>
  </div>
</article></body></html>
"""


class KPLAdapterTests(unittest.TestCase):
    def test_search_url_and_partition_policy(self) -> None:
        url = build_search_url("lo", "ຈີນ", date(2014, 1, 1), date(2014, 12, 31), page=4)
        self.assertIn("/search.aspx?", url)
        self.assertIn("search=%E0%BA%88%E0%BA%B5%E0%BA%99", url)
        self.assertIn("fd=01%2F01%2F2014", url)
        self.assertTrue(url.endswith("page=4"))
        lao = plan_search_partitions("lao", ["ຈີນ"], date(2014, 6, 1), date(2016, 2, 1))
        english = plan_search_partitions("english", ["China", "Chinese"], date(2012, 1, 1), date(2026, 8, 7))
        self.assertEqual(3, len(lao))
        self.assertEqual(2, len(english))
        self.assertEqual(date(2014, 6, 1), lao[0].date_from)
        self.assertEqual(date(2016, 2, 1), lao[-1].date_to)

    def test_parse_lao_search_page_and_unicode(self) -> None:
        url = build_search_url("lo", "ຈີນ", date(2014, 1, 1), date(2014, 12, 31), page=1)
        page = parse_search_page(LAO_SEARCH.encode("utf-8"), url)
        self.assertEqual(12, page.total)
        self.assertEqual(2, page.expected_pages)
        self.assertEqual(1, len(page.articles))
        article = page.articles[0]
        self.assertEqual("KPL-LAO-000053", article.record_id)
        self.assertEqual("lo", article.language)
        self.assertEqual("2014-06-24T11:20:00", article.published_at)
        self.assertEqual("ຄະນະ ຜູ້ ແທນ ລາວ ແລະ ສປ ຈີນ", article.title_original)
        self.assertEqual(["ຈີນ"], article.matched_queries)
        self.assertEqual("A2", article.evidence_grade)

    def test_invalid_search_response_is_not_silent(self) -> None:
        with self.assertRaises(KPLParseError):
            parse_search_page("<html>access denied</html>", "https://kpl.gov.la/search.aspx")
        with self.assertRaises(KPLParseError):
            parse_search_page("<script>var total=2</script>", "https://kpl.gov.la/search.aspx")

    def test_detail_preserves_both_dates(self) -> None:
        search_url = build_search_url("en", "China", date(2012, 1, 1), date(2026, 8, 7))
        base = parse_search_page(
            """<script>var total=1;</script><li><h3><a href='detail.aspx?id=95'>China &amp; Laos strengthen ties</a></h3><p>19/07/2014 09:16</p><p>Summary</p></li>""",
            search_url,
        ).articles[0]
        article = parse_detail_page(EN_DETAIL, "https://kpl.gov.la/En/detail.aspx?id=95", base_record=base)
        self.assertEqual("2014-07-19T09:16:00", article.published_at)
        self.assertEqual("2014-07-19T09:16:00", article.metadata["search_indexed_at"])
        self.assertEqual("2014-07-22T09:16:00", article.metadata["article_published_at"])
        self.assertEqual(3, article.metadata["date_delta_days"])
        self.assertTrue(article.metadata["dates_differ"])
        self.assertIn("expand cooperation", article.body_original or "")
        self.assertEqual(["Officials at the meeting"], article.metadata["image_captions"])
        self.assertEqual("A1", article.evidence_grade)

    def test_lao_normalization_keeps_combining_marks(self) -> None:
        self.assertEqual("ຈີນ ລາວ", normalize_text("ຈີນ\u200b  ລາວ"))


class PreviewImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.preview = Path(__file__).resolve().parents[2] / "preview.md"
        cls.records = load_preview(cls.preview)

    def test_full_preview_baseline(self) -> None:
        stats = preview_stats(self.records)
        self.assertEqual(8936, stats.parsed)
        self.assertEqual(8936, stats.unique)
        self.assertEqual(7605, stats.lao)
        self.assertEqual(1331, stats.english)
        first = self.records[0]
        self.assertEqual("KPL-LAO-000045", first.record_id)
        self.assertEqual("2014-06-17T03:37:00", first.published_at)
        self.assertEqual("ຈີນ", first.matched_queries[0])
        self.assertEqual("A1", first.evidence_grade)
        self.assertIn("中国本国", first.metadata["scope"])

    def test_sqlite_import_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        first = import_preview(conn, self.preview)
        second = import_preview(conn, self.preview)
        count = conn.execute("SELECT count(*) FROM articles").fetchone()[0]
        self.assertEqual(8936, count)
        self.assertEqual(first, second)
        row = conn.execute(
            "SELECT language,evidence_grade,metadata_json FROM articles WHERE record_id='KPL-EN-000095'"
        ).fetchone()
        self.assertEqual("en", row["language"])
        self.assertEqual("A1", row["evidence_grade"])


if __name__ == "__main__":
    unittest.main()
