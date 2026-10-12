import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MAX_BOUND = Decimal('999999999.99')

def clean_dec(val) -> Decimal:
    """Robustly converts any value to a Decimal for financial-grade math."""
    try:
        cleaned = re.sub(r'[^\d.-]', '', str(val))
        return Decimal(cleaned) if cleaned and cleaned not in ('.', '-') else Decimal('0')
    except InvalidOperation:
        return Decimal('0')

def validate_line_items(extracted_data: dict) -> dict:
    doc_type = extracted_data.get("type", "unknown")
    records: list = extracted_data.get("data", [])
    vendor_or_entity_name = str(extracted_data.get("vendor_or_entity_name", "N/A"))
    grand_total = clean_dec(extracted_data.get("grand_total", 0.0))

    valid_records = []
    anomalies = []

    if grand_total > MAX_BOUND:
        anomalies.append({
             "description": "Document Total",
             "errors": [f"Boundary Exceeded / OCR Parsing Error: grand_total {grand_total} exceeds 999,999,999.99 limit."]
        })
        grand_total = Decimal('0')

    if not records:
        return {"doc_type": doc_type, "status": "FAIL", "valid_records": [], "anomalies": anomalies, "accuracy_score": 0.0}

    # 1. SMART ANOMALY: Missing Context Rule
    if not vendor_or_entity_name or vendor_or_entity_name.lower() in ["n/a", "none", "null", "missing"]:
        anomalies.append({
             "description": "Document Context",
             "errors": ["Missing Context Anomaly: vendor_or_entity_name is 'N/A' or missing. Anonymous financial documents represent a critical compliance risk."]
        })

    # 2. SMART ANOMALY: Duplicate Detection Rule
    seen_items = {}
    duplicate_indices = set()
    for i, rec in enumerate(records):
        desc = str(rec.get("description", rec.get("item", ""))).lower().strip()
        qty = clean_dec(rec.get("qty", rec.get("quantity", 1)))
        total = clean_dec(rec.get("total", rec.get("amount", 0)))
        
        if desc and desc != "none" and total != Decimal('0'):
            key = (desc, qty, total)
            if key in seen_items:
                duplicate_indices.add(i)
                duplicate_indices.add(seen_items[key])
            else:
                seen_items[key] = i

    # ── INVOICE ──────────────────────────────────────────────────────────────
    if doc_type == "invoice":
        subtotal = Decimal('0')
        for rec in records:
            desc = str(rec.get("description", rec.get("item", ""))).lower()
            is_fee = "payment gateway fee" in desc or "gateway commission" in desc
            if not is_fee:
                item_tot = clean_dec(rec.get("total", rec.get("amount", 0)))
                if item_tot <= MAX_BOUND:
                    subtotal += item_tot

        for i, rec in enumerate(records):
            row = dict(rec)
            desc = str(row.get("description", row.get("item", ""))).lower()
            is_fee = "payment gateway fee" in desc or "gateway commission" in desc
            row_errors = []

            if i in duplicate_indices:
                row_errors.append("Potential Duplicate Billing: exact same description, quantity, and total found elsewhere.")

            qty = clean_dec(row.get("qty", row.get("quantity", 1)))
            price = clean_dec(row.get("price", row.get("rate", 0)))
            total = clean_dec(row.get("total", row.get("amount", 0)))
            discount = clean_dec(row.get("discount", 0))

            if total > MAX_BOUND or price > MAX_BOUND:
                row_errors.append(f"Boundary Exceeded / OCR Parsing Error: Values (total={total}, price={price}) exceed 999,999,999.99 limit.")
                row["errors"] = row_errors
                anomalies.append(row)
                continue

            if grand_total > Decimal('0') and price > (grand_total * Decimal('0.5')):
                row_errors.append(f"Outlier/Suspicious Proportion: unit_price ({price}) exceeds 50% of the grand_total ({grand_total}).")

            if is_fee:
                expected_fee = (subtotal * Decimal('0.005')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if abs(total - expected_fee) > Decimal('0.05'):
                    row_errors.append(f"Critical Vendor Overcharge: fee {total} != 0.5% of subtotal {subtotal} (expected {expected_fee})")
            else:
                expected_total = (qty * price * (1 - discount / 100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if total != Decimal('0') and abs(expected_total - total) > Decimal('0.50'):
                    row_errors.append(f"Math fail: {qty}x{price} -{discount}% = {expected_total}, got {total}")

            if row_errors:
                row["errors"] = row_errors
                anomalies.append(row)
            else:
                valid_records.append(row)

    # ── IRS FORM 1040 ─────────────────────────────────────────────────────────
    elif doc_type == "tax_1040":
        for i, rec in enumerate(records):
            row = dict(rec)
            row_errors = []
            
            if i in duplicate_indices:
                row_errors.append("Potential Duplicate Record: exact same description and total found elsewhere.")

            wages = clean_dec(row.get("wages", 0))
            interest = clean_dec(row.get("interest", 0))
            dividends = clean_dec(row.get("dividends", 0))
            total_income = clean_dec(row.get("total_income", 0))

            if total_income > MAX_BOUND or wages > MAX_BOUND:
                row_errors.append(f"Boundary Exceeded / OCR Parsing Error: Value exceeds 999,999,999.99 limit.")
                row["errors"] = row_errors
                anomalies.append(row)
                continue

            expected_income = wages + interest + dividends
            if total_income != Decimal('0') and abs(expected_income - total_income) > Decimal('1.00'):
                row_errors.append(f"Tax math fail: wages+interest+dividends={expected_income}, reported total_income={total_income}")

            if row_errors:
                row["errors"] = row_errors
                anomalies.append(row)
            else:
                valid_records.append(row)

    # ── INSURANCE CLAIM ───────────────────────────────────────────────────────
    elif doc_type == "insurance_claim":
        for i, rec in enumerate(records):
            row = dict(rec)
            row_errors = []

            claim_amount = clean_dec(row.get("claim_amount", 0))
            deductible = clean_dec(row.get("deductible", 0))
            covered = clean_dec(row.get("covered_amount", 0))

            if claim_amount > MAX_BOUND or covered > MAX_BOUND:
                row_errors.append(f"Boundary Exceeded / OCR Parsing Error: Value exceeds 999,999,999.99 limit.")
                row["errors"] = row_errors
                anomalies.append(row)
                continue

            if grand_total > Decimal('0') and claim_amount > (grand_total * Decimal('0.5')):
                row_errors.append(f"Outlier/Suspicious Proportion: claim_amount ({claim_amount}) exceeds 50% of the grand_total ({grand_total}).")

            if i in duplicate_indices:
                row_errors.append("Potential Duplicate Record: exact same description and total found elsewhere.")

            expected_covered = claim_amount - deductible
            if covered != Decimal('0') and abs(expected_covered - covered) > Decimal('1.00'):
                row_errors.append(f"Insurance math fail: claim({claim_amount}) - deductible({deductible}) = {expected_covered}, reported covered={covered}")

            if row_errors:
                row["errors"] = row_errors
                anomalies.append(row)
            else:
                valid_records.append(row)

    # ── LOGS / UNKNOWN ────────────────────────────────────────────────────────
    else:
        for i, rec in enumerate(records):
            row = dict(rec)
            if i in duplicate_indices:
                row["errors"] = ["Potential Duplicate Record: exact same description, quantity, and total found elsewhere."]
                anomalies.append(row)
            else:
                valid_records.append(row)

    total_checked = len(valid_records) + len(anomalies)
    accuracy = len(valid_records) / total_checked if total_checked > 0 else 0.0

    return {
        "doc_type": doc_type,
        "status": "PASS" if not anomalies else "FAIL",
        "valid_records": valid_records,
        "anomalies": anomalies,
        "accuracy_score": accuracy,
    }