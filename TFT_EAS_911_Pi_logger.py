#!/usr/bin/env python3
import os
import sys
import time
import json
import hashlib
import re
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
# Configuration
# =============================

# Platform detection
IS_PI = os.path.exists("/sys/class/gpio") or os.path.exists("/proc/device-tree/model")
IS_LAPTOP = not IS_PI

# Paths - use home directory on all platforms
JSONL_FILE = str(Path.home() / "events.jsonl")
TEXT_FILE = str(Path.home() / "events.log")

# Serial port configuration
PORT = "/dev/ttyUSB0"  # Default for TFT EAS 911 board (can be overridden via environment variable)
BAUD = 1200

# TFT EAS 911 often pads with 0xAB
FILLER = b"\xAB"

DEDUPE_WINDOW_SEC = 120
NTFY_URL = "https://ntfy.sh/owen_tft911"  # optional; leave blank to disable

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
            timeout=5
        )
    except Exception:
        pass


# =============================
# Serial Connection
# =============================

def wait_for_cable(port: str) -> None:
    if IS_LAPTOP:
        return  # Skip on laptop
    if os.path.exists(port):
        return
    print(f"[{now_local()}] Serial cable not detected ({port}). Waiting...")
    while not os.path.exists(port):
        time.sleep(1)
    print(f"[{now_local()}] Serial cable detected ({port}).")

def open_serial(port: str, baud: int) -> serial.Serial | None:
    """Open serial port. Returns None on non-Pi systems (stdin mode)."""
    if IS_LAPTOP:
        print(f"[{now_local()}] Running in TEST MODE (reading from stdin).")
        print(f"[{now_local()}] Pipe data from virtual_tft.py or other source.")
        return None
    
    if not SERIAL_AVAILABLE:
        print(f"[{now_local()}] ERROR: pyserial not installed. Install with: pip install pyserial")
        sys.exit(1)
    
    while True:
        wait_for_cable(port)
        try:
            ser = serial.Serial(port, baud, timeout=1)
            print(f"[{now_local()}] Opened {port} @ {baud} baud")
            return ser
        except SerialException as e:
            print(f"[{now_local()}] Could not open {port}: {e}. Retrying...")
            time.sleep(1)


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
    print(f"[{now_local()}] TFT 911 logger (EAS2Text) starting…")

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
                break  # Exit on Ctrl+C in laptop mode
            print(f"[{now_local()}] Serial error (unplugged?): {e}")
            try:
                ser.close()
            except Exception:
                pass
            ser = open_serial(PORT, BAUD)
            continue

        buf += text
        if len(buf) > 200000:
            buf = buf[-100000:]

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

            send_phone(str(title), block)

            print(block)

if __name__ == "__main__":
    main()
