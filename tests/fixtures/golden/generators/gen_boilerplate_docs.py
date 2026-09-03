"""
Generator: boilerplate_a.pdf, boilerplate_b.pdf
§18.1 role: two other corpus documents sharing the same legal preamble as bloated_manual.pdf.
Having 3+ documents with the identical preamble text triggers corpus-level boilerplate detection.
Run: uv run python tests/fixtures/golden/generators/gen_boilerplate_docs.py
"""

from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

# Fixed creation date — keeps /CreationDate deterministic across regenerations.
FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)

CORPUS = Path(__file__).parent.parent / "corpus"

# Exact same text as in gen_bloated_manual.py - deliberate for boilerplate detection
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


def mc(pdf: FPDF, text: str, h: float = 6) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, h, text, new_x="LMARGIN", new_y="NEXT")


def build_doc(title: str, body: str, output: Path) -> None:
    pdf = FPDF()
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 10)
    mc(pdf, LEGAL_PREAMBLE)

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Content", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    mc(pdf, body)

    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output))
    print(f"Written: {output}")


def build() -> None:
    build_doc(
        title="ACME Safety Procedures v2",
        body=(
            "This document describes mandatory safety procedures for all ACME facilities. "
            "All personnel must read and acknowledge this document before beginning work. "
            "Section 1 covers personal protective equipment requirements. "
            "Section 2 describes emergency evacuation procedures. "
            "Section 3 contains incident reporting requirements.\n\n"
            "PPE Requirements:\n"
            "- Hard hat required in all manufacturing areas.\n"
            "- Safety glasses required at all times on the production floor.\n"
            "- Steel-toed boots required in warehouse and shipping areas.\n"
            "- High-visibility vest required in vehicle traffic zones.\n\n"
            "Emergency Contacts:\n"
            "- Site safety officer: ext. 2200\n"
            "- First aid station: Building B, Room 101\n"
            "- Emergency services: 911"
        ),
        output=CORPUS / "boilerplate_a.pdf",
    )

    build_doc(
        title="ACME Quality Management Handbook v1",
        body=(
            "This handbook defines the quality management system for ACME Corp operations. "
            "It is issued under the authority of the VP of Engineering and is mandatory for "
            "all engineering, manufacturing, and procurement personnel.\n\n"
            "Section 1 - Quality Policy:\n"
            "ACME Corp is committed to delivering products that consistently meet or exceed "
            "customer requirements. The QMS is certified to ISO 9001:2015.\n\n"
            "Section 2 - Non-Conformance Procedure:\n"
            "Any non-conforming product must be tagged with a red NCR label and quarantined "
            "in the designated hold area. Complete NCR form QM-401 within 24 hours of discovery. "
            "The quality manager must approve all dispositions (rework, scrap, or use-as-is).\n\n"
            "Section 3 - Document Control:\n"
            "Controlled documents are identified by a revision number and an effective date. "
            "Uncontrolled copies are marked UNCONTROLLED. Verify that you hold the current "
            "revision before using any controlled document for work."
        ),
        output=CORPUS / "boilerplate_b.pdf",
    )


if __name__ == "__main__":
    build()
