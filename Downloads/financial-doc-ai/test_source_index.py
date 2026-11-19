import pymupdf

import ocr
import source_index
from validator import validate_line_items


FILLER = "Statement of account for the period, all amounts in rupees"   # indexes need some text to be usable


def make_pdf(pages):
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for n, line in enumerate([FILLER] + lines):
            page.insert_text((50, 72 + n * 18), line)
    return doc.tobytes()


def test_number_key_treats_formats_alike():
    assert source_index.number_key("1,234.50") == source_index.number_key(1234.5)
    assert source_index.number_key("(1234.50)") == source_index.number_key(1234.5)
    assert source_index.number_key("abc") is None
    assert source_index.number_key(0) is None


def test_row_is_found_on_the_right_page():
    data = make_pdf([["Widget   2   10.00   20.00"], ["Gadget   1   7.25   7.25", "Bolt  4  1.50  6.00"]])
    index = source_index.SourceIndex.from_pdf(data)
    assert index.pages_for({"total": 7.25}) == [2]
    assert index.problem({"total": 7.25, "price": 7.25}) is None


def test_value_missing_from_source_is_reported():
    index = source_index.SourceIndex.from_pdf(make_pdf([["Widget  20.00"]]))
    assert "Not found in the source" in index.problem({"total": 99.99})


def test_amounts_on_different_pages_are_reported():
    index = source_index.SourceIndex.from_pdf(make_pdf([["Widget  20.00"], ["Other  35.00"]]))
    assert "do not appear together" in index.problem({"price": 20.00, "total": 35.00})


def test_page_numbers_only_added_for_multi_page_sources():
    rows = [{"total": 7.25}]
    index = source_index.SourceIndex.from_pdf(make_pdf([["a  1.00"], ["b  7.25"]]))
    assert source_index.add_page_numbers(rows, index)[0]["page"] == 2
    single = source_index.SourceIndex.from_pdf(make_pdf([["b  7.25"]]))
    assert "page" not in source_index.add_page_numbers([{"total": 7.25}], single)[0]


def test_render_returns_a_png():
    index = source_index.SourceIndex.from_pdf(make_pdf([["Widget   2   10.00   20.00"]]))
    assert index.render(1, {"total": 20.0}).startswith(b"\x89PNG")


def test_images_have_no_index():
    assert source_index.build_index("scan.png", b"x") is None


def test_validator_uses_the_evidence_hook():
    rows = [{"description": "Widget", "quantity": 2, "price": 10.0, "total": 20.0}]
    report = validate_line_items({"type": "invoice", "data": rows},
                                 evidence=lambda row: "Not found in the source document: total")
    assert report["anomalies"] and "Not found in the source" in report["anomalies"][0]["errors"][-1]


def test_repeated_page_headers_are_stripped_but_amounts_kept():
    pages = [f"ACME BANK LTD\nPage {n}\nrow {n}  100.00" for n in range(1, 11)]
    cleaned = ocr.strip_page_furniture(pages)
    assert "ACME BANK LTD" not in cleaned[3]
    assert "ACME BANK LTD" in cleaned[0]           # page 1 is left alone
    assert all("100.00" in page for page in cleaned)


def test_rate_limit_waits_longer_than_other_errors():
    other = ocr._retry_delay(RuntimeError("boom"), 1)
    limited = ocr._retry_delay(RuntimeError("429 RESOURCE_EXHAUSTED"), 1)
    assert limited > other


def test_usage_adds_up_across_calls():
    class Meta:
        prompt_token_count, candidates_token_count = 100, 20

    class Resp:
        usage_metadata = Meta()

    usage = ocr.Usage()
    usage.add(Resp())
    usage.add(Resp())
    assert usage.as_dict() == {"calls": 2, "input_tokens": 200, "output_tokens": 40}
