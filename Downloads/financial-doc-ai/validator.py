import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MAX_BOUND = Decimal('999999999.99')
CENT = Decimal('0.01')

# Vendor-specific fee rules: if a line description contains one of these phrases, its total
# must equal `rate` x (sum of the non-fee line totals). Edit or empty this dict to suit your vendors.
PERCENT_FEE_RULES = {
    "payment gateway fee": Decimal('0.005'),
    "gateway commission": Decimal('0.005'),
}
FEE_TOLERANCE = Decimal('0.05')
LINE_MATH_TOLERANCE = Decimal('0.50')
BALANCE_TOLERANCE = Decimal('0.01')
# The "unit price is >50% of the document total" outlier rule only makes sense when there are
# several lines; a one-item invoice legitimately has price == total.
OUTLIER_MIN_RECORDS = 3


def clean_dec(val) -> Decimal:
    """Robustly converts any value to a Decimal for financial-grade math."""
    if val is None or isinstance(val, bool):
        return Decimal('0')
    try:
        if isinstance(val, (int, float, Decimal)):
            if isinstance(val, float) and not math.isfinite(val):
                return Decimal('0')
            return Decimal(str(val))          # keeps exponents like 1e-05 intact
        text = str(val).strip()
        negative = text.startswith('(') and text.endswith(')')   # accounting style: (123.45) = -123.45
        cleaned = re.sub(r'[^\d.-]', '', text)
        if not cleaned or cleaned in ('.', '-'):
            return Decimal('0')
        result = Decimal(cleaned)
        return -abs(result) if negative else result
    except InvalidOperation:
        return Decimal('0')


def _get(rec: dict, *keys, default=None):
    """First value among `keys` that is present and not None/blank."""
    for k in keys:
        v = rec.get(k)
        if v is not None and v != "":
            return v
    return default


def _opt_dec(rec: dict, *keys):
    """Decimal value of the first present key, or None when the document didn't state it."""
    v = _get(rec, *keys)
    return None if v is None else clean_dec(v)


def _desc(rec: dict) -> str:
    return str(_get(rec, "description", "item", default="")).lower().strip()


def _amount(rec: dict) -> Decimal:
    """Headline amount of a record, whatever the document type calls it."""
    for key in ("total", "amount", "total_income", "claim_amount", "debit", "credit"):
        v = _opt_dec(rec, key)
        if v:
            return v
    return Decimal('0')


def _find_duplicates(records: list) -> set:
    seen, dupes = {}, set()
    for i, rec in enumerate(records):
        desc = _desc(rec)
        amount = _amount(rec)
        if desc and desc != "none" and amount != 0:
            key = (desc, clean_dec(_get(rec, "qty", "quantity", default=1)), amount, str(rec.get("date", "")))
            if key in seen:
                dupes.add(i)
                dupes.add(seen[key])
            else:
                seen[key] = i
    return dupes


def _finish(row, row_errors, valid_records, anomalies):
    if row_errors:
        row["errors"] = row_errors
        anomalies.append(row)
    else:
        valid_records.append(row)


def validate_line_items(extracted_data: dict) -> dict:
    doc_type = extracted_data.get("type", "unknown")
    raw_records = extracted_data.get("data", []) or []
    vendor = str(extracted_data.get("vendor_or_entity_name") or "N/A")
    grand_total = clean_dec(extracted_data.get("grand_total", 0.0))

    valid_records, anomalies, document_issues = [], [], []
    reconciliation = None

    # Anything that isn't a dict (e.g. a header row passed as a list) can't be validated.
    records = [r for r in raw_records if isinstance(r, dict)]
    if len(records) != len(raw_records):
        document_issues.append(f"Malformed Data: {len(raw_records) - len(records)} record(s) were not key/value rows and were ignored.")

    for w in extracted_data.get("extraction_warnings", []) or []:
        document_issues.append(f"Incomplete Extraction: {w}")

    if grand_total > MAX_BOUND:
        document_issues.append(f"Boundary Exceeded / OCR Parsing Error: grand_total {grand_total} exceeds 999,999,999.99 limit.")
        grand_total = Decimal('0')

    if not records:
        document_issues.append("No records were extracted from this document.")
        return {"doc_type": doc_type, "status": "FAIL", "valid_records": [], "anomalies": anomalies,
                "document_issues": document_issues, "reconciliation": None, "accuracy_score": 0.0}

    if vendor.strip().lower() in ["", "n/a", "none", "null", "missing"]:
        document_issues.append("Missing Context Anomaly: vendor_or_entity_name is 'N/A' or missing. Anonymous financial documents represent a critical compliance risk.")

    duplicates = _find_duplicates(records)

    # ── INVOICE ──────────────────────────────────────────────────────────────
    if doc_type == "invoice":
        def fee_rate(rec):
            d = _desc(rec)
            return next((rate for phrase, rate in PERCENT_FEE_RULES.items() if phrase in d), None)

        subtotal = sum((_amount(r) for r in records if fee_rate(r) is None and abs(_amount(r)) <= MAX_BOUND), Decimal('0'))

        for i, rec in enumerate(records):
            row = dict(rec)
            row_errors = []
            if i in duplicates:
                row_errors.append("Potential Duplicate Billing: exact same description, quantity, and total found elsewhere.")

            qty = _opt_dec(row, "qty", "quantity")
            price = _opt_dec(row, "price", "rate")
            total = _opt_dec(row, "total", "amount")
            discount = _opt_dec(row, "discount") or Decimal('0')

            if (total is not None and total > MAX_BOUND) or (price is not None and price > MAX_BOUND):
                row_errors.append(f"Boundary Exceeded / OCR Parsing Error: Values (total={total}, price={price}) exceed 999,999,999.99 limit.")
                _finish(row, row_errors, valid_records, anomalies)
                continue

            if price is not None and len(records) >= OUTLIER_MIN_RECORDS and grand_total > 0 and price > grand_total * Decimal('0.5'):
                row_errors.append(f"Outlier/Suspicious Proportion: unit_price ({price}) exceeds 50% of the grand_total ({grand_total}).")

            rate = fee_rate(row)
            if rate is not None:
                if total is not None:
                    expected_fee = (subtotal * rate).quantize(CENT, rounding=ROUND_HALF_UP)
                    if abs(total - expected_fee) > FEE_TOLERANCE:
                        row_errors.append(f"Critical Vendor Overcharge: fee {total} != {rate * 100:.2f}% of subtotal {subtotal} (expected {expected_fee})")
            elif total is not None and price is not None:
                # Only check the math when the document actually stated a unit price and a total.
                q = qty if qty is not None else Decimal('1')
                expected_total = (q * price * (1 - discount / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
                if abs(expected_total - total) > LINE_MATH_TOLERANCE:
                    row_errors.append(f"Math fail: {q}x{price} -{discount}% = {expected_total}, got {total}")

            _finish(row, row_errors, valid_records, anomalies)

        # Informational cross-check of the line items against the stated grand total.
        all_lines = sum((_amount(r) for r in records if abs(_amount(r)) <= MAX_BOUND), Decimal('0'))
        if grand_total > 0:
            diff = all_lines - grand_total
            reconciliation = {"line_items_sum": float(all_lines), "grand_total": float(grand_total),
                              "difference": float(diff), "matches": abs(diff) <= LINE_MATH_TOLERANCE}

    # ── IRS FORM 1040 ─────────────────────────────────────────────────────────
    elif doc_type == "tax_1040":
        for i, rec in enumerate(records):
            row, row_errors = dict(rec), []
            if i in duplicates:
                row_errors.append("Potential Duplicate Record: exact same description and amount found elsewhere.")

            wages = clean_dec(row.get("wages"))
            interest = clean_dec(row.get("interest"))
            dividends = clean_dec(row.get("dividends"))
            total_income = _opt_dec(row, "total_income")

            if (total_income or 0) > MAX_BOUND or wages > MAX_BOUND:
                row_errors.append("Boundary Exceeded / OCR Parsing Error: Value exceeds 999,999,999.99 limit.")
            elif total_income is not None:
                expected_income = wages + interest + dividends
                if abs(expected_income - total_income) > Decimal('1.00'):
                    row_errors.append(f"Tax math fail: wages+interest+dividends={expected_income}, reported total_income={total_income}")
            _finish(row, row_errors, valid_records, anomalies)

    # ── INSURANCE CLAIM ───────────────────────────────────────────────────────
    elif doc_type == "insurance_claim":
        for i, rec in enumerate(records):
            row, row_errors = dict(rec), []
            claim_amount = _opt_dec(row, "claim_amount")
            deductible = clean_dec(row.get("deductible"))
            covered = _opt_dec(row, "covered_amount")

            if (claim_amount or 0) > MAX_BOUND or (covered or 0) > MAX_BOUND:
                row_errors.append("Boundary Exceeded / OCR Parsing Error: Value exceeds 999,999,999.99 limit.")
            else:
                if claim_amount is not None and len(records) >= OUTLIER_MIN_RECORDS and grand_total > 0 and claim_amount > grand_total * Decimal('0.5'):
                    row_errors.append(f"Outlier/Suspicious Proportion: claim_amount ({claim_amount}) exceeds 50% of the grand_total ({grand_total}).")
                if i in duplicates:
                    row_errors.append("Potential Duplicate Record: exact same description and amount found elsewhere.")
                if claim_amount is not None and covered is not None:
                    expected_covered = claim_amount - deductible
                    if abs(expected_covered - covered) > Decimal('1.00'):
                        row_errors.append(f"Insurance math fail: claim({claim_amount}) - deductible({deductible}) = {expected_covered}, reported covered={covered}")
            _finish(row, row_errors, valid_records, anomalies)

    # ── BANK STATEMENT ────────────────────────────────────────────────────────
    elif doc_type == "bank_statement":
        opening = _opt_dec(extracted_data, "opening_balance")
        # Possible "true" balances before the current row. After a mismatch we can't tell whether the
        # balance or the amount was misread, so we keep both candidates; the next row settles it and
        # a single bad value doesn't flag every row after it.
        candidates = [opening] if opening is not None else []
        for i, rec in enumerate(records):
            row, row_errors = dict(rec), []
            debit = clean_dec(row.get("debit"))
            credit = clean_dec(row.get("credit"))
            balance = _opt_dec(row, "balance")

            if i in duplicates:
                row_errors.append("Potential Duplicate Transaction: same date, description and amount found elsewhere.")
            if debit < 0 or credit < 0:
                row_errors.append("Negative debit/credit amount: debits and credits must be entered as positive numbers.")
            if debit > 0 and credit > 0:
                row_errors.append("Transaction has both a debit and a credit.")
            if max(debit, credit) > MAX_BOUND:
                row_errors.append("Boundary Exceeded / OCR Parsing Error: Value exceeds 999,999,999.99 limit.")

            computed = [c + credit - debit for c in candidates]
            if balance is not None:
                if computed and all(abs(e - balance) > BALANCE_TOLERANCE for e in computed):
                    row_errors.append(f"Running balance mismatch: {candidates[0]} + credit {credit} - debit {debit} = {computed[0]}, statement shows {balance}")
                    candidates = [balance] + computed[:1]
                else:
                    candidates = [balance]
            else:
                candidates = computed
            _finish(row, row_errors, valid_records, anomalies)

        closing = _opt_dec(extracted_data, "closing_balance")
        if closing is not None and candidates and all(abs(closing - c) > BALANCE_TOLERANCE for c in candidates):
            document_issues.append(f"Closing balance mismatch: transactions lead to {candidates[0]}, statement closing balance is {closing}.")

    # ── LOGS / UNKNOWN ────────────────────────────────────────────────────────
    else:
        for i, rec in enumerate(records):
            row = dict(rec)
            _finish(row, ["Potential Duplicate Record: exact same description, quantity, and total found elsewhere."] if i in duplicates else [],
                    valid_records, anomalies)

    total_checked = len(valid_records) + len(anomalies)
    pass_rate = len(valid_records) / total_checked if total_checked else 0.0

    return {
        "doc_type": doc_type,
        "status": "PASS" if not anomalies and not document_issues else "FAIL",
        "valid_records": valid_records,
        "anomalies": anomalies,
        "document_issues": document_issues,
        "reconciliation": reconciliation,
        # Share of rows that passed the arithmetic checks. This is NOT an OCR/extraction confidence.
        "accuracy_score": pass_rate,
    }
