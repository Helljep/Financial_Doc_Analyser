import hashlib
import json

import pandas as pd
import streamlit as st

from audit import canonical_json, generate_audit_hash, verify_ledger
from hyper_export import create_hyper_extract
from llm import generate_report
from ocr import extract_financial_data
from report import create_charts, create_pdf_report
from validator import validate_line_items

st.set_page_config(
    page_title="Financial Gauntlet | Analytics", 
    page_icon="📈", 
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom Premium CSS injecting Glassmorphism and sophisticated corporate colors
st.markdown("""
<style>
    .stApp { background-color: #f7f9fc; color: #2b3035; font-family: 'Inter', 'Segoe UI', sans-serif; }
    h1, h2, h3 { color: #1e3a8a !important; font-weight: 700 !important; letter-spacing: -0.5px; }
    h4 { color: #4f46e5 !important; font-weight: 600 !important; }
    .css-1vkfs00 { border-radius: 16px; border: 2px dashed #cbd5e1 !important; background: linear-gradient(135deg, rgba(255, 255, 255, 0.9), rgba(241, 245, 249, 0.9)); backdrop-filter: blur(10px); padding: 40px; transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); }
    .css-1vkfs00:hover { border-color: #60a5fa !important; box-shadow: 0 8px 32px rgba(96, 165, 250, 0.15); background-color: #ffffff; }
    [data-testid="stMetricValue"] { font-size: 2.8rem !important; font-weight: 800 !important; color: #0f172a !important; }
    [data-testid="stMetricLabel"] { font-size: 1.1rem !important; font-weight: 600 !important; color: #64748b !important; text-transform: uppercase; letter-spacing: 1px; }
    div[data-testid="metric-container"] { background: linear-gradient(145deg, #ffffff, #f8fafc); border: 1px solid #e2e8f0; border-radius: 16px; padding: 24px; box-shadow: 0 4px 15px rgba(0,0,0,0.03); text-align: center; transition: transform 0.2s; }
    div[data-testid="metric-container"]:hover { transform: translateY(-5px); box-shadow: 0 8px 25px rgba(0,0,0,0.06); }
    .stDownloadButton > button { background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%) !important; color: #ffffff !important; border: none !important; border-radius: 12px !important; padding: 14px 28px !important; font-weight: 600 !important; font-size: 1.1rem !important; width: 100%; text-transform: uppercase; letter-spacing: 0.5px; transition: all 0.3s ease !important; box-shadow: 0 4px 15px rgba(59, 130, 246, 0.3) !important; }
    .stDownloadButton > button:hover { transform: translateY(-3px) scale(1.02); box-shadow: 0 8px 25px rgba(59, 130, 246, 0.5) !important; }
    .streamlit-expanderHeader { background-color: #ffffff !important; border-radius: 12px !important; color: #2563eb !important; font-weight: 600; border: 1px solid #e2e8f0; }
    .stAlert { border-radius: 12px !important; border-left: 5px solid !important; background-color: #ffffff !important; color: #334155 !important; box-shadow: 0 2px 8px rgba(0,0,0,0.04); border: 1px solid #e2e8f0; }
    hr { border-color: #cbd5e1 !important; opacity: 0.8; }
    p { color: #475569; }
</style>
""", unsafe_allow_html=True)

# Header Section
st.title("📈 Financial Gauntlet Analytics")
st.markdown("<p style='font-size: 18px; color: #8b949e; margin-bottom: 30px;'>An intelligent, hybrid pipeline ingesting unstructured system logs and structured invoices to generate deep, mathematically-validated AI insights.</p>", unsafe_allow_html=True)

DOC_TYPES = ["invoice", "tax_1040", "insurance_claim", "bank_statement", "unknown"]
MAX_MB = 250   # keep in sync with server.maxUploadSize in .streamlit/config.toml


def clean_rows(df: pd.DataFrame) -> list:
    """Editor output -> list of dicts, dropping blank cells and fully empty rows."""
    rows = []
    for rec in df.to_dict("records"):
        row = {k: v for k, v in rec.items() if not (v is None or v == "" or pd.isna(v))}
        if row:
            rows.append(row)
    return rows


def run_extraction(file, file_id: str):
    with st.status("⚙️ Extracting document data...", expanded=True) as status:
        st.write("🔍 Parsing document & extracting the raw data matrix...")
        bar = st.progress(0.0, text="Reading document...")

        def on_progress(done, total):
            bar.progress(done / total, text=f"Extracted part {done} of {total}")

        extracted = extract_financial_data(file, on_progress=on_progress)
        bar.empty()
        if extracted.get("type") == "error":
            status.update(label="Extraction failed", state="error")
            st.error(f"The extraction engine failed: {extracted.get('error', 'unknown error')}")
            st.stop()
        initial = validate_line_items(extracted)
        status.update(label="✅ Extraction complete", state="complete", expanded=False)
    st.session_state.update(file_id=file_id, extracted=extracted,
                            had_issues=bool(initial["anomalies"] or initial["document_issues"]))
    for stale in ("report_key", "artifacts"):
        st.session_state.pop(stale, None)


# Upload Section inside a stylish container
with st.container():
    st.markdown("### 📥 Document Intake")
    uploaded_file = st.file_uploader("Upload your Document (PDF, Word .docx, TXT, CSV, Images) to begin analysis.",
                                     type=["pdf", "txt", "csv", "log", "png", "jpg", "jpeg", "docx"])

if uploaded_file:
    file_bytes = uploaded_file.getvalue()
    file_size_mb = len(file_bytes) / (1024 * 1024)
    if file_size_mb == 0:
        st.error("Error: Uploaded file is empty (0 bytes).")
        st.stop()
    elif file_size_mb > MAX_MB:
        st.error(f"Error: File size ({file_size_mb:.2f} MB) exceeds the {MAX_MB}MB limit.")
        st.stop()

    file_id = hashlib.sha256(file_bytes).hexdigest()

    # Streamlit re-runs this script on every interaction. Extraction and the AI report are the
    # expensive/non-deterministic steps, so they run once per file (and once per distinct edit).
    if st.session_state.get("file_id") != file_id:
        run_extraction(uploaded_file, file_id)

    extracted_data = st.session_state["extracted"]
    detected = extracted_data.get("type", "unknown")

    # --- DOCUMENT TYPE (the AI can misclassify; the wrong type means the wrong rule set) ---
    st.markdown("### 🧾 Classification")
    doc_type = st.selectbox(
        "Document type (auto-detected: change it if the AI got it wrong)",
        DOC_TYPES,
        index=DOC_TYPES.index(detected) if detected in DOC_TYPES else DOC_TYPES.index("unknown"),
        key=f"doctype_{file_id}",
    )
    if doc_type == "unknown":
        st.warning("Format unrecognized: only generic duplicate detection will run.", icon="⚠️")
    elif doc_type == detected:
        st.success(f"Document classified as {doc_type.upper()}", icon="✅")
    else:
        st.info(f"Validating as {doc_type.upper()} (AI detected {detected.upper()}).", icon="✏️")

    # --- HUMAN-IN-THE-LOOP: edit any extracted value; everything below re-validates ---
    original_rows = extracted_data["data"]
    with st.expander("✏️ Review & correct extracted data", expanded=st.session_state.get("had_issues", False)):
        st.caption("Fix any misread values here. Validation, the report, the exports and the audit hash all update from your edits.")
        edited_df = st.data_editor(pd.DataFrame(original_rows), num_rows="dynamic",
                                   width="stretch", key=f"editor_{file_id}")
    edited_rows = clean_rows(edited_df)
    manually_edited = edited_rows != clean_rows(pd.DataFrame(original_rows))

    working = dict(extracted_data, type=doc_type, data=edited_rows)
    validation_report = validate_line_items(working)

    for issue in validation_report.get("document_issues", []):
        st.warning(issue, icon="📄")
    if validation_report["anomalies"]:
        st.warning("⚠️ Anomalies detected. Correct the values above (or the document type) to resolve them.")
        st.dataframe([{**{k: v for k, v in a.items() if k != "errors"}, "issues": "; ".join(a.get("errors", []))}
                      for a in validation_report["anomalies"]], width="stretch")
    elif not validation_report.get("document_issues"):
        st.success("✅ All checks passed.")

    # --- REPORT + EXPORTS (regenerated only when the validated data actually changes) ---
    audit_payload = dict(validation_report, source_sha256=file_id, manually_edited=manually_edited)
    report_key = hashlib.sha256(canonical_json(audit_payload).encode("utf-8")).hexdigest()

    if st.session_state.get("report_key") != report_key:
        with st.status("⚙️ Building report...", expanded=True) as status:
            st.write("🧠 Synthesizing intelligence via Gemini 2.5 Flash...")
            report_json_str = generate_report(validation_report, extracted_data["text"], extracted_data.get("extraction_reasoning", ""))
            st.write("📄 Rendering PDF report...")
            tag = report_key[:10]
            pdf_path = create_pdf_report(validation_report, report_json_str, output_filename=f"report_{tag}.pdf")
            st.write("📦 Generating Tableau data extract (.hyper)...")
            hyper_path = create_hyper_extract(validation_report, output_filename=f"analytics_{doc_type.upper()}_{tag}.hyper")
            st.write("🛡️ Recording hash in the audit ledger...")
            audit_hash = generate_audit_hash(audit_payload)
            status.update(label=f"✅ Report ready (mode: {doc_type.upper()})", state="complete", expanded=False)
        st.session_state["artifacts"] = dict(report_json_str=report_json_str, pdf_path=pdf_path,
                                             hyper_path=hyper_path, audit_hash=audit_hash)
        st.session_state["report_key"] = report_key

    art = st.session_state["artifacts"]
    report_json_str, pdf_path, hyper_path, audit_hash = art["report_json_str"], art["pdf_path"], art["hyper_path"], art["audit_hash"]
    doc_type_fmt = doc_type.upper()

    ledger_ok, ledger_msg = verify_ledger()
    st.caption(f"🛡️ Audit hash (SHA-256 of the validated data): `{audit_hash}` · {ledger_msg}")
    if not ledger_ok:
        st.error(f"Audit ledger integrity check failed: {ledger_msg}")

    st.markdown("---")

    # --- KPI DASHBOARD ---
    st.markdown("### 📊 Key Performance Indicators")
    tot = len(validation_report["valid_records"]) + len(validation_report["anomalies"])
    anom = len(validation_report["anomalies"])
    acc = validation_report.get("accuracy_score", 0.0)
    recon = validation_report.get("reconciliation")

    cols = st.columns(4 if recon else 3)
    cols[0].metric("Total Records Parsed", f"{tot:,}")
    cols[1].metric("Anomalies Detected", anom, delta=f"-{anom}" if anom > 0 else "0", delta_color="inverse")
    cols[2].metric("Validation Pass Rate", f"{acc * 100:.1f}%",
                   help="Share of rows whose arithmetic checks passed. It does not measure how accurately the AI read the document.")
    if recon:
        cols[3].metric("Line Items vs Grand Total", "Match" if recon["matches"] else f"Off by {recon['difference']:,.2f}",
                       help=f"Sum of line items {recon['line_items_sum']:,.2f} vs stated total {recon['grand_total']:,.2f}. "
                            "A gap can be legitimate (e.g. tax or shipping not listed as a line).")

    st.markdown("<br>", unsafe_allow_html=True)

    # --- MAIN CONTENT LAYOUT ---
    st.markdown("### 🧬 Deep Analysis")
    main_col1, main_col2 = st.columns([1.1, 1.9], gap="large")

    with main_col1:
        st.markdown("#### 🧠 AI Executive Summary")
        try:
            insights = json.loads(report_json_str)
            with st.container(border=True):
                st.info(f"**Brief:** {insights.get('executive_summary', 'No summary available.')}", icon="📌")
                st.write(f"**Trend Analysis:** {insights.get('trend_analysis', 'Insufficient data for trends.')}")

                st.markdown("---")
                st.error("**⚠️ Critical Risk Factors**")
                for risk in insights.get('risk_factors', []):
                    st.markdown(f"- {risk}")

                st.markdown("<br>", unsafe_allow_html=True)

                st.success("**🎯 Recommended Actions**")
                for action in insights.get('recommended_actions', []):
                    st.markdown(f"- {action}")

        except json.JSONDecodeError:
            st.warning("Failed to parse Structured AI Analytics output.")
            with st.expander("View Raw API Response"):
                st.text(report_json_str)

    with main_col2:
        st.markdown("#### 📉 Visual Telemetry")
        buf1, buf2 = create_charts(report_json_str)
        if buf1 and buf2:
            st.image(buf1, width="stretch")
            st.markdown("<br>", unsafe_allow_html=True)
            st.image(buf2, width="stretch")
        else:
            st.warning("Insufficient numeric variance to render telemetry charts.", icon="ℹ️")

    st.markdown("---")

    # --- FINAL EXPORT ---
    st.markdown("### 📤 Report Export")
    st.markdown("<p style='color: #8b949e; margin-bottom: 20px;'>Download the mathematically validated PDF report or the core database extract for Tableau.</p>", unsafe_allow_html=True)

    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        with open(pdf_path, "rb") as pdf_file:
            st.download_button(
                label="📄 Download Validated Report (.pdf)",
                data=pdf_file.read(),
                file_name=f"Financial_Gauntlet_{doc_type_fmt}.pdf",
                mime="application/pdf",
            )
    with col_dl2:
        if hyper_path:
            with open(hyper_path, "rb") as hyper_file:
                st.download_button(
                    label="📊 Download Tableau Extract (.hyper)",
                    data=hyper_file.read(),
                    file_name=f"Financial_Gauntlet_{doc_type_fmt}.hyper",
                    mime="application/octet-stream",
                )
        else:
            st.info("Tableau extract unavailable (no tabular data, or the Tableau Hyper API could not be loaded on this machine).")
