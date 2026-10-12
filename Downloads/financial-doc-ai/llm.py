import os
import json
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import List

# API key MUST be set via GEMINI_API_KEY environment variable
client = genai.Client(api_key="AIzaSyCnaDrmUqHn0yl6_BVjNFtawLRhm5vnCJQ")

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
    # Pass the full document text — no truncation — for 100+ page documents
    text_chunk = raw_text if raw_text else "No raw text provided."

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
    {json.dumps(validation_report, indent=2)}
    
    Raw Document Text:
    {text_chunk}
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
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
