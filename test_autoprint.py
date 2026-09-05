#!/usr/bin/env python3
"""Self-check: python3 test_autoprint.py (no printer required)."""
import os
import struct
import sys
import tempfile

from printer_probe import build_request, parse_response, OP_PRINT_JOB
from monthly_print import DEFAULTS, PrintError, jpeg_info, job_attributes, media_dimensions

# --- paper sizes ---------------------------------------------------------
assert media_dimensions("iso_a4_210x297mm") == (21000, 29700)
assert media_dimensions("na_letter_8.5x11in") == (21590, 27940)
assert media_dimensions("na_index-4x6_4x6in") == (10160, 15240)
try:
    media_dimensions("not_a_paper_size")
    assert False, "expected an error on an unknown paper size"
except PrintError:
    pass

# --- JPEG validation ------------------------------------------------------
# Minimal JPEG: SOI + APP0 + SOF0 (800x600) + EOI
jpg = (b"\xff\xd8"
       b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 +
       b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 600, 800) + b"\x00" * 10 +
       b"\xff\xd9")
with tempfile.TemporaryDirectory() as d:
    good = os.path.join(d, "ok.jpg")
    with open(good, "wb") as f:
        f.write(jpg)
    assert jpeg_info(good) == (800, 600)

    for name, blob in (("png.jpg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40),
                       ("empty.jpg", b""),
                       ("truncated.jpg", jpg[:12]),
                       ("nosof.jpg", b"\xff\xd8\xff\xd9")):
        bad = os.path.join(d, name)
        with open(bad, "wb") as f:
            f.write(blob)
        try:
            jpeg_info(bad)
            assert False, f"expected an error on {name}"
        except PrintError:
            pass

# --- IPP encoding/decoding ----------------------------------------------
cfg = dict(DEFAULTS, printer_uri="ipp://1.2.3.4/ipp/print", borderless=False)
req = build_request(OP_PRINT_JOB, cfg["printer_uri"], job_attrs=job_attributes(cfg))
op, attrs = parse_response(req)
assert op == OP_PRINT_JOB
assert attrs["printer-uri"] == ["ipp://1.2.3.4/ipp/print"]
assert attrs["print-color-mode"] == ["color"]
assert attrs["print-quality"] == [5]
assert attrs["print-scaling"] == ["fit"]
assert attrs["media"] == ["iso_a4_210x297mm"]
assert attrs["copies"] == [1]

# borderless: media-col with four zero margins instead of 'media'
bl = build_request(OP_PRINT_JOB, cfg["printer_uri"], job_attrs=job_attributes(dict(cfg, borderless=True)))
_, bl_attrs = parse_response(bl)
assert "media" not in bl_attrs, "borderless must not also send 'media'"
assert b"media-col" in bl and b"x-dimension" in bl
assert struct.pack(">i", 21000) in bl and struct.pack(">i", 29700) in bl
for side in ("top", "bottom", "left", "right"):
    assert f"media-{side}-margin".encode() in bl
# monochrome + normal quality
_, mono = parse_response(build_request(
    OP_PRINT_JOB, cfg["printer_uri"],
    job_attrs=job_attributes(dict(cfg, color=False, print_quality="normal"))))
assert mono["print-color-mode"] == ["monochrome"] and mono["print-quality"] == [4]

try:
    job_attributes(dict(cfg, print_quality="fotografica"))
    assert False, "expected an invalid-quality error"
except PrintError:
    pass

print("OK - all checks passed")
