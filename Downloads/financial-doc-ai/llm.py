"""Ask Gemini to write the executive summary, risks and chart data for a validated document."""
import json
from collections import Counter

from pydantic import BaseModel

from config import MODEL_NAME, get_client
from validator import headline_amount

# A 2,000-page document has thousands of rows, far more than fits in one prompt. Past these
# limits the model gets counts, error tallies, the biggest rows and a sample instead.
MAX_REPORT_ROWS = 40
MAX_REPORT_TEXT_CHARS = 60_000

SYSTEM_INSTRUCTION = (
    "You are an expert data analyst. Review the anomalies found in this financial document "
    "and the document text, then write a professional analytical report. "
    "The 'executive_summary' must cite the steps in the 'extraction_reasoning' field and be exactly "
    "one short sentence. "
    "Also pick the 5 most significant numeric data points to chart (highest invoices, largest "
    "anomalies, biggest differences) and give a short 'chart_title', 5 'chart_labels' (strings) "
    "and 5 'chart_values' (floats)."
)


class ReportInsights(BaseModel):
    executive_summary: str
    trend_analysis: str
    risk_factors: list[str]
    recommended_actions: list[str]
    chart_title: str
    chart_labels: list[str]
    chart_values: list[float]


def compact_validation_report(report: dict) -> dict:
    """Shrink a big validation report so it fits in a prompt."""
    valid, anomalies = report.get("valid_records", []), report.get("anomalies", [])
    if len(valid) + len(anomalies) <= MAX_REPORT_ROWS * 2:
        return report

    def trim(rows):
        return [{k: v for k, v in row.items() if k != "extraction_reasoning"} for row in rows]

    biggest = sorted(valid + anomalies, key=lambda row: abs(headline_amount(row)), reverse=True)
    error_types = Counter(error.split(":")[0] for row in anomalies for error in row.get("errors", []))
    return {
        "doc_type": report.get("doc_type"),
        "status": report.get("status"),
        "document_issues": report.get("document_issues", []),
        "reconciliation": report.get("reconciliation"),
        "pass_rate": report.get("accuracy_score"),
        "counts": {"valid_records": len(valid), "anomalies": len(anomalies)},
        "anomaly_types": dict(error_types.most_common()),
        "largest_records": trim(biggest[:10]),
        "anomaly_samples": trim(anomalies[:MAX_REPORT_ROWS]),
        "note": f"Large document: samples only ({len(valid) + len(anomalies)} rows in total).",
    }


def compact_text(text: str) -> str:
    """Keep the start and end of a very long text."""
    if len(text) <= MAX_REPORT_TEXT_CHARS:
        return text
    half = MAX_REPORT_TEXT_CHARS // 2
    omitted = len(text) - MAX_REPORT_TEXT_CHARS
    return text[:half] + f"\n\n[... {omitted:,} characters omitted ...]\n\n" + text[-half:]


def generate_report(validation_report: dict, raw_text: str, overall_reasoning: str = "") -> str:
    """Returns the insights as a JSON string. If the API call fails, returns a placeholder instead."""
    document_text = compact_text(raw_text) if raw_text else "No raw text provided."
    prompt = (
        f"Overall extraction reasoning:\n{overall_reasoning}\n\n"
        "Validation report (rows include their own extraction_reasoning):\n"
        f"{json.dumps(compact_validation_report(validation_report), indent=2, default=str)}\n\n"
        f"Document text:\n{document_text}"
    )

    try:
        from google.genai import types
        response = get_client().models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=ReportInsights,
                temperature=0.2,
            ),
        )
        return response.text
    except Exception as error:
        fallback = ReportInsights(
            executive_summary=f"Could not generate AI insights: {error}",
            trend_analysis="N/A",
            risk_factors=["The AI call failed, so the data could not be cross-checked."],
            recommended_actions=["Check the Gemini API key and quota."],
            chart_title="No data",
            chart_labels=["Error"],
            chart_values=[0.0],
        )
        return fallback.model_dump_json()
