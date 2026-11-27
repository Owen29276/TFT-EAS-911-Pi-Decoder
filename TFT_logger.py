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
import argparse
import platform
import threading
import configparser
from collections import Counter
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from pathlib import Path

from settings import CONFIG_PATH, read_config, resolve_path
from eas_utils import header_sender, strip_sender
from typing import Callable

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
    """Load config.ini (+ secrets.ini) if present, otherwise fall back to built-in defaults."""
    config_path = CONFIG_PATH
    # secrets.ini (gitignored, chmod 600) holds MQTT / transcription / stream credentials
    config = read_config(with_secrets=True)

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

    cfg['log_dir']    = resolve_path(cfg['log_dir'])
    cfg['alerts_dir'] = resolve_path(cfg['alerts_dir'])
    cfg['_config_found'] = config_path.exists()
    cfg['_parser'] = config
    return cfg


CONFIG = load_config()

PORT     = CONFIG['serial_port']
BAUD     = CONFIG['serial_baud']
FILLER   = bytes([CONFIG['filler_byte']])
NTFY_URL = f"https://ntfy.sh/{CONFIG['ntfy_topic']}" if CONFIG['ntfy_topic'].strip() else ''

ALERTS_DIR = Path(CONFIG['alerts_dir'])
LOGS_DIR   = Path(CONFIG['log_dir'])
JSONL_FILE = str(ALERTS_DIR / "events.jsonl")
TEXT_FILE  = str(ALERTS_DIR / "events.log")
TRANSCRIPTS_FILE = str(ALERTS_DIR / "transcripts.jsonl")

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

_SAME_TIME_RE = re.compile(r'\+(\d{4})-(\d{3})(\d{2})(\d{2})-')

def parse_issue_time(canonical: str, received: datetime | None = None) -> datetime | None:
    """
    Parse the JJJHHMM issue time from a SAME header.

    SAME headers carry no year, so the year comes from the receive time. An alert
    issued late Dec 31 but decoded after midnight Jan 1 would otherwise land a year
    in the future — anything more than 2 days ahead is treated as last year.
    """
    m = _SAME_TIME_RE.search(canonical)
    if not m:
        return None
    jjj, hh, mm = int(m.group(2)), int(m.group(3)), int(m.group(4))
    if not (1 <= jjj <= 366 and hh < 24 and mm < 60):
        return None
    ref = received or datetime.now(timezone.utc)
    offset = timedelta(days=jjj - 1, hours=hh, minutes=mm)
    issue = datetime(ref.year, 1, 1, tzinfo=timezone.utc) + offset
    if issue - ref > timedelta(days=2):
        issue = datetime(ref.year - 1, 1, 1, tzinfo=timezone.utc) + offset
    return issue


def compute_expires_utc(canonical: str, received: datetime | None = None) -> str | None:
    """Derive expires_utc from the SAME header's issue time + duration (None for +0000)."""
    try:
        m = _SAME_TIME_RE.search(canonical)
        if not m:
            return None
        dur_h, dur_m = int(m.group(1)[:2]), int(m.group(1)[2:])
        if dur_h == 0 and dur_m == 0:
            return None
        issue = parse_issue_time(canonical, received)
        if issue is None:
            return None
        return (issue + timedelta(hours=dur_h, minutes=dur_m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def issued_utc(canonical: str, received: datetime | None = None) -> str | None:
    issue = parse_issue_time(canonical, received)
    return issue.strftime("%Y-%m-%dT%H:%M:%SZ") if issue else None


def vote_header(headers: list[str]) -> tuple[str, int]:
    """
    Majority-vote the SAME header copies in one burst (normally 3).

    Returns (canonical_header, copies_received). If two or more copies agree,
    that copy wins. If every copy differs (bit errors), vote character by
    character across the copies that share the most common length.
    """
    clean = [" ".join(h.split()) for h in headers if h]
    if not clean:
        raise ValueError("no headers to vote on")
    best, count = Counter(clean).most_common(1)[0]
    if count > 1 or len(clean) == 1:
        return best, len(clean)
    length   = Counter(len(h) for h in clean).most_common(1)[0][0]
    same_len = [h for h in clean if len(h) == length]
    voted    = "".join(Counter(col).most_common(1)[0][0] for col in zip(*same_len))
    return voted, len(clean)


MAX_SUPPRESS = 6 * 3600   # never suppress a repeat for longer than this


def event_key(canonical: str) -> str:
    """
    Identity of an alert regardless of who relayed it.

    A forwarded message keeps ORG/EEE/locations/duration/issue time and only swaps
    the sender (47 CFR 11.31). So the same Tornado Warning heard on NWR (CH1) and
    again via an ERN relay (CH2), or re-sent by this TFT itself, has one event key.
    """
    return fingerprint(strip_sender(canonical))


def suppress_until(canonical: str, received: datetime, window: int) -> float:
    """
    Ignore repeats until the alert expires (relays can arrive minutes later),
    but at least `window` seconds and at most MAX_SUPPRESS.
    """
    start = received.timestamp()
    until = start + window
    exp = compute_expires_utc(canonical, received)
    if exp:
        exp_ts = datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        until = max(until, exp_ts)
    return min(until, start + MAX_SUPPRESS)


def load_recent_fingerprints(jsonl_path: str, window: int, now: float | None = None) -> dict[str, tuple[float, str]]:
    """
    Rebuild the dedupe table (event_key → (suppress_until, alert_id)) from recent
    JSONL records so a restart doesn't re-log a repeat or a late relay.
    """
    now = now if now is not None else time.time()
    seen: dict[str, tuple[float, str]] = {}
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            tail = f.readlines()[-200:]
    except OSError:
        return seen
    for line in tail:
        try:
            rec = json.loads(line)
            recv = datetime.strptime(rec["received_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            until = suppress_until(rec["canonical_header"], recv, window)
            if until > now:
                seen[event_key(rec["canonical_header"])] = (until, rec.get("alert_id", "?"))
        except (ValueError, KeyError, TypeError):
            continue
    return seen

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
# Burst parsing
# =============================

# Match a SAME header — printable ASCII only to filter serial noise
HEADER_RE = re.compile(r"(ZCZC-[\x20-\x7E]*?-)(?=ZCZC|NNNN|$)")


def extract_bursts(buf: str) -> tuple[list[str], str]:
    """
    Pull every complete ZCZC…NNNN burst out of the buffer.
    Returns (bursts, leftover_buffer). Junk before the first ZCZC is dropped.
    """
    bursts = []
    while True:
        start = buf.find("ZCZC")
        if start < 0:
            # Keep a short tail in case "ZCZC" is split across reads
            return bursts, buf[-3:]
        end = buf.find("NNNN", start)
        if end < 0:
            return bursts, buf[start:]
        bursts.append(buf[start:end + 4])
        buf = buf[end + 4:]


# A complete, well-formed header — used to identify an alert while it is still
# on air (before NNNN), so a repeat/relay doesn't start the speakers again.
LIVE_HEADER_RE = re.compile(
    r"ZCZC-[A-Z]{3}-[A-Z?]{3}(?:-\d{6})+\+\d{4}-\d{7}-[^-]{1,8}-")


def first_complete_header(buf: str) -> str | None:
    m = LIVE_HEADER_RE.search(" ".join(buf.split()))
    return m.group(0) if m else None


def header_event_text(header: str) -> str:
    """'a Tornado Warning' for a header, or '' if EAS2Text can't decode it."""
    if not EAS2TEXT_AVAILABLE:
        return ""
    try:
        return getattr(EAS2Text(header), "evntText", "") or ""  # type: ignore[misc]
    except Exception:
        return ""


def is_recent_repeat(header: str, seen: dict, now: float | None = None) -> bool:
    entry = seen.get(event_key(header))
    return bool(entry) and entry[0] > (now if now is not None else time.time())


def headers_in_burst(raw_burst: str) -> list[str]:
    return [h for h in HEADER_RE.findall(raw_burst) if h.startswith("ZCZC-")]


# =============================
# Alert handling
# =============================

def _notify_async(title: str, body: str) -> None:
    """Send the ntfy push off the main loop so a slow network never delays logging."""
    def run():
        receipt = send_notification(title, body)
        logger.debug(f"Notification receipt for '{title}': {receipt}")
    threading.Thread(target=run, name="ntfy", daemon=False).start()


def _run_hook(hook, record: dict) -> None:
    if hook is None:
        return
    try:
        hook(record)
    except Exception:
        logger.exception("Post-save hook failed (alert is still logged)")


def handle_burst(raw_burst: str, seen: dict[str, tuple[float, str]],
                 on_saved: Callable[[dict], None] | None = None) -> dict | None:
    """
    Decode, dedupe, save and notify one burst. Returns the saved record (or None).
    on_saved(record) runs after the record is on disk (audio attach, Home Assistant).
    """
    headers = headers_in_burst(raw_burst)
    if not headers:
        logger.warning("Burst had no valid SAME headers — discarding.")
        return None

    canonical, copies = vote_header(headers)
    logger.info(f"SAME burst detected ({copies} header cop{'y' if copies == 1 else 'ies'})")

    # --- Deduplicate (same alert via NWR, an ERN relay, or our own forward) ---
    fp  = fingerprint(canonical)
    key = event_key(canonical)
    received_dt = datetime.now(timezone.utc)
    now = received_dt.timestamp()
    for k in [k for k, (until, _) in seen.items() if until <= now]:
        del seen[k]
    if key in seen:
        logger.info(f"Duplicate of {seen[key][1]} (sender {header_sender(canonical) or '?'}) — skipping.")
        return None

    received_local = now_local()
    alert_id = f"{received_dt:%Y%m%dT%H%M%SZ}-{fp[:8]}"
    seen[key] = (suppress_until(canonical, received_dt, CONFIG['dedupe_window']), alert_id)
    base = {
        "alert_id":         alert_id,
        "received_utc":     received_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "received_local":   received_local,
        "canonical_header": canonical,
        "repeat_count":     copies,
        "issued_utc":       issued_utc(canonical, received_dt),
        "expires_utc":      compute_expires_utc(canonical, received_dt),
        "raw_burst":        raw_burst,
    }
    ntfy_enabled = bool(NTFY_URL and requests is not None)

    # --- Decode with EAS2Text ---
    decode_error = None
    oof = None
    if not EAS2TEXT_AVAILABLE:
        decode_error = "EAS2Text not installed"
        logger.warning("EAS2Text not installed — alert logged without decode")
    else:
        try:
            oof = EAS2Text(canonical)  # type: ignore[misc]
        except Exception as ex:
            decode_error = str(ex)
            logger.exception(f"EAS2Text decode failed: {ex}")

    if oof is None:
        record = {**base, "decode_error": decode_error, "notification": {"attempted": ntfy_enabled}}
        append_line(JSONL_FILE, json.dumps(record, ensure_ascii=False))
        if ntfy_enabled:
            _notify_async("EAS alert (undecoded)", canonical)
        _run_hook(on_saved, record)
        return record

    eas_text  = getattr(oof, "EASText",  None) or "EAS Event"
    title     = eas_text.split('\n')[0]
    fips_list = getattr(oof, "FIPSText", []) or []
    locations = [str(x) for x in fips_list] if isinstance(fips_list, list) else ([str(fips_list)] if fips_list else [])
    org_text  = getattr(oof, "orgText",  None) or "Unknown"
    evt_text  = getattr(oof, "evntText", None) or ""
    sender    = getattr(oof, "fromText", None) or header_sender(canonical)

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

    record = {
        **base,
        "event_code":       getattr(oof, "evnt", None) or "",
        "originator_code":  getattr(oof, "org",  None) or "",
        "sender":           sender,
        "event_text":       evt_text,
        "org_text":         org_text,
        "eas_text":         eas_text,
        "locations_pretty": locations,
        "notification":     {"attempted": ntfy_enabled},
    }

    # --- Save first, then notify: a slow/failed push can never lose the record ---
    append_line(JSONL_FILE, json.dumps(record, ensure_ascii=False))
    append_line(TEXT_FILE,  text_block + "\n")
    logger.info(f"Logged: {title} | {len(locations)} location(s)")
    print(f"\n{text_block}\n", flush=True)
    if ntfy_enabled:
        _notify_async(title, text_block)
    _run_hook(on_saved, record)
    return record


# =============================
# Main Loop
# =============================

def read_recent_records(path: str, limit: int = 200) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def start_integrations(cp: configparser.ConfigParser):
    """
    Start optional audio capture, transcription upload and Home Assistant MQTT.
    Returns (audio, ha, on_saved). Each piece is independent and fails soft:
    the logger keeps logging even if all of them are broken.
    """
    from audio_capture import AudioConfig, start_audio
    from transcribe_client import TranscribeConfig, TranscribeClient
    from ha_mqtt import MQTTConfig, HAPublisher
    from live_stream import StreamConfig, listen_url, start_stream

    acfg = AudioConfig.from_parser(cp, Path(__file__).parent)
    scfg = StreamConfig.from_parser(cp)
    stream = None
    if scfg.enabled and not acfg.enabled:
        logger.error("[stream] needs [audio] enabled = true (it shares the recorder) — live stream off")
    elif scfg.enabled:
        stream = start_stream(scfg, acfg.sample_rate)

    mcfg = MQTTConfig.from_parser(cp)
    ha = HAPublisher(mcfg, stream_url=listen_url(scfg, mcfg.host or "192.0.2.1") if stream else "")
    if not ha.start(read_recent_records(JSONL_FILE)):
        ha = None

    transcriber = TranscribeClient(
        TranscribeConfig.from_parser(cp), TRANSCRIPTS_FILE,
        on_transcript=(ha.publish_transcript if ha else None))
    transcriber.start()

    if not cp.has_option("audio", "audio_dir"):
        acfg.audio_dir = str(ALERTS_DIR / "audio")
    audio = start_audio(acfg, on_ready=transcriber.submit,
                        sinks=[stream.write] if stream else None,
                        on_air=((lambda on, hdr: ha.publish_on_air(on, hdr, header_event_text(hdr) if hdr else ""))
                                if ha else None))
    if audio:
        logger.info(f"Audio capture on → {acfg.audio_dir}"
                    + ("" if transcriber.cfg.enabled else " (no [transcribe] url — audio saved only)"))
        if ha and acfg.mode != "forward":
            logger.info("[audio] mode = lookback — the On air sensor needs forward mode, it will stay off")

    def on_saved(record: dict) -> None:
        if audio:
            audio.attach(record)
        if ha:
            ha.publish_alert(record)

    return audio, ha, on_saved


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="TFT EAS 911 SAME logger")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--stdin",  action="store_true",
                     help="read SAME data from stdin (e.g. python3 virtual_tft.py 1 | python3 TFT_logger.py --stdin)")
    src.add_argument("--serial", action="store_true",
                     help="read from the configured serial port (default unless stdin is piped)")
    return p.parse_args(argv)


def stdin_is_piped(fd: int = 0) -> bool:
    """
    True when stdin is a pipe or redirected file (`virtual_tft.py | TFT_logger.py`).
    Under systemd stdin is /dev/null (a char device) and on a terminal it's a TTY —
    both mean "use the serial port". Works the same on a Pi, a Proxmox VM or a laptop.
    """
    import stat
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISREG(mode)


def main(argv=None) -> None:
    args = parse_args(argv)
    # Default: serial on a Pi, stdin elsewhere. Flags override the guess.
    use_stdin = args.stdin or (not args.serial and stdin_is_piped())

    logger.info(f"EAS Logger starting | Host: {platform.node()} | "
                f"Input: {'stdin' if use_stdin else PORT}")
    if CONFIG['_config_found']:
        logger.info(f"Config: config.ini | Port: {PORT} @ {BAUD} | ntfy: {'on' if NTFY_URL else 'off'} | dedupe: {CONFIG['dedupe_window']}s")
    else:
        logger.warning("config.ini not found — using built-in defaults.")

    seen = load_recent_fingerprints(JSONL_FILE, CONFIG['dedupe_window'])
    if seen:
        logger.info(f"Restored {len(seen)} recent alert fingerprint(s) for dedupe")
    audio, ha, on_saved = start_integrations(CONFIG['_parser'])
    buf = ""
    live_pending = False        # capture started, waiting for its first full header
    ser = None if use_stdin else open_serial(PORT, BAUD)

    try:
        while True:
            # --- Read from serial or stdin ---
            try:
                if use_stdin:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                else:
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

            if audio:
                window = buf[-3:] + text        # catch markers split across reads
                if "ZCZC" in window and audio.trigger():   # no-op if already capturing
                    live_pending = True
                if live_pending:
                    hdr = first_complete_header(buf + text)
                    if hdr:
                        live_pending = False
                        if is_recent_repeat(hdr, seen):
                            logger.info(f"Repeat of a recent alert on air ({header_sender(hdr) or '?'}) "
                                        "— not signalling On air")
                        elif audio.go_live(hdr):
                            logger.info(f"On air: {hdr}")
                if "NNNN" in window:
                    audio.note_eom()
                    live_pending = False

            bursts, buf = extract_bursts(buf + text)
            for raw_burst in bursts:
                try:
                    rec = handle_burst(raw_burst, seen, on_saved)
                    if rec is None and audio:
                        audio.discard()         # duplicate / junk burst: free the recorder now
                except Exception:
                    # Never let one bad burst kill the daemon
                    logger.exception("Unhandled error processing burst")

    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        try:
            if ser: ser.close()
        except Exception:
            pass
        if ha:
            ha.stop()
        logger.info("Logger shut down.")


if __name__ == "__main__":
    main()
