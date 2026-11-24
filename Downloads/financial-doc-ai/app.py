"""Streamlit front end: upload, review, export."""
import hashlib
import json
from pathlib import Path

import pandas as pd
import streamlit as st

import source_index
from audit import canonical_json, generate_audit_hash, verify_ledger
from hyper_export import create_hyper_extract
from llm import generate_report, summary_failed
from ocr import extract_financial_data
from report import create_charts, create_pdf_report
from validator import validate_line_items

DOC_TYPES = ["invoice", "tax_1040", "insurance_claim", "bank_statement", "unknown"]
MAX_MB = 250            # keep in step with server.maxUploadSize in .streamlit/config.toml
UPLOAD_TYPES = ["pdf", "docx", "txt", "csv", "log", "png", "jpg", "jpeg"]

st.set_page_config(page_title="Financial Gauntlet", page_icon="📄", layout="wide")
st.markdown(f"<style>{Path(__file__).with_name('style.css').read_text()}</style>", unsafe_allow_html=True)


def clean_rows(df: pd.DataFrame) -> list:
    """Editor output back to a list of dicts, without blank cells or empty rows."""
    rows = []
    for record in df.to_dict("records"):
        row = {k: v for k, v in record.items() if not (v is None or v == "" or pd.isna(v))}
        if row:
            rows.append(row)
    return rows


def run_extraction(file, file_id: str, data: bytes):
    """Read the document once and keep the result in the session."""
    with st.status("Reading the document...", expanded=True) as status:
        bar = st.progress(0.0, text="Starting")

        def on_progress(done, total):
            bar.progress(done / total, text=f"Part {done} of {total} done")

        extracted = extract_financial_data(file, on_progress=on_progress)
        bar.empty()
        if extracted.get("type") == "error":
            status.update(label="Could not read the document", state="error")
            st.error(extracted.get("error", "Unknown error"))
            st.stop()

        index = source_index.build_index(file.name, data, extracted.get("text", ""))
        source_index.add_page_numbers(extracted["data"], index)
        first_pass = validate_line_items(extracted, evidence=index.problem if index else None)
        status.update(label="Done reading", state="complete", expanded=False)

    st.session_state.update(file_id=file_id, extracted=extracted, index=index,
                            had_issues=bool(first_pass["anomalies"] or first_pass["document_issues"]))
    for stale in ("report_key", "artifacts"):
        st.session_state.pop(stale, None)


def build_exports(report, extracted, doc_type, audit_payload, report_key):
    """Summary, PDF, Tableau file and audit entry. Slow, so it only runs when the data changes."""
    tag = report_key[:10]
    with st.status("Building the report...", expanded=False) as status:
        insights_json = generate_report(report, extracted["text"], extracted.get("extraction_reasoning", ""))
        pdf_path = create_pdf_report(report, insights_json, output_filename=f"report_{tag}.pdf")
        hyper_path = create_hyper_extract(report, output_filename=f"analytics_{doc_type}_{tag}.hyper")
        audit_hash = generate_audit_hash(audit_payload)
        status.update(label="Report ready", state="complete")
    st.session_state["artifacts"] = dict(insights_json=insights_json, pdf_path=pdf_path,
                                         hyper_path=hyper_path, audit_hash=audit_hash,
                                         summary_failed=summary_failed(insights_json))
    st.session_state["report_key"] = report_key


def show_overview(report, extracted, insights_json):
    total = len(report["valid_records"]) + len(report["anomalies"])
    flagged = len(report["anomalies"])
    recon = report.get("reconciliation")

    cols = st.columns(4 if recon else 3)
    cols[0].metric("Rows read", f"{total:,}")
    cols[1].metric("Rows flagged", f"{flagged:,}")
    cols[2].metric("Checks passed", f"{report.get('accuracy_score', 0.0) * 100:.1f}%",
                   help="Share of rows whose arithmetic checks out. It says nothing about "
                        "whether the values were read correctly.")
    if recon:
        cols[3].metric("Items vs stated total", "Match" if recon["matches"] else f"Off by {recon['difference']:,.2f}",
                       help=f"Line items {recon['line_items_sum']:,.2f} + tax {recon['tax_total']:,.2f}; the document "
                            f"states {recon['grand_total']:,.2f}. Shipping or a missed tax line can explain a gap.")

    for warning in extracted.get("extraction_warnings", []):
        st.warning(warning)
    for issue in report.get("document_issues", []):
        st.warning(issue)
    if not (flagged or report.get("document_issues") or extracted.get("extraction_warnings")):
        st.success("Every check passed.")

    usage = extracted.get("usage")
    if usage and usage.get("calls"):
        st.caption(f"{usage['calls']} Gemini calls, {usage['input_tokens']:,} tokens in, "
                   f"{usage['output_tokens']:,} out.")

    st.divider()
    if summary_failed(insights_json):
        st.warning("Gemini couldn't write the summary this time, usually because it is busy. "
                   "Everything else is unaffected.")
        if st.button("Try the summary again"):
            st.session_state.pop("report_key", None)     # forces a rebuild on the next run
            st.rerun()
        return
    try:
        insights = json.loads(insights_json)
    except ValueError:
        st.warning("The summary could not be read.")
        return

    left, right = st.columns([1, 1.2], gap="large")
    with left:
        st.subheader("Summary")
        st.write(insights.get("executive_summary", ""))
        st.write(insights.get("trend_analysis", ""))
        st.markdown("**Risks**")
        for item in insights.get("risk_factors", []):
            st.markdown(f"- {item}")
        st.markdown("**Next steps**")
        for item in insights.get("recommended_actions", []):
            st.markdown(f"- {item}")
    with right:
        bars, donut = create_charts(insights_json)
        if bars and donut:
            st.image(bars)
            st.image(donut)
        else:
            st.caption("Not enough numeric data to draw charts.")


def show_source(report, index):
    """Pick a flagged row and see where it sits in the original document."""
    st.markdown("#### Flagged rows")
    flagged = report["anomalies"]
    if not flagged:
        st.caption("Nothing flagged.")
        return
    table = [{**{k: v for k, v in row.items() if k not in ("errors", "extraction_reasoning")},
              "why": "; ".join(row.get("errors", []))} for row in flagged]
    st.dataframe(table, hide_index=True)

    if index is None:
        st.caption("No source view for this file: it is an image or has no text layer.")
        return

    st.markdown("#### Where it came from")
    labels = [f"{n + 1}. " + str(row.get("description") or row.get("transaction_id") or "row")[:50]
              for n, row in enumerate(flagged)]
    choice = st.selectbox("Row", range(len(flagged)), format_func=lambda i: labels[i])
    row = flagged[choice]
    pages = index.pages_for(row)
    if not pages:
        st.caption("The amounts in this row could not be found in the source text.")
        return
    page_no = st.selectbox("Page", pages) if len(pages) > 1 else pages[0]
    if index.can_draw:
        st.image(index.render(page_no, row), caption=f"Page {page_no}")
    else:
        st.caption(f"Page {page_no}" if len(index.pages) > 1 else "Source text")
        st.code(index.snippet(page_no, row) or "No matching line.", language=None)


def show_exports(art, doc_type, audit_hash):
    pdf_col, hyper_col = st.columns(2)
    with pdf_col:
        st.download_button("Download PDF report", Path(art["pdf_path"]).read_bytes(),
                           file_name=f"report_{doc_type}.pdf", mime="application/pdf")
    with hyper_col:
        if art["hyper_path"]:
            st.download_button("Download Tableau extract", Path(art["hyper_path"]).read_bytes(),
                               file_name=f"analytics_{doc_type}.hyper", mime="application/octet-stream")
        else:
            st.caption("The Tableau extract is not available here: there are no rows, "
                       "or the Hyper library could not start on this machine.")

    ok, message = verify_ledger()
    st.markdown("**Audit entry**")
    st.code(audit_hash, language=None)
    if ok:
        st.caption(f"SHA-256 of the validated data. {message}")
    else:
        st.error(f"Audit ledger check failed: {message}")


# ---------------------------------------------------------------- page

st.title("Financial Gauntlet")
st.markdown("<div class='tagline'>Reads a messy financial document, re-checks the arithmetic, "
            "and lets you correct anything before it is exported.</div>", unsafe_allow_html=True)

uploaded = st.file_uploader("Drop a document here", type=UPLOAD_TYPES,
                            help="PDF, Word, text, CSV or a scanned image.")
if not uploaded:
    st.stop()

data = uploaded.getvalue()
size_mb = len(data) / (1024 * 1024)
if not data:
    st.error("That file is empty.")
    st.stop()
if size_mb > MAX_MB:
    st.error(f"That file is {size_mb:.0f} MB. The limit is {MAX_MB} MB.")
    st.stop()

file_id = hashlib.sha256(data).hexdigest()

# Streamlit reruns this script on every click, so the slow steps are kept in the session.
if st.session_state.get("file_id") != file_id:
    run_extraction(uploaded, file_id, data)

extracted = st.session_state["extracted"]
index = st.session_state.get("index")
detected = extracted.get("type", "unknown")
if detected not in DOC_TYPES:
    detected = "unknown"

doc_type = st.selectbox("Document type", DOC_TYPES, index=DOC_TYPES.index(detected),
                        key=f"doctype_{file_id}",
                        help="Detected automatically. Change it if the guess is wrong: "
                             "each type has its own checks.")

tab_overview, tab_review, tab_exports = st.tabs(["Overview", "Review data", "Exports"])

with tab_review:
    st.caption("Fix any misread value. Checks, summary, exports and the audit entry all update.")
    original = extracted["data"]
    edited_df = st.data_editor(pd.DataFrame(original), num_rows="dynamic", key=f"editor_{file_id}")

edited = clean_rows(edited_df)
manually_edited = edited != clean_rows(pd.DataFrame(original))
report = validate_line_items(dict(extracted, type=doc_type, data=edited),
                             evidence=index.problem if index else None)

with tab_review:
    show_source(report, index)

audit_payload = dict(report, source_sha256=file_id, manually_edited=manually_edited)
report_key = hashlib.sha256(canonical_json(audit_payload).encode("utf-8")).hexdigest()
if st.session_state.get("report_key") != report_key:
    build_exports(report, extracted, doc_type, audit_payload, report_key)
art = st.session_state["artifacts"]

with tab_overview:
    show_overview(report, extracted, art["insights_json"])
with tab_exports:
    show_exports(art, doc_type, art["audit_hash"])
