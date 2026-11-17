from decimal import Decimal

from validator import clean_dec, validate_line_items


def run(doc_type, rows, **extra):
    return validate_line_items({"type": doc_type, "vendor_or_entity_name": "Acme Ltd", "grand_total": 0, "data": rows, **extra})


def errors(report):
    return [e for a in report["anomalies"] for e in a["errors"]]


# ── clean_dec ────────────────────────────────────────────────────────────────
def test_clean_dec_handles_formats():
    assert clean_dec("$1,234.50") == Decimal("1234.50")
    assert clean_dec("(100.00)") == Decimal("-100.00")
    assert clean_dec(None) == 0
    assert clean_dec("abc") == 0
    assert clean_dec(float("nan")) == 0


def test_clean_dec_keeps_exponent_floats():
    assert clean_dec(1e-05) == Decimal("0.00001")      # used to be mangled to 1-05
    assert clean_dec(1.5e10) == Decimal("15000000000")


# ── invoices ─────────────────────────────────────────────────────────────────
def test_invoice_clean_passes():
    r = run("invoice", [{"description": "Widget", "qty": 2, "price": 10, "total": 20},
                        {"description": "Gadget", "qty": 1, "price": 5, "discount": 10, "total": 4.5}])
    assert r["status"] == "PASS" and r["accuracy_score"] == 1.0 and not r["anomalies"]


def test_invoice_math_fail_flagged():
    r = run("invoice", [{"description": "Widget", "qty": 2, "price": 10, "total": 25}])
    assert any("Math fail" in e for e in errors(r)) and r["status"] == "FAIL"


def test_invoice_line_with_only_a_total_is_not_a_false_math_fail():
    # The schema returns qty/price as null for lines like "Shipping"; these used to be flagged (expected 0).
    r = run("invoice", [{"description": "Shipping", "total": 12.5}])
    assert r["status"] == "PASS"


def test_invoice_duplicates_flagged():
    row = {"description": "Widget", "qty": 1, "price": 10, "total": 10}
    r = run("invoice", [dict(row), dict(row)])
    assert sum("Duplicate" in e for e in errors(r)) == 2


def test_gateway_fee_rule_and_overcharge():
    items = [{"description": "Consulting", "qty": 1, "price": 1000, "total": 1000}]
    ok = run("invoice", items + [{"description": "Payment Gateway Fee", "total": 5.0}])
    bad = run("invoice", items + [{"description": "Payment Gateway Fee", "total": 50.0}])
    assert ok["status"] == "PASS"
    assert any("Overcharge" in e for e in errors(bad))


def test_single_item_invoice_not_flagged_as_outlier():
    r = run("invoice", [{"description": "Retainer", "qty": 1, "price": 900, "total": 900}], grand_total=900)
    assert r["status"] == "PASS"


def test_outlier_flagged_with_several_lines():
    rows = [{"description": f"Item {i}", "qty": 1, "price": p, "total": p} for i, p in enumerate([900, 50, 50])]
    r = run("invoice", rows, grand_total=1000)
    assert any("Outlier" in e for e in errors(r))


def test_reconciliation_against_grand_total():
    rows = [{"description": "A", "qty": 1, "price": 10, "total": 10}, {"description": "B", "qty": 1, "price": 20, "total": 20}]
    assert run("invoice", rows, grand_total=30)["reconciliation"]["matches"] is True
    off = run("invoice", rows, grand_total=45)["reconciliation"]
    assert off["matches"] is False and off["difference"] == -15


# ── document-level issues ───────────────────────────────────────────────────
def test_missing_vendor_is_a_document_issue_not_a_fake_row():
    r = validate_line_items({"type": "invoice", "vendor_or_entity_name": "N/A", "grand_total": 10,
                             "data": [{"description": "A", "qty": 1, "price": 10, "total": 10}]})
    assert r["status"] == "FAIL"
    assert not r["anomalies"] and r["accuracy_score"] == 1.0          # row stats are not polluted
    assert any("Missing Context" in i for i in r["document_issues"])


def test_no_records_fails():
    r = run("invoice", [])
    assert r["status"] == "FAIL" and r["accuracy_score"] == 0.0


def test_list_style_rows_do_not_crash():
    r = run("invoice", [["description", "total"], ["Widget", 5]])      # the old (broken) edit format
    assert r["status"] == "FAIL" and any("Malformed" in i for i in r["document_issues"])


def test_boundary_exceeded():
    r = run("invoice", [{"description": "Huge", "qty": 1, "price": 5e12, "total": 5e12}])
    assert any("Boundary" in e for e in errors(r))


# ── tax / insurance ─────────────────────────────────────────────────────────
def test_tax_math():
    assert run("tax_1040", [{"wages": 1000, "interest": 50, "dividends": 25, "total_income": 1075}])["status"] == "PASS"
    bad = run("tax_1040", [{"wages": 1000, "interest": 50, "dividends": 25, "total_income": 2000}])
    assert any("Tax math fail" in e for e in errors(bad))


def test_insurance_math():
    assert run("insurance_claim", [{"claim_amount": 1000, "deductible": 100, "covered_amount": 900}])["status"] == "PASS"
    bad = run("insurance_claim", [{"claim_amount": 1000, "deductible": 100, "covered_amount": 500}])
    assert any("Insurance math fail" in e for e in errors(bad))


def test_tax_duplicates_now_detected():
    row = {"description": "W-2 Acme", "wages": 500, "total_income": 500}
    r = run("tax_1040", [dict(row), dict(row)])
    assert sum("Duplicate" in e for e in errors(r)) == 2


# ── bank statements ─────────────────────────────────────────────────────────
STATEMENT = [
    {"date": "01-01", "description": "Salary", "credit": 1000, "balance": 1500},
    {"date": "01-02", "description": "Rent", "debit": 400, "balance": 1100},
    {"date": "01-03", "description": "Coffee", "debit": 5, "balance": 1095},
]


def test_bank_statement_balances_pass():
    r = run("bank_statement", [dict(x) for x in STATEMENT], opening_balance=500, closing_balance=1095)
    assert r["status"] == "PASS"


def test_bank_statement_running_balance_error_does_not_cascade():
    rows = [dict(x) for x in STATEMENT]
    rows[1]["balance"] = 1000                      # wrong: should be 1100
    r = run("bank_statement", rows, opening_balance=500)
    assert len(r["anomalies"]) == 1 and "Running balance mismatch" in r["anomalies"][0]["errors"][0]


def test_bank_statement_closing_balance_mismatch():
    r = run("bank_statement", [dict(x) for x in STATEMENT], opening_balance=500, closing_balance=9999)
    assert any("Closing balance mismatch" in i for i in r["document_issues"])


def test_bank_statement_negative_amount_flagged():
    r = run("bank_statement", [{"description": "X", "debit": -5}])
    assert any("Negative" in e for e in errors(r))


# ── unknown ─────────────────────────────────────────────────────────────────
def test_unknown_type_only_checks_duplicates():
    row = {"description": "Log line", "total": 3}
    r = run("unknown", [dict(row), dict(row), {"description": "Other", "total": 1}])
    assert len(r["anomalies"]) == 2 and len(r["valid_records"]) == 1
