import io
import json
import mimetypes
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum

from PyPDF2 import PdfReader, PdfWriter
from pydantic import BaseModel, Field

from config import MODEL_NAME, get_client

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_ATTEMPTS = 3

# Chunking limits. ~50k characters is roughly 13k input tokens per call, which leaves plenty of room
# for the structured answer (rows are the expensive part of the output).
CHUNK_CHARS = 50_000
HEADER_CONTEXT_LINES = 12
PAGES_PER_SCANNED_CHUNK = 15
MAX_WORKERS = 4


class DocType(str, Enum):
    INVOICE = "invoice"
    TAX_1040 = "tax_1040"
    INSURANCE_CLAIM = "insurance_claim"
    BANK_STATEMENT = "bank_statement"
    UNKNOWN = "unknown"


class LineItemCompact(BaseModel):
    """A row without the per-row reasoning note: used for large documents, where thousands of rows make
    that note the single biggest cost in output tokens and time."""
    description: str | None = None
    date: str | None = None
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
    "For tax_1040: include wages, interest, dividends, and total_income. "
    "For insurance_claim: include claim_amount, deductible, and covered_amount. "
    "For bank_statement: include date, description, debit, credit and the running balance for each "
    "transaction, plus the statement's opening_balance and closing_balance. "
    "Only fill a field if the document actually states it; otherwise leave it null. Never invent values. "
    "DO NOT echo back the raw document text. Focus only on structured numerical data."
)


def _extract_docx_text(file) -> str:
    from docx import Document  # python-docx

    file.seek(0)
    doc = Document(io.BytesIO(file.read()))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    file.seek(0)
    return "\n".join(parts)


def _extract_text_locally(file, filename: str) -> tuple[str, str]:
    """
    Extracts raw text from the document LOCALLY without any API calls.
    Returns (extracted_text, mime_type).
    """
    mime_type, _ = mimetypes.guess_type(filename)
    if filename.endswith(".docx"):
        mime_type = DOCX_MIME
    elif not mime_type:
        mime_type = "application/pdf" if filename.endswith(".pdf") else "text/plain"

    # --- Word (.docx): paragraphs + tables ---
    if mime_type == DOCX_MIME:
        try:
            return _extract_docx_text(file), mime_type
        except Exception:
            file.seek(0)
            return "", mime_type

    # --- PDF: Extract text from ALL pages locally ---
    if mime_type == "application/pdf":
        try:
            reader = PdfReader(file)
            all_text = []
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    all_text.append(f"--- PAGE {i + 1} ---\n{page_text}")
            file.seek(0)
            return "\n\n".join(all_text), mime_type
        except Exception:
            file.seek(0)
            return "", mime_type

    # --- Plain text / CSV / Log files ---
    elif mime_type.startswith("text/") or filename.endswith((".log", ".csv", ".txt")):
        try:
            raw = file.read()
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace"), "text/plain"
            return raw, "text/plain"
        except Exception:
            file.seek(0)
            return "", mime_type

    # --- Images (PNG, JPG): No local text extraction possible ---
    else:
        return "", mime_type


def _call_gemini(contents, config, attempts=MAX_ATTEMPTS):
    """Call Gemini, retrying transient failures (rate limits, 5xx) with exponential backoff."""
    last_error = None
    for attempt in range(attempts):
        try:
            return get_client().models.generate_content(model=MODEL_NAME, contents=contents, config=config)
        except Exception as e:
            last_error = e
            if type(e).__name__ == "MissingAPIKeyError":
                raise
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    raise last_error


def _drop_nulls(record: dict) -> dict:
    """The schema returns every field (null when absent); keep only what the document stated."""
    return {k: v for k, v in record.items() if v is not None}


# ── Chunking: documents far larger than one model call can read or write ─────────────────────
def split_text(text: str, max_chars: int = CHUNK_CHARS) -> list:
    """Split on line boundaries so no table row is cut in half."""
    chunks, cur, size = [], [], 0
    for line in text.splitlines(keepends=True):
        if len(line) > max_chars:                       # pathological single line: hard split
            if cur:
                chunks.append("".join(cur))
                cur, size = [], 0
            chunks.extend(line[i:i + max_chars] for i in range(0, len(line), max_chars))
            continue
        if size + len(line) > max_chars and cur:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


_MONEY = re.compile(r"\d[\d,]*\.\d{2}(?![\d])")


def _header_context(text: str) -> str:
    """Leading title/column-heading lines, up to (never including) the first data row.

    Later chunks get this so the model knows what the columns mean. It must stop before the first data
    row, otherwise those rows would be extracted a second time from every chunk.
    """
    header = []
    for line in text.splitlines()[:HEADER_CONTEXT_LINES * 2]:
        if len(_MONEY.findall(line)) >= 2:            # a row with an amount AND a balance/total: data starts here
            break
        header.append(line)
    return "\n".join(header[:HEADER_CONTEXT_LINES])[:3000]


def _split_pdf_pages(file, pages_per_chunk: int = PAGES_PER_SCANNED_CHUNK) -> list:
    """Cut a scanned PDF into small sub-PDFs (bytes). Falls back to the whole file."""
    try:
        file.seek(0)
        reader = PdfReader(file)
        n = len(reader.pages)
        if n > pages_per_chunk:
            parts = []
            for start in range(0, n, pages_per_chunk):
                writer = PdfWriter()
                for page in reader.pages[start:start + pages_per_chunk]:
                    writer.add_page(page)
                buf = io.BytesIO()
                writer.write(buf)
                parts.append(buf.getvalue())
            file.seek(0)
            return parts
    except Exception:
        pass
    file.seek(0)
    return [file.read()]


def _prompt(label: str, n: int, header: str, body: str) -> str:
    if n == 1:
        return f"Analyze this financial document and extract ALL structured data:\n\n{body}"
    ctx = f"Document header (context only; do NOT extract rows from it):\n{header}\n\n" if header else ""
    return (f"This is part {label} of {n} of a much longer financial document. Extract ALL structured records that "
            f"appear in THIS part only. Fill grand_total, opening_balance and closing_balance only if they are "
            f"actually stated in this part; otherwise leave them null.\n\n{ctx}PART TEXT:\n{body}")


def _extract_text_part(text, label, n, header, gen_config, depth=0) -> list:
    """Extract one text chunk. If the answer is cut off or fails, retry on two halves."""
    try:
        response = _call_gemini(_prompt(label, max(n, 2) if depth else n, header, text), gen_config, attempts=5)
        return [json.loads(response.text)]
    except Exception as e:
        if type(e).__name__ == "MissingAPIKeyError" or depth >= 3 or len(text) < 8000:
            raise
        halves = split_text(text, len(text) // 2 + 1)
        if len(halves) < 2:
            raise
        out = []
        for k, half in enumerate(halves, 1):
            out += _extract_text_part(half, f"{label}.{k}", n, header, gen_config, depth + 1)
        return out


def _extract_binary_part(contents, gen_config) -> list:
    return [json.loads(_call_gemini(contents, gen_config, attempts=5).text)]


def _merge(parts: list) -> dict:
    """Combine per-chunk results into one document."""
    types_seen = [p.get("document_type") for p in parts if p.get("document_type") not in (None, "unknown")]
    vendors = [p.get("vendor_or_entity_name") for p in parts
               if p.get("vendor_or_entity_name") and str(p["vendor_or_entity_name"]).strip().lower() not in ("n/a", "none", "null")]
    totals = [p.get("grand_total") for p in parts if p.get("grand_total")]
    openings = [p.get("opening_balance") for p in parts if p.get("opening_balance") is not None]
    closings = [p.get("closing_balance") for p in parts if p.get("closing_balance") is not None]
    records = [r for p in parts for r in p.get("records", [])]
    return {
        "document_type": Counter(types_seen).most_common(1)[0][0] if types_seen else "unknown",
        "vendor_or_entity_name": vendors[0] if vendors else "N/A",
        "grand_total": totals[-1] if totals else 0.0,          # the stated total normally appears last
        "opening_balance": openings[0] if openings else None,  # ...and the opening balance first
        "closing_balance": closings[-1] if closings else None,
        "records": records,
        "extraction_reasoning": parts[0].get("extraction_reasoning", "") if parts else "",
    }


def extract_financial_data(file, on_progress=None):
    """
    Hybrid extraction pipeline for documents of any size:
    1. Extract text locally (0 API calls): PDF, DOCX, TXT/CSV/LOG.
    2. Split large documents into chunks on line boundaries (CHUNK_CHARS each) and extract each chunk
       with Gemini in parallel; scanned PDFs are split into small page groups and sent as vision input.
    3. Merge the per-chunk results. A chunk that still fails after retries is reported in
       `extraction_warnings` (never silently dropped) so the validator can flag the gap.

    `on_progress(done, total)` is called as chunks finish. On failure returns
    {"type": "error", "error": <message>, "data": []}.
    """
    filename = file.name.lower()

    if filename.endswith(".doc"):
        return {"type": "error", "error": "Legacy .doc files are not supported. Please save as .docx or PDF.", "text": "", "data": []}

    local_text, mime_type = _extract_text_locally(file, filename)

    try:
        from google.genai import types
        def make_config(multi_part: bool):
            return types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION, response_mime_type="application/json",
                response_schema=FinancialDocumentCompact if multi_part else FinancialDocument,
                temperature=0.1, max_output_tokens=65536,
            )

        header = ""
        if local_text and len(local_text.strip()) > 50:
            chunks = split_text(local_text)
            n = len(chunks)
            gen_config = make_config(n > 1)
            if n > 1:
                header = _header_context(local_text)
            tasks = [(str(i), (lambda t=c, i=i: _extract_text_part(t, str(i), n, header, gen_config))) for i, c in enumerate(chunks, 1)]
        else:
            if mime_type == DOCX_MIME or mime_type.startswith("text/"):
                return {"type": "error", "error": "No readable text could be found in this document.", "text": "", "data": []}
            if mime_type == "application/pdf":
                blobs = _split_pdf_pages(file)
            else:
                file.seek(0)
                blobs = [file.read()]
            n = len(blobs)
            gen_config = make_config(n > 1)
            tasks = []
            for i, blob in enumerate(blobs, 1):
                contents = [types.Part.from_bytes(data=blob, mime_type=mime_type)]
                if n > 1:
                    contents.append(f"This is part {i} of {n} of a longer scanned document. Extract the records in this part only.")
                tasks.append((str(i), (lambda c=contents: _extract_binary_part(c, gen_config))))

        results, failures, done = {}, {}, 0
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(tasks))) as pool:
            futures = {pool.submit(fn): (idx, label) for idx, (label, fn) in enumerate(tasks)}
            for fut in as_completed(futures):
                idx, label = futures[fut]
                try:
                    results[idx] = fut.result()
                except Exception as e:
                    if type(e).__name__ == "MissingAPIKeyError":
                        raise
                    failures[idx] = f"{type(e).__name__}: {str(e)[:150]}"
                done += 1
                if on_progress:
                    on_progress(done, len(tasks))

        if not results:
            first = next(iter(failures.values()), "no response")
            return {"type": "error", "error": f"Extraction failed for every part. First error: {first}", "text": local_text, "data": []}

        parts = [p for idx in sorted(results) for p in results[idx]]
        merged = _merge(parts)
        doc_type_val = merged["document_type"]
        if doc_type_val not in [e.value for e in DocType]:
            doc_type_val = DocType.UNKNOWN.value

        warnings = [f"Part {idx + 1} of {len(tasks)} could not be extracted ({why}). Its rows are MISSING from these results."
                    for idx, why in sorted(failures.items())]

        return {
            "type": doc_type_val,
            "vendor_or_entity_name": merged["vendor_or_entity_name"],
            "grand_total": merged["grand_total"],
            "opening_balance": merged["opening_balance"],
            "closing_balance": merged["closing_balance"],
            "text": local_text or "Text extracted via multimodal vision.",
            "data": [_drop_nulls(r) for r in merged["records"]],
            "extraction_reasoning": merged["extraction_reasoning"],
            "extraction_warnings": warnings,
            "chunks": len(tasks),
        }

    except Exception as e:
        return {"type": "error", "error": f"{type(e).__name__}: {e}", "text": local_text, "data": []}
