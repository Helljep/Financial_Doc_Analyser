"""Charts and the PDF report."""
import io
import json
import os

import matplotlib
matplotlib.use("Agg")                       # draw to files only, never open a window
import matplotlib.pyplot as plt
import seaborn as sns
from fpdf import FPDF

MAX_PDF_ROWS = 300
HIDDEN_COLUMNS = ("errors", "extraction_reasoning")

# fpdf's built-in fonts only handle latin-1, so swap the common symbols and replace the rest.
SYMBOLS = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
           "•": "-", "…": "...", "₹": "Rs.", "€": "EUR", " ": " ", "→": "->"}


def _safe(text) -> str:
    text = str(text)
    for symbol, plain in SYMBOLS.items():
        text = text.replace(symbol, plain)
    return text.encode("latin-1", "replace").decode("latin-1")


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


def create_charts(insights_json: str):
    """A bar chart and a donut chart as PNG buffers, or (None, None) if there's nothing to plot."""
    try:
        insights = json.loads(insights_json)
        title = insights.get("chart_title", "Key metrics")
        labels = insights.get("chart_labels", [])
        values = insights.get("chart_values", [])
        if not labels or len(labels) != len(values) or not any(v > 0 for v in values) or any(v < 0 for v in values):
            return None, None

        sns.set_theme(style="whitegrid")
        bars, donut = io.BytesIO(), io.BytesIO()

        fig, ax = plt.subplots(figsize=(8, 4))
        sns.barplot(x=values, y=labels, hue=labels, legend=False, ax=ax, palette="viridis")
        ax.set_title(title)
        fig.savefig(bars, format="png", bbox_inches="tight")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(8, 4))
        wedges, _, _ = ax.pie(values[:5], autopct="%1.1f%%", startangle=90, pctdistance=0.80)
        ax.add_artist(plt.Circle((0, 0), 0.60, fc="white"))          # the hole in the donut
        ax.legend(wedges, labels[:5], title="Items", loc="center left", bbox_to_anchor=(1, 0, 0.5, 1))
        ax.set_title(f"{title} (share)")
        fig.savefig(donut, format="png", bbox_inches="tight")
        plt.close(fig)
    except Exception:
        return None, None

    bars.seek(0)
    donut.seek(0)
    return bars, donut


def _insights_page(pdf, insights_json):
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "1. Summary and insights", new_x="LMARGIN", new_y="NEXT")
    try:
        insights = json.loads(insights_json)
    except ValueError:
        pdf.set_font("helvetica", "", 11)
        pdf.multi_cell(0, 6, "The AI analysis could not be read.", new_x="LMARGIN", new_y="NEXT")
        return

    for key in ("executive_summary", "trend_analysis", "risk_factors", "recommended_actions"):
        pdf.set_font("helvetica", "B", 12)
        pdf.cell(0, 8, _safe(key.replace("_", " ").title() + ":"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 10)
        value = insights.get(key, [])
        for item in value if isinstance(value, list) else [value]:
            line = f"- {item}" if isinstance(value, list) else item
            pdf.multi_cell(0, 5, _safe(line), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)


def _charts_page(pdf, insights_json):
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "2. Charts", new_x="LMARGIN", new_y="NEXT")
    bars, donut = create_charts(insights_json)
    if bars and donut:
        pdf.image(bars, x=10, y=30, w=190)
        pdf.image(donut, x=10, y=130, w=190)
    else:
        pdf.set_font("helvetica", "", 11)
        pdf.cell(0, 10, "Not enough numeric data to plot.", new_x="LMARGIN", new_y="NEXT")


def _data_page(pdf, report):
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "3. Extracted data", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    if report.get("document_issues"):
        pdf.set_font("helvetica", "B", 11)
        pdf.cell(0, 8, "Issues with the document as a whole:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9)
        for issue in report["document_issues"]:
            pdf.multi_cell(0, 5, _safe(f"- {issue}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    anomalies, valid = report.get("anomalies", []), report.get("valid_records", [])
    rows = (anomalies + valid)[:MAX_PDF_ROWS]          # problems first, they matter most
    if len(anomalies) + len(valid) > MAX_PDF_ROWS:
        pdf.set_font("helvetica", "I", 9)
        pdf.multi_cell(0, 5, f"Showing {MAX_PDF_ROWS} of {len(anomalies) + len(valid)} rows, all {len(anomalies)} anomalies first. "
                             "The full data is in the Tableau extract.", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    if not rows:
        return

    columns = list(dict.fromkeys(key for row in rows for key in row if key not in HIDDEN_COLUMNS))[:10]
    width = 190 / len(columns) if columns else 40

    pdf.set_font("helvetica", "", 8)
    pdf.set_fill_color(200, 220, 255)
    for name in columns:
        pdf.cell(width, 8, _safe(name)[:15], border=1, fill=True, align="C")
    pdf.ln()

    for i, row in enumerate(rows):
        flagged = "errors" in row
        pdf.set_fill_color(255, 200, 200) if flagged else pdf.set_fill_color(245, 245, 245)
        for name in columns:
            pdf.cell(width, 8, _safe(row.get(name, ""))[:20], border=1, fill=flagged or i % 2 == 0, align="C")
        pdf.ln()
        if flagged:                                    # say why the row was flagged
            pdf.set_font("helvetica", "I", 7)
            for error in row["errors"]:
                pdf.multi_cell(0, 4, _safe("   ! " + error), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("helvetica", "", 8)


def create_pdf_report(validation_report, insights_json, output_filename="validated_report.pdf"):
    pdf = PDFReport()
    pdf.set_auto_page_break(auto=True, margin=15)
    _insights_page(pdf, insights_json)
    _charts_page(pdf, insights_json)
    _data_page(pdf, validation_report)

    os.makedirs("uploads", exist_ok=True)
    path = os.path.join("uploads", output_filename)
    pdf.output(path)
    return path
