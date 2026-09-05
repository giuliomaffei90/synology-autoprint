#!/usr/bin/env python3
"""Printer discovery: open ports plus IPP attributes.

Also used as a module: monthly_print.py imports the IPP helpers from here.
Standard library only.
"""
import http.client
import socket
import ssl
import struct
import sys

# --- IPP tags --------------------------------------------------------------
TAG_INT, TAG_BOOL, TAG_ENUM = 0x21, 0x22, 0x23
TAG_RANGE, TAG_BEGCOLL, TAG_ENDCOLL = 0x33, 0x34, 0x37
TAG_TEXT, TAG_NAME, TAG_KEYWORD = 0x41, 0x42, 0x44
TAG_URI, TAG_CHARSET, TAG_LANG, TAG_MIME = 0x45, 0x47, 0x48, 0x49
TAG_MEMBER = 0x4A

OP_PRINT_JOB = 0x0002
OP_GET_PRINTER_ATTRS = 0x000B

PRINTER_STATE = {3: "idle", 4: "processing", 5: "stopped"}


def _attr(tag, name, value):
    return struct.pack(">BH", tag, len(name)) + name + struct.pack(">H", len(value)) + value


def build_request(op, printer_uri, op_attrs=(), job_attrs=(), request_id=1):
    """IPP 2.0 header + operation group (+ job group) + end-of-attributes."""
    out = struct.pack(">HHI", 0x0200, op, request_id)
    out += b"\x01"
    out += _attr(TAG_CHARSET, b"attributes-charset", b"utf-8")
    out += _attr(TAG_LANG, b"attributes-natural-language", b"en")
    out += _attr(TAG_URI, b"printer-uri", printer_uri.encode())
    for t, n, v in op_attrs:
        out += _attr(t, n, v)
    if job_attrs:
        out += b"\x02"
        for t, n, v in job_attrs:
            out += _attr(t, n, v)
    return out + b"\x03"


def _decode(tag, raw):
    if tag in (TAG_INT, TAG_ENUM):
        return struct.unpack(">i", raw)[0]
    if tag == TAG_BOOL:
        return bool(raw[0])
    if tag == TAG_RANGE:
        lo, hi = struct.unpack(">ii", raw)
        return f"{lo}-{hi}"
    if tag == 0x32:  # resolution
        x, y, unit = struct.unpack(">iiB", raw)
        return f"{x}x{y}{'dpi' if unit == 3 else 'dpcm'}"
    return raw.decode("utf-8", "replace")


def parse_response(data):
    """-> (status_code, {name: [values]}). Nested collections are skipped."""
    status = struct.unpack(">H", data[2:4])[0]
    attrs, last, depth, i = {}, None, 0, 8
    while i < len(data):
        tag = data[i]
        i += 1
        if tag == 0x03:
            break
        if tag < 0x10:  # group delimiter
            continue
        (nlen,) = struct.unpack(">H", data[i:i + 2])
        i += 2
        name = data[i:i + nlen].decode("utf-8", "replace")
        i += nlen
        (vlen,) = struct.unpack(">H", data[i:i + 2])
        i += 2
        raw = data[i:i + vlen]
        i += vlen
        if tag == TAG_BEGCOLL:
            depth += 1
            continue
        if tag == TAG_ENDCOLL:
            depth -= 1
            continue
        if depth:  # collection member: not needed for any decision here
            continue
        val = _decode(tag, raw)
        if nlen == 0 and last:
            attrs[last].append(val)
        else:
            attrs[name] = [val]
            last = name
    return status, attrs


def ipp_call(uri, body, timeout=20):
    """POST application/ipp to uri. -> (ipp_status, attrs). Raises on HTTP != 200.

    ipp:// is plain text, ipps:// is TLS. An HTTP 426 (Upgrade Required) is retried
    over TLS. The certificate is not verified: printers ship self-signed ones.
    """
    scheme, _, rest = uri.partition("://")
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    tls = scheme == "ipps"
    for attempt in (1, 2):
        if tls:
            ctx = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection(host, int(port or 631), timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, int(port or 631), timeout=timeout)
        try:
            conn.request("POST", "/" + path, body,
                         {"Content-Type": "application/ipp", "Content-Length": str(len(body))})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status == 426 and attempt == 1 and not tls:
                tls = True
                continue
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} {resp.reason} from {uri}")
            return parse_response(data)
        finally:
            conn.close()


def get_printer_attributes(uri, timeout=10):
    return ipp_call(uri, build_request(OP_GET_PRINTER_ATTRS, uri), timeout)


def port_open(host, port, timeout=2.0):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


# --- CLI -------------------------------------------------------------------
INTERESTING = [
    "printer-make-and-model", "printer-name", "printer-state", "printer-state-reasons",
    "printer-is-accepting-jobs", "document-format-supported", "media-default",
    "media-supported", "media-ready", "media-left-margin-supported",
    "media-right-margin-supported", "media-top-margin-supported",
    "media-bottom-margin-supported", "printer-resolution-supported",
    "print-color-mode-supported", "print-quality-supported", "print-scaling-supported",
    "orientation-requested-supported", "sides-supported", "ipp-versions-supported",
    "marker-names", "marker-levels",
]


def main(argv):
    if len(argv) < 2:
        print("usage: printer_probe.py <printer-ip> [ipp-path]")
        return 2
    ip = argv[1]
    paths = [argv[2]] if len(argv) > 2 else ["/ipp/print", "/ipp/printer", "/ipp", "/"]

    print(f"== Printer {ip} ==")
    for port, label in ((631, "IPP"), (9100, "RAW/JetDirect"), (515, "LPD")):
        print(f"  port {port:5d} {label:14s} {'OPEN' if port_open(ip, port) else 'closed'}")

    print("\n== IPP attributes ==")
    for path in paths:
        uri = f"ipp://{ip}{path}"
        try:
            status, attrs = get_printer_attributes(uri)
        except Exception as e:
            print(f"  {uri}: {e}")
            continue
        if status >= 0x0100:
            print(f"  {uri}: IPP response status 0x{status:04x}")
            continue
        print(f"  VALID ENDPOINT: {uri}\n")
        for key in INTERESTING:
            if key in attrs:
                vals = attrs[key]
                if key == "printer-state":
                    vals = [f"{vals[0]} ({PRINTER_STATE.get(vals[0], '?')})"]
                print(f"  {key}:\n    " + "\n    ".join(str(v) for v in vals))
        borderless = all(0 in attrs.get(f"media-{s}-margin-supported", [])
                         for s in ("left", "right", "top", "bottom"))
        print(f"\n  -> borderless (zero margins supported): {'YES' if borderless else 'NO'}")
        extra = sorted(set(attrs) - set(INTERESTING))
        print(f"  -> {len(extra)} other attributes: {', '.join(extra[:15])}...")
        return 0
    print("\nNo valid IPP endpoint found.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
