"""Generate a ~2000-page bank statement (PDF + TXT) with KNOWN planted errors, for stress-testing.

    python make_huge_sample.py            # 60,000 transactions -> sample_docs/huge_bank_statement.{pdf,txt}
    python make_huge_sample.py 6000       # smaller, ~200 pages

Nothing here is real data. Fixed seed, so the same errors land in the same places every run.
"""
import datetime as dt
import os
import random
import sys

random.seed(2026)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 60_000
ROWS_PER_PAGE = 30
os.makedirs("sample_docs", exist_ok=True)

NAMES = ["AMAZON MKTPLACE", "UBER *TRIP", "Swiggy Order", "STARBUCKS #4412", "NETFLIX.COM", "Shell Fuel Stn", "BigBasket",
         "Zomato", "ATM WDL CHENNAI", "Electricity Board", "Airtel Postpaid", "Apollo Pharmacy", "UPI/Rahul S", "IKEA",
         "Myntra", "Interest Credit", "SALARY ACME CORP", "Rent - Sunrise Apts", "Reliance Fresh", "IRCTC E-Ticket"]
CREDITS = {"UPI/Rahul S", "Interest Credit", "SALARY ACME CORP"}

# 1. The true ledger: [date, name, amount, kind] --------------------------------------------------
start = dt.date(2024, 1, 1)
ledger = []
for i in range(N):
    name = random.choice(NAMES)
    kind = "Cr" if name in CREDITS else "Dr"
    amount = round(random.uniform(100, 1500) if kind == "Cr" else random.uniform(15, 250), 2)   # keeps the balance positive
    ledger.append([start + dt.timedelta(days=i * 730 // N), name, amount, kind])

# 2. Double-posted transactions: the same posting appears twice in a row (so balances stay consistent)
planted = []
base = sorted(int(N * f) for f in (0.23, 0.52, 0.78))
for pos in reversed(base):                                    # high index first so earlier positions don't shift
    ledger.insert(pos + 1, list(ledger[pos]))
# final 0-based index of each copy: +1 for its own insert, +k for the k inserts before it
dup_positions = [pos + 1 + k for k, pos in enumerate(base)]

# 3. True running balances, then misprint a few on the page ------------------------------------------
opening = 25_000.00
bal = opening
printed = []
for d, name, amount, kind in ledger:
    bal = round(bal + amount if kind == "Cr" else bal - amount, 2)
    printed.append(bal)
closing_true = bal

misprint_positions = [int(len(ledger) * f) for f in (0.12, 0.37, 0.62, 0.86, 0.97)]
for pos, delta in zip(misprint_positions, (100.0, 250.0, -75.5, 1200.0, 9.99)):
    printed[pos] = round(printed[pos] + delta, 2)
closing_printed = round(closing_true + 4321.00, 2)

def page_of(idx):
    return idx // ROWS_PER_PAGE + 1

for pos in misprint_positions:
    planted.append(f"wrong printed balance on transaction #{pos + 1} (page {page_of(pos)}, {ledger[pos][1]})")
for pos in dup_positions:
    planted.append(f"duplicate posting: transaction #{pos + 1} repeats #{pos} (page {page_of(pos)}, {ledger[pos][1]} {ledger[pos][2]:,.2f})")
planted.append(f"printed closing balance {closing_printed:,.2f} should be {closing_true:,.2f}")

# 4. Lay out pages ------------------------------------------------------------------------------------
pages = -(-len(ledger) // ROWS_PER_PAGE)
lines_by_page = []
for p in range(pages):
    lines = ["SUNRISE BANK LTD   STATEMENT OF ACCOUNT   Acct XXXX-XXXX-4821   Holder: J. PAWARE"]
    if p == 0:
        lines.append(f"Opening Balance: Rs. {opening:,.2f}   Period: 01/01/2024 to 31/12/2025")
    lines.append("DATE        NARRATION                     AMOUNT         BALANCE")
    for idx in range(p * ROWS_PER_PAGE, min((p + 1) * ROWS_PER_PAGE, len(ledger))):
        d, name, amount, kind = ledger[idx]
        lines.append(f"{d:%d/%m/%Y}  {name:<28} {amount:>10,.2f} {kind}  {printed[idx]:>12,.2f}")
    if p == pages - 1:
        lines.append(f"Closing Balance: Rs. {closing_printed:,.2f}")
    lines.append(f"-- Page {p + 1} of {pages} -- computer generated statement, errors & omissions excepted --")
    lines_by_page.append(lines)

with open("sample_docs/huge_bank_statement.txt", "w", encoding="utf-8") as f:
    f.write("\n\n".join("\n".join(l) for l in lines_by_page))

try:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    pdf.set_font("courier", size=7)
    for lines in lines_by_page:
        pdf.add_page()
        for line in lines:
            pdf.cell(0, 4.2, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output("sample_docs/huge_bank_statement.pdf")
    pdf_note = f"{pages}-page PDF"
except ImportError:
    pdf_note = "fpdf2 not installed, PDF skipped"

print(f"Wrote sample_docs/huge_bank_statement.txt and .pdf  [{len(ledger):,} transactions, {pdf_note}]")
print("Planted problems (the app should find all of these):")
for line in planted:
    print("  -", line)
