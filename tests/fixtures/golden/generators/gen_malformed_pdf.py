"""
Generator: malformed_structure.pdf
§18.1 + Segment taxonomy Finding F-2 role: PDF with a deliberately corrupted object stream
that still partially parses - provides a cleaner test for the `unknown` segment type
and for parser robustness.

Strategy:
  1. Build a valid PDF using fpdf2.
  2. Read the raw bytes back.
  3. Locate the object stream for a content object and corrupt part of it:
     - Replace a valid stream operator sequence with garbage bytes.
     - Leave the cross-reference table intact so the reader can still open the file.
  4. Write the mutated bytes as the output.

Result: a PDF that most readers will partially open (the uncorrupted pages display normally)
but that has an invalid content stream on one page, triggering parse errors.

Run: uv run python tests/fixtures/golden/generators/gen_malformed_pdf.py
"""

from pathlib import Path

from fpdf import FPDF

OUTPUT = Path(__file__).parent.parent / "corpus" / "malformed_structure.pdf"


def build() -> None:
    # Step 1: build a clean PDF with two pages
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Incident Report - INC-2025-0042", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        0, 6,
        "Incident date: 2025-07-14\n"
        "Reported by: J. Martinez\n"
        "Location: Riverside Facility, Bay 4\n"
        "Classification: Near-miss\n\n"
        "Description:\n"
        "A forklift operator failed to observe a stop marking on the floor and entered "
        "the pedestrian crossing zone while a maintenance technician was present. No contact "
        "occurred. The technician was alerted by a colleague. The area has been re-marked "
        "with high-visibility paint and a warning buzzer has been installed on the forklift "
        "approach lane.\n\n"
        "Corrective actions:\n"
        "1. Re-mark floor crossing zone (completed 2025-07-15).\n"
        "2. Install audible alert on forklift lane approach (completed 2025-07-18).\n"
        "3. Mandatory forklift safety refresher training for all operators (due 2025-08-01).\n"
        "4. Review site traffic management plan (due 2025-08-15).",
        new_x="LMARGIN", new_y="NEXT",
    )

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Follow-up Actions - INC-2025-0042", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        0, 6,
        "Action review date: 2025-08-20\n"
        "All corrective actions from the initial report have been completed. "
        "The site traffic management plan has been updated and approved by the "
        "Safety Manager. This incident is now closed.\n\n"
        "Lessons learned:\n"
        "- Floor markings should be inspected monthly and refreshed when faded.\n"
        "- Forklift lanes that cross pedestrian routes should have active audible warnings.\n"
        "- Near-miss reporting culture is working - employee reported promptly.\n\n"
        "Signed off: Site Safety Manager - 2025-08-20",
        new_x="LMARGIN", new_y="NEXT",
    )

    # Get the PDF as bytes (before writing to disk)
    raw: bytes = bytes(pdf.output())

    # Step 2: corrupt the content stream of the second page
    # We look for the second occurrence of "stream\r\n" or "stream\n" - this is the
    # content stream for page 2. We corrupt a few bytes inside it.
    # This makes the stream parser fail on that object while leaving the xref intact.

    marker = b"stream\n"
    first_idx = raw.find(marker)
    second_idx = raw.find(marker, first_idx + 1) if first_idx >= 0 else -1

    if second_idx >= 0:
        # Replace 32 bytes at offset +20 inside the stream with garbage
        corrupt_start = second_idx + len(marker) + 20
        corrupt_end = corrupt_start + 32
        if corrupt_end < len(raw):
            garbage = b"\x00\xff\xfe\xfd\xfc\xfb\xfa\xf9" * 4  # 32 bytes of garbage
            raw = raw[:corrupt_start] + garbage + raw[corrupt_end:]
            corruption_applied = True
        else:
            corruption_applied = False
    else:
        corruption_applied = False

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(raw)

    if corruption_applied:
        print(f"Written: {OUTPUT}  (stream corruption applied at byte offset ~{second_idx})")
    else:
        print(
            f"Written: {OUTPUT}  "
            "(WARNING: stream corruption could not be applied - fallback to valid PDF)"
        )


if __name__ == "__main__":
    build()
