"""Find where an extracted value sits in the original document.

This works on anything that has a text layer: text PDFs, txt/csv/log files and docx text.
Scanned PDFs and images have nothing to search, so no index is built for them.
"""
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

import pymupdf

# The row fields that hold money amounts. These are what we look for in the source.
AMOUNT_FIELDS = ("total", "amount", "price", "rate", "debit", "credit", "balance",
                 "claim_amount", "deductible", "covered_amount", "wages", "interest",
                 "dividends", "total_income")

IMAGE_TYPES = (".png", ".jpg", ".jpeg")

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def number_key(value):
    """Turn a number into a string that matches however the document printed it.

    1234.5, "1,234.50" and "(1234.50)" all give "1234.5". Zero and non-numbers give None.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip("()$ "))
    except InvalidOperation:
        return None
    if not number.is_finite() or number == 0:
        return None
    return format(abs(number).normalize(), "f")


class SourceIndex:
    def __init__(self, pages, doc=None):
        self.pages = pages          # text of each page
        self._doc = doc             # the open PDF, used for drawing highlights
        self._where = defaultdict(set)    # number -> pages it appears on (1-based)
        for page_no, text in enumerate(pages, start=1):
            for found in _NUMBER.findall(text):
                key = number_key(found)
                if key:
                    self._where[key].add(page_no)

    @classmethod
    def from_pdf(cls, data: bytes):
        doc = pymupdf.open(stream=data, filetype="pdf")
        return cls([page.get_text() for page in doc], doc)

    @classmethod
    def from_text(cls, text: str):
        return cls([text])

    @property
    def usable(self) -> bool:
        return sum(len(text.strip()) for text in self.pages) > 50

    @property
    def can_draw(self) -> bool:
        return self._doc is not None

    def _keys(self, row):
        keys = []
        for field in AMOUNT_FIELDS:
            key = number_key(row.get(field))
            if key and key not in keys:
                keys.append(key)
        return keys

    def pages_for(self, row):
        """Pages where all of this row's amounts appear together."""
        keys = self._keys(row)
        if not keys:
            return []
        common = set(self._where.get(keys[0], ()))
        for key in keys[1:]:
            common &= self._where.get(key, set())
        return sorted(common)

    def problem(self, row):
        """Why this row can't be found in the source, or None if it checks out.

        Rows with no amounts can't be checked, so they also give None.
        """
        if not self.usable or not self._keys(row):
            return None
        missing = [f"{field} {row[field]}" for field in AMOUNT_FIELDS
                   if number_key(row.get(field)) and number_key(row.get(field)) not in self._where]
        if missing:
            return "Not found in the source document: " + ", ".join(missing)
        if not self.pages_for(row):
            return "This row's amounts do not appear together on any one page of the source."
        return None

    def _best_line(self, page_no, row):
        """Lines of the page ranked by how many of the row's amounts they contain."""
        keys = set(self._keys(row))
        scored = []
        for number, line in enumerate(self.pages[page_no - 1].splitlines()):
            hits = {number_key(f) for f in _NUMBER.findall(line)} & keys
            if hits:
                scored.append((len(hits), number, line))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored

    def snippet(self, page_no, row):
        """The source lines around a row, for documents we can't draw on."""
        scored = self._best_line(page_no, row)
        if not scored:
            return ""
        lines = self.pages[page_no - 1].splitlines()
        at = scored[0][1]
        return "\n".join(lines[max(0, at - 2):at + 3])

    def render(self, page_no, row=None, zoom=2.0):
        """PNG of the part of the page around the row, with the row highlighted. PDFs only."""
        if self._doc is None:
            return None
        # Work on a one-page copy so the highlight never touches the real document.
        copy = pymupdf.open()
        copy.insert_pdf(self._doc, from_page=page_no - 1, to_page=page_no - 1)
        page = copy[0]

        keys = set(self._keys(row)) if row else set()
        hits = []
        for x0, y0, x1, y1, word, *_ in page.get_text("words"):
            found = {number_key(f) for f in _NUMBER.findall(word)} & keys
            if found:
                hits.append((y0, y1, pymupdf.Rect(x0, y0, x1, y1), found))

        clip = page.rect
        if hits:
            # Several rows can share an amount; keep the text line that matches the most of them.
            lines = defaultdict(list)
            for hit in hits:
                lines[round(hit[0] / 4)].append(hit)
            best = max(lines.values(), key=lambda line: len(set().union(*(h[3] for h in line))))
            top = min(h[0] for h in best)
            bottom = max(h[1] for h in best)
            band = pymupdf.Rect(page.rect.x0 + 10, top - 1, page.rect.x1 - 10, bottom + 1)
            page.draw_rect(band, color=None, fill=(1, 0.92, 0.3), fill_opacity=0.35)
            for _, _, rect, _ in best:
                page.draw_rect(rect, color=(0.85, 0.1, 0.1), width=1.2)
            clip = pymupdf.Rect(page.rect.x0, max(page.rect.y0, top - 110),
                                page.rect.x1, min(page.rect.y1, bottom + 110))

        picture = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
        copy.close()
        return picture.tobytes("png")


def build_index(filename: str, data: bytes, text: str = ""):
    """Build an index for an uploaded file, or return None if there is nothing to search."""
    name = filename.lower()
    if name.endswith(IMAGE_TYPES):
        return None
    if name.endswith(".pdf"):
        try:
            index = SourceIndex.from_pdf(data)
        except Exception:
            return None
    else:
        index = SourceIndex.from_text(text)
    return index if index.usable else None


def add_page_numbers(rows, index):
    """Record the page each row came from. Single-page sources don't need it."""
    if index is None or len(index.pages) < 2:
        return rows
    for row in rows:
        pages = index.pages_for(row)
        if pages:
            row["page"] = pages[0]
    return rows
