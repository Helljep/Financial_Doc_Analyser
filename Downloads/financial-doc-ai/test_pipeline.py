"""Offline end-to-end test: Gemini is faked, everything else (validator, PDF, Tableau extract, audit) is real."""
import io
import json

import pytest

import ocr
from audit import generate_audit_hash, verify_ledger
from hyper_export import create_hyper_extract
from report import create_pdf_report
from validator import validate_line_items


class FakeUpload(io.BytesIO):
    def __init__(self, name, content: bytes):
        super().__init__(content)
        self.name = name


class FakeResponse:
    def __init__(self, payload):
        self.text = json.dumps(payload)


GEMINI_INVOICE = {
    "extraction_reasoning": "Looks like an invoice.",
    "vendor_or_entity_name": "Vendor A",
    "grand_total": 100.0,
    "document_type": "invoice",
    "records": [{"extraction_reasoning": "row 1", "description": "Widget", "qty": 1.0, "price": 100.0, "total": 100.0,
                 "discount": None, "wages": None, "debit": None}],
}


@pytest.fixture
def fake_gemini(monkeypatch):
    calls = []

    def fake(contents, config, attempts=3):
        calls.append(contents)
        return FakeResponse(GEMINI_INVOICE)

    monkeypatch.setattr(ocr, "_call_gemini", fake)
    return calls


def test_extraction_cleans_nulls_and_validates(fake_gemini):
    text = b"Invoice from Vendor A. Item: Widget, Qty: 1, Price: $100.00. Total: $100.00 (padding so it exceeds 50 chars)"
    extracted = ocr.extract_financial_data(FakeUpload("inv.txt", text))
    assert extracted["type"] == "invoice" and len(fake_gemini) == 1
    assert "discount" not in extracted["data"][0]                      # null fields dropped
    assert validate_line_items(extracted)["status"] == "PASS"


def test_legacy_doc_rejected_without_api_call(fake_gemini):
    out = ocr.extract_financial_data(FakeUpload("old.doc", b"x"))
    assert out["type"] == "error" and not fake_gemini


def test_missing_api_key_is_a_clear_error(monkeypatch):
    import config
    monkeypatch.setattr(config, "_client", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
    out = ocr.extract_financial_data(FakeUpload("inv.txt", b"A" * 80))
    assert out["type"] == "error" and "GEMINI_API_KEY" in out["error"]


def test_exports_and_audit(tmp_path, monkeypatch, fake_gemini):
    monkeypatch.chdir(tmp_path)                                        # exports write into ./uploads
    extracted = ocr.extract_financial_data(FakeUpload("inv.txt", b"x" * 80))
    report = validate_line_items(extracted)
    report["document_issues"].append("Unicode check: ’ ₹ —")   # would crash fpdf's latin-1 fonts unsanitised

    insights = json.dumps({"executive_summary": "It’s fine — totally.", "trend_analysis": "n/a", "risk_factors": ["₹ risk"],
                           "recommended_actions": ["none"], "chart_title": "T", "chart_labels": ["a", "b"], "chart_values": [3.0, 2.0]})
    pdf = create_pdf_report(report, insights, output_filename="t.pdf")
    assert (tmp_path / pdf).stat().st_size > 1000

    hyper = create_hyper_extract(report, output_filename="t.hyper")
    # Where the Hyper engine can run we get a file; where it can't (library missing, or hyperd.exe blocked
    # by Application Control) we must get None rather than a crash.
    assert hyper is None or (tmp_path / hyper).exists()

    ledger = str(tmp_path / "ledger.jsonl")
    generate_audit_hash(report, ledger)
    assert verify_ledger(ledger)[0]
