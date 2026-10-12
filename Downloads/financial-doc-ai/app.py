import streamlit as st
import json
import time
from report import create_charts, create_pdf_report
from ocr import extract_financial_data
from validator import validate_line_items
from llm import generate_report
from hyper_export import create_hyper_extract
from audit import generate_audit_hash

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

# Upload Section inside a stylish container
with st.container():
    st.markdown("### 📥 Document Intake")
    uploaded_file = st.file_uploader("Upload your Document (PDF, Word, TXT, CSV, Images) to begin analysis.", type=["pdf", "txt", "csv", "log", "png", "jpg", "jpeg", "docx", "doc"])

if uploaded_file:
    file_size_mb = uploaded_file.size / (1024 * 1024)
    if file_size_mb == 0:
        st.error("Error: Uploaded file is empty (0 bytes).")
        st.stop()
    elif file_size_mb > 50:
        st.error(f"Error: File size ({file_size_mb:.2f} MB) exceeds the 50MB limit.")
        st.stop()

    with st.status("⚙️ Executing Analytics Pipeline...", expanded=True) as status:
        st.write("🔍 Parsing Document & Extracting Raw Data Matrix (1-Shot AI Approach)...")
        try:
            # 1. Full Document Extraction
            extracted_data = extract_financial_data(uploaded_file)
            
            # Format Classification Check
            doc_type = extracted_data.get("type", "unknown")
            if doc_type == "unknown":
                st.warning("Format Unrecognized: Proceeding with generic anomaly detection.", icon="⚠️")
            else:
                st.success(f"Document Successfully Classified: {doc_type.upper()}", icon="✅")

            # 2. Complete Math Validation
            st.write("🧮 Running Chronological Math & Anomaly Detection...")
            validation_report = validate_line_items(extracted_data)
            
        except Exception as e:
            st.error("Fatal Error: The extraction engine encountered a critical failure.")
            with st.expander("View System Traceback"):
                st.text(str(e))
            st.stop()
            
        # --- HUMAN-IN-THE-LOOP UI ---
        if validation_report.get("anomalies"):
            st.warning("⚠️ Anomalies Detected! Review and fix the data extracted below to proceed.")
            # Show interactive grid
            edited_anomalies = st.data_editor(validation_report["anomalies"], use_container_width=True, key="hitl_editor")
            
            # Detect changes and re-validate dynamically
            if edited_anomalies != validation_report["anomalies"]:
                clean_edited = [{k: v for k, v in a.items() if k != "errors"} for a in edited_anomalies]
                clean_valid = [{k: v for k, v in v.items() if k != "errors"} for v in validation_report.get("valid_records", [])]
                
                doc_type = validation_report.get("doc_type")
                if doc_type == "invoice":
                    headers = list(clean_valid[0].keys()) if clean_valid else list(clean_edited[0].keys())
                    new_table = [headers]
                    for rec in clean_valid + clean_edited:
                        new_table.append([rec.get(h, "") for h in headers])
                    extracted_data["data"] = [new_table]
                elif doc_type == "logs":
                    extracted_data["data"] = clean_valid + clean_edited
                
                st.info("🔄 Modifications detected. Re-validating payload...")
                validation_report = validate_line_items(extracted_data)
                
                if not validation_report.get("anomalies"):
                    st.success("✅ Issues Resolved! Continuing pipeline...")
                else:
                    st.error("⚠️ Overcharge or Math Anomalies still present in the edited data.")
        
        st.write("🧠 Synthesizing Intelligence via Gemini 2.5 Flash...")
        report_json_str = generate_report(validation_report, extracted_data["text"], extracted_data.get("extraction_reasoning", ""))
        
        st.write("📄 Rendering C-Suite PDF Final Report...")
        pdf_path = create_pdf_report(validation_report, report_json_str)
        
        st.write("📦 Generating Tableau Data Extract (.hyper)...")
        doc_type_fmt = validation_report.get('doc_type', 'unknown').upper()
        hyper_path = create_hyper_extract(validation_report, output_filename=f"analytics_{doc_type_fmt}.hyper")
        
        st.write("🛡️ Securing Payload via Immutable Audit Hash...")
        audit_hash = generate_audit_hash(validation_report)
        st.success(f"Audit Hash Generated: {audit_hash}")
        
        status.update(label=f"✅ Pipeline Completed Successfully (Mode: {doc_type_fmt})", state="complete", expanded=False)

    st.markdown("---")

    # --- KPI DASHBOARD ---
    st.markdown("### 📊 Key Performance Indicators")
    col1, col2, col3 = st.columns(3)
    tot = len(validation_report.get("valid_records", [])) + len(validation_report.get("anomalies", []))
    anom = len(validation_report.get("anomalies", []))
    acc = validation_report.get("accuracy_score", 0.0)
    
    col1.metric("Total Records Parsed", f"{tot:,}")
    col2.metric("Anomalies Detected", anom, delta=f"-{anom}" if anom > 0 else "0", delta_color="inverse")
    col3.metric("Data Confidence Score", f"{acc * 100:.1f}%")

    st.markdown("<br>", unsafe_allow_html=True) # Spacer

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
            st.image(buf1, use_container_width=True)
            st.markdown("<br>", unsafe_allow_html=True)
            st.image(buf2, use_container_width=True)
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
                label="📄 Download Validated C-Suite Report (.pdf)",
                data=pdf_file,
                file_name=f"Financial_Gauntlet_{doc_type_fmt}.pdf",
                mime="application/pdf"
            )
    with col_dl2:
        if hyper_path:
            with open(hyper_path, "rb") as hyper_file:
                st.download_button(
                    label="📊 Download Tableau Extract (.hyper)",
                    data=hyper_file,
                    file_name=f"Financial_Gauntlet_{doc_type_fmt}.hyper",
                    mime="application/octet-stream"
                )
        else:
            st.warning("No tabular data generated to extract to Tableau.")