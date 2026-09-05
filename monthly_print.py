#!/usr/bin/env python3
"""Print a JPG on a network printer over IPP. Standard library only.

  python3 monthly_print.py [--dry-run] [--config config.json]

Exit codes: 0 = job submitted, 1 = error, 2 = unknown outcome (do not blindly rerun).
"""
import argparse
import json
import logging
import logging.handlers
import os
import socket
import struct
import sys

from printer_probe import (
    OP_PRINT_JOB, PRINTER_STATE, TAG_BEGCOLL, TAG_ENDCOLL, TAG_ENUM, TAG_INT,
    TAG_KEYWORD, TAG_MEMBER, TAG_MIME, TAG_NAME, build_request, get_printer_attributes,
    ipp_call, port_open,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OK, ERROR, UNKNOWN = 0, 1, 2

DEFAULTS = {
    "printer_uri": "",
    "image_path": "",
    "paper_size": "iso_a4_210x297mm",
    "borderless": True,
    "scaling": "fit",
    "color": True,
    "print_quality": "high",
    "log_file": "logs/autoprint.log",
}
PAPER_ALIASES = {
    "a4": "iso_a4_210x297mm", "a3": "iso_a3_297x420mm", "a5": "iso_a5_148x210mm",
    "letter": "na_letter_8.5x11in", "10x15": "na_index-4x6_4x6in", "13x18": "na_5x7_5x7in",
}
QUALITY = {"draft": 3, "normal": 4, "high": 5}
# IPP state reasons that block printing, on top of any *-error reason.
FATAL_REASONS = {"media-empty", "media-jam", "media-needed", "door-open", "cover-open",
                 "paused", "shutdown", "toner-empty", "marker-supply-empty",
                 "output-area-almost-full", "output-tray-missing", "input-tray-missing"}

log = logging.getLogger("autoprint")


class PrintError(Exception):
    """Handled failure: logged, exit 1."""


class Ambiguous(Exception):
    """Submission not confirmed: exit 2, do not blindly rerun."""


def setup_logging(path):
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3),
                logging.StreamHandler(sys.stdout)]
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def load_config(path):
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        raise PrintError(f"config not found: {path}")
    except json.JSONDecodeError as e:
        raise PrintError(f"invalid config ({path}): {e}")
    for key in ("printer_uri", "image_path"):
        if not cfg[key]:
            raise PrintError(f"config: '{key}' is required")
    cfg["paper_size"] = PAPER_ALIASES.get(str(cfg["paper_size"]).lower(), cfg["paper_size"])
    return cfg


def jpeg_info(path):
    """Validate the JPEG header and return (width, height). Raises if not a JPEG."""
    SOF = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            raise PrintError(f"not a JPEG (SOI marker missing): {path}")
        while True:
            byte = f.read(1)
            if not byte:
                raise PrintError(f"truncated or corrupt JPEG: {path}")
            if byte != b"\xff":
                continue
            marker = f.read(1)
            while marker == b"\xff":
                marker = f.read(1)
            if not marker:
                raise PrintError(f"truncated or corrupt JPEG: {path}")
            m = marker[0]
            if m == 0x01 or 0xd0 <= m <= 0xd8:
                continue
            if m == 0xd9:
                raise PrintError(f"JPEG has no SOF segment: {path}")
            head = f.read(2)
            if len(head) < 2:
                raise PrintError(f"truncated or corrupt JPEG: {path}")
            (seglen,) = struct.unpack(">H", head)
            if m in SOF:
                body = f.read(5)
                if len(body) < 5:
                    raise PrintError(f"truncated or corrupt JPEG: {path}")
                height, width = struct.unpack(">HH", body[1:5])
                return width, height
            f.seek(seglen - 2, os.SEEK_CUR)


def media_dimensions(name):
    """PWG media name (iso_a4_210x297mm) -> (x, y) in hundredths of a millimetre."""
    dims = name.rsplit("_", 1)[-1]
    if dims.endswith("mm"):
        factor, dims = 100.0, dims[:-2]
    elif dims.endswith("in"):
        factor, dims = 2540.0, dims[:-2]
    else:
        raise PrintError(f"unrecognised paper size: {name}")
    try:
        w, h = dims.split("x")
        return round(float(w) * factor), round(float(h) * factor)
    except ValueError:
        raise PrintError(f"unrecognised paper size: {name}")


def job_attributes(cfg):
    quality = QUALITY.get(str(cfg["print_quality"]).lower())
    if quality is None:
        raise PrintError(f"invalid print_quality: {cfg['print_quality']} (draft/normal/high)")
    attrs = [
        (TAG_KEYWORD, b"print-color-mode", b"color" if cfg["color"] else b"monochrome"),
        (TAG_ENUM, b"print-quality", struct.pack(">i", quality)),
        (TAG_KEYWORD, b"print-scaling", str(cfg["scaling"]).encode()),
        (TAG_KEYWORD, b"sides", b"one-sided"),
        (TAG_INT, b"copies", struct.pack(">i", 1)),
    ]
    if not cfg["borderless"]:
        return attrs + [(TAG_KEYWORD, b"media", cfg["paper_size"].encode())]
    # Borderless = media-col with all four margins set to zero.
    x, y = media_dimensions(cfg["paper_size"])
    attrs += [
        (TAG_BEGCOLL, b"media-col", b""),
        (TAG_MEMBER, b"", b"media-size"),
        (TAG_BEGCOLL, b"", b""),
        (TAG_MEMBER, b"", b"x-dimension"), (TAG_INT, b"", struct.pack(">i", x)),
        (TAG_MEMBER, b"", b"y-dimension"), (TAG_INT, b"", struct.pack(">i", y)),
        (TAG_ENDCOLL, b"", b""),
    ]
    for side in ("top", "bottom", "left", "right"):
        attrs += [(TAG_MEMBER, b"", f"media-{side}-margin".encode()),
                  (TAG_INT, b"", struct.pack(">i", 0))]
    return attrs + [(TAG_ENDCOLL, b"", b"")]


def check_printer(cfg):
    """Reachability and state. Returns the printer attributes."""
    host = cfg["printer_uri"].partition("://")[2].partition("/")[0]
    hostname, _, port = host.partition(":")
    port = int(port or 631)
    if not port_open(hostname, port, timeout=5):
        raise PrintError(f"printer unreachable at {hostname}:{port} "
                         "(powered off, asleep, or changed IP?)")
    log.info("Printer reachable at %s:%d", hostname, port)
    try:
        status, attrs = get_printer_attributes(cfg["printer_uri"])
    except Exception as e:
        raise PrintError(f"IPP communication error with {cfg['printer_uri']}: {e}")
    if status >= 0x0100:
        raise PrintError(f"IPP endpoint refused the request (status 0x{status:04x}): "
                         f"{cfg['printer_uri']}")

    model = attrs.get("printer-make-and-model", ["?"])[0]
    state = attrs.get("printer-state", [0])[0]
    reasons = [r for r in attrs.get("printer-state-reasons", []) if r != "none"]
    log.info("Printer: %s - state %s%s", model, PRINTER_STATE.get(state, state),
             " - " + ", ".join(reasons) if reasons else "")

    if not attrs.get("printer-is-accepting-jobs", [True])[0]:
        raise PrintError("the printer is not accepting jobs right now")
    fatal = [r for r in reasons
             if r.endswith("-error") or r.split("-report")[0].split("-warning")[0] in FATAL_REASONS]
    if fatal:
        raise PrintError("printer in error state: " + ", ".join(fatal))
    for warn in reasons:
        log.warning("printer state: %s", warn)

    supported = attrs.get("document-format-supported", [])
    if supported and "image/jpeg" not in supported:
        raise PrintError("printer does not accept image/jpeg over IPP: " + ", ".join(supported))
    media = attrs.get("media-supported", [])
    if media and cfg["paper_size"] not in media:
        raise PrintError(f"paper size not supported: {cfg['paper_size']}")
    if cfg["borderless"] and 0 not in attrs.get("media-top-margin-supported", [0]):
        raise PrintError("borderless not supported by this printer: set \"borderless\": false")
    scalings = attrs.get("print-scaling-supported", [])
    if scalings and cfg["scaling"] not in scalings:
        raise PrintError(f"scaling '{cfg['scaling']}' not supported: {', '.join(scalings)}")
    return attrs


def send_job(cfg, image, job_name):
    op_attrs = [
        (TAG_NAME, b"requesting-user-name", b"autoprint"),
        (TAG_NAME, b"job-name", job_name.encode()),
        (TAG_MIME, b"document-format", b"image/jpeg"),
    ]
    body = build_request(OP_PRINT_JOB, cfg["printer_uri"], op_attrs, job_attributes(cfg),
                         request_id=2) + image
    # One attempt only: a retry risks printing a second copy.
    try:
        status, attrs = ipp_call(cfg["printer_uri"], body, timeout=180)
    except (socket.timeout, TimeoutError):
        raise Ambiguous("timed out while sending: the job may have been accepted. "
                        "Check the printer before rerunning.")
    except Exception as e:
        raise PrintError(f"communication error while sending: {e}")
    if status >= 0x0100:
        raise PrintError(f"job rejected by the printer (IPP status 0x{status:04x}: "
                         f"{attrs.get('status-message', ['no detail'])[0]})")
    return attrs.get("job-id", ["?"])[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Print a JPG over IPP")
    ap.add_argument("--dry-run", action="store_true", help="run every check without printing")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except PrintError as e:
        print(f"ERROR - {e}", file=sys.stderr)
        return ERROR
    setup_logging(cfg["log_file"])
    log.info("Starting monthly print%s", " (dry-run)" if args.dry_run else "")

    try:
        path = cfg["image_path"]
        if not os.path.isfile(path):
            raise PrintError(f"image not found: {path}")
        if not os.access(path, os.R_OK):
            raise PrintError(f"image not readable (permissions): {path}")
        width, height = jpeg_info(path)
        size = os.path.getsize(path)
        log.info("Image: %s (%dx%d px, %.1f MB)", path, width, height, size / 1e6)

        check_printer(cfg)
        log.info("Job: %s, scaling=%s, borderless=%s, %s, quality=%s", cfg["paper_size"],
                 cfg["scaling"], cfg["borderless"],
                 "color" if cfg["color"] else "monochrome", cfg["print_quality"])

        if args.dry_run:
            log.info("Dry-run: all checks passed, no job submitted")
            return OK

        with open(path, "rb") as f:
            image = f.read()
        job_id = send_job(cfg, image, os.path.basename(path))
        log.info("Print job submitted successfully")
        log.info("Job ID: %s", job_id)
        return OK
    except Ambiguous as e:
        log.error("UNKNOWN OUTCOME - %s", e)
        return UNKNOWN
    except PrintError as e:
        log.error("%s", e)
        return ERROR
    except Exception as e:
        log.error("unexpected error: %s: %s", type(e).__name__, e)
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
