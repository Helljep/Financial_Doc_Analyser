"""Large-document handling. Gemini is faked by a tiny parser, so these run offline and fast."""
import io
import json
import re

import pytest

import llm
import ocr
from validator import validate_line_items

ROW = re.compile(r"^(\d+)\s+(\S+)\s+([\d.]+)\s+(Dr|Cr)\s+([\d.]+)$")


class FakeUpload(io.BytesIO):
    def __init__(self, name, content: bytes):
        super().__init__(content)
        self.name = name


class Resp:
    def __init__(self, payload):
        self.text = json.dumps(payload)


def parse_chunk(text):
    """Stand-in for Gemini: turn 'n name amount Dr|Cr balance' lines into bank-statement records."""
    records = []
    for line in text.splitlines():
        m = ROW.match(line.strip())
        if m:
            _, name, amt, kind, bal = m.groups()
            records.append({"extraction_reasoning": "x", "description": name, "date": m.group(1),
                            "debit" if kind == "Dr" else "credit": float(amt), "balance": float(bal)})
    return {"extraction_reasoning": "bank", "vendor_or_entity_name": "Bank", "grand_total": None, "document_type": "bank_statement",
            "opening_balance": 1000.0 if "OPENING" in text else None,
            "closing_balance": float(m.group(1)) if (m := re.search(r"CLOSING BALANCE ([\d.]+)", text)) else None, "records": records}


def make_statement(n):
    bal, lines = 1000.0, ["OPENING BALANCE 1000.00", "No Name Amount Type Balance"]
    for i in range(1, n + 1):
        bal += 1.0
        lines.append(f"{i} shop{i} 1.00 Cr {bal:.2f}")
    lines.append(f"CLOSING BALANCE {bal:.2f}")
    return "\n".join(lines).encode()


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(ocr, "CHUNK_CHARS", 400)
    monkeypatch.setattr(ocr, "split_text", lambda t, m=400, _orig=ocr.split_text: _orig(t, m))


def test_split_text_keeps_lines_whole():
    text = "\n".join(f"row {i} " + "x" * 30 for i in range(200))
    chunks = ocr.split_text(text, 500)
    assert len(chunks) > 5 and "".join(chunks) == text + ("" if text.endswith("\n") else "")
    assert all(len(c) <= 500 for c in chunks)
    assert all(not c.rstrip("\n").endswith(("row", "x" * 31)) for c in chunks)     # no line cut mid-way


def test_split_text_hard_splits_a_giant_line():
    assert all(len(c) <= 100 for c in ocr.split_text("y" * 1000, 100))


def test_chunked_extraction_merges_everything(monkeypatch, small_chunks):
    seen = []

    def fake(contents, config, attempts=3, usage=None):
        seen.append(contents)
        return Resp(parse_chunk(contents))

    monkeypatch.setattr(ocr, "_call_gemini", fake)
    progress = []
    out = ocr.extract_financial_data(FakeUpload("stmt.txt", make_statement(120)), on_progress=lambda d, t: progress.append((d, t)))

    assert out["type"] == "bank_statement" and out["chunks"] > 3 and len(seen) == out["chunks"]
    assert len(out["data"]) == 120                                   # nothing lost or duplicated across chunks
    assert out["opening_balance"] == 1000.0 and out["closing_balance"] == 1120.0
    assert progress[-1] == (out["chunks"], out["chunks"])
    assert not out["extraction_warnings"]
    report = validate_line_items(out)
    assert report["status"] == "PASS" and len(report["valid_records"]) == 120


def test_row_order_is_preserved(monkeypatch, small_chunks):
    monkeypatch.setattr(ocr, "_call_gemini", lambda c, cfg, attempts=3, usage=None: Resp(parse_chunk(c)))
    out = ocr.extract_financial_data(FakeUpload("stmt.txt", make_statement(60)))
    assert [r["date"] for r in out["data"]] == [str(i) for i in range(1, 61)]


def test_failed_chunk_is_reported_not_silently_dropped(monkeypatch, small_chunks):
    def flaky(contents, config, attempts=3, usage=None):
        if "shop30 " in contents:
            raise RuntimeError("429 quota exceeded")
        return Resp(parse_chunk(contents))

    monkeypatch.setattr(ocr, "_call_gemini", flaky)
    monkeypatch.setattr(ocr, "split_text", lambda t, m=400, _o=ocr.split_text: _o(t, m) if m == 400 else [t])  # no halving fallback
    out = ocr.extract_financial_data(FakeUpload("stmt.txt", make_statement(100)))
    assert len(out["extraction_warnings"]) == 1 and len(out["data"]) < 100
    report = validate_line_items(out)
    assert report["status"] == "FAIL"
    assert any("Incomplete Extraction" in i and "MISSING" in i for i in report["document_issues"])


def test_truncated_answer_is_retried_on_halves(monkeypatch):
    big = make_statement(400)
    calls = {"n": 0}

    def fake(contents, config, attempts=3, usage=None):
        calls["n"] += 1
        if len(contents) > 6000:                                      # pretend the answer was cut off (invalid JSON)
            class Bad:
                text = '{"records": [{"descr'
            return Bad()
        return Resp(parse_chunk(contents))

    monkeypatch.setattr(ocr, "_call_gemini", fake)
    out = ocr.extract_financial_data(FakeUpload("stmt.txt", big))
    assert len(out["data"]) == 400 and not out["extraction_warnings"] and calls["n"] > 1


def test_all_chunks_failing_is_an_error(monkeypatch, small_chunks):
    def boom(contents, config, attempts=3, usage=None):
        raise RuntimeError("down")
    monkeypatch.setattr(ocr, "_call_gemini", boom)
    assert ocr.extract_financial_data(FakeUpload("s.txt", make_statement(50)))["type"] == "error"


def test_merge_picks_sensible_document_level_fields():
    merged = ocr._merge([
        {"document_type": "unknown", "vendor_or_entity_name": "N/A", "grand_total": None, "records": [{"a": 1}], "opening_balance": 5.0},
        {"document_type": "invoice", "vendor_or_entity_name": "Acme", "grand_total": 10.0, "records": [{"a": 2}]},
        {"document_type": "invoice", "vendor_or_entity_name": "Other", "grand_total": 99.0, "records": [], "closing_balance": 7.0},
    ])
    assert merged["document_type"] == "invoice" and merged["vendor_or_entity_name"] == "Acme"
    assert merged["grand_total"] == 99.0 and merged["opening_balance"] == 5.0 and merged["closing_balance"] == 7.0
    assert len(merged["records"]) == 2


# ── report input stays small however big the document is ──────────────────────────────────
def test_report_input_is_compacted():
    rows = [{"description": f"item{i}", "total": float(i), "extraction_reasoning": "long text " * 20} for i in range(5000)]
    anomalies = [dict(r, errors=["Math fail: x", "Potential Duplicate Billing: y"]) for r in rows[:300]]
    report = {"doc_type": "invoice", "status": "FAIL", "valid_records": rows[300:], "anomalies": anomalies,
              "document_issues": [], "reconciliation": None, "accuracy_score": 0.94}
    compact = llm.compact_validation_report(report)
    assert len(json.dumps(compact)) < 40_000 < len(json.dumps(report))
    assert compact["counts"] == {"valid_records": 4700, "anomalies": 300}
    assert compact["anomaly_types"] == {"Math fail": 300, "Potential Duplicate Billing": 300}
    assert compact["largest_records"][0]["total"] == 4999.0


def test_small_reports_are_untouched():
    small = {"valid_records": [{"total": 1}], "anomalies": []}
    assert llm.compact_validation_report(small) is small


def test_huge_text_is_trimmed_but_keeps_both_ends():
    text = "START" + "z" * 500_000 + "END"
    out = llm.compact_text(text)
    assert len(out) < 61_000 and out.startswith("START") and out.endswith("END") and "omitted" in out
