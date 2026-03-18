#!/usr/bin/env python3
"""
TFT EAS 911 Pi Logger
Reads SAME headers from the TFT EAS 911 serial output, decodes them,
logs to JSONL + text, and optionally sends mobile notifications via ntfy.sh.
"""

import os
import sys
import re
import time
import json
import hashlib
import logging
import configparser
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import serial
    from serial.serialutil import SerialException
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    SerialException = Exception

try:
    import requests
except ImportError:
    requests = None

from EAS2Text import EAS2Text


# =============================
# Configuration
# =============================

def load_config() -> dict:
    """Load config.ini if present, otherwise fall back to built-in defaults."""
    config_path = Path(__file__).parent / "config.ini"
    config = configparser.ConfigParser()

    # Built-in defaults — used when config.ini is missing or a key is absent
    cfg = {
        'serial_port':          '/dev/ttyUSB0',
        'serial_baud':          1200,
        'serial_timeout':       1.0,
        'serial_retry_delay':   1.0,
        'log_dir':              str(Path(__file__).parent / "logs"),
        'log_level':            'INFO',
        'alerts_dir':           str(Path(__file__).parent / "alerts"),
        'dedupe_window':        120,
        'ntfy_topic':           '',
        'notification_timeout': 5.0,
        'filler_byte':          0xAB,
        'max_buffer_size':      200000,
        'buffer_trim_size':     100000,
    }

    if config_path.exists():
        config.read(config_path)
        s = config
        cfg['serial_port']          = s.get('serial',        'port',                 fallback=cfg['serial_port'])
        cfg['serial_baud']          = s.getint('serial',      'baud',                 fallback=cfg['serial_baud'])
        cfg['serial_timeout']       = s.getfloat('advanced',  'serial_timeout',       fallback=cfg['serial_timeout'])
        cfg['serial_retry_delay']   = s.getfloat('advanced',  'serial_retry_delay',   fallback=cfg['serial_retry_delay'])
        cfg['log_dir']              = s.get('logging',        'log_dir',              fallback=cfg['log_dir'])
        cfg['log_level']            = s.get('logging',        'log_level',            fallback=cfg['log_level'])
        cfg['alerts_dir']           = s.get('alerts',         'alerts_dir',           fallback=cfg['alerts_dir'])
        cfg['dedupe_window']        = s.getint('alerts',       'dedupe_window',        fallback=cfg['dedupe_window'])
        cfg['ntfy_topic']           = s.get('notifications',  'ntfy_topic',           fallback=cfg['ntfy_topic'])
        cfg['notification_timeout'] = s.getfloat('advanced',  'notification_timeout', fallback=cfg['notification_timeout'])
        cfg['max_buffer_size']      = s.getint('advanced',    'max_buffer_size',      fallback=cfg['max_buffer_size'])
        cfg['buffer_trim_size']     = s.getint('advanced',    'buffer_trim_size',     fallback=cfg['buffer_trim_size'])
        filler_str = s.get('hardware', 'filler_byte', fallback='0xAB')
        cfg['filler_byte'] = int(filler_str, 16) if filler_str.startswith('0x') else int(filler_str)

    # Resolve relative paths and ~ to absolute paths
    def resolve(p):
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else str(Path(__file__).parent / p)
    cfg['log_dir']    = resolve(cfg['log_dir'])
    cfg['alerts_dir'] = resolve(cfg['alerts_dir'])

    cfg['_config_found'] = config_path.exists()
    return cfg


CONFIG = load_config()

# Platform detection — determines whether to read from serial or stdin
IS_PI = os.path.exists("/sys/class/gpio") or os.path.exists("/proc/device-tree/model")

# Unpack config into module-level constants for easy access
PORT                 = CONFIG['serial_port']
BAUD                 = CONFIG['serial_baud']
FILLER               = bytes([CONFIG['filler_byte']])
DEDUPE_WINDOW_SEC    = CONFIG['dedupe_window']
MAX_BUFFER_SIZE      = CONFIG['max_buffer_size']
BUFFER_TRIM_SIZE     = CONFIG['buffer_trim_size']
NOTIFICATION_TIMEOUT = CONFIG['notification_timeout']
NTFY_URL             = f"https://ntfy.sh/{CONFIG['ntfy_topic']}" if CONFIG['ntfy_topic'].strip() else ''

ALERTS_DIR = Path(CONFIG['alerts_dir'])
LOGS_DIR   = Path(CONFIG['log_dir'])
JSONL_FILE = str(ALERTS_DIR / "events.jsonl")
TEXT_FILE  = str(ALERTS_DIR / "events.log")

for d in [ALERTS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# =============================
# Logging
# =============================

def setup_logging(log_dir: str, log_level: str) -> logging.Logger:
    logger = logging.getLogger("eas_logger")
    logger.setLevel(logging.DEBUG)

    fmt_console = logging.Formatter('[%(asctime)s] %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    fmt_file    = logging.Formatter('%(asctime)s | %(levelname)-8s | %(funcName)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    console.setFormatter(fmt_console)

    # Rotate at 10 MB, keep 5 backups
    fh = RotatingFileHandler(os.path.join(log_dir, "eas_logger.log"), maxBytes=10*1024*1024, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)

    logger.addHandler(console)
    logger.addHandler(fh)
    return logger


logger = setup_logging(CONFIG['log_dir'], CONFIG['log_level'])


# =============================
# Utilities
# =============================

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def normalize(s: str) -> str:
    # Collapse whitespace — keeps header comparisons consistent
    return " ".join(s.split())

def fingerprint(s: str) -> str:
    return hashlib.sha256(normalize(s).encode()).hexdigest()

def utc_to_local(utc_str: str | None) -> str:
    """Convert an ISO UTC string to a local time string like '5:30 PM'."""
    if not utc_str:
        return "Unknown"
    dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%I:%M %p").lstrip("0")

# Alert file rotation — keeps the Pi from filling up its SD card over time
_ALERT_MAX     = 10 * 1024 * 1024  # 10 MB
_ALERT_BACKUPS = 3

def append_line(path: str, line: str) -> None:
    if os.path.exists(path) and os.path.getsize(path) >= _ALERT_MAX:
        for i in range(_ALERT_BACKUPS - 1, 0, -1):
            src, dst = f"{path}.{i}", f"{path}.{i+1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
        logger.info(f"Rotated {os.path.basename(path)}")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# =============================
# SAME Header Parsing
# =============================

# Match a single SAME header — stops at the next ZCZC, NNNN, or end of string
# [\x20-\x7E] limits to printable ASCII only, keeping serial noise out
HEADER_RE = re.compile(r"(ZCZC-[\x20-\x7E]*?-)(?=ZCZC|NNNN|$)")

def extract_and_vote(raw_burst: str) -> tuple[str, int] | None:
    """
    Pull all SAME header copies from a burst and majority-vote them into one
    canonical header. EAS sends each header 3 times (FCC § 11.33) so a decoder
    can recover from noise by comparing copies character by character.
    Returns (canonical_header, repeat_count) or None if no headers found.
    """
    headers = [h for h in HEADER_RE.findall(raw_burst) if h.startswith("ZCZC-")]
    if not headers:
        return None
    if len(headers) == 1:
        return normalize(headers[0]), 1

    # If the copies differ, log it and vote — take the most common character at each position
    if len(set(headers)) > 1:
        logger.warning("Header copies differ — applying majority vote.")
        for i, h in enumerate(headers):
            logger.debug(f"  Copy {i+1}: {h}")

    max_len = max(len(h) for h in headers)
    voted = []
    for i in range(max_len):
        chars = [h[i] for h in headers if i < len(h)]
        voted.append(max(set(chars), key=chars.count))

    return normalize("".join(voted)), len(headers)


# Pre-compiled regex to pull structured fields out of a SAME header
_FIELDS_RE = re.compile(
    r'^ZCZC-([A-Z]+)-([A-Z]+)-'   # originator, event
    r'[\d\-]+\+(\d{2})(\d{2})-'   # FIPS codes + duration (HHMM)
    r'(\d{3})(\d{2})(\d{2})-'     # issue timestamp (JJJHHMM)
    r'([^-]+)-'                    # sender callsign
)

def parse_same_fields(header: str) -> dict | None:
    """
    Parse structured fields from a SAME header and validate its timestamp.
    Returns None if the alert is already expired (we don't log stale alerts).
    Returns {} if the header format is unrecognized but we accept it anyway.
    Returns a full dict of fields if everything looks good.
    """
    m = _FIELDS_RE.match(header)
    if not m:
        logger.warning("Could not parse SAME fields — accepting header anyway.")
        return {}

    org, evt, dur_hh, dur_mm, jjj, hh, mm, sender = m.groups()
    sender = sender.strip()  # SAME pads sender to 8 chars with spaces

    try:
        now_dt   = datetime.now(timezone.utc)
        issue_dt = datetime(now_dt.year, 1, 1, int(hh), int(mm), tzinfo=timezone.utc) + timedelta(days=int(jjj) - 1)

        # Handle year rollover — if the issue date is >180 days in the past, it's probably next year's day number
        if (now_dt - issue_dt).days > 180:
            issue_dt = issue_dt.replace(year=issue_dt.year + 1)

        # Warn if the timestamp looks like it's in the future — the encoder clock may be wrong
        if (issue_dt - now_dt).total_seconds() > 15 * 60:
            logger.warning(f"Header timestamp {jjj}/{hh}{mm}Z is in the future — encoder clock may be off, accepting anyway.")

        # +0000 duration means no expiry (used for national/presidential alerts)
        dur_secs = (int(dur_hh) * 60 + int(dur_mm)) * 60
        if dur_secs > 0:
            expiry_dt = issue_dt + timedelta(seconds=dur_secs)
            if now_dt > expiry_dt:
                logger.warning(f"Alert expired at {expiry_dt.strftime('%H:%MZ')} — skipping.")
                return None
            expires_utc = expiry_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            expires_utc = None  # No expiry

        issued_utc = issue_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    except Exception as e:
        logger.warning(f"Timestamp validation error: {e} — accepting anyway.")
        issued_utc = expires_utc = None

    return {
        "originator_code": org,
        "event_code":      evt,
        "sender":          sender,
        "issued_utc":      issued_utc,
        "expires_utc":     expires_utc,
    }


def parse_duration(header: str) -> str | None:
    """Parse +HHMM duration from a SAME header into a readable string."""
    match = re.search(r'\+(\d{4})-', header)
    if not match:
        return None
    raw     = match.group(1)
    hours   = int(raw[:2])
    minutes = int(raw[2:])
    if hours == 0 and minutes == 0:
        return "Indefinite"
    if hours and minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h" if hours else f"{minutes}m"


# =============================
# Output Formatting
# =============================

def format_alert(title: str, lines: list[str]) -> str:
    """Build a box-drawn receipt block for console and text file output."""
    width = min(max(len(title), max((len(l) for l in lines), default=0)) + 4, 70)
    out = [f"┏━ {title}"]
    for line in lines:
        out.append(f"  {line}" if line else "")
    out.append(f"┗━{'─' * (width - 2)}")
    return "\n".join(out)


# =============================
# Notifications
# =============================

def send_notification(title: str, message: str) -> dict:
    """Push an alert to a phone via ntfy.sh. Returns a delivery receipt dict."""
    if not NTFY_URL or requests is None:
        return {"attempted": False}
    try:
        r = requests.post(NTFY_URL, data=message.encode(), headers={"Title": title}, timeout=NOTIFICATION_TIMEOUT)
        if r.status_code == 200:
            logger.info(f"Notification sent: {title}")
            return {"attempted": True, "sent": True, "http_status": r.status_code}
        logger.warning(f"Notification failed (HTTP {r.status_code})")
        return {"attempted": True, "sent": False, "http_status": r.status_code}
    except Exception as e:
        logger.warning(f"Notification failed: {e}")
        return {"attempted": True, "sent": False, "error": str(e)}


# =============================
# Serial Port
# =============================

def open_serial(port: str, baud: int):
    """Open the serial port, waiting indefinitely if the cable isn't plugged in yet."""
    if not IS_PI:
        logger.info("Test mode — reading from stdin.")
        return None
    if not SERIAL_AVAILABLE:
        logger.error("pyserial not installed — run: pip install pyserial")
        sys.exit(1)
    import serial as _serial
    while True:
        if not os.path.exists(port):
            logger.warning(f"Serial port {port} not found — waiting for cable...")
            while not os.path.exists(port):
                time.sleep(1)
            logger.info(f"Serial port {port} detected.")
        try:
            ser = _serial.Serial(port, baud, timeout=CONFIG['serial_timeout'])
            logger.info(f"Opened {port} @ {baud} baud")
            return ser
        except SerialException as e:
            logger.error(f"Could not open {port}: {e} — retrying...")
            time.sleep(CONFIG['serial_retry_delay'])


# =============================
# Main Loop
# =============================

def main() -> None:
    logger.info(f"EAS Logger starting | Platform: {'Raspberry Pi' if IS_PI else 'Dev/Test'}")
    if CONFIG['_config_found']:
        logger.info(f"Config: config.ini | Port: {PORT} @ {BAUD} | ntfy: {'on' if NTFY_URL else 'off'} | dedupe: {DEDUPE_WINDOW_SEC}s")
    else:
        logger.warning("config.ini not found — using built-in defaults.")

    seen: dict[str, float] = {}  # fingerprint -> timestamp, for deduplication
    buf  = ""
    ser  = open_serial(PORT, BAUD)

    try:
        while True:
            # --- Read input ---
            try:
                if not IS_PI:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                else:
                    if ser is None:
                        logger.error("No serial connection.")
                        break
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    chunk = chunk.replace(FILLER, b"")  # strip TFT911 preamble bytes
                    if not chunk:
                        continue
                    text = chunk.decode("ascii", errors="ignore")
                    if not text:
                        continue

            except KeyboardInterrupt:
                raise
            except SerialException as e:
                logger.error(f"Serial error: {e}")
                try:
                    ser.close() if ser else None
                except Exception:
                    pass
                ser = open_serial(PORT, BAUD)
                continue

            buf += text

            # Trim buffer if it grows too large — prevents memory buildup on long uptime
            if len(buf) > MAX_BUFFER_SIZE:
                buf = buf[-BUFFER_TRIM_SIZE:]

            # --- Extract complete ZCZC...NNNN bursts from the buffer ---
            while True:
                start = buf.find("ZCZC")
                if start < 0:
                    break
                end = buf.find("NNNN", start)
                if end < 0:
                    if start > 0:
                        buf = buf[start:]  # discard junk before the incomplete burst
                    break

                raw_burst = buf[start:end + 4]
                buf = buf[end + 4:]

                saw_eom = "NNNN" in raw_burst
                result  = extract_and_vote(raw_burst)
                if result is None:
                    logger.warning("Burst had no valid SAME headers — discarding.")
                    continue

                canonical, repeat_count = result
                print(f"  SAME burst detected ({repeat_count} header copies).")

                fields = parse_same_fields(canonical)
                if fields is None:
                    continue  # expired alert

                # --- Deduplicate ---
                fp  = fingerprint(canonical)
                now = time.time()
                if now - seen.get(fp, 0) < DEDUPE_WINDOW_SEC:
                    print(f"  Duplicate {fields.get('event_code', '?')} — skipping.")
                    continue
                seen[fp] = now
                seen = {k: v for k, v in seen.items() if now - v < DEDUPE_WINDOW_SEC}

                # --- Decode with EAS2Text ---
                received_local = now_local()
                try:
                    oof = EAS2Text(canonical)
                except Exception as ex:
                    logger.exception(f"EAS2Text decode failed: {ex}")
                    block = format_alert("EAS Decode Failed", [
                        f"Received: {received_local}",
                        f"Error: {ex}", "",
                        "Raw header:", canonical,
                    ])
                    append_line(TEXT_FILE, block + "\n")
                    print(block)
                    continue

                eas_message  = getattr(oof, "EASText",  None) or "EAS Event"
                title        = eas_message.split('\n')[0]
                fips_list    = getattr(oof, "FIPSText", []) or []
                locations    = [str(x) for x in fips_list] if isinstance(fips_list, list) else ([str(fips_list)] if fips_list else [])
                org_text     = getattr(oof, "orgText",  None) or getattr(oof, "ORG", None) or "Unknown"
                sender_text  = getattr(oof, "fromText", None) or getattr(oof, "fromCode", None)
                start_text   = utc_to_local(fields.get("issued_utc"))
                end_text     = utc_to_local(fields.get("expires_utc"))
                dur_text     = parse_duration(canonical)

                # --- Build display block ---
                lines = [f"Received: {received_local}", f"Originator: {org_text}"]
                if sender_text and sender_text != "Unknown":
                    lines.append(f"From: {sender_text}")
                lines.append(f"Start: {start_text}")
                if end_text != "Unknown":
                    lines.append(f"End: {end_text}")
                if dur_text:
                    lines.append(f"Duration: {dur_text}")
                lines += [f"Repeats: {repeat_count} | EOM: {saw_eom}", "", "Locations:"]
                if locations:
                    for loc in locations[:25]:
                        lines.append(f"  • {loc}")
                    if len(locations) > 25:
                        lines.append(f"  … +{len(locations) - 25} more")
                else:
                    lines.append("  (none)")
                lines += ["", f"Header: {canonical}"]

                block        = format_alert(title, lines)
                ntfy_receipt = send_notification(str(title), block)

                # --- Save to files ---
                record = {
                    "received_utc":     now_utc(),
                    "received_local":   received_local,
                    "canonical_header": canonical,
                    **fields,
                    "repeat_count":     repeat_count,
                    "saw_eom":          saw_eom,
                    "locations_pretty": locations,
                    "eas2text": {
                        "evntText": getattr(oof, "evntText", None),
                        "orgText":  getattr(oof, "orgText",  None),
                        "fromText": getattr(oof, "fromText", None),
                    },
                    "raw_burst":    normalize(raw_burst),
                    "notification": ntfy_receipt,
                }

                append_line(JSONL_FILE, json.dumps(record, ensure_ascii=False))
                append_line(TEXT_FILE,  block + "\n")
                logger.debug(f"Logged: {title} | {len(locations)} location(s) | {repeat_count} repeat(s)")
                print(f"\n{block}")

    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        try:
            ser.close() if ser else None
        except Exception:
            pass
        logger.info("Logger shut down.")


if __name__ == "__main__":
    main()
