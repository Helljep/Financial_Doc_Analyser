"""Pull structured data out of a financial document with Gemini.

Text is read locally first (PDF, DOCX, TXT/CSV/LOG). Big documents are cut into chunks, sent
to Gemini in parallel and merged afterwards. Scans and images go to Gemini as page images.
"""
import io
import json
import mimetypes
import os
import random
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum

from PyPDF2 import PdfReader, PdfWriter
from pydantic import BaseModel, Field

from config import MODEL_NAME, MissingAPIKeyError, get_client

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

CHUNK_CHARS = 50_000                   # roughly 13k tokens of text per request
HEADER_LINES = 12                      # lines of title/column headings repeated for later chunks
PAGES_PER_SCAN_PART = 15
MAX_INLINE_BYTES = 15 * 1024 * 1024    # Gemini rejects inline requests over about 20 MB
MAX_WORKERS = int(os.environ.get("GEMINI_WORKERS", "3"))   # lower this on the free tier
MAX_ATTEMPTS = 3
CHUNK_ATTEMPTS = 6

_MONEY = re.compile(r"\d[\d,]*\.\d{2}(?!\d)")


class DocType(str, Enum):
    INVOICE = "invoice"
    TAX_1040 = "tax_1040"
    INSURANCE_CLAIM = "insurance_claim"
    BANK_STATEMENT = "bank_statement"
    UNKNOWN = "unknown"


class LineItemCompact(BaseModel):
    """One row. Large documents use this version, without the per-row reasoning note,
    because that note is the biggest cost in output tokens once there are thousands of rows."""
    description: str | None = None
    date: str | None = Field(default=None, description="Date as YYYY-MM-DD.")
    qty: float | None = None
    price: float | None = None
    discount: float | None = None
    total: float | None = None
    wages: float | None = None
    interest: float | None = None
    dividends: float | None = None
    total_income: float | None = None
    claim_amount: float | None = None
    deductible: float | None = None
    covered_amount: float | None = None
    debit: float | None = None
    credit: float | None = None
    balance: float | None = None


class LineItem(LineItemCompact):
    extraction_reasoning: str = Field(description="Brief 1-line note on how this value was derived.")


class FinancialDocumentCompact(BaseModel):
    extraction_reasoning: str = Field(description="Brief overall reasoning for document classification.")
    vendor_or_entity_name: str | None = Field(description="The name of the vendor, business, or person on the document. Return 'N/A' if missing.")
    grand_total: float | None = Field(description="The final total amount of the document.")
    tax_total: float | None = Field(default=None, description="Invoices only: total tax charged on the whole document (GST, VAT, sales tax), added on top of the line items. Null if none is shown.")
    opening_balance: float | None = Field(default=None, description="Bank statements only: balance before the first transaction.")
    closing_balance: float | None = Field(default=None, description="Bank statements only: balance after the last transaction.")
    document_type: DocType = Field(description="Must be one of the explicitly defined document types.", default=DocType.UNKNOWN)
    records: list[LineItemCompact] = Field(description="Structured records extracted from the document.")


class FinancialDocument(FinancialDocumentCompact):
    records: list[LineItem] = Field(description="Structured records extracted from the document.")


SYSTEM_INSTRUCTION = (
    "You are an expert Document Intelligence API. "
    "Extract all structured financial data from the document. "
    "Keep 'extraction_reasoning' fields extremely brief (max 1 sentence each). "
    "Classify the document as 'invoice', 'tax_1040', 'insurance_claim', or 'bank_statement'. "
    "For 'records', construct a list of JSON objects representing table rows or logical line items. "
    "For invoices: include description, qty, price, discount, and total. "
    "Put the tax charged on the whole invoice (GST, VAT) in tax_total, not in the line items. "
    "For tax_1040: include wages, interest, dividends, and total_income. "
    "For insurance_claim: include claim_amount, deductible, and covered_amount. "
    "For bank_statement: include date, description, debit, credit and the running balance for each "
    "transaction, plus the statement's opening_balance and closing_balance. "
    "Write every date as YYYY-MM-DD. "
    "If a value applies to several rows but is printed only once (a group heading, or blank cells "
    "under a parent row), repeat it on every row it applies to. "
    "Only fill a field if the document actually states it; otherwise leave it null. Never invent values. "
    "DO NOT echo back the raw document text. Focus only on structured numerical data."
)


class Usage:
    """Token counts for one extraction run, shared by the worker threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, response):
        meta = getattr(response, "usage_metadata", None)
        with self._lock:
            self.calls += 1
            if meta is not None:
                self.input_tokens += getattr(meta, "prompt_token_count", 0) or 0
                self.output_tokens += getattr(meta, "candidates_token_count", 0) or 0

    def as_dict(self):
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


# Reading the file ---------------------------------------------------------------------------

def _shape(line: str) -> str:
    """A line with its digits masked, so 'Page 3 of 90' and 'Page 4 of 90' look the same."""
    return re.sub(r"\s+", " ", re.sub(r"\d", "#", line)).strip()


def strip_page_furniture(pages: list) -> list:
    """Remove headers and footers that repeat on most pages (letterhead, 'Page 3 of 90').

    Lines with a money amount are never removed, so data rows are safe. The first page keeps
    its copy, which is where later chunks get their column headings from.
    """
    if len(pages) < 6:
        return pages
    seen = Counter()
    for text in pages:
        seen.update({_shape(line) for line in text.splitlines() if line.strip()})
    common = {shape for shape, count in seen.items() if count >= 0.6 * len(pages)}

    def keep(line):
        return not line.strip() or _MONEY.search(line) or _shape(line) not in common

    cleaned = [pages[0]]
    for text in pages[1:]:
        cleaned.append("\n".join(line for line in text.splitlines() if keep(line)))
    return cleaned


def _read_docx(file) -> str:
    from docx import Document

    file.seek(0)
    doc = Document(io.BytesIO(file.read()))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    file.seek(0)
    return "\n".join(parts)


def _read_pdf(file) -> str:
    pages = [page.extract_text() or "" for page in PdfReader(file).pages]
    file.seek(0)
    pages = strip_page_furniture(pages)
    return "\n\n".join(f"--- PAGE {n} ---\n{text}" for n, text in enumerate(pages, start=1) if text.strip())


def _read_locally(file, filename: str):
    """Text of the file and its mime type, without calling any API."""
    mime_type, _ = mimetypes.guess_type(filename)
    if filename.endswith(".docx"):
        mime_type = DOCX_MIME
    elif not mime_type:
        mime_type = "application/pdf" if filename.endswith(".pdf") else "text/plain"

    try:
        if mime_type == DOCX_MIME:
            return _read_docx(file), mime_type
        if mime_type == "application/pdf":
            return _read_pdf(file), mime_type
        if mime_type.startswith("text/") or filename.endswith((".log", ".csv", ".txt")):
            raw = file.read()
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            return text, "text/plain"
    except Exception:
        file.seek(0)
        return "", mime_type

    return "", mime_type        # images: nothing to read without OCR


# Talking to Gemini --------------------------------------------------------------------------

def _rate_limited(error) -> bool:
    text = str(error)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def _retry_delay(error, attempt: int) -> float:
    """Seconds to wait before trying again. Rate limits wait much longer than other errors."""
    if not _rate_limited(error):
        return 2 ** attempt
    hint = re.search(r"retry in ([\d.]+)s|retryDelay\W+(\d+)s", str(error))
    hinted = float(next(g for g in hint.groups() if g)) if hint else 0
    return min(90, max(hinted, 5 * 2 ** attempt)) + random.random()


def _call_gemini(contents, config, attempts=MAX_ATTEMPTS, usage=None):
    last_error = None
    for attempt in range(attempts):
        try:
            response = get_client().models.generate_content(model=MODEL_NAME, contents=contents, config=config)
            if usage is not None:
                usage.add(response)
            return response
        except MissingAPIKeyError:
            raise
        except Exception as error:
            last_error = error
            if attempt < attempts - 1:
                time.sleep(_retry_delay(error, attempt))
    raise last_error


def _clean_record(record: dict) -> dict:
    """Keep only what the document stated; the schema returns every field, empty or not."""
    return {k: v for k, v in record.items() if v is not None and v != ""}


# Chunking -----------------------------------------------------------------------------------

def split_text(text: str, max_chars: int = CHUNK_CHARS) -> list:
    """Split on line breaks so a table row is never cut in half."""
    chunks, current, size = [], [], 0
    for line in text.splitlines(keepends=True):
        if len(line) > max_chars:                    # one enormous line: cut it anyway
            if current:
                chunks.append("".join(current))
                current, size = [], 0
            chunks.extend(line[i:i + max_chars] for i in range(0, len(line), max_chars))
            continue
        if size + len(line) > max_chars and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _header_context(text: str) -> str:
    """The title and column headings at the top, up to the first data row.

    Later chunks get these so the model knows what the columns mean. It has to stop before the
    first data row, or that row would be extracted again from every chunk.
    """
    header = []
    for line in text.splitlines()[:HEADER_LINES * 2]:
        if len(_MONEY.findall(line)) >= 2:           # an amount plus a balance or total: data starts here
            break
        header.append(line)
    return "\n".join(header[:HEADER_LINES])[:3000]


def _pdf_parts(file, pages_per_part: int = PAGES_PER_SCAN_PART) -> list:
    """Cut a scanned PDF into small PDFs (as bytes), halving any that are too big to send inline."""
    try:
        file.seek(0)
        reader = PdfReader(file)
        total = len(reader.pages)
    except Exception:
        file.seek(0)
        return [file.read()]

    parts = []

    def add(start, end):
        writer = PdfWriter()
        for page in reader.pages[start:end]:
            writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        data = buffer.getvalue()
        if len(data) > MAX_INLINE_BYTES and end - start > 1:
            middle = (start + end) // 2
            add(start, middle)
            add(middle, end)
        else:
            parts.append(data)

    for start in range(0, total, pages_per_part):
        add(start, min(start + pages_per_part, total))
    file.seek(0)
    return parts


def _prompt(label: str, parts: int, header: str, body: str) -> str:
    if parts == 1:
        return f"Extract all structured data from this financial document:\n\n{body}"
    context = f"Document header (context only, do not extract rows from it):\n{header}\n\n" if header else ""
    return (f"This is part {label} of {parts} of a longer financial document. Extract every record "
            "that appears in this part only. Only fill grand_total, opening_balance and "
            "closing_balance if this part actually states them.\n\n"
            f"{context}PART TEXT:\n{body}")


def _extract_text_part(text, label, parts, header, config, usage, depth=0) -> list:
    """Extract one chunk. If the answer fails or gets cut off, try again on each half."""
    try:
        prompt = _prompt(label, max(parts, 2) if depth else parts, header, text)
        response = _call_gemini(prompt, config, attempts=CHUNK_ATTEMPTS, usage=usage)
        return [json.loads(response.text)]
    except Exception as error:
        if isinstance(error, MissingAPIKeyError) or depth >= 3 or len(text) < 8000:
            raise
        halves = split_text(text, len(text) // 2 + 1)
        if len(halves) < 2:
            raise
        results = []
        for number, half in enumerate(halves, start=1):
            results += _extract_text_part(half, f"{label}.{number}", parts, header, config, usage, depth + 1)
        return results


def _extract_scan_part(contents, config, usage) -> list:
    response = _call_gemini(contents, config, attempts=CHUNK_ATTEMPTS, usage=usage)
    return [json.loads(response.text)]


def _merge(parts: list) -> dict:
    """Combine the per-chunk answers into one document."""
    types_seen = [p.get("document_type") for p in parts if p.get("document_type") not in (None, "unknown")]
    vendors = [p.get("vendor_or_entity_name") for p in parts
               if p.get("vendor_or_entity_name") and str(p["vendor_or_entity_name"]).strip().lower() not in ("n/a", "none", "null")]
    totals = [p.get("grand_total") for p in parts if p.get("grand_total")]
    taxes = [p.get("tax_total") for p in parts if p.get("tax_total")]
    openings = [p.get("opening_balance") for p in parts if p.get("opening_balance") is not None]
    closings = [p.get("closing_balance") for p in parts if p.get("closing_balance") is not None]
    return {
        "document_type": Counter(types_seen).most_common(1)[0][0] if types_seen else "unknown",
        "vendor_or_entity_name": vendors[0] if vendors else "N/A",
        "grand_total": totals[-1] if totals else 0.0,           # the stated total usually comes last
        "tax_total": taxes[-1] if taxes else None,
        "opening_balance": openings[0] if openings else None,   # and the opening balance first
        "closing_balance": closings[-1] if closings else None,
        "records": [r for p in parts for r in p.get("records", [])],
        "extraction_reasoning": parts[0].get("extraction_reasoning", "") if parts else "",
    }


def _error(message: str, text: str = "") -> dict:
    return {"type": "error", "error": message, "text": text, "data": []}


def extract_financial_data(file, on_progress=None):
    """Read a document and return its structured data.

    Large documents are split into parts that are extracted in parallel. A part that still
    fails after retrying is listed in `extraction_warnings` rather than silently dropped.
    `on_progress(done, total)` is called as parts finish. On failure the result has type "error".
    """
    filename = file.name.lower()
    if filename.endswith(".doc"):
        return _error("Legacy .doc files are not supported. Please save as .docx or PDF.")

    text, mime_type = _read_locally(file, filename)
    usage = Usage()

    try:
        from google.genai import types

        def make_config(multi_part: bool):
            return types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=FinancialDocumentCompact if multi_part else FinancialDocument,
                temperature=0.1,
                max_output_tokens=65536,
            )

        tasks = []
        if text and len(text.strip()) > 50:
            chunks = split_text(text)
            count = len(chunks)
            config = make_config(count > 1)
            header = _header_context(text) if count > 1 else ""
            for number, chunk in enumerate(chunks, start=1):
                tasks.append(lambda chunk=chunk, number=number:
                             _extract_text_part(chunk, str(number), count, header, config, usage))
        else:
            if mime_type == DOCX_MIME or mime_type.startswith("text/"):
                return _error("No readable text could be found in this document.")
            if mime_type == "application/pdf":
                blobs = _pdf_parts(file)
            else:
                file.seek(0)
                blobs = [file.read()]
            count = len(blobs)
            config = make_config(count > 1)
            for number, blob in enumerate(blobs, start=1):
                contents = [types.Part.from_bytes(data=blob, mime_type=mime_type)]
                if count > 1:
                    contents.append(f"This is part {number} of {count} of a longer scanned document. "
                                    "Extract the records in this part only.")
                tasks.append(lambda contents=contents: _extract_scan_part(contents, config, usage))

        results, failures = {}, {}
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(tasks))) as pool:
            futures = {pool.submit(task): index for index, task in enumerate(tasks)}
            for done, future in enumerate(as_completed(futures), start=1):
                index = futures[future]
                try:
                    results[index] = future.result()
                except MissingAPIKeyError:
                    raise
                except Exception as error:
                    failures[index] = f"{type(error).__name__}: {str(error)[:150]}"
                if on_progress:
                    on_progress(done, len(tasks))

        if not results:
            first = next(iter(failures.values()), "no response")
            return _error(f"Extraction failed for every part. First error: {first}", text)

        merged = _merge([part for index in sorted(results) for part in results[index]])
        doc_type = merged["document_type"]
        if doc_type not in [t.value for t in DocType]:
            doc_type = DocType.UNKNOWN.value

        return {
            "type": doc_type,
            "vendor_or_entity_name": merged["vendor_or_entity_name"],
            "grand_total": merged["grand_total"],
            "tax_total": merged["tax_total"],
            "opening_balance": merged["opening_balance"],
            "closing_balance": merged["closing_balance"],
            "text": text or "Text extracted via multimodal vision.",
            "data": [_clean_record(r) for r in merged["records"]],
            "extraction_reasoning": merged["extraction_reasoning"],
            "extraction_warnings": [
                f"Part {index + 1} of {len(tasks)} could not be extracted ({why}). Its rows are MISSING from these results."
                for index, why in sorted(failures.items())
            ],
            "chunks": len(tasks),
            "usage": usage.as_dict(),
        }

    except Exception as error:
        return _error(f"{type(error).__name__}: {error}", text)
