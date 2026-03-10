#!/usr/bin/env python3
import os
import sys
import time
import json
import hashlib
import re
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

# EAS2Text-Remastered (per docs)
# https://pypi.org/project/EAS2Text-Remastered/
from EAS2Text import EAS2Text


# =============================
# Configuration Management
# =============================

def load_config() -> dict:
    """Load configuration from config.ini if it exists, otherwise use defaults."""
    config_path = Path(__file__).parent / "config.ini"
    config = configparser.ConfigParser()

    defaults = {
        'serial_port': '/dev/ttyUSB0',
        'serial_baud': 1200,
        'log_dir': str(Path.home() / "eas_logs" / "logs"),
        'log_level': 'INFO',
        'alerts_dir': str(Path.home() / "eas_logs" / "alerts"),
        'dedupe_window': 120,
        'ntfy_url': '',
        'filler_byte': 0xAB,
        'max_buffer_size': 200000,
        'buffer_trim_size': 100000,
        'serial_timeout': 1,
        'serial_retry_delay': 1,
        'notification_timeout': 5,
    }

    if config_path.exists():
        config.read(config_path)

        if config.has_section('serial'):
            defaults['serial_port'] = config.get('serial', 'port', fallback=defaults['serial_port'])
            defaults['serial_baud'] = config.getint('serial', 'baud', fallback=defaults['serial_baud'])

        if config.has_section('logging'):
            defaults['log_dir'] = config.get('logging', 'log_dir', fallback=defaults['log_dir'])
            defaults['log_level'] = config.get('logging', 'log_level', fallback=defaults['log_level'])

        if config.has_section('alerts'):
            defaults['alerts_dir'] = config.get('alerts', 'alerts_dir', fallback=defaults['alerts_dir'])
            defaults['dedupe_window'] = config.getint('alerts', 'dedupe_window', fallback=defaults['dedupe_window'])

        if config.has_section('notifications'):
            defaults['ntfy_url'] = config.get('notifications', 'ntfy_url', fallback=defaults['ntfy_url'])

        if config.has_section('hardware'):
            filler_str = config.get('hardware', 'filler_byte', fallback='0xAB')
            defaults['filler_byte'] = int(filler_str, 16) if filler_str.startswith('0x') else int(filler_str)

        if config.has_section('advanced'):
            defaults['max_buffer_size'] = config.getint('advanced', 'max_buffer_size', fallback=defaults['max_buffer_size'])
            defaults['buffer_trim_size'] = config.getint('advanced', 'buffer_trim_size', fallback=defaults['buffer_trim_size'])
            defaults['serial_timeout'] = config.getfloat('advanced', 'serial_timeout', fallback=defaults['serial_timeout'])
            defaults['serial_retry_delay'] = config.getfloat('advanced', 'serial_retry_delay', fallback=defaults['serial_retry_delay'])
            defaults['notification_timeout'] = config.getfloat('advanced', 'notification_timeout', fallback=defaults['notification_timeout'])

    defaults['log_dir'] = os.path.expanduser(defaults['log_dir'])
    defaults['alerts_dir'] = os.path.expanduser(defaults['alerts_dir'])

    return defaults


# =============================
# Logging Configuration
# =============================

def setup_logging(log_dir: str | None = None, log_level: str = 'INFO') -> logging.Logger:
    """Configure logging with both console and file output."""
    if log_dir is None:
        log_dir = str(Path.home() / "eas_logs" / "logs")

    Path(log_dir).mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("eas_logger")
    logger.setLevel(logging.DEBUG)

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    console_formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(funcName)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_formatter)

    log_file = os.path.join(log_dir, "eas_logger.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger

CONFIG = load_config()
logger = setup_logging(CONFIG['log_dir'], CONFIG['log_level'])


# =============================
# Configuration
# =============================

IS_PI = os.path.exists("/sys/class/gpio") or os.path.exists("/proc/device-tree/model")
IS_LAPTOP = not IS_PI

DATA_DIR = Path(CONFIG['alerts_dir']).parent
LOGS_DIR = Path(CONFIG['log_dir'])
ALERTS_DIR = Path(CONFIG['alerts_dir'])

for dir_path in [LOGS_DIR, ALERTS_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

JSONL_FILE = str(ALERTS_DIR / "events.jsonl")
TEXT_FILE = str(ALERTS_DIR / "events.log")

PORT = os.getenv("EAS_PORT", CONFIG['serial_port'])
BAUD = int(os.getenv("EAS_BAUD", CONFIG['serial_baud']))
FILLER = bytes([CONFIG['filler_byte']])

DEDUPE_WINDOW_SEC = CONFIG['dedupe_window']
NTFY_URL = CONFIG['ntfy_url']
MAX_BUFFER_SIZE = CONFIG['max_buffer_size']
BUFFER_TRIM_SIZE = CONFIG['buffer_trim_size']
SERIAL_TIMEOUT = CONFIG['serial_timeout']
SERIAL_RETRY_DELAY = CONFIG['serial_retry_delay']
NOTIFICATION_TIMEOUT = CONFIG['notification_timeout']

# Regex to extract repeated SAME headers inside a burst (typically repeated 3x)
HEADER_RE = re.compile(r"(ZCZC-[\s\S]*?-)(?=ZCZC|NNNN|$)")


# =============================
# Time / Utilities
# =============================

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def normalize(s: str) -> str:
    return " ".join(s.split())

def fingerprint(s: str) -> str:
    return hashlib.sha256(normalize(s).encode("utf-8")).hexdigest()

def append_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def send_phone(title: str, message: str) -> None:
    if not NTFY_URL.strip() or requests is None:
        return
    try:
        requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={"Title": title},
            timeout=NOTIFICATION_TIMEOUT
        )
        logger.debug(f"Mobile notification sent: {title}")
    except Exception as e:
        logger.warning(f"Failed to send mobile notification: {e}")

def majority_vote(headers: list[str]) -> str:
    """
    Reconstruct the most reliable SAME header using majority voting.

    EAS transmits each header 3 times with no checksums — the spec
    (FCC 47 CFR § 11.33) requires receivers to use 'best two of three'
    comparison. The TFT hardware demodulates each of the 3 audio copies
    independently via FSK, so each copy can have different decoding errors.
    This compares all 3 serial copies character-by-character and picks the
    character that appears most often at each position.
    """
    if len(headers) == 1:
        return headers[0]

    # Warn if copies differ — means the TFT had decoding errors on one or more copies
    if len(set(headers)) > 1:
        logger.warning(f"Header copies differ — TFT FSK decoding error on one or more copies. Applying majority vote.")
        for i, h in enumerate(headers):
            logger.debug(f"  Copy {i+1}: {h}")

    max_len = max(len(h) for h in headers)
    result = []
    for i in range(max_len):
        # Collect the character from each copy at this position (skip if copy is shorter)
        chars = [h[i] for h in headers if i < len(h)]
        # Pick the most common character; if tied, fall back to the first copy's character
        voted = max(set(chars), key=chars.count)
        result.append(voted)

    return "".join(result)


def validate_timestamp(header: str) -> bool:
    """
    Validate the JJJHHMM timestamp embedded in the SAME header.

    Per FCC 47 CFR § 11.33, decoders must reject headers where:
    - The issue time is more than 15 minutes in the future (clock skew / spoofing)
    - The alert has already expired (issue time + duration is in the past)

    Returns True if the timestamp is valid and the alert is still active.
    """
    # Parse issue time: JJJHHMM (Julian day + UTC hour + minute)
    ts_match = re.search(r'-(\d{3})(\d{2})(\d{2})-', header)
    dur_match = re.search(r'\+(\d{2})(\d{2})-', header)
    if not ts_match:
        logger.warning("Could not parse timestamp from header — accepting anyway.")
        return True

    try:
        now_dt = datetime.now(timezone.utc)
        jjj = int(ts_match.group(1))   # Day of year
        hh  = int(ts_match.group(2))   # UTC hour
        mm  = int(ts_match.group(3))   # UTC minute

        # Reconstruct issue datetime using the current year
        issue_dt = datetime(now_dt.year, 1, 1, hh, mm, tzinfo=timezone.utc) + \
                   timedelta(days=jjj - 1)

        # Handle year wrap (e.g., message issued Dec 31, received Jan 1)
        if (now_dt - issue_dt).days > 180:
            issue_dt = issue_dt.replace(year=issue_dt.year + 1)

        # Reject if issued more than 15 minutes in the future
        if (issue_dt - now_dt).total_seconds() > 15 * 60:
            logger.warning(f"Header timestamp {jjj}/{hh:02d}{mm:02d}Z is >15 min in the future — rejecting.")
            return False

        # Check expiry if duration is present (0000 = no expiry, used for national alerts)
        if dur_match:
            dur_hours = int(dur_match.group(1))
            dur_mins  = int(dur_match.group(2))
            if dur_hours > 0 or dur_mins > 0:
                duration_secs = (dur_hours * 60 + dur_mins) * 60
                expiry_dt = issue_dt + timedelta(seconds=duration_secs)
                if now_dt > expiry_dt:
                    logger.warning(f"Alert expired at {expiry_dt.strftime('%H:%MZ')} — rejecting.")
                    return False

    except Exception as e:
        logger.warning(f"Timestamp validation error: {e} — accepting anyway.")

    return True


def parse_duration(header: str) -> str | None:
    """
    Parse alert duration from raw SAME header string.
    EAS encodes duration as +HHMM (e.g., +0130 = 1 hour 30 minutes).
    Returns a human-readable string like '1h 30m', or None if not found.
    """
    match = re.search(r'\+(\d{4})-', header)
    if not match:
        return None
    raw = match.group(1)
    hours = int(raw[:2])
    minutes = int(raw[2:])
    if hours and minutes:
        return f"{hours}h {minutes}m"
    elif hours:
        return f"{hours}h"
    elif minutes:
        return f"{minutes}m"
    return None


# =============================
# Serial Connection
# =============================

def wait_for_cable(port: str) -> None:
    if IS_LAPTOP:
        return
    if os.path.exists(port):
        return
    logger.warning(f"Serial cable not detected ({port}). Waiting...")
    while not os.path.exists(port):
        time.sleep(1)
    logger.info(f"Serial cable detected ({port}).")

def open_serial(port: str, baud: int):
    """Open serial port. Returns None on non-Pi systems (stdin mode)."""
    if IS_LAPTOP:
        logger.info("Running in TEST MODE (reading from stdin).")
        logger.info("Pipe data from virtual_tft.py or other source.")
        return None

    if not SERIAL_AVAILABLE:
        logger.error("pyserial not installed. Install with: pip install pyserial")
        sys.exit(1)

    import serial

    while True:
        wait_for_cable(port)
        try:
            ser = serial.Serial(port, baud, timeout=SERIAL_TIMEOUT)
            logger.info(f"Opened {port} @ {baud} baud")
            return ser
        except SerialException as e:
            logger.error(f"Could not open {port}: {e}. Retrying...")
            time.sleep(SERIAL_RETRY_DELAY)

def close_serial(ser) -> None:
    """Close serial port safely."""
    if ser is None:
        return
    try:
        ser.close()
        logger.debug("Serial port closed.")
    except Exception as e:
        logger.warning(f"Error closing serial port: {e}")


# =============================
# Formatting
# =============================

def receipt_block(title: str, lines: list[str]) -> str:
    """Clean, formatted alert display with box drawing."""
    max_len = max(len(title), max([len(l) for l in lines] if lines else [0]))
    header_width = min(max_len + 4, 70)

    output = [f"┏━ {title}"]
    for line in lines:
        if line:
            output.append(f"  {line}")
        else:
            output.append("")  # preserve blank spacer lines
    output.append(f"┗━{'─' * (header_width - 2)}")

    return "\n".join(output)


# =============================
# Main Loop
# =============================

def main() -> None:
    logger.info("EAS Alert Logger starting…")
    logger.info(f"Platform: {'Raspberry Pi' if IS_PI else 'Development/Test'}")
    logger.info(f"Data directory: {DATA_DIR}")
    logger.info(f"Alert log files: {ALERTS_DIR}")
    logger.info(f"Logs directory: {LOGS_DIR}")

    seen: dict[str, float] = {}
    buf = ""

    ser = open_serial(PORT, BAUD)

    try:
        while True:
            try:
                if IS_LAPTOP:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                else:
                    assert ser is not None  # ser is always open when IS_PI is True
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    chunk = chunk.replace(FILLER, b"")
                    if not chunk:
                        continue
                    text = chunk.decode("ascii", errors="ignore")
                    if not text:
                        continue
            except KeyboardInterrupt:
                raise  # Let the outer try/except handle it cleanly
            except SerialException as e:
                logger.error(f"Serial error (unplugged?): {e}")
                close_serial(ser)
                ser = open_serial(PORT, BAUD)
                continue

            buf += text

            # Trim buffer if it grows too large to prevent memory buildup over long uptime
            if len(buf) > MAX_BUFFER_SIZE:
                buf = buf[-BUFFER_TRIM_SIZE:]

            # Extract bursts: ZCZC ... NNNN
            while True:
                s = buf.find("ZCZC")
                if s < 0:
                    break

                e = buf.find("NNNN", s)
                if e < 0:
                    if s > 0:
                        buf = buf[s:]
                    break

                raw_burst = buf[s:e + 4]
                buf = buf[e + 4:]

                headers = [h for h in HEADER_RE.findall(raw_burst) if h.startswith("ZCZC-")]
                repeat_count = len(headers)
                saw_eom = "NNNN" in raw_burst

                if not headers:
                    logger.warning("Burst contained no valid SAME headers — discarding.")
                    continue

                # Apply majority voting across all 3 copies to correct bit errors (FCC § 11.33)
                canonical = majority_vote(headers)

                # Reject headers with invalid or expired timestamps (FCC § 11.33)
                if not validate_timestamp(canonical):
                    continue

                # Deduplicate - skip if same alert seen within the dedup window
                fp = fingerprint(canonical)
                now = time.time()
                if now - seen.get(fp, 0) < DEDUPE_WINDOW_SEC:
                    continue
                seen[fp] = now

                # Prune old fingerprints so the dict doesn't grow forever on long uptime
                seen = {k: v for k, v in seen.items() if now - v < DEDUPE_WINDOW_SEC}

                received_local = now_local()

                # ---- Decode using EAS2Text ----
                try:
                    oof = EAS2Text(canonical)
                except Exception as ex:
                    logger.error(f"EAS decode failed: {ex}")
                    logger.debug(f"Raw header: {normalize(canonical)}")
                    block = receipt_block("EAS Decode Failed", [
                        f"Received: {received_local}",
                        f"Error: {ex}",
                        "",
                        "Raw header:",
                        normalize(canonical),
                    ])
                    append_line(TEXT_FILE, block + "\n")
                    print(block)
                    continue

                eas_message = getattr(oof, "EASText", None) or "EAS Event"
                title = eas_message.split('\n')[0] if eas_message else "EAS Event"

                fips_text_list = getattr(oof, "FIPSText", []) or []
                pretty_locations = [str(x) for x in fips_text_list] if isinstance(fips_text_list, list) else ([str(fips_text_list)] if fips_text_list else [])

                org_text = getattr(oof, "orgText", None) or getattr(oof, "ORG", None) or "Unknown"
                start_text = getattr(oof, "startTimeText", None) or "Unknown"
                end_text = getattr(oof, "endTimeText", None) or "Unknown"
                sender = getattr(oof, "fromText", None) or getattr(oof, "fromCode", None)

                # Parse duration from raw header - more reliable than EAS2Text attributes
                dur_text = parse_duration(canonical)

                lines = [
                    f"Received: {received_local}",
                    f"Originator: {org_text}",
                ]

                if sender and sender != "Unknown":
                    lines.append(f"From: {sender}")

                lines.extend([
                    f"Start: {start_text}",
                    f"End: {end_text}",
                ])

                if dur_text:
                    lines.append(f"Duration: {dur_text}")

                lines.extend([
                    f"Repeats: {repeat_count} | EOM: {saw_eom}",
                    "",
                    "Locations:",
                ])

                if pretty_locations:
                    for loc in pretty_locations[:25]:
                        lines.append(f"  • {loc}")
                    if len(pretty_locations) > 25:
                        lines.append(f"  … +{len(pretty_locations) - 25} more")
                else:
                    lines.append("  (none)")

                lines += [
                    "",
                    f"Header: {normalize(canonical)}",
                ]

                block = receipt_block(title, lines)

                record = {
                    "received_utc": now_utc(),
                    "received_local": received_local,
                    "canonical_header": normalize(canonical),
                    "repeat_count": repeat_count,
                    "saw_eom": saw_eom,
                    "locations_pretty": pretty_locations,
                    "eas2text": {
                        "evntText": getattr(oof, "evntText", None),
                        "orgText": getattr(oof, "orgText", None),
                        "fromText": getattr(oof, "fromText", None),
                        "startTimeText": getattr(oof, "startTimeText", None),
                        "endTimeText": getattr(oof, "endTimeText", None),
                        "timeText": getattr(oof, "timeText", None),
                        "FIPS": getattr(oof, "FIPS", None),
                        "FIPSText": getattr(oof, "FIPSText", None),
                    },
                    "raw_burst": normalize(raw_burst),
                }

                append_line(JSONL_FILE, json.dumps(record, ensure_ascii=False))
                append_line(TEXT_FILE, block + "\n")

                logger.info(f"Alert received: {eas_message.split(chr(10))[0]} | Locations: {len(pretty_locations)} | Repeats: {repeat_count}")
                logger.debug(f"Header: {normalize(canonical)}")

                send_phone(str(title), block)
                print(block)

    except KeyboardInterrupt:
        logger.info("Interrupted by user (Ctrl+C).")
    finally:
        # Always runs on exit - ensures serial port is closed cleanly
        close_serial(ser)
        logger.info("Logger stopped.")

if __name__ == "__main__":
    main()
