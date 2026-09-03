"""
Generator: policy_v1.pdf, policy_v2.pdf, policy_v3.pdf
§18.1 role: near-duplicate family - three versions of one policy doc.
Small edits between versions; newest (v3) is identifiable by content and title.
Walkthrough Case 5.
Run: uv run python tests/fixtures/golden/generators/gen_near_duplicates.py
"""

from pathlib import Path

from fpdf import FPDF

CORPUS = Path(__file__).parent.parent / "corpus"


POLICY_COMMON = (
    "This policy governs employee leave entitlements at ACME Corp. It applies to all "
    "full-time and part-time employees employed in a permanent capacity. Contractors and "
    "fixed-term employees should refer to their individual employment agreements.\n\n"
    "Annual Leave\n\n"
    "Full-time employees are entitled to 20 days of annual leave per year, accrued on a "
    "pro-rata basis from the commencement date. Annual leave must be taken within 12 months "
    "of accrual. Carry-forward of up to 5 days is permitted with manager approval.\n\n"
    "Sick Leave\n\n"
    "Employees are entitled to 10 days of paid sick leave per year. A medical certificate "
    "is required for absences exceeding 3 consecutive days. Unused sick leave does not "
    "accrue from year to year.\n\n"
    "Parental Leave\n\n"
    "Primary carers are entitled to 16 weeks of paid parental leave. Secondary carers are "
    "entitled to 2 weeks of paid parental leave. Leave may be taken in a single continuous "
    "block or in two separate blocks by agreement with the manager.\n\n"
)

V1_SPECIFIC = (
    "This is the initial version of the ACME Leave Policy (Document no. HR-POL-003, Rev. 1, "
    "Effective 2024-01-10).\n\n"
    "Public Holidays\n\n"
    "Employees are entitled to all public holidays observed in their jurisdiction. "
    "Where an employee is required to work on a public holiday, a substitute day off "
    "will be provided within 30 days.\n\n"
    "Contact HR at hr@acme.example for queries relating to this policy."
)

V2_SPECIFIC = (
    "This is version 2 of the ACME Leave Policy (Document no. HR-POL-003, Rev. 2, "
    "Effective 2025-03-15). Changes from Rev. 1: parental leave increased from 12 to 16 weeks "
    "for primary carers; sick leave certificate requirement reduced from 5 to 3 days.\n\n"
    "Public Holidays\n\n"
    "Employees are entitled to all public holidays observed in their jurisdiction. "
    "Where an employee is required to work on a public holiday, a substitute day off "
    "will be provided within 30 days.\n\n"
    "Bereavement Leave\n\n"
    "Employees are entitled to 3 days of paid bereavement leave for the death of an "
    "immediate family member. Immediate family is defined as spouse, child, parent, or sibling.\n\n"
    "Contact HR at hr@acme.example for queries relating to this policy."
)

V3_SPECIFIC = (
    "This is version 3 of the ACME Leave Policy (Document no. HR-POL-003, Rev. 3, "
    "Effective 2026-07-01). Changes from Rev. 2: annual leave increased from 20 to 22 days "
    "for employees with 5+ years service; secondary carer parental leave increased from 2 to "
    "4 weeks; mental health leave provision added (5 days per year, no certificate required).\n\n"
    "Public Holidays\n\n"
    "Employees are entitled to all public holidays observed in their jurisdiction. "
    "Where an employee is required to work on a public holiday, a substitute day off "
    "will be provided within 30 days.\n\n"
    "Bereavement Leave\n\n"
    "Employees are entitled to 3 days of paid bereavement leave for the death of an "
    "immediate family member, and 1 day for an extended family member. "
    "Immediate family is defined as spouse, child, parent, or sibling. "
    "Extended family includes grandparents, aunts, uncles, and in-laws.\n\n"
    "Mental Health Leave\n\n"
    "Employees are entitled to 5 days of mental health leave per year. No medical "
    "certificate is required. Mental health leave is distinct from sick leave and "
    "does not affect the sick leave balance.\n\n"
    "Contact HR at hr@acme.example for queries relating to this policy. "
    "This version supersedes all previous editions."
)


def mc(pdf: FPDF, text: str, h: float = 6) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, h, text, new_x="LMARGIN", new_y="NEXT")


def build_version(
    title: str,
    subtitle: str,
    effective: str,
    body: str,
    output: Path,
) -> None:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 7, subtitle, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Effective date: {effective}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 11)
    mc(pdf, body)

    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output))
    print(f"Written: {output}")


def build() -> None:
    build_version(
        title="ACME Leave Policy",
        subtitle="Document no. HR-POL-003, Rev. 1",
        effective="2024-01-10",
        body=POLICY_COMMON + V1_SPECIFIC,
        output=CORPUS / "policy_v1.pdf",
    )
    build_version(
        title="ACME Leave Policy",
        subtitle="Document no. HR-POL-003, Rev. 2",
        effective="2025-03-15",
        body=POLICY_COMMON + V2_SPECIFIC,
        output=CORPUS / "policy_v2.pdf",
    )
    build_version(
        title="ACME Leave Policy",
        subtitle="Document no. HR-POL-003, Rev. 3",
        effective="2026-07-01",
        body=POLICY_COMMON + V3_SPECIFIC,
        output=CORPUS / "policy_v3.pdf",
    )


if __name__ == "__main__":
    build()
