"""
Generator: bloated_manual.pdf
§18.1 role: bloated manual with cross-references, boilerplate, mixed content types.
Contains:
  - Legal preamble (pages 1-2, verbatim in 2 other corpus docs for boilerplate detection)
  - Revision history block (page 3)
  - Prose procedures with "See section 4.2" cross-references
  - Specifications table
  - Rasterized "scanned appendix" page
Walkthrough Case 1 structure.
Run: uv run python tests/fixtures/golden/generators/gen_bloated_manual.py
"""

import io
import random
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Fixed creation date — keeps /CreationDate deterministic across regenerations.
FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)

OUTPUT = Path(__file__).parent.parent / "corpus" / "bloated_manual.pdf"

# This exact text also appears in boilerplate_a.pdf and boilerplate_b.pdf
LEGAL_PREAMBLE = (
    "CONFIDENTIAL - PROPERTY OF ACME CORP\n\n"
    "This document is the exclusive property of ACME Corp and contains confidential and "
    "proprietary information. No part of this document may be reproduced, transmitted, "
    "transcribed, stored in a retrieval system, or translated into any language or computer "
    "language, in any form or by any means, electronic, mechanical, magnetic, optical, chemical, "
    "manual, or otherwise, without the prior written permission of ACME Corp.\n\n"
    "ACME Corp makes no representations or warranties with respect to the contents hereof and "
    "specifically disclaims any implied warranties of merchantability or fitness for any "
    "particular purpose. ACME Corp reserves the right to revise this document and to make "
    "changes from time to time in the content hereof without obligation to notify any "
    "person of such revision or change.\n\n"
    "All rights reserved. Copyright 2025 ACME Corp. ACME and the ACME logo are registered "
    "trademarks of ACME Corp in the United States and other countries."
)

RNG = random.Random(99)


def mc(pdf: FPDF, text: str, h: float = 6) -> None:
    """multi_cell with left-margin reset."""
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, h, text, new_x="LMARGIN", new_y="NEXT")


def render_scanned_appendix() -> bytes:
    """Render a fake scanned appendix page as JPEG."""
    img = Image.new("L", (850, 1100), color=240)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Courier.dfont", 13)
    except Exception:
        font = ImageFont.load_default()

    lines = [
        "APPENDIX A - LEGACY PARTS LIST",
        "",
        "Part No.    Description                      Stock  Unit",
        "LG-0041     Drive Belt Assembly               12     EA",
        "LG-0042     Bearing Set (4 per kit)           8      KT",
        "LG-0043     Seal Kit (input shaft)            5      KT",
        "LG-0044     O-Ring Set (assorted)             20     KT",
        "LG-0045     Gasket Material Sheet             3      EA",
        "LG-0046     Carbon Brush Set                  10     PR",
        "LG-0047     Contact Spring Assembly           6      EA",
        "",
        "Note: Parts marked * are no longer manufactured.",
        "Contact procurement for approved substitutes.",
        "",
        "Last inventory count: 1987-03-01",
    ]

    y = 60
    for line in lines:
        draw.text((60, y), line, fill=25, font=font)
        y += 20

    # mild noise
    pixels = img.load()
    for py in range(1100):
        for px in range(850):
            delta = RNG.randint(-15, 15)
            val = max(0, min(255, pixels[px, py] + delta))  # type: ignore[index]
            pixels[px, py] = val  # type: ignore[index]
    img = img.filter(ImageFilter.GaussianBlur(radius=0.7))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


def build() -> None:
    pdf = FPDF()
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_auto_page_break(auto=True, margin=15)

    # === PAGE 1: Legal preamble (part 1) ===
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "ACME Operating Manual v4", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 10)
    mc(pdf, LEGAL_PREAMBLE[:900])

    # === PAGE 2: Legal preamble (part 2) ===
    pdf.add_page()
    pdf.set_font("Helvetica", "", 10)
    mc(pdf, LEGAL_PREAMBLE[900:])

    # === PAGE 3: Revision history ===
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Revision History", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    rev_widths = [18, 30, 142]
    rev_headers = ["Rev", "Date", "Change"]
    revisions = [
        ("4", "2025-08-15", "Added Section 6.4 on cross-references and updated Appendix A."),
        ("3", "2025-01-10", "Revised torque specifications in Section 4.2. See ECO-2025-003."),
        ("2", "2024-06-01", "Added Appendix A (Legacy Parts). Updated legal preamble."),
        ("1", "2024-01-15", "Initial release."),
    ]
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
    pdf.ln(4)

    # === PAGE 4-5: Prose procedures with cross-references ===
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "1. Introduction", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "This manual describes the installation, operation, and maintenance of the ACME Widget "
        "v4 platform. It supersedes all previous editions. For warranty terms, see Section 7.",
    )
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "2. Safety Precautions", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "Read all instructions before installation. Failure to follow these instructions may "
        "result in personal injury, property damage, or equipment failure. All electrical work "
        "must be performed by a qualified electrician in compliance with applicable codes.",
    )
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "3. Installation", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "3.1 Site Preparation", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "Ensure the mounting surface is level to within 2 mm per metre and capable of supporting "
        "the unit weight plus a 50% safety factor. Provide adequate ventilation (minimum 100 mm "
        "clearance on all sides). Install a dedicated circuit breaker per local codes.",
    )
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "3.2 Lubrication Procedure", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "Apply grease (NLGI Grade 2, Lithium complex) to all bearing surfaces before assembly. "
        "The bearing surfaces are identified by orange locating marks on the chassis. "
        "See section 4.2 for torque specifications. Torque all fasteners to the values listed "
        "in Table 4-1. Failure to follow this procedure may void warranty and cause premature "
        "bearing failure. Re-lubricate every 2,000 operating hours or annually, "
        "whichever occurs first.",
    )
    pdf.ln(4)

    # === PAGE 5-6: Specifications section ===
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "4. Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "4.1 Component Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "Table 1 lists the key component specifications for the ACME Widget v4. "
        "All tolerances are manufacturing tolerances; refer to Section 4.2 for assembly torques.",
    )
    pdf.ln(3)

    col_widths = [50, 30, 28, 22, 60]
    headers = ["Component", "Part No.", "Tolerance", "Unit", "Notes"]
    rows = [
        ["Bearing A", "PN-2041", "+/-0.05", "mm", "Deep groove ball bearing"],
        ["Bearing B", "PN-2042", "+/-0.05", "mm", "Cylindrical roller bearing"],
        ["Shaft Seal", "PN-3011", "+/-0.02", "mm", "IP65 lip seal"],
        ["Drive Gear", "PN-4017", "+/-0.10", "mm", "Module 2.5, 40 teeth"],
        ["Motor Ctrl", "PN-5001", "N/A", "N/A", "24 V DC, 5 A max"],
        ["Frame Bolt", "PN-6001", "+/-0.00", "mm", "M8 x 25, Grade 8.8"],
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
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "4.2 Assembly Torque Specifications", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "Torque specifications are provided in Newton-metres (Nm). Use a calibrated torque wrench. "
        "Apply Loctite 243 (medium strength) to all steel fasteners unless otherwise noted. "
        "Do not use impact tools for final torque.",
    )
    pdf.ln(2)

    torque_widths = [55, 30, 50, 55]
    torque_headers = ["Fastener", "Size", "Torque (Nm)", "Threadlock"]
    torque_rows = [
        ["Frame mounting bolt", "M8", "25", "Loctite 243"],
        ["Bearing retainer", "M5", "8", "None"],
        ["Cover plate screw", "M4", "3.5", "None"],
        ["Drive gear nut", "M12", "70", "Loctite 270"],
        ["Terminal block", "M3", "1.2", "None"],
    ]
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(220, 220, 220)
    pdf.set_x(pdf.l_margin)
    for i, h in enumerate(torque_headers):
        pdf.cell(torque_widths[i], 8, h, border=1, fill=True)
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    for row in torque_rows:
        pdf.set_x(pdf.l_margin)
        for i, val in enumerate(row):
            pdf.cell(torque_widths[i], 7, val, border=1)
        pdf.ln()
    pdf.ln(4)

    # More prose
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "5. Maintenance", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "5.1 Inspection Schedule", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    mc(pdf,
        "Perform the following inspections at the intervals shown. Record all inspection results "
        "in the maintenance log. Contact the service centre if any out-of-tolerance condition "
        "is found. Refer to the spare parts list in Appendix A for replacement part numbers.",
    )
    pdf.ln(2)

    # === SCANNED APPENDIX PAGE ===
    scanned_bytes = render_scanned_appendix()
    scan_buf = io.BytesIO(scanned_bytes)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(
        0, 8, "Appendix A - Legacy Parts (scanned original, 1987)",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(2)
    pdf.image(scan_buf, x=10, y=30, w=190)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUTPUT))
    print(f"Written: {OUTPUT}")


if __name__ == "__main__":
    build()
