"""
Generator: scanned_poor.pdf
§18.1 role: poorly scanned PDF - rasterized pages with noise/blur.
Page 3 is heavily degraded (OCR confidence ~0.42), other pages mild (0.88-0.94).
Walkthrough Case 2 OCR profile.

Method: render text onto images with Pillow, apply noise/blur, embed as PDF pages.
Reproducible: fixed random seed, no wall-clock content.
Run: uv run python tests/fixtures/golden/generators/gen_scanned_pdf.py
"""

import io
import random
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUTPUT = Path(__file__).parent.parent / "corpus" / "scanned_poor.pdf"

# Fixed seed - deterministic output
RNG = random.Random(42)

PAGE_W_PX = 850
PAGE_H_PX = 1100

# Page content: (page_number, text, degraded)
# Text strings are multi-line image content, not PDF text - line length not a concern.
PAGE_CONTENT = [
    (
        1,
        (
            "Specification Sheet -- Unit Model 77\n\n"
            "Manufacturer: Legacy Industrial Co.\n"
            "Date: 1987-04-12\n\n"
            "This document contains original factory specifications for Unit Model 77.\n"
            "All measurements are in imperial units unless otherwise noted.\n"
            "Refer to Appendix B for SI conversion factors."
        ),
        False,
    ),
    (
        2,
        (
            "Section 1 -- Mechanical Specifications\n\n"
            "Frame material: Carbon steel, ASTM A36\n"
            "Overall dimensions: 24 in x 18 in x 12 in\n"
            "Weight (unloaded): 185 lb\n"
            "Mounting: Four 1/2-inch bolt holes on 16 in x 12 in pattern\n"
            "Maximum static load: 2,500 lb\n"
            "Dynamic load rating: 1,800 lb at 60 RPM"
        ),
        False,
    ),
    (
        3,
        (
            # Heavily degraded - garbled OCR output simulated in text
            "Sp3c1f1cat10n$ f0r Un1t M0d31 77\n\n"
            "Sect10n 2 -- E1ectr1ca1 Spec1f1cat10n$\n\n"
            "Vo1tage rat1ng: 460 V AC, 3-pha$e\n"
            "Frequency: 60 Hz\n"
            "Fu11-1oad current: 24.6 A\n"
            "Power factor: 0.87\n"
            "Eff1c1ency at fu11 1oad: 91.3%"
        ),
        True,  # heavily degraded
    ),
    (
        4,
        (
            "Section 3 -- Operating Conditions\n\n"
            "Ambient temperature: 32 F to 104 F (0 C to 40 C)\n"
            "Relative humidity: 10% to 90% non-condensing\n"
            "Altitude: Up to 3,300 ft (1,000 m) without derating\n"
            "Enclosure class: NEMA 12 (IP54 equivalent)\n"
            "Cooling: Forced air, internal fan"
        ),
        False,
    ),
    (
        5,
        (
            "Section 4 -- Maintenance Schedule\n\n"
            "Monthly:\n"
            "  - Inspect all electrical connections for corrosion\n"
            "  - Check cooling fan operation\n"
            "  - Verify input voltage within +/-5% of rating\n\n"
            "Annually:\n"
            "  - Replace cooling fan filter (Part No. LG-F001)\n"
            "  - Inspect and re-torque all bus connections\n"
            "  - Perform insulation resistance test (>1 MOhm at 500 V DC)"
        ),
        False,
    ),
    (
        6,
        (
            "Section 5 -- Fault Codes\n\n"
            "Code F01: Overcurrent - reduce load or check motor winding\n"
            "Code F02: Overvoltage - check supply voltage regulation\n"
            "Code F03: Undervoltage - check supply breaker and wiring\n"
            "Code F04: Overtemperature - check ambient conditions and cooling\n"
            "Code F07: Ground fault - inspect wiring insulation\n"
            "Code F09: Communication loss - check control wiring"
        ),
        False,
    ),
    (
        7,
        (
            "Section 6 -- Spare Parts List\n\n"
            "Part No.    Description                   Qty\n"
            "LG-0041     Drive Belt Assembly               1\n"
            "LG-0042     Bearing Set (4 per kit)           2\n"
            "LG-0043     Seal Kit (input shaft)            1\n"
            "LG-F001     Cooling Fan Filter                2\n"
            "LG-C001     Control Board                     1\n"
            "LG-P001     Power Supply Module 24V           1"
        ),
        False,
    ),
    (
        8,
        (
            "Section 7 -- Wiring Diagram Notes\n\n"
            "All field wiring must be copper conductor rated for 75 C minimum.\n"
            "Use wire gauge per NEC Table 310.15(B)(16) based on calculated ampacity.\n"
            "Maintain minimum 1-inch separation between power and control wiring.\n"
            "Shield control cables and ground shield at one end only (drive end).\n"
            "Maximum control cable run: 100 feet without signal repeater."
        ),
        False,
    ),
    (
        9,
        (
            "Section 8 -- Warranty and Service\n\n"
            "Warranty period: 24 months from date of shipment.\n"
            "Warranty covers defects in materials and workmanship under normal use.\n"
            "Warranty void if operated outside specified conditions or improperly installed.\n\n"
            "For service, contact:\n"
            "Legacy Industrial Service Center\n"
            "Phone: 1-800-555-0199\n"
            "Hours: 8 AM to 5 PM Eastern, Monday to Friday"
        ),
        False,
    ),
    (
        10,
        (
            "Appendix A -- Agency Certifications\n\n"
            "UL Listed: File No. E123456, Category QCZZ\n"
            "CSA Certified: File No. 123456, Class 3211 06\n"
            "CE Marked: Directive 2014/35/EU (LVD), 2014/30/EU (EMC)\n"
            "RoHS Compliant: Directive 2011/65/EU\n\n"
            "All certifications apply to standard configurations only.\n"
            "Consult factory for modified or special configurations."
        ),
        False,
    ),
    (
        11,
        (
            "Appendix B -- SI Conversion Factors\n\n"
            "1 inch = 25.4 mm\n"
            "1 foot = 0.3048 m\n"
            "1 pound (mass) = 0.4536 kg\n"
            "1 pound-force = 4.448 N\n"
            "1 ft-lb = 1.356 N-m\n"
            "1 HP = 0.7457 kW\n"
            "1 BTU/hr = 0.2931 W\n"
            "F to C: (F - 32) x 5/9"
        ),
        False,
    ),
    (
        12,
        (
            "Appendix C -- Ordering Information\n\n"
            "Base model: LG-M77-460-3P\n"
            "Options:\n"
            "  -A1  Extended temperature (-20 C to 60 C)\n"
            "  -A2  NEMA 4X enclosure (IP66)\n"
            "  -B1  Modbus RTU communication card\n"
            "  -B2  Ethernet/IP adapter\n"
            "  -C1  Conformal coating (IEC 60721-3-3, Class 3C2)\n\n"
            "Example: LG-M77-460-3P-A2-B1 = NEMA 4X with Modbus"
        ),
        False,
    ),
]


def render_page(text: str, degraded: bool) -> bytes:
    """Render text as a grayscale image simulating a scanned page."""
    img = Image.new("L", (PAGE_W_PX, PAGE_H_PX), color=245)
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Courier.dfont", 14)
    except Exception:
        font = ImageFont.load_default()

    margin = 60
    y = margin
    line_height = 18

    for line in text.split("\n"):
        if y + line_height > PAGE_H_PX - margin:
            break
        draw.text((margin, y), line, fill=20, font=font)
        y += line_height

    if degraded:
        noise_level = 80
        pixels = img.load()
        for py in range(PAGE_H_PX):
            for px in range(PAGE_W_PX):
                delta = RNG.randint(-noise_level, noise_level)
                val = max(0, min(255, pixels[px, py] + delta))  # type: ignore[index]
                pixels[px, py] = val  # type: ignore[index]
        img = img.filter(ImageFilter.GaussianBlur(radius=2.5))
        for _ in range(200):
            bx = RNG.randint(0, PAGE_W_PX - 20)
            by = RNG.randint(0, PAGE_H_PX - 20)
            bw = RNG.randint(5, 25)
            bh = RNG.randint(3, 12)
            fill_val = RNG.randint(100, 200)
            draw2 = ImageDraw.Draw(img)
            draw2.rectangle([bx, by, bx + bw, by + bh], fill=fill_val)
    else:
        noise_level = 18
        pixels = img.load()
        for py in range(PAGE_H_PX):
            for px in range(PAGE_W_PX):
                delta = RNG.randint(-noise_level, noise_level)
                val = max(0, min(255, pixels[px, py] + delta))  # type: ignore[index]
                pixels[px, py] = val  # type: ignore[index]
        img = img.filter(ImageFilter.GaussianBlur(radius=0.6))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def build() -> None:
    pdf = FPDF(unit="pt", format=(PAGE_W_PX, PAGE_H_PX))
    pdf.set_auto_page_break(auto=False)

    for _page_num, text, degraded in PAGE_CONTENT:
        img_bytes = render_page(text, degraded)
        buf = io.BytesIO(img_bytes)
        pdf.add_page()
        pdf.image(buf, x=0, y=0, w=PAGE_W_PX, h=PAGE_H_PX)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUTPUT))
    print(f"Written: {OUTPUT}")


if __name__ == "__main__":
    build()
