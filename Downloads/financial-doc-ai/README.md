# Financial Doc Analyser

AI reads messy financial documents (invoices, bank statements, tax forms, insurance claims); plain Python then re-checks the math and flags errors. Handles documents of any size, up to thousands of pages.

**Outputs:** validated data you can correct in the app, a PDF report with AI insights, a Tableau extract, and a tamper-evident audit log.

## Run

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your-key        # PowerShell: $env:GEMINI_API_KEY = "your-key"
python -m streamlit run app.py
```

## Test

```bash
python -m pytest
```

`python make_huge_sample.py` generates a 2,000-page test statement with known planted errors.
