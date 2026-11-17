"""Generate large, messy sample documents with KNOWN planted errors for testing the pipeline.

    python make_sample_docs.py        # writes into ./sample_docs/
"""
import os
import random

random.seed(7)
os.makedirs("sample_docs", exist_ok=True)

# ── 1. Messy bank statement: ~150 transactions, OCR-style noise, 3 planted problems ──────────
merchants = ["AMAZON MKTPLACE", "UBER *TRIP", "Swiggy Order", "STARBUCKS #4412", "NETFLIX.COM", "Shell Fuel Stn",
             "BigBasket", "Zomato", "ATM WDL CHENNAI", "Electricity Board", "Airtel Postpaid", "Apollo Pharmacy",
             "SALARY ACME CORP", "UPI/Rahul S", "Interest Credit", "Rent - Sunrise Apts", "IKEA", "Myntra"]
fmts = ["{d:02d}/{m:02d}/2026", "{d:02d}-{m:02d}-26", "{d} {mon} 2026"]
mons = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

balance = opening = 5000.00
lines = ["SUNRISE BANK LTD  --  STATEMENT OF ACCOUNT", "Acct No: XXXX-XXXX-4821    Holder: J. PAWARE",
         "Period: 01/01/2026 to 30/06/2026", f"Opening Balance:   Rs. {opening:,.2f}", "",
         "DATE          NARRATION                         DEBIT        CREDIT       BALANCE",
         "-" * 80]
txns = []
for n in range(150):
    m = 1 + n * 6 // 150
    d = random.randint(1, 28)
    name = random.choice(merchants)
    if name in ("SALARY ACME CORP", "Interest Credit", "UPI/Rahul S") and random.random() < 0.8:
        debit, credit = 0.0, round(random.uniform(50, 4000), 2) if name != "SALARY ACME CORP" else 3500.00
    else:
        debit, credit = round(random.uniform(40, 900), 2), 0.0
    balance = round(balance - debit + credit, 2)
    txns.append([d, m, name, debit, credit, balance])

# planted problems
txns[40][5] = round(txns[40][5] + 250.00, 2)            # (A) wrong running balance on txn 41
txns[90] = list(txns[89])                               # (B) exact duplicate of txn 90 (same date/amount/balance fields)
txns[90][5] = round(txns[89][5] - txns[89][3] + txns[89][4], 2)
for i in range(91, 150):                                # keep later balances consistent after the duplicate
    prev = txns[i - 1][5]
    txns[i][5] = round(prev - txns[i][3] + txns[i][4], 2)

for d, m, name, debit, credit, bal in txns:
    date = random.choice(fmts).format(d=d, m=m, mon=mons[m])
    deb = f"{debit:>10,.2f}" if debit else " " * 10
    cre = f"{credit:>10,.2f}" if credit else " " * 10
    noise = random.choice(["", "", "", "  ref#" + str(random.randint(10**6, 10**7)), " *"])
    lines.append(f"{date:<13} {name:<30}{deb}  {cre}  {bal:>11,.2f}{noise}")
closing = txns[-1][5]
lines += ["-" * 80, f"Closing Balance:   Rs. {closing + 1000:,.2f}      <-- (C) wrong on purpose (+1000)", "",
          "This is a computer generated statement. Page 1 of 4 .. cont'd .. Page 2 of 4",
          "Disclaimer: errors & omissions excepted"]
open("sample_docs/messy_bank_statement.txt", "w", encoding="utf-8").write("\n".join(lines))

# ── 2. Messy invoice: 28 lines, mixed formats, 4 planted problems ───────────────────────────
items = [("Cloud hosting - prod cluster", 3, 1200.00, 0), ("Premium support (annual)", 1, 5400.00, 10),
         ("Data egress overage", 12, 85.50, 0), ("SSL certificates", 4, 149.00, 0),
         ("Consulting hrs - migration", 40, 95.00, 5), ("Load balancer", 2, 310.00, 0),
         ("Backup storage 5TB", 5, 210.00, 0), ("Monitoring seats", 25, 18.00, 0), ("Training workshop", 2, 750.00, 0),
         ("Dedicated IP", 6, 12.00, 0), ("Log retention add-on", 3, 99.00, 0), ("Security audit", 1, 2800.00, 0)]
rows, subtotal = [], 0.0
for k in range(28):
    desc, q, p, disc = items[k % len(items)]
    if k >= len(items):
        desc += f" (phase {k // len(items) + 1})"
        q += k % 3
    total = round(q * p * (1 - disc / 100), 2)
    rows.append([desc, q, p, disc, total])
rows[5][4] = round(rows[5][4] + 180.00, 2)             # (A) math error on line 6
rows[19] = list(rows[11])                              # (B) duplicate billing: line 20 repeats line 12
subtotal = sum(r[4] for r in rows)
fee_ok = round(subtotal * 0.005, 2)
fee_bad = round(fee_ok + 140.00, 2)                    # (C) gateway fee overcharged by 140

out = ["", "   NORTHWIND CLOUD  SERVICES   Pvt  Ltd.", "   GSTIN: 33AABCN1234F1Z5      INVOICE  #  INV-2026-0098",
       "   Bill to: Sunrise  Retail  Co.        Date: 3rd Nov 2026       Terms: Net 30", "",
       "  #   Description                              Qty     Rate      Disc%      Amount"]
for i, (desc, q, p, disc, total) in enumerate(rows, 1):
    out.append(f" {i:>2}.  {desc:<40}{q:>4}  {p:>9,.2f}   {disc:>3}%   {total:>11,.2f}")
out.append(f" {len(rows) + 1:>2}.  {'Payment Gateway Fee':<40}{'':>4}  {'':>9}   {'':>4}   {fee_bad:>11,.2f}")
out += ["", f"                                         Subtotal :  {subtotal:,.2f}",
        f"                                         GRAND TOTAL :  {subtotal + fee_bad:,.2f}", "",
        "Notes: pls remit within terms.  Late fee 1.5%/mo.  Bank: Sunrise Bank  IFSC SUNR0001234",
        "Scanned copy - some figures may be unclear.  (C) gateway fee should be 0.5% of subtotal = " + f"{fee_ok:,.2f}"]
open("sample_docs/messy_invoice.txt", "w", encoding="utf-8").write("\n".join(out))

print("Wrote sample_docs/messy_bank_statement.txt and sample_docs/messy_invoice.txt")
print(f"Bank: {len(txns)} transactions. Planted: (A) wrong balance on txn 41, (B) duplicate txn 91, (C) closing balance +1000.")
print(f"Invoice: {len(rows) + 1} lines. Planted: (A) line 6 math error, (B) line 20 duplicates line 12, (C) gateway fee overcharged ({fee_bad} vs {fee_ok}).")
