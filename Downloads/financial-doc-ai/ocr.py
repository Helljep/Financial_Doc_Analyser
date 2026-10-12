import mimetypes
import json
from PyPDF2 import PdfReader
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from enum import Enum

class DocType(str, Enum):
    INVOICE = "invoice"
    TAX_1040 = "tax_1040"
    INSURANCE_CLAIM = "insurance_claim"
    BANK_STATEMENT = "bank_statement"
    UNKNOWN = "unknown"

# Note: For production, move this to an environment variable!
client = genai.Client(api_key="AIzaSyCnaDrmUqHn0yl6_BVjNFtawLRhm5vnCJQ")

class LineItem(BaseModel):
    extraction_reasoning: str = Field(description="Brief 1-line note on how this value was derived.")
    description: str | None = None
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

class FinancialDocument(BaseModel):
    extraction_reasoning: str = Field(description="Brief overall reasoning for document classification.")
    vendor_or_entity_name: str | None = Field(description="The name of the vendor, business, or person on the document. Return 'N/A' if missing.")
    grand_total: float | None = Field(description="The final total amount of the document.")
    document_type: DocType = Field(description="Must be one of the explicitly defined document types.", default=DocType.UNKNOWN)
    records: list[LineItem] = Field(description="Structured records extracted from the document.")


def _extract_text_locally(file, filename: str) -> tuple[str, str]:
    """
    Extracts raw text from the document LOCALLY without any API calls.
    Returns (extracted_text, mime_type).
    """
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = "application/pdf" if filename.endswith(".pdf") else "text/plain"

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
    elif mime_type.startswith("text/"):
        try:
            raw = file.read()
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace"), mime_type
            return raw, mime_type
        except Exception:
            file.seek(0)
            return "", mime_type

    # --- Images (PNG, JPG): No local text extraction possible ---
    else:
        return "", mime_type


def extract_financial_data(file):
    """
    Hybrid extraction pipeline:
    1. Try local text extraction first (0 API calls)
    2. If text found: send TEXT to Gemini for structured analysis (1 API call)
    3. If no text (scanned/image): send BINARY to Gemini multimodal (1 API call)
    """
    filename = file.name.lower()
    
    # Step 1: Local text extraction (FREE — no API calls)
    local_text, mime_type = _extract_text_locally(file, filename)
    
    system_instruction = (
        "You are an expert Document Intelligence API. "
        "Extract all structured financial data from the document. "
        "Keep 'extraction_reasoning' fields extremely brief (max 1 sentence each). "
        "Classify the document as 'invoice', 'tax_1040', 'insurance_claim', or 'bank_statement'. "
        "For 'records', construct a list of JSON objects representing table rows or logical line items. "
        "For invoices: include description, qty, price, discount, and total. "
        "For tax_1040: include wages, interest, dividends, and total_income. "
        "For insurance_claim: include claim_amount, deductible, and covered_amount. "
        "DO NOT echo back the raw document text. Focus only on structured numerical data."
    )

    try:
        if local_text and len(local_text.strip()) > 50:
            # ========================================
            # PATH A: Text-based document (PDF, TXT, CSV, LOG)
            # Send extracted text to Gemini for structured JSON.
            # max_output_tokens=65536 prevents truncation on huge documents.
            # ========================================
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=f"Analyze this financial document and extract ALL structured data:\n\n{local_text}",
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=FinancialDocument,
                    temperature=0.1,
                    max_output_tokens=65536,
                ),
            )
        else:
            # ========================================
            # PATH B: Scanned PDF or Image (PNG, JPG)
            # Send raw binary for multimodal vision.
            # ========================================
            file.seek(0)
            file_bytes = file.read()
            part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[part],
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=FinancialDocument,
                    temperature=0.1,
                    max_output_tokens=65536,
                ),
            )
        
        parsed = json.loads(response.text)
        doc_type_val = parsed.get("document_type", "unknown")
        
        if doc_type_val not in [e.value for e in DocType]:
            doc_type_val = DocType.UNKNOWN.value

        return {
            "type": doc_type_val,
            "vendor_or_entity_name": parsed.get("vendor_or_entity_name", "N/A"),
            "grand_total": parsed.get("grand_total", 0.0),
            "text": local_text or "Text extracted via multimodal vision.",
            "data": parsed.get("records", []),
            "extraction_reasoning": parsed.get("extraction_reasoning", "")
        }
        
    except Exception as e:
        return {"type": "error", "text": str(e), "data": []}