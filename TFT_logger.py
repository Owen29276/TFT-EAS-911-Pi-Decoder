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

try:
    from EAS2Text import EAS2Text
    EAS2TEXT_AVAILABLE = True
except ImportError:
    EAS2Text = None
    EAS2TEXT_AVAILABLE = False


# =============================
# Configuration
# =============================

def load_config() -> dict:
    """Load config.ini if present, otherwise fall back to built-in defaults."""
    config_path = Path(__file__).parent / "config.ini"
    config = configparser.ConfigParser()

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
    }

    if config_path.exists():
        config.read(config_path)
        s = config
        cfg['serial_port']          = s.get('serial',       'port',                 fallback=cfg['serial_port'])
        cfg['serial_baud']          = s.getint('serial',    'baud',                 fallback=cfg['serial_baud'])
        cfg['serial_timeout']       = s.getfloat('advanced','serial_timeout',       fallback=cfg['serial_timeout'])
        cfg['serial_retry_delay']   = s.getfloat('advanced','serial_retry_delay',   fallback=cfg['serial_retry_delay'])
        cfg['log_dir']              = s.get('logging',      'log_dir',              fallback=cfg['log_dir'])
        cfg['log_level']            = s.get('logging',      'log_level',            fallback=cfg['log_level'])
        cfg['alerts_dir']           = s.get('alerts',       'alerts_dir',           fallback=cfg['alerts_dir'])
        cfg['dedupe_window']        = s.getint('alerts',    'dedupe_window',        fallback=cfg['dedupe_window'])
        cfg['ntfy_topic']           = s.get('notifications','ntfy_topic',           fallback=cfg['ntfy_topic'])
        cfg['notification_timeout'] = s.getfloat('advanced','notification_timeout', fallback=cfg['notification_timeout'])
        filler_str = s.get('hardware', 'filler_byte', fallback='0xAB')
        cfg['filler_byte'] = int(filler_str, 16) if filler_str.startswith('0x') else int(filler_str)

    def resolve(p):
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else str(Path(__file__).parent / p)
    cfg['log_dir']    = resolve(cfg['log_dir'])
    cfg['alerts_dir'] = resolve(cfg['alerts_dir'])
    cfg['_config_found'] = config_path.exists()
    return cfg


CONFIG = load_config()

IS_PI    = os.path.exists("/sys/class/gpio") or os.path.exists("/proc/device-tree/model")
PORT     = CONFIG['serial_port']
BAUD     = CONFIG['serial_baud']
FILLER   = bytes([CONFIG['filler_byte']])
NTFY_URL = f"https://ntfy.sh/{CONFIG['ntfy_topic']}" if CONFIG['ntfy_topic'].strip() else ''

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
    return " ".join(s.split())

def fingerprint(s: str) -> str:
    return hashlib.sha256(normalize(s).encode()).hexdigest()

def _compute_expires_utc(canonical: str) -> str | None:
    """Derive expires_utc from the SAME header's timestamp + duration fields."""
    try:
        m = re.search(r'\+(\d{4})-(\d{3})(\d{2})(\d{2})-', canonical)
        if not m:
            return None
        dur_h, dur_m = int(m.group(1)[:2]), int(m.group(1)[2:])
        if dur_h == 0 and dur_m == 0:
            return None
        jjj, hh, mm = int(m.group(2)), int(m.group(3)), int(m.group(4))
        year  = datetime.now(timezone.utc).year
        issue = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=jjj - 1, hours=hh, minutes=mm)
        return (issue + timedelta(hours=dur_h, minutes=dur_m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None

# Alert file rotation — keeps the Pi SD card from filling up
_ALERT_MAX     = 10 * 1024 * 1024  # 10 MB
_ALERT_BACKUPS = 3

def append_line(path: str, line: str) -> None:
    # Skip write if disk is critically full
    try:
        stat = os.statvfs(os.path.dirname(path) or '.')
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        if free_mb < 10:
            logger.error(f"Disk critically full ({free_mb:.0f}MB free) — skipping write")
            return
        if free_mb < 100:
            logger.warning(f"Disk low ({free_mb:.0f}MB free)")
    except Exception:
        pass

    # Rotate if the file has grown past the limit
    if os.path.exists(path) and os.path.getsize(path) >= _ALERT_MAX:
        for i in range(_ALERT_BACKUPS - 1, 0, -1):
            src, dst = f"{path}.{i}", f"{path}.{i+1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
        logger.info(f"Rotated {os.path.basename(path)}")

    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


# =============================
# Notifications
# =============================

def send_notification(title: str, message: str) -> dict:
    """Push alert to phone via ntfy.sh. Returns delivery receipt."""
    if not NTFY_URL or requests is None:
        return {"attempted": False}
    try:
        r = requests.post(NTFY_URL, data=message.encode(), headers={"Title": title}, timeout=CONFIG['notification_timeout'])
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
    """Open serial port, waiting indefinitely if not present yet."""
    if not IS_PI:
        logger.info("Test mode — reading from stdin.")
        return None
    if not SERIAL_AVAILABLE:
        logger.error("pyserial not installed — run: pip install pyserial")
        sys.exit(1)
    import serial as _serial
    while True:
        if not os.path.exists(port):
            logger.warning(f"Serial port {port} not found — waiting...")
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

# Match a SAME header — printable ASCII only to filter serial noise
HEADER_RE = re.compile(r"(ZCZC-[\x20-\x7E]*?-)(?=ZCZC|NNNN|$)")

def main() -> None:
    logger.info(f"EAS Logger starting | Platform: {'Raspberry Pi' if IS_PI else 'Dev/Test'}")
    if CONFIG['_config_found']:
        logger.info(f"Config: config.ini | Port: {PORT} @ {BAUD} | ntfy: {'on' if NTFY_URL else 'off'} | dedupe: {CONFIG['dedupe_window']}s")
    else:
        logger.warning("config.ini not found — using built-in defaults.")

    seen: dict[str, float] = {}  # fingerprint → timestamp for deduplication
    buf  = ""
    ser  = open_serial(PORT, BAUD)

    try:
        while True:
            # --- Read from serial or stdin ---
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
                    if ser: ser.close()
                except Exception:
                    pass
                ser = open_serial(PORT, BAUD)
                continue

            buf += text

            # --- Extract complete ZCZC...NNNN bursts ---
            while True:
                start = buf.find("ZCZC")
                if start < 0:
                    break
                end = buf.find("NNNN", start)
                if end < 0:
                    if start > 0:
                        buf = buf[start:]
                    break

                raw_burst = buf[start:end + 4]
                buf = buf[end + 4:]

                # Pull the first clean header from the burst
                # The TFT has already majority-voted the three copies internally
                headers = [h for h in HEADER_RE.findall(raw_burst) if h.startswith("ZCZC-")]
                if not headers:
                    logger.warning("Burst had no valid SAME headers — discarding.")
                    continue

                canonical = " ".join(headers[0].split())

                logger.info("SAME burst detected")

                # --- Deduplicate ---
                fp  = fingerprint(canonical)
                now = time.time()
                if now - seen.get(fp, 0) < CONFIG['dedupe_window']:
                    logger.info("Duplicate alert — skipping.")
                    continue
                seen[fp] = now
                seen = {k: v for k, v in seen.items() if now - v < CONFIG['dedupe_window']}

                # --- Decode with EAS2Text ---
                received_local = now_local()
                if not EAS2TEXT_AVAILABLE:
                    logger.warning("EAS2Text not installed — alert logged without decode")
                    record = {
                        "received_utc":     now_utc(),
                        "received_local":   received_local,
                        "canonical_header": canonical,
                        "expires_utc":      _compute_expires_utc(canonical),
                        "decode_error":     "EAS2Text not installed",
                        "notification":     {"attempted": False},
                    }
                    append_line(JSONL_FILE, json.dumps(record, ensure_ascii=False))
                    continue
                try:
                    oof = EAS2Text(canonical)  # type: ignore[misc]
                except Exception as ex:
                    logger.exception(f"EAS2Text decode failed: {ex}")
                    record = {
                        "received_utc":     now_utc(),
                        "received_local":   received_local,
                        "canonical_header": canonical,
                        "expires_utc":      _compute_expires_utc(canonical),
                        "decode_error":     str(ex),
                        "notification":     {"attempted": False},
                    }
                    append_line(JSONL_FILE, json.dumps(record, ensure_ascii=False))
                    continue

                eas_text  = getattr(oof, "EASText",  None) or "EAS Event"
                title     = eas_text.split('\n')[0]
                fips_list = getattr(oof, "FIPSText", []) or []
                locations = [str(x) for x in fips_list] if isinstance(fips_list, list) else ([str(fips_list)] if fips_list else [])
                org_text  = getattr(oof, "orgText",  None) or "Unknown"
                evt_text  = getattr(oof, "evntText", None) or ""
                sender    = getattr(oof, "fromText", None) or ""

                # --- Build readable text block ---
                loc_str = ", ".join(locations[:5])
                if len(locations) > 5:
                    loc_str += f" +{len(locations) - 5} more"
                text_block = (
                    f"━━━ EAS ALERT ━━━\n"
                    f"{title}\n"
                    f"Received:  {received_local}\n"
                    f"From:      {sender or org_text}\n"
                    f"Locations: {loc_str or 'Unknown'}\n"
                    f"Header:    {canonical}\n"
                    f"━━━━━━━━━━━━━━━━━"
                )

                # --- Notify ---
                ntfy_receipt = send_notification(title, text_block)

                # --- Save to files ---
                record = {
                    "received_utc":     now_utc(),
                    "received_local":   received_local,
                    "canonical_header": canonical,
                    "expires_utc":      _compute_expires_utc(canonical),
                    "event_code":       getattr(oof, "evnt",     None) or "",
                    "originator_code":  getattr(oof, "org",      None) or "",
                    "sender":           sender,
                    "event_text":       evt_text,
                    "org_text":         org_text,
                    "eas_text":         eas_text,
                    "locations_pretty": locations,
                    "notification":     ntfy_receipt,
                }

                append_line(JSONL_FILE, json.dumps(record, ensure_ascii=False))
                append_line(TEXT_FILE,  text_block + "\n")
                logger.info(f"Logged: {title} | {len(locations)} location(s)")
                print(f"\n{text_block}\n", flush=True)

    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        try:
            if ser: ser.close()
        except Exception:
            pass
        logger.info("Logger shut down.")


if __name__ == "__main__":
    main()