"""
Generator: unservable set — password-protected PDF stub, tiny .wav, tiny .mp4 stub,
image-only PDF, .dwg-extension binary.
§18.1 role: files that cannot be served — each triggers a different exclusion reason.

Notes on each file:
  - password_protected.pdf: a valid PDF with /Encrypt dictionary (minimal, not truly encrypted
    but contains the encryption markers that make readers treat it as protected). Parsers that
    respect /Encrypt will refuse to extract text without a password.
  - audio_stub.wav: valid RIFF/WAVE header with 1 second of silence (44 bytes of PCM zeros).
  - video_stub.mp4: minimal ftyp + mdat ISO Base Media file — players won't render it but
    the magic bytes (ftyp) are correct for .mp4.
  - image_only.pdf: a PDF whose sole page is a rasterized JPEG — no text layer at all.
  - cad_binary.dwg: a file with a DWG magic byte header (AC1015 = AutoCAD 2000 format marker)
    followed by deterministic binary content. Not a real DWG file but has plausible magic bytes.

Run: uv run python tests/fixtures/golden/generators/gen_unservable.py
"""

import io
import struct
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw

CORPUS = Path(__file__).parent.parent / "corpus"


# ──────────────────────────────────────────────────────────────────────────────
# Password-protected PDF stub
# ──────────────────────────────────────────────────────────────────────────────

# Encryption marker bytes - split for line-length compliance
_O_HASH = b"D\xe8\x87\x9e\xed\xf9\xc3\xd6" * 2 + b"\xde\xad\xbe\xef" + b"\x00" * 13
_U_HASH = b"\xe8\x87\x9e\xed\xf9\xc3\xd6" * 2 + b"\xde\xad\xbe\xef" + b"\x00" * 14

PASSWORD_PROTECTED_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n"
    b"<< /Type /Catalog /Pages 2 0 R /Encrypt 5 0 R >>\n"
    b"endobj\n\n"
    b"2 0 obj\n"
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n"
    b"endobj\n\n"
    b"3 0 obj\n"
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\n"
    b"endobj\n\n"
    b"4 0 obj\n"
    b"<< /Filter /Standard /V 2 /R 3 /Length 128 /P -3904\n"
    b"   /O (" + _O_HASH + b")\n"
    b"   /U (" + _U_HASH + b") >>\n"
    b"endobj\n\n"
    b"5 0 obj\n"
    b"4 0 R\n"
    b"endobj\n\n"
    b"xref\n"
    b"0 6\n"
    b"0000000000 65535 f\n"
    b"0000000009 00000 n\n"
    b"0000000068 00000 n\n"
    b"0000000125 00000 n\n"
    b"0000000206 00000 n\n"
    b"0000000502 00000 n\n\n"
    b"trailer\n"
    b"<< /Size 6 /Root 1 0 R /Encrypt 4 0 R\n"
    b"   /ID [<DEADBEEF00000000DEADBEEF00000000>"
    b" <DEADBEEF00000000DEADBEEF00000000>] >>\n"
    b"startxref\n"
    b"521\n"
    b"%%EOF\n"
)


def build_password_protected() -> None:
    out = CORPUS / "password_protected.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(PASSWORD_PROTECTED_PDF)
    print(f"Written: {out}")


# ──────────────────────────────────────────────────────────────────────────────
# Tiny .wav — 1 second of 44100 Hz mono silence (PCM 16-bit)
# ──────────────────────────────────────────────────────────────────────────────

def build_wav() -> None:
    sample_rate = 44100
    num_channels = 1
    bits_per_sample = 16
    num_samples = sample_rate  # 1 second
    data_size = num_samples * num_channels * (bits_per_sample // 8)

    buf = io.BytesIO()
    # RIFF header
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))  # chunk size
    buf.write(b"WAVE")
    # fmt sub-chunk
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))  # sub-chunk size
    buf.write(struct.pack("<H", 1))   # PCM format
    buf.write(struct.pack("<H", num_channels))
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", sample_rate * num_channels * bits_per_sample // 8))  # byte rate
    buf.write(struct.pack("<H", num_channels * bits_per_sample // 8))  # block align
    buf.write(struct.pack("<H", bits_per_sample))
    # data sub-chunk
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(b"\x00" * data_size)  # silence

    out = CORPUS / "audio_stub.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(buf.getvalue())
    print(f"Written: {out}  ({len(buf.getvalue())} bytes)")


# ──────────────────────────────────────────────────────────────────────────────
# Minimal .mp4 stub — valid ftyp box + empty mdat box
# ──────────────────────────────────────────────────────────────────────────────

def build_mp4() -> None:
    def box(box_type: bytes, payload: bytes) -> bytes:
        size = 8 + len(payload)
        return struct.pack(">I", size) + box_type + payload

    # ftyp box
    ftyp_payload = (
        b"isom"   # major brand
        + struct.pack(">I", 0x200)  # minor version
        + b"isomiso2mp41"  # compatible brands
    )
    ftyp = box(b"ftyp", ftyp_payload)

    # minimal moov box (just enough to be structurally present)
    mvhd_payload = (
        b"\x00"  # version 0
        + b"\x00\x00\x00"  # flags
        + struct.pack(">I", 0)   # creation time
        + struct.pack(">I", 0)   # modification time
        + struct.pack(">I", 1000)  # timescale
        + struct.pack(">I", 0)   # duration
        + struct.pack(">I", 0x00010000)  # rate = 1.0
        + struct.pack(">H", 0x0100)     # volume = 1.0
        + b"\x00" * 70  # reserved + matrix + pre-defined
        + struct.pack(">I", 2)  # next track ID
    )
    mvhd = box(b"mvhd", mvhd_payload)
    moov = box(b"moov", mvhd)

    # empty mdat box
    mdat = box(b"mdat", b"")

    data = ftyp + moov + mdat

    out = CORPUS / "video_stub.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Written: {out}  ({len(data)} bytes)")


# ──────────────────────────────────────────────────────────────────────────────
# Image-only PDF — rasterized page, no text layer
# ──────────────────────────────────────────────────────────────────────────────

def build_image_only_pdf() -> None:
    # Create a synthetic "document" image
    img = Image.new("RGB", (850, 1100), color=(255, 255, 240))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 810, 1060], outline=(180, 180, 180), width=2)
    draw.text((60, 80), "ACME Corp — Archived Diagram (Image Only)", fill=(50, 50, 50))
    draw.text((60, 110), "Document No. ARC-2019-0047", fill=(80, 80, 80))
    draw.text((60, 140), "Rev. A — 2019-05-22", fill=(80, 80, 80))
    # Draw a simple diagram-like shape (no text — all visual)
    draw.rectangle([150, 200, 700, 400], outline=(100, 100, 200), width=3)
    draw.text((300, 280), "[Schematic — see original drawing]", fill=(120, 120, 120))
    draw.line([(150, 300), (700, 300)], fill=(150, 150, 150), width=2)
    draw.ellipse([310, 420, 540, 620], outline=(100, 180, 100), width=3)
    draw.text((340, 500), "[Component A]", fill=(100, 150, 100))
    draw.rectangle([150, 660, 700, 800], outline=(200, 100, 100), width=2)
    draw.text((350, 720), "[Assembly View]", fill=(180, 80, 80))
    draw.text(
        (60, 1000),
        "This document contains no machine-readable text layer.",
        fill=(120, 120, 120),
    )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    buf.seek(0)

    pdf = FPDF(unit="pt", format=(850, 1100))
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.image(buf, x=0, y=0, w=850, h=1100)

    out = CORPUS / "image_only.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    print(f"Written: {out}")


# ──────────────────────────────────────────────────────────────────────────────
# CAD binary — .dwg extension with AutoCAD magic bytes
# ──────────────────────────────────────────────────────────────────────────────

def build_dwg() -> None:
    # AutoCAD DWG files start with a 6-byte version sentinel: e.g. "AC1015" for AutoCAD 2000
    # Followed by a binary header. We emit the sentinel + deterministic filler.
    magic = b"AC1015"  # AutoCAD 2000 format sentinel
    # Deterministic filler — 2048 bytes of a repeating pattern
    filler = bytes((i * 7 + 43) % 256 for i in range(2048))
    data = magic + filler

    out = CORPUS / "cad_binary.dwg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Written: {out}  ({len(data)} bytes)")


def build() -> None:
    build_password_protected()
    build_wav()
    build_mp4()
    build_image_only_pdf()
    build_dwg()


if __name__ == "__main__":
    build()
