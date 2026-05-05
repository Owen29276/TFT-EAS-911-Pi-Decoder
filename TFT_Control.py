#!/usr/bin/env python3
"""
TFT EAS 911 Remote Control
Sends commands to the TFT EAS 911 via COM3 (J303) using the PC/DTMF interface.

Requirements:
- COM3 (J303) connected to a USB-RS232 adapter
- Menu 19 on the TFT set to PC/DTMF INTERFACE with an access PIN configured

Usage:
    python3 tft_control.py rwt          — send weekly test with attention tone
    python3 tft_control.py rwt_notone   — send weekly test without attention tone
    python3 tft_control.py eom          — send end of message
    python3 tft_control.py reboot       — reboot the TFT unit
    python3 tft_control.py record       — record voice announcement from CH1
    python3 tft_control.py play         — play back recorded announcement
    python3 tft_control.py originate <event> <locations> <duration> [audio]
        event     — event number from TFTData (e.g. 34 for RWT, 42 for TOR)
        locations — location key numbers (e.g. 1 for key 1, 12 for keys 1 and 2)
        duration  — duration code (e.g. 01 = 15 min, 02 = 30 min, 04 = 1 hr)
        audio     — n (no audio), p (pre-recorded), l (live) — default: p
"""

import sys
import time
import logging
import configparser
from pathlib import Path

try:
    import serial
    from serial.serialutil import SerialException # type: ignore
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    SerialException = Exception


# =============================
# Configuration
# =============================

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.ini"
    cfg = {
        'com3_port':          '/dev/ttyUSB1',
        'com3_baud':          9600,
        'com3_pin':           '911',
        'com3_cmd_delay':     0.5,   # seconds between DTMF commands
        'com3_retry_delay':   2.0,
        'log_level':          'INFO',
        'log_dir':            str(Path(__file__).parent / "logs"),
    }
    if config_path.exists():
        c = configparser.ConfigParser()
        c.read(config_path)
        cfg['com3_port']      = c.get('com3', 'port',      fallback=cfg['com3_port'])
        cfg['com3_baud']      = c.getint('com3', 'baud',   fallback=cfg['com3_baud'])
        cfg['com3_pin']       = c.get('com3', 'pin',       fallback=cfg['com3_pin'])
        cfg['com3_cmd_delay'] = c.getfloat('com3', 'cmd_delay', fallback=cfg['com3_cmd_delay'])
        cfg['log_level']      = c.get('logging', 'log_level', fallback=cfg['log_level'])
        cfg['log_dir']        = c.get('logging', 'log_dir',   fallback=cfg['log_dir'])

    def resolve(p):
        p = str(p)
        p = p if not p.startswith('~') else str(Path(p).expanduser())
        return p if Path(p).is_absolute() else str(Path(__file__).parent / p)
    cfg['log_dir'] = resolve(cfg['log_dir'])
    return cfg


CONFIG = load_config()
PIN    = CONFIG['com3_pin']
DELAY  = CONFIG['com3_cmd_delay']

# =============================
# Logging
# =============================

logging.basicConfig(
    level=getattr(logging, CONFIG['log_level'].upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger("tft_control")


# =============================
# Serial connection
# =============================

def open_com3() -> 'serial.Serial':
    """Open COM3. Raises if not available."""
    if not SERIAL_AVAILABLE:
        raise RuntimeError("pyserial not installed — run: pip install pyserial")
    port = CONFIG['com3_port']
    baud = CONFIG['com3_baud']
    if not Path(port).exists():
        raise RuntimeError(f"COM3 port {port} not found — is the adapter plugged in?")
    ser = serial.Serial(port, baud, bytesize=8, stopbits=1, timeout=2) # type: ignore
    logger.info(f"COM3 opened: {port} @ {baud} baud")
    return ser


def send(ser: 'serial.Serial', cmd: str) -> None:
    """Send a single DTMF command string and wait for the TFT to process it."""
    ser.write(cmd.encode('utf-8'))
    logger.debug(f"Sent: {cmd!r}")
    time.sleep(DELAY)


# =============================
# Commands
# =============================

def cmd_rwt(ser: 'serial.Serial', attention_tone: bool = True) -> None:
    """Send a Required Weekly Test."""
    code = '31' if attention_tone else '30'
    send(ser, f'*{PIN}{code}#')
    logger.info(f"RWT sent {'with' if attention_tone else 'without'} attention tone")


def cmd_eom(ser: 'serial.Serial') -> None:
    """Send End of Message."""
    send(ser, f'*{PIN}43#')
    logger.info("EOM sent")


def cmd_reboot(ser: 'serial.Serial') -> None:
    """Reboot the TFT unit."""
    send(ser, f'*{PIN}91#')
    logger.info("Reboot command sent")


def cmd_record_announcement(ser: 'serial.Serial') -> None:
    """
    Start recording the voice announcement from CH1.
    Audio must already be playing into CH1 before calling this.
    Call cmd_stop() when recording is complete.
    """
    send(ser, f'*{PIN}21#')
    logger.info("Recording announcement from CH1 — call stop() when done")


def cmd_play_announcement(ser: 'serial.Serial') -> None:
    """Play back the recorded voice announcement."""
    send(ser, f'*{PIN}22#')
    logger.info("Playing announcement")


def cmd_stop(ser: 'serial.Serial') -> None:
    """Stop the current operation (recording, playback, live patch)."""
    send(ser, '#')
    logger.info("Stop sent")


def cmd_live_patch(ser: 'serial.Serial') -> None:
    """
    Patch CH1 audio live through the main output and trigger the on-air relay.
    Call cmd_stop() to end the patch.
    """
    send(ser, f'*{PIN}20#')
    logger.info("Live patch active — call stop() to end")


def cmd_originate(
    ser:       'serial.Serial',
    event:     str,
    locations: str,
    duration:  str,
    audio:     str = 'p',
) -> None:
    """
    Originate an EAS alert.

    event     — event number string from TFTData.json (e.g. '34' for RWT)
    locations — location key string (e.g. '1' or '12' for keys 1 and 2)
    duration  — duration code (e.g. '01'=15min, '02'=30min, '04'=1hr)
    audio     — 'n' no audio | 'p' pre-recorded announcement | 'l' live patch
    """
    audio = audio.lower()
    if audio not in ('n', 'p', 'l'):
        raise ValueError(f"audio must be n, p, or l — got {audio!r}")

    # Select origination command based on audio mode
    originate_cmd = '41' if audio == 'p' else '40'

    send(ser, f'*{PIN}{originate_cmd}#')
    send(ser, f'*{event}#')
    send(ser, f'*{locations}#')
    send(ser, f'*{duration}#')

    logger.info(f"Originating event={event} locations={locations} duration={duration} audio={audio}")

    if audio == 'l':
        logger.info("Live audio mode — call cmd_stop() to send EOM when done")


# =============================
# TFT event code lookup
# =============================

# Event codes and their TFT front panel key numbers
# Source: TFTData.json from tftcmd
TFT_EVENTS = {
    "EAN": "N/A", "EAT": "N/A", "NIC": "N/A", "NPT": "N/A",
    "ADR": "1",  "AVA": "2",  "AVW": "3",  "BZW": "4",
    "CAE": "5",  "CDW": "6",  "CEM": "7",  "CFA": "8",
    "CFW": "9",  "DSW": "10", "EQW": "11", "EVI": "12",
    "FRW": "13", "FFA": "14", "FFW": "15", "FFS": "16",
    "FLA": "17", "FLS": "18", "FLW": "19", "HMW": "20",
    "HWA": "21", "HWW": "22", "HUA": "23", "HUW": "24",
    "HLS": "25", "LEW": "26", "LAE": "27", "NMN": "28",
    "TOE": "29", "NUW": "30", "DMO": "31", "RHW": "32",
    "RMT": "33", "RWT": "34", "SVA": "35", "SVR": "36",
    "SVS": "37", "SPW": "38", "SMW": "39", "SPS": "40",
    "TOA": "41", "TOR": "42", "TRA": "43", "TRW": "44",
    "TSA": "45", "TSW": "46", "VOA": "47", "VOW": "48",
    "WSA": "49", "WSW": "50",
}

def event_code_to_number(code: str) -> str:
    """Convert an EAS event code (e.g. 'RWT') to its TFT key number."""
    code = code.upper()
    if code not in TFT_EVENTS:
        raise ValueError(f"Unknown event code: {code!r}")
    num = TFT_EVENTS[code]
    if num == "N/A":
        raise ValueError(f"{code} cannot be originated — national alerts only")
    return num


# =============================
# CLI entry point
# =============================

def usage():
    print(__doc__)
    sys.exit(1)

def main():
    if len(sys.argv) < 2:
        usage()

    cmd = sys.argv[1].lower()

    try:
        ser = open_com3()
    except Exception as e:
        logger.error(f"Could not open COM3: {e}")
        sys.exit(1)

    try:
        if cmd == 'rwt':
            cmd_rwt(ser, attention_tone=True)

        elif cmd == 'rwt_notone':
            cmd_rwt(ser, attention_tone=False)

        elif cmd == 'eom':
            cmd_eom(ser)

        elif cmd == 'reboot':
            confirm = input("Reboot the TFT unit? (y/N): ").strip().lower()
            if confirm == 'y':
                cmd_reboot(ser)
            else:
                print("Cancelled.")

        elif cmd == 'record':
            print("Make sure audio is playing into CH1, then press Enter to start recording...")
            input()
            cmd_record_announcement(ser)
            print("Recording... press Enter when done.")
            input()
            cmd_stop(ser)

        elif cmd == 'play':
            cmd_play_announcement(ser)

        elif cmd == 'originate':
            if len(sys.argv) < 5:
                print("Usage: originate <event_code> <locations> <duration> [audio]")
                print("  event_code — e.g. RWT, TOR, DMO")
                print("  locations  — location key numbers e.g. 1 or 12")
                print("  duration   — 01=15min 02=30min 04=1hr")
                print("  audio      — n p l (default: p)")
                sys.exit(1)
            try:
                event_num = event_code_to_number(sys.argv[2])
            except ValueError as e:
                logger.error(str(e))
                sys.exit(1)
            locations = sys.argv[3]
            duration  = sys.argv[4]
            audio     = sys.argv[5].lower() if len(sys.argv) > 5 else 'p'
            cmd_originate(ser, event_num, locations, duration, audio)
            if audio != 'l':
                print("Alert sent. Press Enter to send EOM.")
                input()
                cmd_eom(ser)

        elif cmd == 'patch':
            cmd_live_patch(ser)
            print("Live patch active. Press Enter to stop.")
            input()
            cmd_stop(ser)

        else:
            print(f"Unknown command: {cmd!r}")
            usage()

    except KeyboardInterrupt:
        logger.info("Interrupted.")
    finally:
        ser.close()
        logger.info("COM3 closed.")


if __name__ == "__main__":
    main()