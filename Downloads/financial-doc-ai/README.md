# Financial Doc Analyser

Upload a messy financial document (invoice, bank statement, tax form, insurance claim) and the app does four things:

1. Gemini reads it and pulls out the rows.
2. Plain Python re-checks the arithmetic, so a wrong total doesn't depend on the AI noticing.
3. You review the flagged rows next to the original page and correct anything that was misread.
4. It exports a PDF report, a Tableau extract and an audit entry.

Large documents are split into parts, read in parallel and merged, so a statement with thousands of pages works the same way as a one-page invoice.

## Run it

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your-key        # PowerShell: $env:GEMINI_API_KEY = "your-key"
python -m streamlit run app.py
```

Run the tests with `python -m pytest`. `python make_huge_sample.py` builds a 2,000-page statement with known errors planted in it, which is handy for stress testing.

## How it fits together

| File | What it does |
| --- | --- |
| `app.py`, `style.css` | The Streamlit page: Overview, Review data, Exports |
| `ocr.py` | Reads the file, splits big ones, calls Gemini, merges the parts |
| `validator.py` | The arithmetic checks, one set per document type |
| `source_index.py` | Finds each row in the original PDF and draws the highlight |
| `llm.py` | Asks Gemini for the summary and chart data |
| `report.py`, `hyper_export.py` | PDF report and Tableau extract |
| `audit.py` | Hash-chained log of what was validated |

## What the checks cover

- Invoices: quantity times price, line totals, tax and fee rates, outliers, duplicates, and line items against the stated total.
- Bank statements: the running balance row by row, then opening plus movements against the closing balance.
- Tax forms and insurance claims: the sums and limits that belong to each.
- Every row is also looked up in the source text. A value that isn't in the document at all is flagged, which catches numbers the AI made up.

## Limits

- It can't promise a perfect read of every document. Gemini can misread a figure, and a wrong figure that still adds up passes the arithmetic. That is why flagged rows show the source page, and why you can edit anything.
- "Checks passed" is the share of rows that passed the arithmetic. It is not a measure of how accurately the document was read.
- Scanned PDFs and photos have no text to search, so they get no source view and no source check.
- The free Gemini tier has a daily request limit. A 2,000-page document needs roughly 90 calls, so retries and waits are built in, but a very large file can still run into the limit.
- The audit log shows tampering, it doesn't prevent it. Someone who can rewrite the file can rewrite the whole chain.
- The Tableau extract needs the Hyper library to start. On some locked-down machines it can't, and the app says so instead of failing.
- Not tested yet on a real 2,000-page document, only on the generated one.

## Roadmap

- A confidence score per row.
- Reconciling invoices against the bank statement that paid them.
- An option to run a local model instead of Gemini.
- A separate schema for each document type.
- Gemini bounding boxes, so scans can get a source view too.

## Keys

The API key is read from the environment and never stored in the code. If a key was ever committed, revoke it and make a new one.
