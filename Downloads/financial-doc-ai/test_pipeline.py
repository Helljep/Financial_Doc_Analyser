import os
from ocr import extract_financial_data
from validator import validate_line_items
from llm import generate_report
from report import create_charts, create_pdf_report
from hyper_export import create_hyper_extract
from audit import generate_audit_hash

class DummyFile:
    def __init__(self, name, content):
        self.name = name
        self.content = content
        self.pos = 0
    def read(self):
        return self.content
    def seek(self, pos):
        self.pos = pos

def test_pipeline():
    file = DummyFile("test.txt", b"Invoice from Vendor A\nTotal: $100.00\nItem: Widget, Qty: 1, Price: $100.00")
    print("Extracting...")
    # NOTE: extract_financial_data reads file
    # We will just manually construct extracted_data to bypass the API key since it might fail or cost money
    # Wait, the code in ocr.py has a hardcoded API key: AIza...
    # Let's try to run it. If it fails due to API key, we know the code works until that point.
    try:
        extracted = extract_financial_data(file)
        print("Extracted:", extracted)
        
        # Validating
        validated = validate_line_items(extracted)
        print("Validated:", validated)
        
        # LLM Report
        report = generate_report(validated, "Raw text here")
        print("Report:", report)
        
        # PDF
        pdf = create_pdf_report(validated, report, "test.pdf")
        print("PDF:", pdf)
        
        # Hyper
        hyper = create_hyper_extract(validated, "test.hyper")
        print("Hyper:", hyper)
        
        # Audit
        audit = generate_audit_hash(validated)
        print("Audit:", audit)
        
        print("SUCCESS")
    except Exception as e:
        print("FAILED:", str(e))

if __name__ == "__main__":
    test_pipeline()
