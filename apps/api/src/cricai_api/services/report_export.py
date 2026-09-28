"""US-K5: report print/export — A4-legible PDF/PNG typeset from the report HTML.

Pure-Python path, no new dependencies: the printable HTML from
``cricai_coaching.report.render_report_html`` (the single wording source the
web surface mirrors, so no divergent copies — US-K5 AC) is flattened to
styled text lines with the stdlib HTML parser, wrapped and laid out on A4
pages, then typeset by matplotlib's deterministic Agg/PDF backends —
already in the API's dependency closure via ``cricai-vision`` (the US-C6
heatmap PNGs use the same backend). Monospace wrapping keeps layout a pure
function of the text, and no timestamps are embedded, so identical bodies
export identical bytes (the ST snapshot contract).
"""

from __future__ import annotations

import io
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure

LineStyle = Literal["title", "heading", "body"]

#: A4 geometry, inches. PNG rasterizes at PNG_DPI (A4 width -> ~1240 px, so
#: 10 pt body text lands around 21 px tall: legible on tablet and paper).
A4_WIDTH_IN = 8.27
A4_HEIGHT_IN = 11.69
MARGIN_IN = 0.9
PNG_DPI = 150

#: Extra leading above a title/heading line that is not at the top of a page.
SECTION_GAP_IN = 0.14

#: Per-style typography: (font size pt, line height in, wrap width chars).
#: Widths are exact for the monospace face at these sizes within the margins.
STYLES: dict[LineStyle, tuple[float, float, int]] = {
    "title": (16.0, 0.34, 48),
    "heading": (12.5, 0.28, 62),
    "body": (10.0, 0.21, 77),
}

#: Report-HTML tags whose text content becomes one line of the given style.
#: Everything else (section/ul/details/code/...) contributes text only.
_TEXT_TAGS: dict[str, LineStyle] = {
    "h1": "title",
    "h2": "heading",
    "summary": "heading",
    "p": "body",
    "li": "body",
}

_FONT_FAMILY = "DejaVu Sans Mono"

#: One laid-out page: (baseline y from the page top in inches, line).
Page = list[tuple[float, "TextLine"]]


@dataclass(frozen=True)
class TextLine:
    text: str
    style: LineStyle


class _TextExtractor(HTMLParser):
    """Flatten report HTML to (text, style) lines in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[TextLine] = []
        self._styles: list[LineStyle] = []
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        style = _TEXT_TAGS.get(tag)
        if style is not None:
            self._styles.append(style)
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag not in _TEXT_TAGS or not self._styles:
            return
        style = self._styles.pop()
        text = " ".join("".join(self._buffer).split())
        if text:
            self.lines.append(TextLine(text=text, style=style))
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._styles:
            self._buffer.append(data)


def html_to_lines(html: str) -> list[TextLine]:
    """Text lines of the report HTML, in document order, entities decoded."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.lines


def wrap_lines(lines: Sequence[TextLine]) -> list[TextLine]:
    """Word-wrap each logical line to its style's monospace column budget."""
    wrapped: list[TextLine] = []
    for line in lines:
        width = STYLES[line.style][2]
        wrapped.extend(TextLine(piece, line.style) for piece in textwrap.wrap(line.text, width))
    return wrapped


def layout_pages(
    lines: Sequence[TextLine], page_height_in: float | None
) -> tuple[list[Page], float]:
    """Place wrapped lines top-down; break to a new page past the bottom margin.

    ``page_height_in=None`` lays everything on one page that grows to fit
    (the PNG path — a PNG has no page breaks); a fixed height paginates for
    the multi-page PDF. Returns the pages and the page height used.
    """
    pages: list[Page] = [[]]
    y = MARGIN_IN
    for line in lines:
        gap = SECTION_GAP_IN if line.style != "body" and pages[-1] else 0.0
        height = STYLES[line.style][1]
        limit = None if page_height_in is None else page_height_in - MARGIN_IN
        if limit is not None and pages[-1] and y + gap + height > limit:
            pages.append([])
            y = MARGIN_IN
            gap = 0.0
        y += gap + height
        pages[-1].append((y, line))
    used_height = page_height_in if page_height_in is not None else max(A4_HEIGHT_IN, y + MARGIN_IN)
    return pages, used_height


def _draw_page(figure: Figure, page: Page, page_height_in: float) -> None:
    for y_in, line in page:
        size = STYLES[line.style][0]
        figure.text(
            MARGIN_IN / A4_WIDTH_IN,
            (page_height_in - y_in) / page_height_in,
            line.text,
            fontsize=size,
            fontweight="normal" if line.style == "body" else "bold",
            fontfamily=_FONT_FAMILY,
            ha="left",
            va="baseline",
        )


def render_pdf(html: str) -> bytes:
    """A4 multi-page PDF of the report HTML — deterministic, timestamp-free."""
    pages, height = layout_pages(wrap_lines(html_to_lines(html)), A4_HEIGHT_IN)
    buffer = io.BytesIO()
    with PdfPages(buffer, metadata={"CreationDate": None}) as pdf:
        for page in pages:
            figure = Figure(figsize=(A4_WIDTH_IN, height))
            _draw_page(figure, page, height)
            pdf.savefig(figure)
    return buffer.getvalue()


def render_png(html: str) -> bytes:
    """A4-width PNG of the report HTML; the canvas grows past A4 rather than
    truncating a long report (a PNG has no second page to break to)."""
    pages, height = layout_pages(wrap_lines(html_to_lines(html)), None)
    figure = Figure(figsize=(A4_WIDTH_IN, height))
    FigureCanvasAgg(figure)
    _draw_page(figure, pages[0], height)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=PNG_DPI, metadata={})  # no timestamps
    return buffer.getvalue()
