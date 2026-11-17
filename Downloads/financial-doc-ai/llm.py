import os
import json
from collections import Counter
from typing import List

from pydantic import BaseModel

from config import MODEL_NAME, get_client
from validator import _amount

# A 2,000-page document has thousands of rows; sending them all would overflow the model's context.
# Above these limits the report is built from counts, error-type tallies, the largest values and samples.
MAX_REPORT_ROWS = 40
MAX_REPORT_TEXT_CHARS = 60_000


def compact_validation_report(report: dict) -> dict:
    valid, anomalies = report.get("valid_records", []), report.get("anomalies", [])
    if len(valid) + len(anomalies) <= MAX_REPORT_ROWS * 2:
        return report
    everything = sorted(valid + anomalies, key=lambda r: abs(_amount(r)), reverse=True)
    tally = Counter(e.split(":")[0] for a in anomalies for e in a.get("errors", []))
    keep = lambda rows: [{k: v for k, v in r.items() if k != "extraction_reasoning"} for r in rows]
    return {
        "doc_type": report.get("doc_type"), "status": report.get("status"),
        "document_issues": report.get("document_issues", []), "reconciliation": report.get("reconciliation"),
        "pass_rate": report.get("accuracy_score"),
        "counts": {"valid_records": len(valid), "anomalies": len(anomalies)},
        "anomaly_types": dict(tally.most_common()),
        "largest_records": keep(everything[:10]),
        "anomaly_samples": keep(anomalies[:MAX_REPORT_ROWS]),
        "note": f"Large document: showing samples only ({len(valid) + len(anomalies)} rows total).",
    }


def compact_text(text: str) -> str:
    if len(text) <= MAX_REPORT_TEXT_CHARS:
        return text
    half = MAX_REPORT_TEXT_CHARS // 2
    omitted = len(text) - MAX_REPORT_TEXT_CHARS
    return text[:half] + f"\n\n[... {omitted:,} characters omitted ...]\n\n" + text[-half:]



class ReportInsights(BaseModel):
    executive_summary: str
    trend_analysis: str
    risk_factors: List[str]
    recommended_actions: List[str]
    chart_title: str
    chart_labels: List[str]
    chart_values: List[float]

def generate_report(validation_report: dict, raw_text: str, overall_reasoning: str = "") -> str:
    """
    Generates structured AI insights analyzing the math anomalies and raw text.
    """
    # Very large documents are compacted (see compact_text) so the prompt always fits.
    text_chunk = compact_text(raw_text) if raw_text else "No raw text provided."

    system_instruction = (
        "You are an Expert Data Analyst. Review the chronological log data anomalies "
        "and corresponding raw text from the system dump. "
        "Generate a professional analytical report. "
        "CRITICAL INSTRUCTION: Your 'executive_summary' MUST cite the exact steps in the 'extraction_reasoning' field. Keep it extremely small—exactly ONE short, concise sentence summarizing everything. No fluff. "
        "Additionally, identify the 'Top 5' most significant numeric data points to visualize (e.g. Highest Invoices, Largest Anomalies, biggest Deltas) and provide a concise 'chart_title', 5 'chart_labels' (strings), and 5 'chart_values' (floats)."
    )
    
    user_prompt = f"""
    Overall OCR Extraction Reasoning:
    {overall_reasoning}

    Validation Report Data (includes line-item 'extraction_reasoning'):
    {json.dumps(compact_validation_report(validation_report), indent=2, default=str)}
    
    Raw Document Text:
    {text_chunk}
    """

    try:
        from google.genai import types
        response = get_client().models.generate_content(
            model=MODEL_NAME,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=ReportInsights,
                temperature=0.2,
            ),
        )
        
        # Returns raw JSON string 
        return response.text
        
    except Exception as e:
        # Fallback in case API fails
        fallback = ReportInsights(
            executive_summary=f"Failed to generate AI insights due to an API Error: {str(e)}",
            trend_analysis="N/A",
            risk_factors=["API failure unable to cross-reference data"],
            recommended_actions=["Check Gemini API key or quota limits."],
            chart_title="API Failure - No Data",
            chart_labels=["Error"],
            chart_values=[0.0]
        )
        return fallback.model_dump_json()
