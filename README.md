# AutoPrint

Scheduled printing of a JPG from a **Synology NAS** to a network printer over **IPP**.
Born from a concrete problem: an inkjet printer left idle for too long dries out its
print heads, so once a month it gets sent a full-page colour photo to keep it working.

- **Zero dependencies**: Python standard library only. No `pip install`.
- **No CUPS**, no Docker, no always-on daemon.
- A single executable to schedule with Synology DSM's Task Scheduler.
- Speaks IPP directly: the protocol encoder and parser are included (~100 lines).

Tested on DSM 7.3.2 (Python 3.8.15, aarch64) with an **Epson EcoTank ET-8550**.

## Why IPP and nothing else

Recent EcoTank models expose IPP on port 631 and accept `image/jpeg` **directly**,
along with the attributes for paper size, scaling, quality and margins. Nothing needs
rasterising, CUPS is unnecessary, and no driver is involved. On an ET-8550 ports 9100
(RAW) and 515 (LPD) are closed, so IPP is not merely the best option — it is the only
one.

Note: that printer answers `426 Upgrade Required` over plain HTTP and wants TLS. The
script handles this on its own — you configure `ipp://` and it switches to TLS when
required, without verifying the certificate, since printers ship self-signed ones.

## Installation

```bash
git clone https://github.com/giuliomaffei90/synology-autoprint.git
cd synology-autoprint
cp config.example.json config.json
```

Copy the folder to the NAS, for example to `/volume1/Contents/AutoPrint/`. If `scp` is
refused by the NAS — it often is, because DSM frequently ships with the sftp subsystem
disabled — go through tar instead:

```bash
tar cf - *.py config.json | ssh user@NAS 'mkdir -p /volume1/Contents/AutoPrint && tar xf - -C /volume1/Contents/AutoPrint'
```

## Finding the printer

```bash
python3 printer_probe.py 192.168.1.100
```

It reports the open ports (631/9100/515), the working IPP endpoint, and the attributes
needed to configure everything else: supported paper sizes, accepted MIME types,
resolutions, colour modes, quality levels, current state and ink levels.

If you do not know the printer's IP, find it with `dns-sd -B _ipp._tcp` (macOS),
`avahi-browse -rt _ipp._tcp` (Linux), or from the printer's own front panel.

## Usage

```bash
python3 monthly_print.py --dry-run   # every check, nothing printed
python3 monthly_print.py             # print
```

Exit codes: `0` job submitted, `1` error, `2` unknown outcome (a timeout while
sending: the job may have gone through, so check the printer before rerunning).

Before submitting, the script verifies that the file exists, is readable and is a
structurally valid JPEG; and that the printer answers, accepts jobs, is not in an
error state, and supports the requested paper size, scaling and borderless mode. There
is a single submission attempt and no automatic retry: failing is better than ending
up with ten copies of the same sheet.

## Scheduling on DSM

DSM 7 has no command for creating scheduled tasks (`synoschedtask` only exposes
`--get/--del/--run`), so this has to be done through the web interface:

**Control Panel > Task Scheduler > Create > Scheduled Task > User-defined script**

| Field | Value |
|---|---|
| User | the file's owner; root is not needed |
| Schedule | monthly |
| Email notification | *"send run details only when the script terminates abnormally"* |
| Script | `/usr/bin/python3 /volume1/Contents/AutoPrint/monthly_print.py` |

The absolute path to the interpreter is required: the DSM scheduler's PATH is minimal
and bare `python3` will not resolve.

## config.json

| key | value |
|---|---|
| `printer_uri` | `ipp://IP/ipp/print` |
| `image_path` | absolute path to the JPG |
| `paper_size` | PWG name (`iso_a4_210x297mm`) or alias `a4`/`a3`/`a5`/`letter`/`10x15`/`13x18` |
| `borderless` | `true` → zero margins via `media-col` |
| `scaling` | `fit` / `fill` / `auto` / `auto-fit` / `none` |
| `color` | `true` for colour |
| `print_quality` | `draft` / `normal` / `high` |
| `log_file` | relative to the script's own directory |

Replace the JPG with another file of the same name and the next run picks up the new
image automatically. The original is never modified.

### fit versus fill

`fit` scales the image to the page while preserving its aspect ratio: nothing is
cropped, but if the photo's proportions differ from the sheet's you get white bands
along two edges. `fill` genuinely covers the whole sheet, at the cost of cropping at
the edges. A true edge-to-edge borderless print needs `fill`.

Which values are actually accepted varies between printers; `printer_probe.py` lists
them under `print-scaling-supported`.

## Logging

Append-only to `logs/autoprint.log`, rotating at 1 MB × 3 files.

```text
2026-09-05 02:03:57 - INFO - Starting monthly print
2026-09-05 02:03:57 - INFO - Image: /volume1/Contents/AutoPrint/print.jpg (1189x2000 px, 0.3 MB)
2026-09-05 02:03:57 - INFO - Printer reachable at 192.168.1.100:631
2026-09-05 02:03:57 - INFO - Printer: EPSON ET-8550 Series - state idle
2026-09-05 02:03:58 - INFO - Print job submitted successfully
2026-09-05 02:03:58 - INFO - Job ID: 8
```

## Tests

```bash
python3 test_autoprint.py
```

Covers PWG paper-name parsing, JPEG validation (including truncated files, files with
no SOF segment, and non-JPEGs) and an IPP encode/decode round trip, the borderless
`media-col` included. No printer required.

## Layout

```text
monthly_print.py    checks, job submission, logging
printer_probe.py    discovery plus the shared IPP encoder/parser
test_autoprint.py   self-check, no printer needed
config.example.json
```

## Licence

MIT — see [LICENSE](LICENSE).
