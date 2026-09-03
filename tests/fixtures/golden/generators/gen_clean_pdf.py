"""
Generator: clean_native.pdf
§18.1 role: clean native-text PDF - headings, prose, simple table, list.
Reproducible: no wall-clock-dependent content, fixed seed not needed (deterministic layout).
Run: uv run python tests/fixtures/golden/generators/gen_clean_pdf.py
"""

from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

OUTPUT = Path(__file__).parent.parent / "corpus" / "clean_native.pdf"

# Fixed creation date — keeps /CreationDate deterministic across regenerations.
FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def mc(pdf: FPDF, text: str, h: float = 6) -> None:
    """multi_cell that resets x to left margin before and after."""
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, h, text, new_x="LMARGIN", new_y="NEXT")


def build() -> None:
    pdf = FPDF()
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # --- Heading 1 ---
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "ACME Widget Installation Guide", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # --- Front matter / intro prose ---
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "This guide describes the installation and initial configuration of the ACME Widget "
        "v3.2. Follow all steps in order. Read Section 4 before applying power. "
        "Contact support@acme.example if you encounter issues not covered here."
    )
    pdf.ln(4)

    # --- Heading 2: Prerequisites ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "1. Prerequisites", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 11)
    mc(pdf, "Before beginning, ensure all of the following conditions are met:")
    pdf.ln(2)

    # --- List ---
    items = [
        "Power supply: 24 V DC, minimum 2 A.",
        "Mounting surface rated for 5 kg static load.",
        "Ambient temperature between 0 degC and 50 degC.",
        "Firmware package ACME-FW-3.2.tar.gz available on local filesystem.",
        "Network switch port with 100 Mbit/s or higher.",
    ]
    for item in items:
        mc(pdf, "  - " + item)
    pdf.ln(4)

    # --- Heading 2: Installation Procedure ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "2. Installation Procedure", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "2.1 Mechanical Mounting", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "Attach the mounting bracket to the DIN rail using the four M4 bolts supplied. "
        "Torque each bolt to 2.5 Nm. Slide the Widget chassis onto the bracket until the "
        "locking tab engages. Apply grease (NLGI Grade 2) to all exposed bearing surfaces "
        "before securing the cover plate. See Section 4.2 for torque specifications."
    )
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "2.2 Electrical Connections", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "Connect the 24 V DC supply to terminal block TB1. Polarity is marked on the chassis "
        "silkscreen. Do NOT reverse polarity; doing so will void the warranty and may damage "
        "internal protection circuitry. The earth bonding point (green/yellow lug) MUST be "
        "connected to the enclosure protective earth."
    )
    pdf.ln(4)

    # --- Heading 2: Specifications table ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "3. Component Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 11)
    mc(pdf, "Table 1 lists the key component specifications for the ACME Widget v3.2.")
    pdf.ln(3)

    # Table
    col_widths = [55, 35, 30, 30, 30]
    headers = ["Component", "Part No.", "Tolerance", "Unit", "Rating"]
    rows = [
        ["Bearing A", "PN-2041", "+/-0.05", "mm", "5 kN"],
        ["Bearing B", "PN-2042", "+/-0.05", "mm", "8 kN"],
        ["Shaft Seal", "PN-3011", "+/-0.02", "mm", "IP65"],
        ["Drive Gear", "PN-4017", "+/-0.10", "mm", "50 Nm"],
        ["Motor Ctrl", "PN-5001", "N/A", "N/A", "24 V DC"],
    ]

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(220, 220, 220)
    pdf.set_x(pdf.l_margin)
    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], 8, h, border=1, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 10)
    for row in rows:
        pdf.set_x(pdf.l_margin)
        for i, val in enumerate(row):
            pdf.cell(col_widths[i], 7, val, border=1)
        pdf.ln()

    pdf.ln(4)

    # --- Heading 2: Revision history ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "4. Revision History", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    revisions = [
        ("v3.2", "2025-08-01", "Added Section 2.2 electrical notes."),
        ("v3.1", "2025-03-15", "Corrected torque value in 2.1."),
        ("v3.0", "2024-11-01", "Initial release of v3 platform."),
    ]
    rev_widths = [18, 30, 142]
    rev_headers = ["Rev", "Date", "Change"]
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(220, 220, 220)
    pdf.set_x(pdf.l_margin)
    for i, h in enumerate(rev_headers):
        pdf.cell(rev_widths[i], 8, h, border=1, fill=True)
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    for rev, date, note in revisions:
        pdf.set_x(pdf.l_margin)
        pdf.cell(rev_widths[0], 7, rev, border=1)
        pdf.cell(rev_widths[1], 7, date, border=1)
        pdf.cell(rev_widths[2], 7, note, border=1)
        pdf.ln()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUTPUT))
    print(f"Written: {OUTPUT}")


if __name__ == "__main__":
    build()
