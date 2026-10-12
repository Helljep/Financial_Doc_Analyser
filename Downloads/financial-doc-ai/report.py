import os
import json
import io
import matplotlib.pyplot as plt
import seaborn as sns
from fpdf import FPDF
import re

class PDFReport(FPDF):
    def header(self):
        self.set_font("helvetica", "B", 18)
        self.cell(0, 10, "Financial Gauntlet: Analytics Report", align="C", new_x="LMARGIN", new_y="NEXT")
        self.line(10, 22, 200, 22)
        self.ln(10)
    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

def create_charts(llm_json):
    sns.set_theme(style="whitegrid")
    buf1, buf2 = io.BytesIO(), io.BytesIO()

    try:
        insights = json.loads(llm_json)
        title = insights.get("chart_title", "Key Metrics Distribution")
        labels = insights.get("chart_labels", [])
        values = insights.get("chart_values", [])

        if labels and values and len(labels) == len(values):
            # Chart 1: Horizontal Bar
            fig1, ax1 = plt.subplots(figsize=(8, 4))
            sns.barplot(x=values, y=labels, ax=ax1, palette="viridis")
            ax1.set_title(title)
            fig1.savefig(buf1, format="png", bbox_inches='tight')
            plt.close(fig1)
            
            # Chart 2: Modern Donut Chart
            fig2, ax2 = plt.subplots(figsize=(8, 4))
            wedges, texts, autotexts = ax2.pie(values[:5], autopct='%1.1f%%', startangle=90, pctdistance=0.80)
            centre_circle = plt.Circle((0,0), 0.60, fc='white') # Creates the Donut hole
            fig2.gca().add_artist(centre_circle)
            ax2.legend(wedges, labels[:5], title="Items", loc="center left", bbox_to_anchor=(1, 0, 0.5, 1))
            ax2.set_title(title + " (Distribution)")
            fig2.savefig(buf2, format="png", bbox_inches='tight')
            plt.close(fig2)
        else:
            return None, None
            
    except Exception as e:
        return None, None

    buf1.seek(0)
    buf2.seek(0)
    return buf1, buf2

def create_pdf_report(validation_report, llm_json, output_filename="validated_report.pdf"):
    pdf = PDFReport()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    # Page 1: AI Insights
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "1. Executive Summary & AI Insights", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 11)
    try:
        insights = json.loads(llm_json)
        for key in ["executive_summary", "trend_analysis", "risk_factors", "recommended_actions"]:
            pdf.set_font("helvetica", "B", 12)
            pdf.cell(0, 8, key.replace("_", " ").title() + ":", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("helvetica", "", 10)
            
            data = insights.get(key, [])
            if isinstance(data, list):
                for item in data:
                    pdf.multi_cell(0, 5, f"- {item}", new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.multi_cell(0, 5, str(data), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
    except:
        pdf.multi_cell(0, 6, "AI Analysis Output Failed.", new_x="LMARGIN", new_y="NEXT")

    # Page 2: Visualizations
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "2. Data Visualizations", new_x="LMARGIN", new_y="NEXT")
    b1, b2 = create_charts(llm_json)
    if b1 and b2:
        # Place charts side-by-side or stacked cleanly
        pdf.image(b1, x=10, y=30, w=190)
        pdf.image(b2, x=10, y=130, w=190)
    else:
        pdf.set_font("helvetica", "", 11)
        pdf.cell(0, 10, "Not enough numeric data to plot.", new_x="LMARGIN", new_y="NEXT")

    # Page 3: Data Grid Table (Bringing it back!)
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "3. Extracted Data Grid", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    all_logs = validation_report.get("valid_records", []) + validation_report.get("anomalies", [])
    if all_logs:
        pdf.set_font("helvetica", "", 8)
        # Extract headers from the first dictionary keys
        headers = list(all_logs[0].keys())
        col_width = 190 / len(headers) if headers else 40
        
        # Header Row
        pdf.set_fill_color(200, 220, 255)
        for header in headers:
            pdf.cell(col_width, 8, str(header)[:15], border=1, fill=True, align="C")
        pdf.ln()

        # Data Rows
        for i, row in enumerate(all_logs):
            fill = (i % 2 == 0)
            pdf.set_fill_color(245, 245, 245)
            # Highlight anomalies in light red
            if "errors" in row:
                pdf.set_fill_color(255, 200, 200)
                
            for key in headers:
                val = str(row.get(key, ""))[:20]
                pdf.cell(col_width, 8, val, border=1, fill=fill, align="C")
            pdf.ln()
            
    os.makedirs("uploads", exist_ok=True)
    filepath = os.path.join("uploads", output_filename)
    pdf.output(filepath)
    return filepath