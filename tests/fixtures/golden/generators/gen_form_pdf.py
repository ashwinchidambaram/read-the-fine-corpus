"""
Generator: form_filled.pdf
§18.1 + Segment taxonomy Finding F-1 role: filled form PDF with label/value pairs.
Covers the form_field segment type - the only type not covered by the original §18.1 fixtures.
Run: uv run python tests/fixtures/golden/generators/gen_form_pdf.py
"""

from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

OUTPUT = Path(__file__).parent.parent / "corpus" / "form_filled.pdf"

# Fixed creation date — keeps /CreationDate deterministic across regenerations.
FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)

# Filled form data - deterministic, no wall-clock dependency
FORM_DATA = {
    "Employee Name": "Alex Johnson",
    "Employee ID": "EMP-00417",
    "Department": "Engineering",
    "Manager": "Dr. Sarah Kim",
    "Date of Request": "2025-08-20",
    "Leave Type": "Annual Leave",
    "Leave Start Date": "2025-09-08",
    "Leave End Date": "2025-09-12",
    "Number of Days Requested": "5",
    "Reason for Leave": "Family vacation - pre-approved by manager",
    "Emergency Contact (during leave)": "Sam Johnson - +1 555 0174",
    "Employee Signature Date": "2025-08-20",
    "Manager Approval": "Approved",
    "Manager Signature Date": "2025-08-21",
    "HR Notes": "Annual leave balance: 14 days remaining after this request.",
}


def draw_field(pdf: FPDF, label: str, value: str, y: float) -> float:
    """Draw a label-value field pair. Returns new y position."""
    pdf.set_xy(15, y)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(65, 8, label + ":", border="B")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(82)
    pdf.multi_cell(113, 8, value, border="B", new_x="LMARGIN", new_y="NEXT")
    return pdf.get_y() + 3


def build() -> None:
    pdf = FPDF()
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Header
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "ACME Corp", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Leave Request Form", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(
        0,
        6,
        "Form No. HR-F-001 | Rev. 2 | Effective 2025-01-01",
        new_x="LMARGIN",
        new_y="NEXT",
        align="C",
    )
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        0,
        5,
        "Complete all sections. Submit to your manager for approval, then to HR. "
        "Retain a copy for your records. This form must be submitted at least 5 working "
        "days before the first day of leave except in cases of emergency.",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(4)

    # Section 1
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(220, 220, 220)
    pdf.cell(0, 7, "  Section 1 - Employee Information", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    y = pdf.get_y()
    for label in [
        "Employee Name",
        "Employee ID",
        "Department",
        "Manager",
        "Date of Request",
    ]:
        y = draw_field(pdf, label, FORM_DATA[label], y)

    pdf.ln(3)

    # Section 2
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(220, 220, 220)
    pdf.cell(0, 7, "  Section 2 - Leave Details", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    y = pdf.get_y()
    for label in [
        "Leave Type",
        "Leave Start Date",
        "Leave End Date",
        "Number of Days Requested",
        "Reason for Leave",
        "Emergency Contact (during leave)",
    ]:
        y = draw_field(pdf, label, FORM_DATA[label], y)

    pdf.ln(3)

    # Section 3
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(220, 220, 220)
    pdf.cell(0, 7, "  Section 3 - Authorisation", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    y = pdf.get_y()
    for label in [
        "Employee Signature Date",
        "Manager Approval",
        "Manager Signature Date",
        "HR Notes",
    ]:
        y = draw_field(pdf, label, FORM_DATA[label], y)

    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        0,
        5,
        "For HR use only. Original to be retained in employee personnel file. "
        "Copy to payroll department. Personal data processed in accordance with GDPR "
        "and the ACME Corp Data Protection Policy (HR-POL-011).",
        new_x="LMARGIN",
        new_y="NEXT",
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUTPUT))
    print(f"Written: {OUTPUT}")


if __name__ == "__main__":
    build()
