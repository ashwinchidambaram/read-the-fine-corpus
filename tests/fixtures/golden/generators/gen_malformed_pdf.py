"""
Generator: malformed_structure.pdf
§18.1 + Segment taxonomy Finding F-2 role: PDF with a deliberately corrupted content stream
that still partially parses - provides a cleaner test for the `unknown` segment type
and for parser robustness.

Strategy:
  1. Build a valid PDF using fpdf2.
  2. Read the raw bytes back.
  3. Locate the FlateDecode content stream for page 2 by finding the second decompressible
     stream and injecting garbage inside it so the decompressor itself fails.
     - This is stronger than corrupting a page-dictionary object: xref-following parsers
       cannot sidestep it because the filter itself breaks.
  4. Write the mutated bytes as the output.

Result: a PDF that readers can partially open (page 1 content stream is valid and page 1
displays normally) but page 2's FlateDecode stream is broken, triggering parse errors
on that page.

Run: uv run python tests/fixtures/golden/generators/gen_malformed_pdf.py
"""

import zlib
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

OUTPUT = Path(__file__).parent.parent / "corpus" / "malformed_structure.pdf"

# Fixed creation date — keeps /CreationDate deterministic across regenerations.
FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def build() -> None:
    # Step 1: build a clean PDF with two pages
    pdf = FPDF()
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Incident Report - INC-2025-0042", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        0,
        6,
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
        new_x="LMARGIN",
        new_y="NEXT",
    )

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Follow-up Actions - INC-2025-0042", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        0,
        6,
        "Action review date: 2025-08-20\n"
        "All corrective actions from the initial report have been completed. "
        "The site traffic management plan has been updated and approved by the "
        "Safety Manager. This incident is now closed.\n\n"
        "Lessons learned:\n"
        "- Floor markings should be inspected monthly and refreshed when faded.\n"
        "- Forklift lanes that cross pedestrian routes should have active audible warnings.\n"
        "- Near-miss reporting culture is working - employee reported promptly.\n\n"
        "Signed off: Site Safety Manager - 2025-08-20",
        new_x="LMARGIN",
        new_y="NEXT",
    )

    # Get the PDF as bytes (before writing to disk)
    raw: bytes = bytes(pdf.output())

    # Step 2: locate and corrupt the FlateDecode content stream of page 2.
    # We scan for all "stream\n" markers and verify each by attempting zlib decompress.
    # The FIRST decompressible stream = page 1 content (keep intact).
    # The SECOND decompressible stream = page 2 content (corrupt its body).

    stream_marker = b"stream\n"
    decompressible_found = 0
    corruption_applied = False
    idx = 0

    while idx < len(raw):
        pos = raw.find(stream_marker, idx)
        if pos < 0:
            break
        stream_start = pos + len(stream_marker)
        stream_end = raw.find(b"endstream", stream_start)
        if stream_end < 0:
            idx = pos + 1
            continue

        content = raw[stream_start:stream_end]
        try:
            zlib.decompress(content)
            decompressible_found += 1
        except Exception:
            idx = pos + 1
            continue

        if decompressible_found == 2:
            # This is page 2's content stream — inject garbage mid-body.
            # We place garbage at offset +8 inside the compressed data to break the
            # zlib header/body so the decompressor itself raises an error.
            corrupt_offset = stream_start + 8
            corrupt_end = corrupt_offset + 32
            if corrupt_end <= stream_end:
                garbage = b"\x00\xff\xfe\xfd\xfc\xfb\xfa\xf9" * 4  # 32 bytes
                raw = raw[:corrupt_offset] + garbage + raw[corrupt_end:]
                corruption_applied = True
            break

        idx = pos + 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(raw)

    if corruption_applied:
        print(f"Written: {OUTPUT}  (FlateDecode content stream corruption applied to page 2 body)")
    else:
        print(
            f"Written: {OUTPUT}  "
            "(WARNING: stream corruption could not be applied - fallback to valid PDF)"
        )


if __name__ == "__main__":
    build()
