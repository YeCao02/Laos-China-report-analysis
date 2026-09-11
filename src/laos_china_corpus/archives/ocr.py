"""Local PDF text extraction with optional LiteParse OCR.

Born-digital text is attempted before OCR.  Optional dependencies are loaded
lazily so archive discovery remains usable in a minimal installation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, Callable, Iterable


@dataclass(slots=True)
class TextItem:
    text: str
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["bbox"] = list(self.bbox) if self.bbox is not None else None
        return data


@dataclass(slots=True)
class PageExtraction:
    page_num: int
    text: str
    text_items: list[TextItem] = field(default_factory=list)
    confidence: float | None = None
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_num": self.page_num,
            "text": self.text,
            "text_items": [item.to_dict() for item in self.text_items],
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "review_reasons": list(self.review_reasons),
        }


@dataclass(slots=True)
class PDFExtractionResult:
    source_file: str
    status: str
    method: str
    pages: list[PageExtraction]
    text: str
    confidence: float | None
    needs_review: bool
    review_reasons: list[str] = field(default_factory=list)
    message: str | None = None
    ocr_requested: bool = True
    ocr_language: str = "lao+eng"
    dpi: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "status": self.status,
            "method": self.method,
            "pages": [page.to_dict() for page in self.pages],
            "text": self.text,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "review_reasons": list(self.review_reasons),
            "message": self.message,
            "ocr_requested": self.ocr_requested,
            "ocr_language": self.ocr_language,
            "dpi": self.dpi,
        }


def _join_pages(pages: Iterable[PageExtraction]) -> str:
    return "\n\n".join(page.text.strip() for page in pages if page.text.strip())


def _text_length(pages: Iterable[PageExtraction]) -> int:
    return sum(len("".join(page.text.split())) for page in pages)


def _normalise_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence > 1.0 and confidence <= 100.0:
        confidence /= 100.0
    return max(0.0, min(1.0, confidence))


def _extract_with_pdfplumber(path: Path) -> list[PageExtraction]:
    import pdfplumber  # type: ignore[import-not-found]

    pages: list[PageExtraction] = []
    with pdfplumber.open(path) as document:
        for page_num, page in enumerate(document.pages, start=1):
            words = page.extract_words() or []
            items = [
                TextItem(
                    text=str(word.get("text", "")),
                    bbox=(
                        float(word.get("x0", 0.0)),
                        float(word.get("top", 0.0)),
                        float(word.get("x1", 0.0)) - float(word.get("x0", 0.0)),
                        float(word.get("bottom", 0.0)) - float(word.get("top", 0.0)),
                    ),
                )
                for word in words
                if str(word.get("text", "")).strip()
            ]
            text = page.extract_text() or " ".join(item.text for item in items)
            if text.strip() and not items:
                items = [TextItem(text=text)]
            pages.append(PageExtraction(page_num=page_num, text=text, text_items=items))
    return pages


def _extract_with_pypdf(path: Path) -> list[PageExtraction]:
    from pypdf import PdfReader  # type: ignore[import-not-found]

    document = PdfReader(str(path))
    pages: list[PageExtraction] = []
    for page_num, page in enumerate(document.pages, start=1):
        text = page.extract_text() or ""
        items = [TextItem(text=text)] if text.strip() else []
        pages.append(PageExtraction(page_num=page_num, text=text, text_items=items))
    return pages


def extract_native_text(path: str | Path) -> tuple[str, list[PageExtraction], list[str]]:
    """Try pdfplumber and pypdf, returning the strongest native extraction."""

    source = Path(path)
    attempts: list[tuple[str, list[PageExtraction]]] = []
    diagnostics: list[str] = []
    for method, extractor in (("pdfplumber", _extract_with_pdfplumber), ("pypdf", _extract_with_pypdf)):
        try:
            attempts.append((method, extractor(source)))
        except ImportError:
            diagnostics.append(f"{method} is not installed")
        except Exception as exc:  # a damaged PDF should still be eligible for OCR
            diagnostics.append(f"{method} could not parse PDF: {exc}")
    if not attempts:
        return "none", [], diagnostics
    # Prefer more extracted text; use spatial pdfplumber output to break ties.
    method, pages = max(
        attempts,
        key=lambda candidate: (
            _text_length(candidate[1]),
            sum(item.bbox is not None for page in candidate[1] for item in page.text_items),
        ),
    )
    return method, pages, diagnostics


def _load_liteparse() -> tuple[Callable[..., Any] | None, str | None]:
    try:
        from liteparse import LiteParse  # type: ignore[import-not-found]
    except ImportError:
        return None, "LiteParse is not installed; install liteparse==2.0.0 to enable OCR"
    return LiteParse, None


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _bbox(item: Any) -> tuple[float, float, float, float] | None:
    direct = _value(item, "bbox")
    if direct is not None:
        try:
            values = tuple(float(value) for value in direct)
        except (TypeError, ValueError):
            values = ()
        if len(values) == 4:
            return values  # type: ignore[return-value]
    values = [_value(item, name) for name in ("x", "y", "width", "height")]
    if all(value is not None for value in values):
        try:
            return tuple(float(value) for value in values)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def _liteparse_pages(result: Any, confidence_threshold: float) -> list[PageExtraction]:
    parsed_pages = _value(result, "pages", []) or []
    pages: list[PageExtraction] = []
    for fallback_num, page in enumerate(parsed_pages, start=1):
        raw_items = _value(page, "text_items", []) or []
        items = [
            TextItem(
                text=str(_value(item, "text", "")),
                bbox=_bbox(item),
                confidence=_normalise_confidence(_value(item, "confidence")),
            )
            for item in raw_items
            if str(_value(item, "text", "")).strip()
        ]
        text = str(_value(page, "text", "") or "")
        if not text.strip():
            text = " ".join(item.text for item in items)
        confidences = [item.confidence for item in items if item.confidence is not None]
        confidence = fmean(confidences) if confidences else None
        reasons: list[str] = []
        if not text.strip():
            reasons.append("empty_ocr_page")
        if confidence is None:
            reasons.append("missing_ocr_confidence")
        elif confidence < confidence_threshold:
            reasons.append("low_ocr_confidence")
        if items and any(item.bbox is None for item in items):
            reasons.append("missing_text_item_bbox")
        page_num = _value(page, "page_num", fallback_num)
        pages.append(
            PageExtraction(
                page_num=int(page_num if page_num is not None else fallback_num),
                text=text,
                text_items=items,
                confidence=confidence,
                needs_review=bool(reasons),
                review_reasons=reasons,
            )
        )
    if not pages:
        text = str(_value(result, "text", "") or "")
        if text.strip():
            pages.append(
                PageExtraction(
                    page_num=1,
                    text=text,
                    text_items=[TextItem(text=text)],
                    needs_review=True,
                    review_reasons=["missing_ocr_confidence", "missing_page_layout"],
                )
            )
    return pages


def _result_confidence(pages: Iterable[PageExtraction]) -> float | None:
    values = [page.confidence for page in pages if page.confidence is not None]
    return fmean(values) if values else None


def extract_pdf(
    path: str | Path,
    *,
    enable_ocr: bool = True,
    native_min_chars: int = 80,
    confidence_threshold: float = 0.75,
    dpi: int = 300,
    ocr_language: str = "lao+eng",
    liteparse_factory: Callable[..., Any] | None = None,
) -> PDFExtractionResult:
    """Extract a PDF, using LiteParse OCR only when native text is insufficient."""

    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if native_min_chars < 0:
        raise ValueError("native_min_chars must be non-negative")
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError("confidence_threshold must be between 0 and 1")
    if dpi <= 0:
        raise ValueError("dpi must be positive")

    native_method, native_pages, diagnostics = extract_native_text(source)
    native_length = _text_length(native_pages)
    if native_length >= native_min_chars:
        return PDFExtractionResult(
            source_file=str(source),
            status="native_text",
            method=native_method,
            pages=native_pages,
            text=_join_pages(native_pages),
            confidence=None,
            needs_review=False,
            message="; ".join(diagnostics) or None,
            ocr_requested=enable_ocr,
            ocr_language=ocr_language,
            dpi=dpi,
        )

    if not enable_ocr:
        reasons = ["insufficient_native_text"] if native_length else ["no_native_text"]
        return PDFExtractionResult(
            source_file=str(source),
            status="native_text_insufficient",
            method=native_method,
            pages=native_pages,
            text=_join_pages(native_pages),
            confidence=None,
            needs_review=True,
            review_reasons=reasons,
            message="; ".join(diagnostics) or "OCR disabled",
            ocr_requested=False,
            ocr_language=ocr_language,
            dpi=dpi,
        )

    unavailable_message: str | None = None
    if liteparse_factory is None:
        liteparse_factory, unavailable_message = _load_liteparse()
    if liteparse_factory is None:
        return PDFExtractionResult(
            source_file=str(source),
            status="ocr_unavailable",
            method=native_method,
            pages=native_pages,
            text=_join_pages(native_pages),
            confidence=None,
            needs_review=True,
            review_reasons=["insufficient_native_text", "ocr_dependency_unavailable"],
            message="; ".join([*diagnostics, unavailable_message or "LiteParse unavailable"]),
            ocr_requested=True,
            ocr_language=ocr_language,
            dpi=dpi,
        )

    try:
        parser = liteparse_factory(
            output_format="json",
            ocr_enabled=True,
            ocr_language=ocr_language,
            dpi=dpi,
            quiet=True,
        )
        parsed = parser.parse(str(source))
        pages = _liteparse_pages(parsed, confidence_threshold)
    except Exception as exc:
        return PDFExtractionResult(
            source_file=str(source),
            status="ocr_error",
            method=native_method,
            pages=native_pages,
            text=_join_pages(native_pages),
            confidence=None,
            needs_review=True,
            review_reasons=["insufficient_native_text", "ocr_runtime_error"],
            message=f"LiteParse OCR failed: {exc}",
            ocr_requested=True,
            ocr_language=ocr_language,
            dpi=dpi,
        )

    confidence = _result_confidence(pages)
    reasons = sorted({reason for page in pages for reason in page.review_reasons})
    if not _join_pages(pages):
        reasons.append("no_ocr_text")
    needs_review = bool(reasons)
    return PDFExtractionResult(
        source_file=str(source),
        status="ocr_review_required" if needs_review else "ocr_complete",
        method="liteparse",
        pages=pages,
        text=_join_pages(pages),
        confidence=confidence,
        needs_review=needs_review,
        review_reasons=sorted(set(reasons)),
        message="; ".join(diagnostics) or None,
        ocr_requested=True,
        ocr_language=ocr_language,
        dpi=dpi,
    )
