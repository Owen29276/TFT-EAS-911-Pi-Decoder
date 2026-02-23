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
from datetime import datetime, timezone
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
        'log_dir': str(Path.home() / "eas_data" / "logs"),
        'log_level': 'INFO',
        'alerts_dir': str(Path.home() / "eas_data" / "alerts"),
        'dedupe_window': 120,
        'ntfy_url': '',
        'filler_byte': 0xAB,
        'max_buffer_size': 200000,
        'buffer_trim_size': 100000,
        'serial_timeout': 1,
        'serial_retry_delay': 1,
        'notification_timeout': 5,
    }
    
    # Try to load config file
    if config_path.exists():
        config.read(config_path)
        
        # Extract values with defaults
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
    
    # Expand ~ in paths
    defaults['log_dir'] = os.path.expanduser(defaults['log_dir'])
    defaults['alerts_dir'] = os.path.expanduser(defaults['alerts_dir'])
    
    return defaults


# =============================
# Logging Configuration
# =============================

def setup_logging(log_dir: str | None = None, log_level: str = 'INFO') -> logging.Logger:
    """Configure logging with both console and file output."""
    if log_dir is None:
        log_dir = str(Path.home() / "eas_data" / "logs")
    
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("eas_logger")
    logger.setLevel(logging.DEBUG)
    
    # Parse log level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Create formatters
    console_formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(funcName)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler (configured level and above)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_formatter)
    
    # File handler with rotation (DEBUG and above)
    log_file = os.path.join(log_dir, "eas_logger.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5  # Keep 5 backup files
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger

# Load configuration from config.ini if it exists
CONFIG = load_config()

# Initialize logger with config settings
logger = setup_logging(CONFIG['log_dir'], CONFIG['log_level'])


# =============================
# Configuration
# =============================

# Platform detection
IS_PI = os.path.exists("/sys/class/gpio") or os.path.exists("/proc/device-tree/model")
IS_LAPTOP = not IS_PI

# Directory structure (from config)
DATA_DIR = Path(CONFIG['alerts_dir']).parent
LOGS_DIR = Path(CONFIG['log_dir'])
ALERTS_DIR = Path(CONFIG['alerts_dir'])

# Create directories
for dir_path in [LOGS_DIR, ALERTS_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

# Alert output files (organized by type)
JSONL_FILE = str(ALERTS_DIR / "events.jsonl")  # Machine-readable events
TEXT_FILE = str(ALERTS_DIR / "events.log")     # Human-readable events

# Serial port configuration (from config or environment)
PORT = os.getenv("EAS_PORT", CONFIG['serial_port'])
BAUD = int(os.getenv("EAS_BAUD", CONFIG['serial_baud']))

# Serial decoder filler byte (from config)
FILLER = bytes([CONFIG['filler_byte']])

DEDUPE_WINDOW_SEC = CONFIG['dedupe_window']
NTFY_URL = CONFIG['ntfy_url']

# Buffer configuration
MAX_BUFFER_SIZE = CONFIG['max_buffer_size']
BUFFER_TRIM_SIZE = CONFIG['buffer_trim_size']

# Serial and notification timeouts
SERIAL_TIMEOUT = CONFIG['serial_timeout']
SERIAL_RETRY_DELAY = CONFIG['serial_retry_delay']
NOTIFICATION_TIMEOUT = CONFIG['notification_timeout']

# Extract repeated SAME headers inside a burst (typically repeated 3x)
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


# =============================
# Serial Connection
# =============================

def wait_for_cable(port: str) -> None:
    if IS_LAPTOP:
        return  # Skip on laptop
    if os.path.exists(port):
        return
    logger.warning(f"Serial cable not detected ({port}). Waiting...")
    while not os.path.exists(port):
        time.sleep(1)
    logger.info(f"Serial cable detected ({port}).")

def open_serial(port: str, baud: int) -> serial.Serial | None:
    """Open serial port. Returns None on non-Pi systems (stdin mode)."""
    if IS_LAPTOP:
        logger.info("Running in TEST MODE (reading from stdin).")
        logger.info("Pipe data from virtual_tft.py or other source.")
        return None
    
    if not SERIAL_AVAILABLE:
        logger.error("pyserial not installed. Install with: pip install pyserial")
        sys.exit(1)
    
    while True:
        wait_for_cable(port)
        try:
            ser = serial.Serial(port, baud, timeout=SERIAL_TIMEOUT)
            logger.info(f"Opened {port} @ {baud} baud")
            return ser
        except SerialException as e:
            logger.error(f"Could not open {port}: {e}. Retrying...")
            time.sleep(SERIAL_RETRY_DELAY)


# =============================
# Formatting
# =============================

def receipt_block(title: str, lines: list[str]) -> str:
    """
    Clean, formatted alert display with box drawing.
    """
    # Calculate width for header
    max_len = max(len(title), max([len(l) for l in lines] if lines else [0]))
    header_width = min(max_len + 4, 70)  # Cap at 70 chars
    
    # Format output
    output = [f"┏━ {title}"]
    
    # Add all details with clean formatting
    for line in lines:
        if line:  # Skip empty lines for compactness
            output.append(f"  {line}")
    
    # Footer separator
    output.append(f"┗━{'─' * (header_width - 2)}")
    
    return "\n".join(output)


# =============================
# Main Loop
# =============================

def main() -> None:
    logger.info("EAS Alert Logger starting…")
    logger.info(f"Platform: {'Raspberry Pi' if not IS_LAPTOP else 'Development/Test'}")
    logger.info(f"Data directory: {DATA_DIR}")
    logger.info(f"Alert log files: {ALERTS_DIR}")
    logger.info(f"Logs directory: {LOGS_DIR}")

    seen: dict[str, float] = {}
    buf = ""

    ser = open_serial(PORT, BAUD)

    while True:
        try:
            if IS_LAPTOP:
                # Read from stdin (for piping from virtual_tft.py)
                line = sys.stdin.readline()
                if not line:
                    break
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
            else:
                # Read from serial port (Pi mode)
                chunk = ser.read(256)
                if not chunk:
                    continue
                chunk = chunk.replace(FILLER, b"")
                if not chunk:
                    continue
                text = chunk.decode("ascii", errors="ignore")
                if not text:
                    continue
        except (SerialException, KeyboardInterrupt) as e:
            if IS_LAPTOP:
                logger.info("Interrupted by user (Ctrl+C).")
                break  # Exit on Ctrl+C in laptop mode
            logger.error(f"Serial error (unplugged?): {e}")
            try:
                ser.close()
            except Exception:
                pass
            ser = open_serial(PORT, BAUD)
            continue

        buf += text
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
            canonical = headers[0] if headers else raw_burst
            repeat_count = len(headers)
            saw_eom = "NNNN" in raw_burst

            fp = fingerprint(canonical)
            now = time.time()
            if now - seen.get(fp, 0) < DEDUPE_WINDOW_SEC:
                continue
            seen[fp] = now

            received_local = now_local()

            # ---- Decode using EAS2Text ----
            try:
                oof = EAS2Text(canonical)
            except Exception as ex:
                logger.error(f"EAS decode failed: {ex}")
                logger.debug(f"Raw header: {normalize(canonical)}")
                # Still log raw if decode fails
                title = "EAS Decode Failed"
                block = receipt_block(title, [
                    f"Received: {received_local}",
                    f"Error: {ex}",
                    "",
                    "Raw header:",
                    normalize(canonical),
                ])
                append_line(TEXT_FILE, block + "\n")
                print(block)
                continue

            # Get full formatted message and locations from EAS2Text
            eas_message = getattr(oof, "EASText", None) or "EAS Event"
            title = eas_message.split('\n')[0] if eas_message else "EAS Event"

            fips_text_list = getattr(oof, "FIPSText", []) or []
            pretty_locations = [str(x) for x in fips_text_list] if isinstance(fips_text_list, list) else ([str(fips_text_list)] if fips_text_list else [])

            org_text = getattr(oof, "orgText", None) or getattr(oof, "ORG", None) or "Unknown"
            start_text = getattr(oof, "startTimeText", None) or "Unknown"
            end_text = getattr(oof, "endTimeText", None) or "Unknown"
            dur_text = getattr(oof, "timeText", None) or getattr(oof, "durationText", None)
            sender = getattr(oof, "fromText", None) or getattr(oof, "fromCode", None)

            # Build clean output - only include fields that have real data
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
            
            if dur_text and dur_text != "Unknown":
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

            # Log the alert
            logger.info(f"Alert received: {eas_message.split(chr(10))[0]} | Locations: {len(pretty_locations)} | Repeats: {repeat_count}")
            logger.debug(f"Header: {normalize(canonical)}")

            send_phone(str(title), block)

            print(block)

if __name__ == "__main__":
    main()
