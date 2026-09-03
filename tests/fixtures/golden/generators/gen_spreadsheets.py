"""
Generator: report_spreadsheet.xlsx, database_spreadsheet.xlsx, model_spreadsheet.xlsx
§18.1 role: three spreadsheet kinds — report, database, model.
Walkthrough Case 3 — all three triage paths.
Run: uv run python tests/fixtures/golden/generators/gen_spreadsheets.py
"""

from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

CORPUS = Path(__file__).parent.parent / "corpus"


# ──────────────────────────────────────────────────────────────────────────────
# Report spreadsheet: formatted sheets, commentary, summary tables
# ──────────────────────────────────────────────────────────────────────────────

def build_report() -> None:
    wb = openpyxl.Workbook()

    # Sheet 1: Executive Summary
    ws1 = wb.active
    ws1.title = "Executive Summary"

    ws1["A1"] = "Q4 2025 Revenue Report"
    ws1["A1"].font = Font(bold=True, size=16)
    ws1["A2"] = "Prepared by: Finance Team"
    ws1["A3"] = "Period: October 1 – December 31, 2025"
    ws1["A4"] = "Classification: Internal Use Only"
    ws1.row_dimensions[1].height = 24

    ws1["A6"] = "Executive Summary"
    ws1["A6"].font = Font(bold=True, size=13)
    ws1["A7"] = (
        "Q4 2025 revenue of $12.4M exceeded target by 8.3%. All three product lines "
        "grew year-over-year. The Widget line showed the strongest growth at +23% vs Q4 2024. "
        "Operating margin improved to 18.2% from 15.7% in Q3 2025, driven by manufacturing "
        "efficiency gains in the Riverside facility."
    )
    ws1["A7"].alignment = Alignment(wrap_text=True)
    ws1.row_dimensions[7].height = 72
    ws1.column_dimensions["A"].width = 90

    ws1["A9"] = "Key Metrics"
    ws1["A9"].font = Font(bold=True)
    metrics = [
        ("Metric", "Q4 2025", "Q4 2024", "Change"),
        ("Total Revenue ($M)", "12.4", "11.1", "+11.7%"),
        ("Gross Margin", "38.5%", "36.2%", "+2.3pp"),
        ("Operating Margin", "18.2%", "16.8%", "+1.4pp"),
        ("Units Shipped", "48,210", "43,890", "+9.8%"),
    ]
    header_fill = PatternFill("solid", fgColor="DDDDDD")
    for r_idx, row in enumerate(metrics, start=10):
        for c_idx, val in enumerate(row, start=1):
            cell = ws1.cell(row=r_idx, column=c_idx, value=val)
            if r_idx == 10:
                cell.font = Font(bold=True)
                cell.fill = header_fill

    # Sheet 2: Revenue Analysis
    ws2 = wb.create_sheet("Revenue Analysis")
    ws2["A1"] = "Revenue Analysis — Q4 2025"
    ws2["A1"].font = Font(bold=True, size=13)
    ws2["A3"] = (
        "Revenue by product line is shown in Table 1. The Widget line benefited from the "
        "launch of the Widget Pro SKU in October 2025, which captured premium market share "
        "in the industrial automation segment. Gadget revenue was flat due to component "
        "supply constraints in November; these constraints were resolved in December."
    )
    ws2["A3"].alignment = Alignment(wrap_text=True)
    ws2.row_dimensions[3].height = 60
    ws2.column_dimensions["A"].width = 80

    ws2["A5"] = "Table 1: Revenue by Product Line ($000)"
    ws2["A5"].font = Font(bold=True)
    revenue_data = [
        ("Product Line", "Oct 2025", "Nov 2025", "Dec 2025", "Q4 Total", "Q4 2024", "YoY%"),
        ("Widget", "1,820", "2,140", "2,380", "6,340", "5,150", "+23.1%"),
        ("Gadget", "1,240", "1,050", "1,310", "3,600", "3,580", "+0.6%"),
        ("Service", "820", "840", "800", "2,460", "2,370", "+3.8%"),
        ("Total", "3,880", "4,030", "4,490", "12,400", "11,100", "+11.7%"),
    ]
    header_fill2 = PatternFill("solid", fgColor="CCDDEE")
    for r_idx, row in enumerate(revenue_data, start=6):
        for c_idx, val in enumerate(row, start=1):
            cell = ws2.cell(row=r_idx, column=c_idx, value=val)
            if r_idx == 6:
                cell.font = Font(bold=True)
                cell.fill = header_fill2
            if r_idx == 10:  # Total row
                cell.font = Font(bold=True)
    for col in range(1, 8):
        ws2.column_dimensions[get_column_letter(col)].width = 14

    ws2["A12"] = (
        "Note: Service revenue includes maintenance contracts and professional services. "
        "The Q4 2024 comparison figures have been restated to reflect the reclassification "
        "of professional services from the Widget line to the Service line."
    )
    ws2["A12"].alignment = Alignment(wrap_text=True)
    ws2.row_dimensions[12].height = 48
    ws2.column_dimensions["A"].width = 80

    # Sheet 3: Commentary
    ws3 = wb.create_sheet("Commentary")
    ws3["A1"] = "Analyst Commentary — Q4 2025"
    ws3["A1"].font = Font(bold=True, size=13)
    ws3["A3"] = "Outlook"
    ws3["A3"].font = Font(bold=True)
    ws3["A4"] = (
        "Management reaffirms full-year 2026 revenue guidance of $50–52M. The order book "
        "as of December 31, 2025 stands at $18.7M, up 15% year-over-year. Key growth drivers "
        "in 2026 include the North American Widget Pro expansion and the new service contract "
        "with Meridian Manufacturing."
    )
    ws3["A4"].alignment = Alignment(wrap_text=True)
    ws3.row_dimensions[4].height = 60
    ws3.column_dimensions["A"].width = 90

    ws3["A6"] = "Risks"
    ws3["A6"].font = Font(bold=True)
    risks = [
        "1. Component supply: Lead times for PN-5001 remain extended at 18 weeks. Mitigation: "
        "safety stock increased to 120 days.",
        "2. FX exposure: 35% of Widget revenue is EUR-denominated. A 10% EUR/USD movement "
        "impacts revenue by approximately $0.4M per quarter.",
        "3. Competitive pricing: A new competitor entered the Widget market in Q3 2025. "
        "No material pricing impact observed in Q4; monitor in 2026.",
    ]
    for i, risk in enumerate(risks, start=7):
        ws3[f"A{i}"] = risk
        ws3[f"A{i}"].alignment = Alignment(wrap_text=True)
        ws3.row_dimensions[i].height = 40
    ws3.column_dimensions["A"].width = 90

    out = CORPUS / "report_spreadsheet.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    print(f"Written: {out}")


# ──────────────────────────────────────────────────────────────────────────────
# Database spreadsheet: 500+ uniform rows, row-per-record
# ──────────────────────────────────────────────────────────────────────────────

def build_database() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Customers"

    headers = [
        "customer_id", "company_name", "contact_name", "contact_email",
        "country", "region", "segment", "annual_revenue_usd", "employee_count",
        "contract_start", "contract_end", "status",
    ]
    header_fill = PatternFill("solid", fgColor="DDDDDD")
    for c_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c_idx, value=h)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        ws.column_dimensions[get_column_letter(c_idx)].width = 18

    # Deterministic data generation — no random seed needed, pure formula
    countries = ["US", "CA", "GB", "DE", "FR", "JP", "AU", "NL", "SE", "CH"]
    regions = ["NA", "NA", "EMEA", "EMEA", "EMEA", "APAC", "APAC", "EMEA", "EMEA", "EMEA"]
    segments = ["Enterprise", "Mid-Market", "SMB"]

    for i in range(1, 502):  # 501 data rows — well over 500
        country_idx = (i - 1) % len(countries)
        seg_idx = (i - 1) % len(segments)
        rev = 50_000 + (i * 3_700) % 9_500_000
        emp = 10 + (i * 17) % 4990
        start_year = 2020 + (i % 5)
        end_year = start_year + 3
        status = "Active" if (i % 7 != 0) else "Churned"
        row = [
            f"CUST-{i:05d}",
            f"Company {i} {countries[country_idx]}",
            f"Contact {i}",
            f"contact{i}@company{i}.example",
            countries[country_idx],
            regions[country_idx],
            segments[seg_idx],
            rev,
            emp,
            f"{start_year}-01-01",
            f"{end_year}-12-31",
            status,
        ]
        for c_idx, val in enumerate(row, start=1):
            ws.cell(row=i + 1, column=c_idx, value=val)

    out = CORPUS / "database_spreadsheet.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    print(f"Written: {out}")


# ──────────────────────────────────────────────────────────────────────────────
# Model spreadsheet: formula-dense amortization model
# ──────────────────────────────────────────────────────────────────────────────

def build_model() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Amortization"

    ws["A1"] = "Loan Amortization Model"
    ws["A1"].font = Font(bold=True, size=14)

    # Input parameters
    ws["A3"] = "Inputs"
    ws["A3"].font = Font(bold=True)
    inputs = [
        ("Principal ($)", 500_000),
        ("Annual Rate (%)", 6.5),
        ("Term (months)", 120),
        ("Start Month", 1),
        ("Start Year", 2025),
    ]
    for i, (label, val) in enumerate(inputs, start=4):
        ws[f"A{i}"] = label
        ws[f"B{i}"] = val
    ws["A9"] = "Monthly Rate"
    ws["B9"] = "=B5/100/12"  # formula referencing inputs
    ws["A10"] = "Monthly Payment ($)"
    ws["B10"] = "=B4*(B9*(1+B9)^B6)/((1+B9)^B6-1)"

    ws["A12"] = "Note: All payment values in rows 15+ are formula-derived from cells B4:B10."
    ws["A12"].font = Font(italic=True)

    # Amortization schedule header
    headers = ["Month", "Payment ($)", "Principal ($)", "Interest ($)", "Balance ($)"]
    header_fill = PatternFill("solid", fgColor="DDDDDD")
    for c_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=14, column=c_idx, value=h)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        ws.column_dimensions[get_column_letter(c_idx)].width = 16

    # 120 months of formula-driven amortization
    for month in range(1, 121):
        row = 14 + month
        if month == 1:
            balance_prev = "$B$4"  # initial principal
        else:
            balance_prev = f"E{row - 1}"

        ws.cell(row=row, column=1, value=month)  # month number — literal
        ws.cell(row=row, column=2, value="=$B$10")  # payment — formula
        ws.cell(row=row, column=4, value=f"={balance_prev}*$B$9")  # interest — formula
        ws.cell(row=row, column=3, value=f"=$B$10-D{row}")  # principal — formula
        ws.cell(row=row, column=5, value=f"={balance_prev}-C{row}")  # balance — formula

    ws["A136"] = "Summary"
    ws["A136"].font = Font(bold=True)
    ws["A137"] = "Total Payments ($)"
    ws["B137"] = f"=SUM(B15:B{14+120})"
    ws["A138"] = "Total Interest ($)"
    ws["B138"] = f"=SUM(D15:D{14+120})"
    ws["A139"] = "Total Principal ($)"
    ws["B139"] = f"=SUM(C15:C{14+120})"

    out = CORPUS / "model_spreadsheet.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    print(f"Written: {out}")


def build() -> None:
    build_report()
    build_database()
    build_model()


if __name__ == "__main__":
    build()
