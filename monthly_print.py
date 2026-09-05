#!/usr/bin/env python3
"""Stampa mensile di un JPG su stampante di rete via IPP. Solo stdlib.

  python3 monthly_print.py [--dry-run] [--config config.json]

Exit code: 0 = job inviato, 1 = errore, 2 = esito ignoto (non rilanciare alla cieca).
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
# Motivi IPP che bloccano la stampa (oltre a qualsiasi *-error).
FATAL_REASONS = {"media-empty", "media-jam", "media-needed", "door-open", "cover-open",
                 "paused", "shutdown", "toner-empty", "marker-supply-empty",
                 "output-area-almost-full", "output-tray-missing", "input-tray-missing"}

log = logging.getLogger("autoprint")


class PrintError(Exception):
    """Errore gestito: log + exit 1."""


class Ambiguous(Exception):
    """Invio non confermato: exit 2, non rilanciare alla cieca."""


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
        raise PrintError(f"config non trovata: {path}")
    except json.JSONDecodeError as e:
        raise PrintError(f"config non valida ({path}): {e}")
    for key in ("printer_uri", "image_path"):
        if not cfg[key]:
            raise PrintError(f"config: '{key}' obbligatorio")
    cfg["paper_size"] = PAPER_ALIASES.get(str(cfg["paper_size"]).lower(), cfg["paper_size"])
    return cfg


def jpeg_info(path):
    """Valida l'header JPEG e ritorna (larghezza, altezza). Solleva se non è un JPEG."""
    SOF = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            raise PrintError(f"non è un JPEG (marker SOI mancante): {path}")
        while True:
            byte = f.read(1)
            if not byte:
                raise PrintError(f"JPEG troncato o corrotto: {path}")
            if byte != b"\xff":
                continue
            marker = f.read(1)
            while marker == b"\xff":
                marker = f.read(1)
            if not marker:
                raise PrintError(f"JPEG troncato o corrotto: {path}")
            m = marker[0]
            if m == 0x01 or 0xd0 <= m <= 0xd8:
                continue
            if m == 0xd9:
                raise PrintError(f"JPEG senza segmento SOF: {path}")
            head = f.read(2)
            if len(head) < 2:
                raise PrintError(f"JPEG troncato o corrotto: {path}")
            (seglen,) = struct.unpack(">H", head)
            if m in SOF:
                body = f.read(5)
                if len(body) < 5:
                    raise PrintError(f"JPEG troncato o corrotto: {path}")
                height, width = struct.unpack(">HH", body[1:5])
                return width, height
            f.seek(seglen - 2, os.SEEK_CUR)


def media_dimensions(name):
    """Nome PWG (iso_a4_210x297mm) -> (x, y) in centesimi di millimetro."""
    dims = name.rsplit("_", 1)[-1]
    if dims.endswith("mm"):
        factor, dims = 100.0, dims[:-2]
    elif dims.endswith("in"):
        factor, dims = 2540.0, dims[:-2]
    else:
        raise PrintError(f"formato carta non riconosciuto: {name}")
    try:
        w, h = dims.split("x")
        return round(float(w) * factor), round(float(h) * factor)
    except ValueError:
        raise PrintError(f"formato carta non riconosciuto: {name}")


def job_attributes(cfg):
    quality = QUALITY.get(str(cfg["print_quality"]).lower())
    if quality is None:
        raise PrintError(f"print_quality non valida: {cfg['print_quality']} (draft/normal/high)")
    attrs = [
        (TAG_KEYWORD, b"print-color-mode", b"color" if cfg["color"] else b"monochrome"),
        (TAG_ENUM, b"print-quality", struct.pack(">i", quality)),
        (TAG_KEYWORD, b"print-scaling", str(cfg["scaling"]).encode()),
        (TAG_KEYWORD, b"sides", b"one-sided"),
        (TAG_INT, b"copies", struct.pack(">i", 1)),
    ]
    if not cfg["borderless"]:
        return attrs + [(TAG_KEYWORD, b"media", cfg["paper_size"].encode())]
    # Borderless = media-col con i quattro margini a 0.
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
    """Raggiungibilità + stato. Ritorna gli attributi della stampante."""
    host = cfg["printer_uri"].partition("://")[2].partition("/")[0]
    hostname, _, port = host.partition(":")
    port = int(port or 631)
    if not port_open(hostname, port, timeout=5):
        raise PrintError(f"stampante non raggiungibile su {hostname}:{port} "
                         "(spenta, in standby o IP cambiato?)")
    log.info("Printer reachable at %s:%d", hostname, port)
    try:
        status, attrs = get_printer_attributes(cfg["printer_uri"])
    except Exception as e:
        raise PrintError(f"errore di comunicazione IPP con {cfg['printer_uri']}: {e}")
    if status >= 0x0100:
        raise PrintError(f"endpoint IPP rifiutato (status 0x{status:04x}): {cfg['printer_uri']}")

    model = attrs.get("printer-make-and-model", ["?"])[0]
    state = attrs.get("printer-state", [0])[0]
    reasons = [r for r in attrs.get("printer-state-reasons", []) if r != "none"]
    log.info("Printer: %s - stato %s%s", model, PRINTER_STATE.get(state, state),
             " - " + ", ".join(reasons) if reasons else "")

    if not attrs.get("printer-is-accepting-jobs", [True])[0]:
        raise PrintError("la stampante non accetta job in questo momento")
    fatal = [r for r in reasons if r.endswith("-error") or r.split("-report")[0].split("-warning")[0] in FATAL_REASONS]
    if fatal:
        raise PrintError("stampante in errore: " + ", ".join(fatal))
    for warn in reasons:
        log.warning("stato stampante: %s", warn)

    supported = attrs.get("document-format-supported", [])
    if supported and "image/jpeg" not in supported:
        raise PrintError("la stampante non accetta image/jpeg via IPP: " + ", ".join(supported))
    media = attrs.get("media-supported", [])
    if media and cfg["paper_size"] not in media:
        raise PrintError(f"formato carta non supportato: {cfg['paper_size']}")
    if cfg["borderless"] and 0 not in attrs.get("media-top-margin-supported", [0]):
        raise PrintError("borderless non supportato dalla stampante: imposta \"borderless\": false")
    scalings = attrs.get("print-scaling-supported", [])
    if scalings and cfg["scaling"] not in scalings:
        raise PrintError(f"scaling '{cfg['scaling']}' non supportato: {', '.join(scalings)}")
    return attrs


def send_job(cfg, image, job_name):
    op_attrs = [
        (TAG_NAME, b"requesting-user-name", b"autoprint"),
        (TAG_NAME, b"job-name", job_name.encode()),
        (TAG_MIME, b"document-format", b"image/jpeg"),
    ]
    body = build_request(OP_PRINT_JOB, cfg["printer_uri"], op_attrs, job_attributes(cfg),
                         request_id=2) + image
    # Un solo tentativo: un retry rischierebbe una seconda copia stampata.
    try:
        status, attrs = ipp_call(cfg["printer_uri"], body, timeout=180)
    except (socket.timeout, TimeoutError):
        raise Ambiguous("timeout durante l'invio: il job potrebbe essere stato accettato. "
                        "Controlla la stampante prima di rilanciare.")
    except Exception as e:
        raise PrintError(f"errore di comunicazione durante l'invio: {e}")
    if status >= 0x0100:
        raise PrintError(f"job rifiutato dalla stampante (status IPP 0x{status:04x}: "
                         f"{attrs.get('status-message', ['nessun dettaglio'])[0]})")
    return attrs.get("job-id", ["?"])[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stampa mensile di un JPG via IPP")
    ap.add_argument("--dry-run", action="store_true", help="esegue tutti i controlli senza stampare")
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
            raise PrintError(f"immagine non trovata: {path}")
        if not os.access(path, os.R_OK):
            raise PrintError(f"immagine non leggibile (permessi): {path}")
        width, height = jpeg_info(path)
        size = os.path.getsize(path)
        log.info("Image: %s (%dx%d px, %.1f MB)", path, width, height, size / 1e6)

        check_printer(cfg)
        log.info("Job: %s, scaling=%s, borderless=%s, %s, qualità=%s", cfg["paper_size"],
                 cfg["scaling"], cfg["borderless"],
                 "colore" if cfg["color"] else "b/n", cfg["print_quality"])

        if args.dry_run:
            log.info("Dry-run: tutti i controlli superati, nessun job inviato")
            return OK

        with open(path, "rb") as f:
            image = f.read()
        job_id = send_job(cfg, image, os.path.basename(path))
        log.info("Print job submitted successfully")
        log.info("Job ID: %s", job_id)
        return OK
    except Ambiguous as e:
        log.error("ESITO IGNOTO - %s", e)
        return UNKNOWN
    except PrintError as e:
        log.error("%s", e)
        return ERROR
    except Exception as e:
        log.error("errore inatteso: %s: %s", type(e).__name__, e)
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
