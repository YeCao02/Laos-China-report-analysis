from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from typing import Iterable, Literal
from urllib.parse import parse_qs, quote, urljoin, urlparse

from ..models import ArticleRecord


BASE_URL = "https://pasaxon.org.la"
LAOS_TZ = timezone(timedelta(hours=7))
_ARTICLE_ID_RE = re.compile(r"-(\d+)\.html(?:$|[?#])", re.IGNORECASE)
_ISSUE_RE = re.compile(r"(?P<number>\d{1,3}\.\d{1,4})\s*\((?P<date>\d{2}-\d{2}-\d{4})\)")
_DATETIME_RE = re.compile(
    r"(?:(?P<time1>\d{1,2}:\d{2})\s+)?(?P<date>\d{2}/\d{2}/\d{4})"
    r"(?:\s+(?P<time2>\d{1,2}:\d{2}))?"
)
_SPACE_RE = re.compile(r"[\t\r\f\v ]+")
_BLOCK_TAGS = {"p", "div", "section", "article", "h1", "h2", "h3", "li", "br", "tr"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class PasaxonParseError(ValueError):
    """Raised when a fetched Pasaxon page cannot yield an auditable record."""


@dataclass(slots=True)
class PasaxonDiscovery:
    kind: Literal["article", "epaper_issue"]
    source_article_id: str | None
    title_original: str
    url: str
    published_at: str | None = None
    query: str | None = None
    search_url: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ListingPage:
    discoveries: list[PasaxonDiscovery]
    next_url: str | None
    displayed_total: int | None = None


@dataclass(frozen=True, slots=True)
class CrawlRequest:
    kind: Literal["search", "tag", "epaper"]
    url: str
    query: str | None = None
    page: int = 1


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None = None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []
        self.parent = parent

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        parts: list[str] = []

        def walk(node: _Node) -> None:
            if node.tag in {"script", "style", "noscript"}:
                return
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    if child.tag in _BLOCK_TAGS:
                        parts.append("\n")
                    walk(child)
                    if child.tag in _BLOCK_TAGS:
                        parts.append("\n")

        walk(self)
        return _clean_text("".join(parts))

    def descendants(self, tag: str | None = None, class_name: str | None = None) -> list[_Node]:
        found: list[_Node] = []

        def walk(node: _Node) -> None:
            for child in node.children:
                if not isinstance(child, _Node):
                    continue
                if (tag is None or child.tag == tag) and (
                    class_name is None or class_name in child.classes
                ):
                    found.append(child)
                walk(child)

        walk(self)
        return found

    def first(self, tag: str | None = None, class_name: str | None = None) -> _Node | None:
        matches = self.descendants(tag, class_name)
        return matches[0] if matches else None


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {key.lower(): value or "" for key, value in attrs}, self.current)
        self.current.children.append(node)
        if node.tag not in _VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.current.tag == tag.lower() and tag.lower() not in _VOID_TAGS:
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.lower()
        node = self.current
        while node is not self.root:
            if node.tag == wanted:
                self.current = node.parent or self.root
                return
            node = node.parent or self.root

    def handle_data(self, data: str) -> None:
        self.current.children.append(data)


def _parse(html: str) -> _Node:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _clean_text(value: str) -> str:
    lines = []
    for line in value.replace("\u200b", "").replace("\xa0", " ").splitlines():
        line = _SPACE_RE.sub(" ", line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


def _absolute(base_url: str, href: str | None) -> str | None:
    return urljoin(base_url, href) if href else None


def _article_id(url: str) -> str | None:
    match = _ARTICLE_ID_RE.search(urlparse(url).path)
    return match.group(1) if match else None


def _published_at(value: str) -> str | None:
    match = _DATETIME_RE.search(value)
    if not match:
        return None
    clock = match.group("time1") or match.group("time2")
    template = "%d/%m/%Y %H:%M" if clock else "%d/%m/%Y"
    parsed = datetime.strptime(f"{match.group('date')} {clock}" if clock else match.group("date"), template)
    if clock:
        return parsed.replace(tzinfo=LAOS_TZ).isoformat()
    return parsed.date().isoformat()


def _is_article_href(href: str) -> bool:
    path = urlparse(href).path
    return bool(_ARTICLE_ID_RE.search(path)) and not path.startswith(("/search/", "/tags/"))


def _find_next(root: _Node, page_url: str, kind: str) -> str | None:
    current_page = int(parse_qs(urlparse(page_url).query).get("page", ["1"])[0] or 1)
    candidates: list[tuple[int, str]] = []
    for link in root.descendants("a"):
        href = link.attrs.get("href", "")
        parsed = urlparse(urljoin(page_url, href))
        pages = parse_qs(parsed.query).get("page")
        if not pages or not pages[0].isdigit():
            continue
        page = int(pages[0])
        valid = parsed.path == "/epaper.html" if kind == "epaper" else parsed.path.startswith("/tags/")
        if valid and page > current_page:
            candidates.append((page, urljoin(page_url, href)))
    return min(candidates)[1] if candidates else None


def _listing_scope(root: _Node, next_url: str | None, page_url: str) -> _Node:
    if next_url:
        target_path = urlparse(next_url).path
        target_page = parse_qs(urlparse(next_url).query).get("page", [""])[0]
        for link in root.descendants("a"):
            absolute = urljoin(page_url, link.attrs.get("href", ""))
            parsed = urlparse(absolute)
            if parsed.path != target_path or parse_qs(parsed.query).get("page", [""])[0] != target_page:
                continue
            scope = link.parent
            while scope is not None and scope is not root:
                if scope.descendants("article"):
                    return scope
                scope = scope.parent
    for class_name in ("search-results", "list-news", "main-col", "epaper-list"):
        scope = root.first(class_name=class_name)
        if scope and scope.descendants("article"):
            return scope
    main = root.first("main")
    return main or root


def build_search_url(query: str, base_url: str = BASE_URL) -> str:
    return urljoin(base_url.rstrip("/") + "/", f"search/{quote(query, safe='')}.html")


def build_tag_url(query: str, page: int = 1, base_url: str = BASE_URL) -> str:
    if page < 1:
        raise ValueError("page must be at least 1")
    return urljoin(base_url.rstrip("/") + "/", f"tags/{quote(query, safe='')}.html?page={page}")


def build_epaper_url(page: int = 1, base_url: str = BASE_URL) -> str:
    if page < 1:
        raise ValueError("page must be at least 1")
    return urljoin(base_url.rstrip("/") + "/", f"epaper.html?page={page}")


def plan_small_crawl(
    queries: Iterable[str],
    *,
    pages_per_query: int = 2,
    epaper_pages: int = 1,
    base_url: str = BASE_URL,
) -> list[CrawlRequest]:
    """Plan a bounded probe; it deliberately cannot turn into a whole-site crawl."""
    if not 1 <= pages_per_query <= 3:
        raise ValueError("pages_per_query must be between 1 and 3")
    if not 0 <= epaper_pages <= 2:
        raise ValueError("epaper_pages must be between 0 and 2")
    requests: list[CrawlRequest] = []
    seen: set[str] = set()
    for raw_query in queries:
        query = _clean_text(raw_query)
        if not query or query in seen:
            continue
        seen.add(query)
        requests.append(CrawlRequest("search", build_search_url(query, base_url), query, 1))
        for page in range(2, pages_per_query + 1):
            requests.append(CrawlRequest("tag", build_tag_url(query, page, base_url), query, page))
    for page in range(1, epaper_pages + 1):
        requests.append(CrawlRequest("epaper", build_epaper_url(page, base_url), None, page))
    return requests


def parse_listing(html: str, page_url: str, query: str | None = None) -> ListingPage:
    root = _parse(html)
    kind = "epaper" if urlparse(page_url).path == "/epaper.html" else "article"
    next_url = _find_next(root, page_url, "epaper" if kind == "epaper" else "article")
    scope = _listing_scope(root, next_url, page_url)
    discoveries: list[PasaxonDiscovery] = []
    seen_urls: set[str] = set()

    for article in scope.descendants("article"):
        links = [link for link in article.descendants("a") if _is_article_href(link.attrs.get("href", ""))]
        if not links:
            continue
        link = next((item for item in links if item.text()), links[0])
        url = _absolute(page_url, link.attrs.get("href"))
        if not url or url in seen_urls:
            continue
        title_node = article.first("h2") or article.first("h3")
        title = (title_node.text() if title_node else "") or link.text()
        if not title:
            image = article.first("img")
            title = _clean_text(image.attrs.get("alt", "")) if image else ""
        if not title:
            continue
        seen_urls.add(url)
        issue_match = _ISSUE_RE.search(title) if kind == "epaper" else None
        published = None
        issue_number = None
        if issue_match:
            issue_number = issue_match.group("number")
            published = datetime.strptime(issue_match.group("date"), "%d-%m-%Y").date().isoformat()
        if published is None:
            time_node = article.first("time")
            published = _published_at(time_node.text()) if time_node else _published_at(article.text())
        discoveries.append(
            PasaxonDiscovery(
                kind="epaper_issue" if kind == "epaper" else "article",
                source_article_id=_article_id(url),
                title_original=title,
                url=url,
                published_at=published,
                query=query,
                search_url=page_url,
                metadata={"issue_number": issue_number} if issue_number else {},
            )
        )

    displayed_total = None
    if kind != "epaper":
        page_text = root.text()
        total_match = re.search(r"(?:ພົບເຫັນ|found)\s*(\d+)\s*(?:ຜົນການຄົ້ນຫາ|results?)", page_text, re.IGNORECASE)
        if total_match:
            displayed_total = int(total_match.group(1))
    return ListingPage(discoveries, next_url, displayed_total)


def parse_search_results(html: str, page_url: str, query: str) -> ListingPage:
    return parse_listing(html, page_url, query)


def parse_epaper_results(html: str, page_url: str) -> ListingPage:
    return parse_listing(html, page_url)


def _language(text: str) -> str:
    lao_chars = sum("\u0e80" <= char <= "\u0eff" for char in text)
    letters = sum(char.isalpha() for char in text)
    return "lo" if lao_chars >= max(1, letters // 3) else "en"


def _retrieval_tier(queries: Iterable[str]) -> str:
    joined = " ".join(queries).casefold()
    if any(term in joined for term in ("ລາວ-ຈີນ", "ຈີນ-ລາວ", "lao-china", "laos-china", "china-laos")):
        return "T2_BILATERAL_VARIANT"
    if any(term in joined for term in ("ຈີນ", "china", "chinese")):
        return "T1_DIRECT_CHINA"
    return "T3_ENTITY_TOPIC" if joined else "T4_ARCHIVE_DISCOVERY"


def parse_article(
    html: str,
    url: str,
    *,
    matched_queries: Iterable[str] = (),
    search_url: str | None = None,
    retrieved_at: str | None = None,
) -> ArticleRecord:
    root = _parse(html)
    container = root.first(class_name="article")
    title_node = root.first("h1", "article__title") or (container.first("h1") if container else None)
    title = title_node.text() if title_node else ""
    if not title:
        raise PasaxonParseError(f"article title missing: {url}")

    meta = (container.first(class_name="article__meta") if container else None) or root.first(class_name="article__meta")
    published_at = _published_at(meta.text()) if meta else None
    sapo_node = (container.first(class_name="article__sapo") if container else None) or root.first(class_name="article__sapo")
    body_node = (container.first(class_name="article__body") if container else None) or root.first(class_name="article__body")
    excerpt = sapo_node.text() if sapo_node else None
    body_parts: list[str] = [excerpt] if excerpt else []
    if body_node:
        paragraphs = body_node.descendants("p")
        if paragraphs:
            body_parts.extend(paragraph.text() for paragraph in paragraphs if paragraph.text())
        else:
            body = body_node.text()
            body = re.sub(r"^(?:fb|tw)(?:\s*(?:fb|tw))*\s*", "", body, flags=re.IGNORECASE)
            if body:
                body_parts.append(body)
    body_original = "\n\n".join(dict.fromkeys(part for part in body_parts if part))
    if not body_original:
        raise PasaxonParseError(f"article body missing: {url}")

    source_article_id = _article_id(url)
    fallback_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    stable_id = source_article_id or fallback_id
    queries = list(dict.fromkeys(_clean_text(query) for query in matched_queries if _clean_text(query)))
    section_node = root.first("h2", "main")
    section = section_node.text() if section_node else None
    captions: list[str] = []
    image_alts: list[str] = []
    if container:
        for figure in container.descendants("figure"):
            caption = figure.first("figcaption")
            if caption and caption.text():
                captions.append(caption.text())
            image = figure.first("img")
            if image:
                alt = _clean_text(image.attrs.get("alt", ""))
                if alt and alt != title:
                    image_alts.append(alt)
    author_line = next((line for line in reversed(body_original.splitlines()) if line.startswith("ຂ່າວ:")), None)
    content_sha256 = hashlib.sha256(body_original.encode("utf-8")).hexdigest()
    complete = bool(published_at and body_original)
    language = _language(title + "\n" + body_original)

    return ArticleRecord(
        record_id=f"PASAXON-{language.upper()}-{stable_id}",
        source_code="pasaxon",
        source_article_id=source_article_id,
        story_id=f"PASAXON-STORY-{stable_id}",
        language=language,
        title_original=title,
        published_at=published_at,
        date_precision="article_timestamp" if published_at and "T" in published_at else "article_date",
        excerpt_original=excerpt,
        body_original=body_original,
        body_method="pasaxon_dom",
        content_origin="local_byline" if author_line else "unknown",
        matched_queries=queries,
        original_url=url,
        search_url=search_url,
        evidence_grade="A1" if complete else "B1",
        retrieval_tier=_retrieval_tier(queries),
        content_sha256=content_sha256,
        retrieved_at=retrieved_at,
        metadata={
            "section": section,
            "image_captions": list(dict.fromkeys(captions)),
            "image_alt_texts": list(dict.fromkeys(image_alts)),
            "author_line": author_line,
            "parser": "pasaxon_dom_v1",
        },
    )


__all__ = [
    "BASE_URL",
    "CrawlRequest",
    "ListingPage",
    "PasaxonDiscovery",
    "PasaxonParseError",
    "build_epaper_url",
    "build_search_url",
    "build_tag_url",
    "parse_article",
    "parse_epaper_results",
    "parse_listing",
    "parse_search_results",
    "plan_small_crawl",
]
