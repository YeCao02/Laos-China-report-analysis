from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
from html.parser import HTMLParser
from typing import Iterable, Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from ..models import ArticleRecord


KPL_BASE_URL = "https://kpl.gov.la"
KPL_SOURCE_CODES = {"lo": "kpl_lao", "en": "kpl_english"}
_DETAIL_RE = re.compile(r"(?:^|/)(?:En/)?detail\.aspx\?[^#]*\bid=(\d+)", re.I)
_TOTAL_RE = re.compile(r"\bvar\s+total\s*=\s*(\d+)\s*;?", re.I)
_TIMESTAMP_RE = re.compile(
    r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](20\d{2})"
    r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)
_ISO_TIMESTAMP_RE = re.compile(
    r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


class KPLParseError(ValueError):
    """Raised when a KPL response is syntactically successful but unusable."""


def normalize_kpl_language(language: str) -> str:
    normalized = language.strip().casefold().replace("-", "_")
    if normalized in {"lo", "lao", "la", "kpl_lao", "老挝语"}:
        return "lo"
    if normalized in {"en", "eng", "english", "kpl_en", "kpl_english", "英语"}:
        return "en"
    raise ValueError(f"Unsupported KPL language: {language!r}")


def normalize_text(value: str | None) -> str:
    """Normalize markup text without disturbing Lao combining characters."""

    if not value:
        return ""
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\u00a0", " ").replace("\ufeff", "")
    # KPL uses zero-width spaces as visual word separators. Treat them as
    # spaces so titles from search/detail templates compare deterministically.
    value = value.replace("\u200b", " ").replace("\u00ad", "")
    return re.sub(r"\s+", " ", value).strip()


def decode_html(payload: str | bytes) -> str:
    if isinstance(payload, str):
        return payload
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig")
    head = payload[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", head, re.I)
    candidates = [match.group(1)] if match else []
    candidates.extend(["utf-8", "windows-1252"])
    for encoding in candidates:
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def parse_kpl_timestamp(value: str) -> tuple[str, str]:
    normalized = normalize_text(value)
    iso_match = _ISO_TIMESTAMP_RE.search(normalized)
    if iso_match:
        year, month, day = (int(iso_match.group(i)) for i in range(1, 4))
        if iso_match.group(4) is None:
            return date(year, month, day).isoformat(), "day"
        hour, minute = int(iso_match.group(4)), int(iso_match.group(5))
        second = int(iso_match.group(6) or 0)
        parsed_dt = datetime(year, month, day, hour, minute, second)
        return parsed_dt.isoformat(timespec="seconds"), "minute" if iso_match.group(6) is None else "second"
    match = _TIMESTAMP_RE.search(normalized)
    if not match:
        raise KPLParseError(f"Invalid KPL timestamp: {value!r}")
    day, month, year = (int(match.group(i)) for i in range(1, 4))
    if match.group(4) is None:
        parsed = date(year, month, day)
        return parsed.isoformat(), "day"
    hour, minute = int(match.group(4)), int(match.group(5))
    second = int(match.group(6) or 0)
    parsed_dt = datetime(year, month, day, hour, minute, second)
    return parsed_dt.isoformat(timespec="seconds"), "minute" if match.group(6) is None else "second"


@dataclass(slots=True)
class SearchPartition:
    language: str
    query: str
    date_from: date
    date_to: date

    def __post_init__(self) -> None:
        self.language = normalize_kpl_language(self.language)
        if self.date_to < self.date_from:
            raise ValueError("date_to must not precede date_from")

    @property
    def source_code(self) -> str:
        return KPL_SOURCE_CODES[self.language]

    def page_url(self, page: int = 1) -> str:
        return build_search_url(
            self.language,
            self.query,
            self.date_from,
            self.date_to,
            page=page,
        )


@dataclass(slots=True)
class SearchPage:
    url: str
    language: str
    query: str | None
    page: int
    total: int
    articles: list[ArticleRecord] = field(default_factory=list)

    @property
    def expected_pages(self) -> int:
        return math.ceil(self.total / 10)


def build_search_url(
    language: str,
    query: str,
    date_from: date,
    date_to: date,
    *,
    page: int = 1,
) -> str:
    language = normalize_kpl_language(language)
    if page < 1:
        raise ValueError("page must be at least 1")
    path = "/search.aspx" if language == "lo" else "/En/search.aspx"
    params = {
        "cat": "1",
        "search": query,
        "fd": date_from.strftime("%d/%m/%Y"),
        "td": date_to.strftime("%d/%m/%Y"),
        "page": str(page),
    }
    return f"{KPL_BASE_URL}{path}?{urlencode(params)}"


def build_detail_url(language: str, article_id: str | int) -> str:
    language = normalize_kpl_language(language)
    numeric_id = str(article_id).strip()
    if not numeric_id.isdigit():
        raise ValueError(f"KPL article ID must be numeric: {article_id!r}")
    path = "/detail.aspx" if language == "lo" else "/En/detail.aspx"
    return f"{KPL_BASE_URL}{path}?id={int(numeric_id)}"


def plan_search_partitions(
    language: str,
    queries: Iterable[str],
    date_from: date,
    date_to: date,
) -> list[SearchPartition]:
    """Plan reproducible KPL searches.

    Lao searches are split by calendar year. KPL's English endpoint has been
    observed to ignore date bounds, so it is deliberately planned as one
    whole-range partition per query and filtered locally after parsing.
    """

    language = normalize_kpl_language(language)
    unique_queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    if date_to < date_from:
        raise ValueError("date_to must not precede date_from")
    if language == "en":
        return [SearchPartition(language, query, date_from, date_to) for query in unique_queries]
    partitions: list[SearchPartition] = []
    for query in unique_queries:
        for year in range(date_from.year, date_to.year + 1):
            start = max(date_from, date(year, 1, 1))
            end = min(date_to, date(year, 12, 31))
            partitions.append(SearchPartition(language, query, start, end))
    return partitions


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = None
    children: list[_Node | str] = field(default_factory=list)

    def text(self) -> str:
        pieces: list[str] = []

        def visit(node: _Node) -> None:
            if node.tag in {"script", "style", "noscript"}:
                return
            for child in node.children:
                if isinstance(child, str):
                    pieces.append(child)
                else:
                    visit(child)

        visit(self)
        return normalize_text(" ".join(pieces))

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())


class _DOMParser(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.casefold(), {k.casefold(): v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if node.tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag.casefold() and tag.casefold() not in self._VOID:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _parse_dom(payload: str | bytes) -> _Node:
    parser = _DOMParser()
    parser.feed(decode_html(payload))
    parser.close()
    return parser.root


def _walk(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _nearest(node: _Node, tag: str) -> _Node | None:
    current = node.parent
    while current:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _node_with_class(root: _Node, class_name: str) -> _Node | None:
    return next((node for node in _walk(root) if class_name in node.classes()), None)


def _language_from_url(url: str) -> str:
    return "en" if re.search(r"/(?:en)(?:/|$)", urlparse(url).path, re.I) else "lo"


def _article_id_from_url(url: str) -> str:
    if not re.search(r"(?:^|/)detail\.aspx$", urlparse(url).path, re.I):
        raise KPLParseError(f"Not a KPL detail URL: {url}")
    query_id = parse_qs(urlparse(url).query).get("id", [""])[0]
    if query_id.isdigit():
        return str(int(query_id))
    match = _DETAIL_RE.search(url)
    if not match:
        raise KPLParseError(f"No numeric KPL article ID in URL: {url}")
    return str(int(match.group(1)))


def _record_id(language: str, article_id: str) -> str:
    marker = "LAO" if language == "lo" else "EN"
    return f"KPL-{marker}-{int(article_id):06d}"


def _query_from_url(url: str) -> str | None:
    return parse_qs(urlparse(url).query).get("search", [None])[0]


def parse_search_page(
    payload: str | bytes,
    page_url: str,
    *,
    query: str | None = None,
    strict: bool = True,
) -> SearchPage:
    html = decode_html(payload)
    total_match = _TOTAL_RE.search(html)
    if not total_match:
        raise KPLParseError("KPL search page is missing the 'var total' validity marker")
    total = int(total_match.group(1))
    language = _language_from_url(page_url)
    query = query if query is not None else _query_from_url(page_url)
    page_raw = parse_qs(urlparse(page_url).query).get("page", ["1"])[0]
    page_number = int(page_raw) if page_raw.isdigit() else 1
    root = _parse_dom(html)
    articles: list[ArticleRecord] = []
    seen_ids: set[str] = set()

    for anchor in (node for node in _walk(root) if node.tag == "a"):
        href = anchor.attrs.get("href", "")
        absolute_url = urljoin(page_url, href)
        try:
            article_id = _article_id_from_url(absolute_url)
        except KPLParseError:
            continue
        container = _nearest(anchor, "li")
        if container is None:
            continue
        timestamp_node = next(
            (node for node in _walk(container) if _TIMESTAMP_RE.search(node.text())),
            None,
        )
        if timestamp_node is None or article_id in seen_ids:
            continue
        title = normalize_text(anchor.text())
        if not title:
            continue
        indexed_at, indexed_precision = parse_kpl_timestamp(timestamp_node.text())
        paragraphs = [node.text() for node in _walk(container) if node.tag == "p"]
        excerpt = next(
            (text for text in reversed(paragraphs) if text and not _TIMESTAMP_RE.fullmatch(text)),
            None,
        )
        record_id = _record_id(language, article_id)
        articles.append(
            ArticleRecord(
                record_id=record_id,
                source_code=KPL_SOURCE_CODES[language],
                source_article_id=article_id,
                story_id=record_id,
                language=language,
                title_original=title,
                excerpt_original=excerpt,
                published_at=indexed_at,
                date_precision="indexed_timestamp",
                body_method="none",
                matched_queries=[query] if query else [],
                original_url=build_detail_url(language, article_id),
                search_url=page_url,
                evidence_grade="A2",
                retrieval_tier="T1_DIRECT_CHINA",
                metadata={
                    "search_indexed_at": indexed_at,
                    "search_indexed_at_raw": normalize_text(timestamp_node.text()),
                    "search_index_precision": indexed_precision,
                    "search_result_page": page_number,
                    "parser_version": "kpl-search-v1",
                },
            )
        )
        seen_ids.add(article_id)

    if strict and total > 0 and not articles:
        raise KPLParseError("Non-empty KPL search response yielded no result records")
    return SearchPage(page_url, language, query, page_number, total, articles)


def _meta_content(root: _Node, key: str) -> str:
    key = key.casefold()
    for node in _walk(root):
        if node.tag != "meta":
            continue
        label = (node.attrs.get("property") or node.attrs.get("name") or "").casefold()
        if label == key:
            return normalize_text(node.attrs.get("content"))
    return ""


def _generic_class_text(root: _Node, names: set[str]) -> str | None:
    for node in _walk(root):
        classes = node.classes()
        if classes & names:
            value = node.text()
            if value:
                return value
    return None


def parse_detail_page(
    payload: str | bytes,
    page_url: str,
    *,
    base_record: ArticleRecord | None = None,
    retrieved_at: str | None = None,
    raw_file: str | None = None,
    response_sha256: str | None = None,
) -> ArticleRecord:
    root = _parse_dom(payload)
    language = _language_from_url(page_url)
    article_id = _article_id_from_url(page_url)
    title = _meta_content(root, "og:title")
    if not title:
        title = next(
            (
                node.text()
                for node in _walk(root)
                if node.tag == "h1" and "uk-hidden" not in node.classes() and node.text()
            ),
            "",
        )
    timestamp_node = _node_with_class(root, "post-time")
    if not title or timestamp_node is None:
        raise KPLParseError("KPL detail page is missing title or post-time")
    article_at, article_precision = parse_kpl_timestamp(timestamp_node.text())
    body_node = _node_with_class(root, "post-ct-entry")
    body = body_node.text() if body_node else ""
    if not body:
        raise KPLParseError("KPL detail page has no extractable post-ct-entry body")
    summary_node = _node_with_class(root, "post-summary")
    excerpt = summary_node.text() if summary_node else _meta_content(root, "description") or None
    image_captions = []
    if body_node:
        image_captions = list(
            dict.fromkeys(
                normalize_text(node.attrs.get("alt") or node.attrs.get("title"))
                for node in _walk(body_node)
                if node.tag == "img" and normalize_text(node.attrs.get("alt") or node.attrs.get("title"))
            )
        )
    byline = _generic_class_text(root, {"author", "byline", "post-author"})
    section = _generic_class_text(root, {"category", "post-category", "news-category"})

    if base_record is not None:
        expected_id = str(int(base_record.source_article_id or article_id))
        if expected_id != article_id:
            raise KPLParseError(
                f"Detail ID {article_id} does not match base record ID {base_record.source_article_id}"
            )
        if normalize_kpl_language(base_record.language) != language:
            raise KPLParseError("Detail language does not match base record")
        indexed_at = base_record.metadata.get("search_indexed_at") or base_record.published_at
        published_at = base_record.published_at
        date_precision = base_record.date_precision
        metadata = dict(base_record.metadata)
        matched_queries = list(base_record.matched_queries)
        search_url = base_record.search_url
        story_id = base_record.story_id
        content_origin = base_record.content_origin
        topic_labels = list(base_record.topic_labels)
        china_note_zh = base_record.china_note_zh
    else:
        indexed_at = None
        published_at = article_at
        date_precision = "article_timestamp"
        metadata = {}
        matched_queries = []
        search_url = None
        story_id = _record_id(language, article_id)
        content_origin = "unknown"
        topic_labels = []
        china_note_zh = None

    dates_differ = bool(indexed_at and str(indexed_at)[:10] != article_at[:10])
    date_delta_days = None
    if indexed_at:
        date_delta_days = (
            date.fromisoformat(article_at[:10]) - date.fromisoformat(str(indexed_at)[:10])
        ).days
    title_matches = base_record is None or normalize_text(base_record.title_original) == title
    metadata.update(
        {
            "search_indexed_at": indexed_at,
            "article_published_at": article_at,
            "article_published_at_raw": normalize_text(timestamp_node.text()),
            "article_date_precision": article_precision,
            "dates_differ": dates_differ,
            "date_delta_days": date_delta_days,
            "title_matches_search": title_matches,
            "byline": byline,
            "section": section,
            "image_captions": image_captions,
            "parser_version": "kpl-detail-v1",
        }
    )
    body_hash = sha256(body.encode("utf-8")).hexdigest()
    return ArticleRecord(
        record_id=_record_id(language, article_id),
        source_code=KPL_SOURCE_CODES[language],
        source_article_id=article_id,
        story_id=story_id,
        language=language,
        title_original=title,
        excerpt_original=excerpt,
        body_original=body,
        body_method="html_dom:kpl-post-ct-entry-v1",
        published_at=published_at,
        date_precision=date_precision,
        china_note_zh=china_note_zh,
        topic_labels=topic_labels,
        content_origin=content_origin,
        matched_queries=matched_queries,
        original_url=build_detail_url(language, article_id),
        search_url=search_url,
        raw_file=raw_file,
        evidence_grade="A1" if title_matches else "B1",
        retrieval_tier=base_record.retrieval_tier if base_record else "T4_ARCHIVE_DISCOVERY",
        content_sha256=body_hash,
        retrieved_at=retrieved_at,
        metadata={**metadata, "response_sha256": response_sha256},
    )


class _FetchResult(Protocol):
    payload: bytes
    fetched_at: str
    content_sha256: str
    raw_file: object


class _Fetcher(Protocol):
    def fetch(self, url: str, source_code: str, *, force: bool = False) -> _FetchResult: ...


def fetch_search_pages(
    fetcher: _Fetcher,
    partition: SearchPartition,
    *,
    max_pages: int = 1,
    force: bool = False,
) -> list[SearchPage]:
    """Fetch a deliberately bounded slice of one partition (default: one page)."""

    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    first_result = fetcher.fetch(partition.page_url(1), partition.source_code, force=force)
    first = parse_search_page(first_result.payload, partition.page_url(1), query=partition.query)
    _attach_search_fetch_metadata(first, first_result)
    pages = [first]
    limit = min(first.expected_pages, max_pages)
    for page_number in range(2, limit + 1):
        result = fetcher.fetch(partition.page_url(page_number), partition.source_code, force=force)
        parsed = parse_search_page(result.payload, partition.page_url(page_number), query=partition.query)
        _attach_search_fetch_metadata(parsed, result)
        pages.append(parsed)
    return pages


def _attach_search_fetch_metadata(page: SearchPage, result: _FetchResult) -> None:
    for article in page.articles:
        article.raw_file = str(result.raw_file)
        article.retrieved_at = result.fetched_at
        article.metadata["search_response_sha256"] = result.content_sha256


def fetch_article_details(
    fetcher: _Fetcher,
    records: Iterable[ArticleRecord],
    *,
    limit: int = 1,
    force: bool = False,
) -> list[ArticleRecord]:
    """Hydrate at most ``limit`` records; callers must opt into larger batches."""

    if limit < 0:
        raise ValueError("limit must not be negative")
    hydrated: list[ArticleRecord] = []
    for index, record in enumerate(records):
        if index >= limit:
            break
        url = record.original_url or build_detail_url(record.language, record.source_article_id or "")
        result = fetcher.fetch(url, record.source_code, force=force)
        hydrated.append(
            parse_detail_page(
                result.payload,
                url,
                base_record=record,
                retrieved_at=result.fetched_at,
                raw_file=str(result.raw_file),
                response_sha256=result.content_sha256,
            )
        )
    return hydrated
