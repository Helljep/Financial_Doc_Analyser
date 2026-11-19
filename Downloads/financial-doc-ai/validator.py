"""Re-check extracted financial data with plain arithmetic.

The AI reads the document; this module doesn't trust it. Every total, balance and duplicate is
worked out again with Decimal maths, and each row that doesn't add up gets an error message.
"""
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MAX_BOUND = Decimal("999999999.99")
CENT = Decimal("0.01")

# If a line description contains one of these phrases, its total must be `rate` times the
# sum of the other lines. Edit this to match the vendors you deal with.
PERCENT_FEE_RULES = {
    "payment gateway fee": Decimal("0.005"),
    "gateway commission": Decimal("0.005"),
}
FEE_TOLERANCE = Decimal("0.05")
LINE_TOLERANCE = Decimal("0.50")
BALANCE_TOLERANCE = Decimal("0.01")

# A one-line invoice legitimately has price == total, so the "price is over half the
# total" rule only applies when there are at least this many lines.
OUTLIER_MIN_ROWS = 3

DUPLICATE_MESSAGES = {
    "invoice": "Potential Duplicate Billing: exact same description, quantity, and total found elsewhere.",
    "bank_statement": "Potential Duplicate Transaction: same date, description and amount found elsewhere.",
}
DEFAULT_DUPLICATE_MESSAGE = "Potential Duplicate Record: exact same description and amount found elsewhere."
TOO_BIG = "Boundary Exceeded / OCR Parsing Error: value exceeds 999,999,999.99 limit."


def clean_dec(value) -> Decimal:
    """Turn whatever the AI gave us into a Decimal (0 if it isn't a number)."""
    if value is None or isinstance(value, bool):
        return Decimal("0")
    try:
        if isinstance(value, (int, float, Decimal)):
            if isinstance(value, float) and not math.isfinite(value):
                return Decimal("0")
            return Decimal(str(value))              # str() keeps exponents like 1e-05 intact
        text = str(value).strip()
        negative = text.startswith("(") and text.endswith(")")     # (123.45) means -123.45
        digits = re.sub(r"[^\d.-]", "", text)
        if digits in ("", ".", "-"):
            return Decimal("0")
        number = Decimal(digits)
        return -abs(number) if negative else number
    except InvalidOperation:
        return Decimal("0")


def _get(row: dict, *keys, default=None):
    """First value among `keys` that is present and not blank."""
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _optional(row: dict, *keys):
    """Decimal of the first key that is present, or None if the document never stated it."""
    value = _get(row, *keys)
    return None if value is None else clean_dec(value)


def _description(row: dict) -> str:
    return str(_get(row, "description", "item", default="")).lower().strip()


def headline_amount(row: dict) -> Decimal:
    """The main amount of a row, whatever this document type calls it."""
    for key in ("total", "amount", "total_income", "claim_amount", "debit", "credit"):
        value = _optional(row, key)
        if value:
            return value
    return Decimal("0")


def _find_duplicates(rows: list) -> set:
    first_seen, duplicates = {}, set()
    for i, row in enumerate(rows):
        description, amount = _description(row), headline_amount(row)
        if not description or description == "none" or amount == 0:
            continue
        key = (description, clean_dec(_get(row, "qty", "quantity", default=1)), amount, str(row.get("date", "")))
        if key in first_seen:
            duplicates.update((i, first_seen[key]))
        else:
            first_seen[key] = i
    return duplicates


@dataclass
class _Document:
    grand_total: Decimal
    extracted: dict
    issues: list = field(default_factory=list)
    reconciliation: dict | None = None


# One checker per document type. Each returns a list of error messages for every row.

def _check_invoice(rows, doc):
    def fee_rate(row):
        description = _description(row)
        return next((rate for phrase, rate in PERCENT_FEE_RULES.items() if phrase in description), None)

    subtotal = sum((headline_amount(r) for r in rows if fee_rate(r) is None and abs(headline_amount(r)) <= MAX_BOUND),
                   Decimal("0"))
    results = []
    for row in rows:
        problems = []
        qty = _optional(row, "qty", "quantity")
        price = _optional(row, "price", "rate")
        total = _optional(row, "total", "amount")
        discount = _optional(row, "discount") or Decimal("0")

        if (total or 0) > MAX_BOUND or (price or 0) > MAX_BOUND:
            results.append([f"Boundary Exceeded / OCR Parsing Error: values (total={total}, price={price}) exceed 999,999,999.99 limit."])
            continue

        if price is not None and len(rows) >= OUTLIER_MIN_ROWS and doc.grand_total > 0 and price > doc.grand_total / 2:
            problems.append(f"Outlier/Suspicious Proportion: unit_price ({price}) exceeds 50% of the grand_total ({doc.grand_total}).")

        rate = fee_rate(row)
        if rate is not None:
            if total is not None:
                expected_fee = (subtotal * rate).quantize(CENT, rounding=ROUND_HALF_UP)
                if abs(total - expected_fee) > FEE_TOLERANCE:
                    problems.append(f"Critical Vendor Overcharge: fee {total} != {rate * 100:.2f}% of subtotal {subtotal} (expected {expected_fee})")
        elif total is not None and price is not None:
            # Only check the maths when the document actually printed a unit price and a total.
            q = qty if qty is not None else Decimal("1")
            expected = (q * price * (1 - discount / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
            if abs(expected - total) > LINE_TOLERANCE:
                problems.append(f"Math fail: {q}x{price} -{discount}% = {expected}, got {total}")
        results.append(problems)

    # Compare the sum of the lines with the stated total. A gap isn't always an error
    # (tax or shipping may not be listed as a line), so this is shown as information only.
    if doc.grand_total > 0:
        lines_sum = sum((headline_amount(r) for r in rows if abs(headline_amount(r)) <= MAX_BOUND), Decimal("0"))
        gap = lines_sum - doc.grand_total
        doc.reconciliation = {"line_items_sum": float(lines_sum), "grand_total": float(doc.grand_total),
                              "difference": float(gap), "matches": abs(gap) <= LINE_TOLERANCE}
    return results


def _check_tax(rows, doc):
    results = []
    for row in rows:
        problems = []
        wages = clean_dec(row.get("wages"))
        total_income = _optional(row, "total_income")
        if (total_income or 0) > MAX_BOUND or wages > MAX_BOUND:
            problems.append(TOO_BIG)
        elif total_income is not None:
            expected = wages + clean_dec(row.get("interest")) + clean_dec(row.get("dividends"))
            if abs(expected - total_income) > Decimal("1.00"):
                problems.append(f"Tax math fail: wages+interest+dividends={expected}, reported total_income={total_income}")
        results.append(problems)
    return results


def _check_insurance(rows, doc):
    results = []
    for row in rows:
        problems = []
        claim = _optional(row, "claim_amount")
        covered = _optional(row, "covered_amount")
        deductible = clean_dec(row.get("deductible"))

        if (claim or 0) > MAX_BOUND or (covered or 0) > MAX_BOUND:
            problems.append(TOO_BIG)
        else:
            if claim is not None and len(rows) >= OUTLIER_MIN_ROWS and doc.grand_total > 0 and claim > doc.grand_total / 2:
                problems.append(f"Outlier/Suspicious Proportion: claim_amount ({claim}) exceeds 50% of the grand_total ({doc.grand_total}).")
            if claim is not None and covered is not None and abs(claim - deductible - covered) > Decimal("1.00"):
                problems.append(f"Insurance math fail: claim({claim}) - deductible({deductible}) = {claim - deductible}, reported covered={covered}")
        results.append(problems)
    return results


def _check_bank(rows, doc):
    opening = _optional(doc.extracted, "opening_balance")
    # The balance we expect before each row. After a mismatch we can't tell whether the balance
    # or the amount was misread, so we carry both guesses; the next row settles it, and one bad
    # figure doesn't flag every row after it.
    candidates = [opening] if opening is not None else []
    results = []
    for row in rows:
        problems = []
        debit = clean_dec(row.get("debit"))
        credit = clean_dec(row.get("credit"))
        balance = _optional(row, "balance")

        if debit < 0 or credit < 0:
            problems.append("Negative debit/credit amount: debits and credits must be entered as positive numbers.")
        if debit > 0 and credit > 0:
            problems.append("Transaction has both a debit and a credit.")
        if max(debit, credit) > MAX_BOUND:
            problems.append(TOO_BIG)

        expected = [c + credit - debit for c in candidates]
        if balance is None:
            candidates = expected
        elif expected and all(abs(e - balance) > BALANCE_TOLERANCE for e in expected):
            problems.append(f"Running balance mismatch: {candidates[0]} + credit {credit} - debit {debit} = {expected[0]}, statement shows {balance}")
            candidates = [balance, expected[0]]
        else:
            candidates = [balance]
        results.append(problems)

    closing = _optional(doc.extracted, "closing_balance")
    if closing is not None and candidates and all(abs(closing - c) > BALANCE_TOLERANCE for c in candidates):
        doc.issues.append(f"Closing balance mismatch: transactions lead to {candidates[0]}, statement closing balance is {closing}.")
    return results


def _check_other(rows, doc):
    return [[] for _ in rows]       # unknown types only get the duplicate check


CHECKERS = {
    "invoice": _check_invoice,
    "tax_1040": _check_tax,
    "insurance_claim": _check_insurance,
    "bank_statement": _check_bank,
}


def validate_line_items(extracted_data: dict, evidence=None) -> dict:
    """Check every row and split them into valid records and anomalies.

    `evidence` is an optional function that takes a row and returns a message when the row's
    values can't be found in the source document (see source_index.SourceIndex.problem).
    """
    doc_type = extracted_data.get("type", "unknown")
    raw_rows = extracted_data.get("data", []) or []
    vendor = str(extracted_data.get("vendor_or_entity_name") or "N/A")
    doc = _Document(grand_total=clean_dec(extracted_data.get("grand_total", 0.0)), extracted=extracted_data)

    # A row that isn't a dict (say, a header row passed as a list) can't be checked.
    rows = [r for r in raw_rows if isinstance(r, dict)]
    if len(rows) != len(raw_rows):
        doc.issues.append(f"Malformed Data: {len(raw_rows) - len(rows)} record(s) were not key/value rows and were ignored.")

    for warning in extracted_data.get("extraction_warnings", []) or []:
        doc.issues.append(f"Incomplete Extraction: {warning}")

    if doc.grand_total > MAX_BOUND:
        doc.issues.append(f"Boundary Exceeded / OCR Parsing Error: grand_total {doc.grand_total} exceeds 999,999,999.99 limit.")
        doc.grand_total = Decimal("0")

    if not rows:
        doc.issues.append("No records were extracted from this document.")
        return {"doc_type": doc_type, "status": "FAIL", "valid_records": [], "anomalies": [],
                "document_issues": doc.issues, "reconciliation": None, "accuracy_score": 0.0}

    if vendor.strip().lower() in ("", "n/a", "none", "null", "missing"):
        doc.issues.append("Missing Context Anomaly: vendor_or_entity_name is 'N/A' or missing. Anonymous financial documents represent a critical compliance risk.")

    per_row = CHECKERS.get(doc_type, _check_other)(rows, doc)
    duplicates = _find_duplicates(rows)
    duplicate_message = DUPLICATE_MESSAGES.get(doc_type, DEFAULT_DUPLICATE_MESSAGE)

    valid_records, anomalies = [], []
    for i, source_row in enumerate(rows):
        problems = list(per_row[i])
        if i in duplicates:
            problems.insert(0, duplicate_message)
        if evidence is not None:
            why = evidence(source_row)
            if why:
                problems.append(why)
        row = dict(source_row)
        if problems:
            row["errors"] = problems
            anomalies.append(row)
        else:
            valid_records.append(row)

    checked = len(valid_records) + len(anomalies)
    return {
        "doc_type": doc_type,
        "status": "FAIL" if anomalies or doc.issues else "PASS",
        "valid_records": valid_records,
        "anomalies": anomalies,
        "document_issues": doc.issues,
        "reconciliation": doc.reconciliation,
        # Share of rows that passed the checks. This says nothing about how well the AI read the document.
        "accuracy_score": len(valid_records) / checked if checked else 0.0,
    }
