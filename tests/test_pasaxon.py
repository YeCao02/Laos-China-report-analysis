from __future__ import annotations

import unittest
from pathlib import Path

from laos_china_corpus.adapters.pasaxon import (
    PasaxonParseError,
    build_epaper_url,
    build_search_url,
    build_tag_url,
    parse_article,
    parse_epaper_results,
    parse_search_results,
    plan_small_crawl,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class PasaxonUrlTests(unittest.TestCase):
    def test_supported_urls_are_percent_encoded_and_paged(self) -> None:
        self.assertEqual(
            build_search_url("ຈີນ"),
            "https://pasaxon.org.la/search/%E0%BA%88%E0%BA%B5%E0%BA%99.html",
        )
        self.assertEqual(
            build_tag_url("China Laos", 2),
            "https://pasaxon.org.la/tags/China%20Laos.html?page=2",
        )
        self.assertEqual(build_epaper_url(4), "https://pasaxon.org.la/epaper.html?page=4")

    def test_small_plan_is_bounded_and_deduplicated(self) -> None:
        plan = plan_small_crawl(["ຈີນ", "ຈີນ", "China"], pages_per_query=2, epaper_pages=1)
        self.assertEqual([item.kind for item in plan], ["search", "tag", "search", "tag", "epaper"])
        self.assertEqual(plan[1].page, 2)
        with self.assertRaises(ValueError):
            plan_small_crawl(["China"], pages_per_query=100)


class PasaxonListingTests(unittest.TestCase):
    def test_search_uses_actual_rows_and_next_not_displayed_total(self) -> None:
        page_url = build_search_url("ຈີນ")
        page = parse_search_results(fixture("pasaxon_search.html"), page_url, "ຈີນ")
        self.assertEqual(page.displayed_total, 999)
        self.assertEqual(len(page.discoveries), 2)
        self.assertEqual(page.discoveries[0].source_article_id, "19715")
        self.assertNotIn("unrelated", " ".join(item.url for item in page.discoveries))
        self.assertEqual(page.next_url, build_tag_url("ຈີນ", 2))

    def test_tag_page_extracts_listing_date_and_next(self) -> None:
        page_url = build_tag_url("ຈີນ", 2)
        page = parse_search_results(fixture("pasaxon_tags.html"), page_url, "ຈີນ")
        self.assertEqual(len(page.discoveries), 1)
        self.assertEqual(page.discoveries[0].published_at, "2020-01-04T09:30:00+07:00")
        self.assertEqual(page.next_url, build_tag_url("ຈີນ", 3))

    def test_epaper_extracts_issue_number_and_issue_date(self) -> None:
        page = parse_epaper_results(fixture("pasaxon_epaper.html"), build_epaper_url(2))
        self.assertEqual(len(page.discoveries), 2)
        first = page.discoveries[0]
        self.assertEqual(first.kind, "epaper_issue")
        self.assertEqual(first.metadata["issue_number"], "15.024")
        self.assertEqual(first.published_at, "2026-07-16")
        self.assertEqual(page.next_url, build_epaper_url(3))


class PasaxonArticleTests(unittest.TestCase):
    def test_article_returns_auditable_article_record(self) -> None:
        url = "https://pasaxon.org.la/lao-china-cooperation-19715.html"
        record = parse_article(
            fixture("pasaxon_article.html"),
            url,
            matched_queries=["ລາວ-ຈີນ"],
            search_url=build_search_url("ຈີນ"),
            retrieved_at="2026-08-07T10:00:00+07:00",
        )
        self.assertEqual(record.record_id, "PASAXON-LO-19715")
        self.assertEqual(record.source_code, "pasaxon")
        self.assertEqual(record.language, "lo")
        self.assertEqual(record.published_at, "2026-07-30T13:48:00+07:00")
        self.assertEqual(record.date_precision, "article_timestamp")
        self.assertIn("ສະຖານທູດ", record.body_original or "")
        self.assertNotIn("fb", record.body_original or "")
        self.assertEqual(record.content_origin, "local_byline")
        self.assertEqual(record.evidence_grade, "A1")
        self.assertEqual(record.retrieval_tier, "T2_BILATERAL_VARIANT")
        self.assertEqual(record.metadata["section"], "ຂ່າວການຮ່ວມມື")
        self.assertEqual(record.metadata["image_captions"], ["ຜູ້ແທນສອງຝ່າຍໃນພິທີ"])
        self.assertEqual(len(record.content_sha256 or ""), 64)

    def test_missing_body_is_explicit_failure(self) -> None:
        html = '<div class="article"><h1 class="article__title">Title</h1></div>'
        with self.assertRaises(PasaxonParseError):
            parse_article(html, "https://pasaxon.org.la/title-1.html")


if __name__ == "__main__":
    unittest.main()
