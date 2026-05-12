#!/usr/bin/env python3
"""
TFT EAS 911 Controller
Remote control of the TFT EAS 911 via COM3 (J303) PC/DTMF interface.

Can be used as:
  - Importable module: from tft_control import TFTController
  - CLI tool:          python3 tft_control.py

Requirements:
  - COM3 (J303) connected via USB-RS232 adapter
  - Menu 19 on TFT set to PC/DTMF INTERFACE with PIN configured
  - espeak installed for TTS announcement recording (sudo apt install espeak)
"""

import sys
import time
import logging
import subprocess
import configparser
from pathlib import Path

from utills import build_same_header, decode_header, fips_table, search_fips, parse_location_keys

try:
    import serial
    from serial.serialutil import SerialException
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    SerialException = Exception


# =============================
# Configuration
# =============================

def load_config() -> dict:
    """Load config.ini, falling back to built-in defaults if missing."""
    config_path = Path(__file__).parent / "config.ini"
    cfg = {
        'com3_port':      '/dev/tft911-cmd',
        'com3_baud':      9600,
        'com3_pin':       '911',
        'com3_cmd_delay': 0.5,
        'log_level':      'INFO',
        'tts_speed':      110,
        'tts_pitch':      35,
        'tz_offset':      None,
        'callsign':       'STATION',
        'org':            'EAS',
    }
    if config_path.exists():
        c = configparser.ConfigParser()
        c.read(config_path)
        cfg['com3_port']      = c.get('control',    'port',      fallback=cfg['com3_port'])
        cfg['com3_baud']      = c.getint('control', 'baud',      fallback=cfg['com3_baud'])
        cfg['com3_pin']       = c.get('control',    'pin',       fallback=cfg['com3_pin'])
        cfg['com3_cmd_delay'] = c.getfloat('control', 'cmd_delay', fallback=cfg['com3_cmd_delay'])
        cfg['log_level']      = c.get('logging',    'log_level', fallback=cfg['log_level'])
        cfg['tts_speed']      = c.getint('tts',     'speed',     fallback=cfg['tts_speed'])
        cfg['tts_pitch']      = c.getint('tts',     'pitch',     fallback=cfg['tts_pitch'])
        cfg['callsign']       = c.get('station',    'callsign',  fallback=cfg['callsign'])
        cfg['org']            = c.get('station',    'org',       fallback=cfg['org'])
        raw_tz = c.get('station', 'tz_offset', fallback='')
        if raw_tz.strip():
            try:
                cfg['tz_offset'] = int(raw_tz.strip())
            except ValueError:
                pass
    return cfg


def load_location_keys() -> dict:
    """
    Load [location_keys] from config.ini.

    Returns a dict keyed by string key number:
        {"1": {"name": "Tompkins County", "fips": ["036109", "036001"]}, ...}

    Returns an empty dict if the section is missing or the file doesn't exist.
    """
    config_path = Path(__file__).parent / "config.ini"
    if not config_path.exists():
        return {}
    c = configparser.ConfigParser()
    c.read(config_path)
    if not c.has_section("location_keys"):
        return {}
    keys = {}
    for k, v in c.items("location_keys"):
        if "|" in v:
            name, fips_str = v.split("|", 1)
            fips_list = [f.strip() for f in fips_str.split(",") if f.strip()]
        else:
            name, fips_list = v.strip(), []
        keys[k] = {"name": name.strip(), "fips": fips_list}
    return keys


# =============================
# Event code lookup
# =============================

# Event codes that exist in EAS but cannot be locally originated
_NON_ORIGINATABLE = frozenset({"EAN", "EAT", "NIC", "NPT"})

# Maps originatable EAS event codes to TFT front panel key numbers
TFT_EVENTS = {
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


# =============================
# Controller class
# =============================

class TFTController:
    """
    Remote control interface for the TFT EAS 911 via COM3 PC/DTMF.

    Usage:
        tft = TFTController()
        tft.connect()
        tft.send_rwt()
        tft.disconnect()

    Or as a context manager:
        with TFTController() as tft:
            tft.send_rwt()
    """

    def __init__(self, config: dict = None):
        """
        Initialize the controller.

        Args:
            config: Optional config dict. If None, loads from config.ini.
        """
        self.config = config or load_config()
        self.ser    = None
        self.pin    = self.config['com3_pin']
        self.delay  = self.config['com3_cmd_delay']
        self.logger = logging.getLogger("tft_control")

    def connect(self) -> None:
        """Open the COM3 serial connection."""
        if not SERIAL_AVAILABLE:
            raise RuntimeError("pyserial not installed — run: pip install pyserial")
        port = self.config['com3_port']
        baud = self.config['com3_baud']
        if not Path(port).exists():
            raise RuntimeError(f"COM3 port {port} not found — is the adapter plugged in?")
        self.ser = serial.Serial(port, baud, bytesize=8, stopbits=1, timeout=2)
        self.logger.info(f"COM3 connected: {port} @ {baud} baud")

    def disconnect(self) -> None:
        """Close the COM3 serial connection."""
        if self.ser and self.ser.is_open:
            self.ser.close()
            self.logger.info("COM3 disconnected.")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    def _send(self, cmd: str) -> None:
        """
        Send a single DTMF command to the TFT and wait for processing.

        Args:
            cmd: Command string e.g. '*91131#'
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Not connected — call connect() first.")
        self.ser.write(cmd.encode('utf-8'))
        self.logger.debug(f"Sent: {cmd!r}")
        time.sleep(self.delay)

    def send_rwt(self, attention_tone: bool = True) -> None:
        """
        Send a Required Weekly Test.

        Args:
            attention_tone: If True, sends with attention tone. Default True.
        """
        cmd = '31' if attention_tone else '30'
        self._send(f'*{self.pin}{cmd}#')
        self.logger.info(f"RWT sent {'with' if attention_tone else 'without'} attention tone")

    def send_eom(self) -> None:
        """Send End of Message."""
        self._send(f'*{self.pin}43#')
        self.logger.info("EOM sent")

    def stop(self) -> None:
        """Stop the current operation (recording, playback, live patch)."""
        self._send('#')
        self.logger.info("Stop sent")

    def reboot(self) -> None:
        """Reboot the TFT unit."""
        self._send(f'*{self.pin}91#')
        self.logger.info("Reboot command sent")

    def record_voice(self) -> None:
        """Start recording a voice message from CH1. Call stop() when done."""
        self._send(f'*{self.pin}09#')
        self.logger.info("Recording voice from CH1")

    def play_voice(self) -> None:
        """Play back the recorded voice message. Call stop() when done."""
        self._send(f'*{self.pin}11#')
        self.logger.info("Playing voice message")

    def record_announcement(self) -> None:
        """Start recording the announcement from CH1. Call stop() when done."""
        self._send(f'*{self.pin}21#')
        self.logger.info("Recording announcement from CH1")

    def play_announcement(self) -> None:
        """Play back the recorded announcement. Call stop() when done."""
        self._send(f'*{self.pin}22#')
        self.logger.info("Playing announcement")

    def live_patch(self) -> None:
        """Patch CH1 audio live through main output. Call stop() when done."""
        self._send(f'*{self.pin}20#')
        self.logger.info("Live patch active")

    def originate(self, event: str, locations: str, duration: str, audio: str = 'p') -> None:
        """
        Originate an EAS alert.

        Args:
            event:     EAS event code e.g. 'RWT', 'TOR', 'DMO'
            locations: Location key string e.g. '1' or '12' for keys 1 and 2
            duration:  Duration code e.g. '01'=15min '02'=30min '04'=1hr
            audio:     'n' no audio | 'p' pre-recorded | 'l' live patch
        """
        audio = audio.lower()
        if audio not in ('n', 'p', 'l'):
            raise ValueError(f"audio must be n, p, or l — got {audio!r}")
        code = TFT_EVENTS.get(event.upper())
        if code is None:
            if event.upper() in _NON_ORIGINATABLE:
                raise ValueError(f"{event!r} is a national-level event and cannot be locally originated")
            raise ValueError(f"Unknown event code: {event!r}")
        originate_cmd = '41' if audio == 'p' else '40'
        dtmf_locs = locations.replace(',', '')
        self._send(f'*{self.pin}{originate_cmd}#')
        self._send(f'*{code}#')
        self._send(f'*{dtmf_locs}#')
        self._send(f'*{duration}#')
        self.logger.info(f"Originating {event} | locations={locations} | duration={duration} | audio={audio}")

    def record_announcement_tts(self, text: str) -> None:
        """
        Generate TTS audio from text using espeak, play it into CH1,
        and record it as the TFT announcement — all in one step.

        Args:
            text: The announcement text to speak.
        """
        speed = self.config['tts_speed']
        pitch = self.config['tts_pitch']
        self.logger.info(f"Recording TTS announcement: {text!r}")

        # Start TFT recording before audio plays
        self.record_announcement()
        time.sleep(0.3)

        try:
            espeak = subprocess.Popen(
                ['espeak', '-s', str(speed), '-p', str(pitch), text, '--stdout'],
                stdout=subprocess.PIPE
            )
            aplay = subprocess.Popen(
                ['aplay', '-D', 'default'],
                stdin=espeak.stdout
            )
            espeak.stdout.close()
            aplay.wait()
            espeak.wait()
        except FileNotFoundError:
            self.stop()
            raise RuntimeError("espeak not found — run: sudo apt install espeak")

        time.sleep(0.2)
        self.stop()
        self.logger.info("TTS announcement recorded successfully")

    def originate_with_tts(self, event: str, locations: str, duration: str) -> str:
        """
        Auto-generate a TFT-style announcement from the alert parameters,
        record it via TTS, then originate with pre-recorded audio.

        Builds a SAME header from the event/locations/duration, decodes it
        to human-readable text using EAS2Text (TFT mode + station timezone),
        records that text as the TFT announcement, then sends the originate
        command with audio='p' (pre-recorded).

        Args:
            event:     EAS event code e.g. 'TOR', 'DMO'
            locations: Location key string e.g. '13' for keys 1 and 3
            duration:  TFT duration code e.g. '01'=15min '04'=1hr

        Returns:
            The decoded announcement text that was recorded.
        """
        loc_keys  = load_location_keys()
        fips_list = []
        for key in parse_location_keys(locations):
            if key in loc_keys:
                fips_list.extend(loc_keys[key]['fips'])
        if not fips_list:
            raise ValueError(f"No FIPS codes found for location keys: {locations!r} — run setup wizard")

        same = build_same_header(
            event, fips_list, duration,
            org=self.config.get('org', 'EAS'),
            callsign=self.config.get('callsign', 'STATION'),
        )
        self.logger.debug(f"Built SAME header: {same}")

        text = decode_header(same, self.config.get('tz_offset'))
        self.logger.info(f"TTS text: {text}")

        self.record_announcement_tts(text)
        self.originate(event, locations, duration, audio='p')
        return text


# =============================
# Setup wizard
# =============================

def _fips_to_name(fips: str) -> str:
    """Look up county name from a 6-digit FIPS code via the EAS2Text table."""
    # fips_table() uses 5-digit keys; our stored codes are 6-digit with leading '0'
    key = fips[1:] if len(fips) == 6 else fips
    return fips_table().get(key, "")


def _select_counties() -> tuple[list, str]:
    """
    Interactive loop for building a location key's FIPS list.

    Each iteration the user can:
      - Type a county name  → shows numbered results, pick by number
      - Type a FIPS code    → added directly (5 or 6 digits)
      - Press Enter         → done, returns what's been collected

    Returns (fips_list, suggested_name).
    """
    selected_fips:  list = []
    selected_names: list = []
    last_results:   list = []

    while True:
        if selected_fips:
            print(f"  Selected: {', '.join(selected_names)}")

        raw = input("  County name / FIPS / pick # from list (blank to finish): ").strip()
        if not raw:
            break

        # Pick from previous search results by number
        if raw.isdigit() and last_results and 1 <= int(raw) <= len(last_results):
            fips, name = last_results[int(raw) - 1]
            selected_fips.append(fips)
            selected_names.append(name)
            print(f"  + {name}  ({fips})")
            last_results = []
            continue

        # Direct FIPS entry (5 or 6 digits)
        if raw.isdigit() and len(raw) in (5, 6):
            fips = raw if len(raw) == 6 else f"0{raw}"
            name = _fips_to_name(fips) or fips
            selected_fips.append(fips)
            selected_names.append(name)
            print(f"  + {name}  ({fips})")
            last_results = []
            continue

        # Name search
        results = search_fips(raw)
        if not results:
            print("  No matches — try a different spelling.")
            last_results = []
            continue

        last_results = results
        for i, (fips, name) in enumerate(results, 1):
            print(f"    {i:2}.  {name:<35}  {fips}")

    suggested = selected_names[0] if selected_names else ""
    return selected_fips, suggested


def _ask(prompt: str, default: str = "") -> str:
    """
    Prompt the user for input, showing a default value if one exists.

    Args:
        prompt:  The question to display.
        default: Pre-filled value shown in brackets. Returned as-is if user
                 just presses Enter.

    Returns:
        The user's answer, or the default if they pressed Enter.
    """
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer if answer else default


def setup_wizard():
    """
    Interactive first-run wizard that builds the [station] section of config.ini.

    Walks the user through:
      - Station identity (callsign, FIPS code, ORG code)
      - Timezone and DST setting
      - COM3 PIN
      - Location key assignments (name + FIPS codes for each encoder key)

    All answers are saved to config.ini so they persist between runs.
    The wizard can be re-run at any time to update the configuration.

    Returns:
        dict: The station configuration that was saved.
    """
    config_path = Path(__file__).parent / "config.ini"
    config = configparser.ConfigParser()

    # Load whatever is already in config.ini so we can show existing values
    # as defaults and avoid making the user retype things they already set.
    if config_path.exists():
        config.read(config_path)

    # Check if setup has already been completed
    already_configured = (
        config.has_section("station") and
        config.get("station", "callsign", fallback="") != ""
    )

    if already_configured:
        print("\n━━━ Station already configured ━━━")
        print(f"  Callsign : {config.get('station', 'callsign')}")
        print(f"  FIPS     : {config.get('station', 'fips')}")
        print(f"  ORG      : {config.get('station', 'org')}")
        print()
        rerun = input("  Re-run setup wizard? (y/N): ").strip().lower()
        if rerun != "y":
            print("  Keeping existing configuration.\n")
            return dict(config["station"])

    print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  TFT EAS 911 — First-Time Setup Wizard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  This wizard saves your station's identity
  to config.ini. Run it once, update any time.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")

    # ── Station identity ──────────────────────────────────────────────────
    print("[ Station identity ]")

    # Callsign: up to 8 characters, stored exactly as entered (TFT limit)
    callsign = _ask("Station callsign (max 8 chars)",
                    config.get("station", "callsign", fallback=""))
    callsign = callsign[:8].upper()

    # FIPS: the 6-digit county code that identifies this station's coverage area
    fips = _ask("Primary FIPS code (6 digits)",
                config.get("station", "fips", fallback=""))

    # ORG: one of EAS, CIV, WXR, PEP — determines the alert originator type
    print("  ORG codes: EAS = EAS participant | CIV = Civil authority")
    print("             WXR = Weather service | PEP = Primary entry point")
    org = _ask("ORG code", config.get("station", "org", fallback="EAS")).upper()

    # ── Timezone & DST ────────────────────────────────────────────────────
    print("\n[ Time zone ]")
    print("  Enter your UTC offset as a signed integer.")
    print("  Eastern = -5, Central = -6, Mountain = -7, Pacific = -8")
    tz_offset = _ask("UTC offset (e.g. -5 for Eastern)",
                     config.get("station", "tz_offset", fallback="-5"))
    dst = _ask("Daylight saving enabled? (yes/no)",
               config.get("station", "dst", fallback="yes")).lower()

    # ── COM3 PIN ──────────────────────────────────────────────────────────
    print("\n[ COM3 remote control ]")
    print("  This is the PIN set in TFT menu item 19 (PC/DTMF interface).")
    pin = _ask("COM3 PIN", config.get("control", "pin", fallback="911"))

    # ── Location keys ─────────────────────────────────────────────────────
    print("\n[ Encoder location keys ]")
    print("  The TFT has 14 location keys. Each key can hold up to 31 FIPS codes.")
    print("  Search by county name, pick from the list, or type a FIPS code directly.")
    print("  Add as many counties as you need per key, then press Enter to move on.\n")

    location_keys = {}
    for key_num in range(1, 15):
        print(f"  ── Key {key_num} ──")
        fips_list, suggested = _select_counties()
        if not fips_list:
            break
        name = _ask(f"  Key {key_num} label", suggested).strip() or suggested
        location_keys[str(key_num)] = {"name": name, "fips": fips_list}
        print()

    # ── Confirm and save ──────────────────────────────────────────────────
    print("\n━━━ Review ━━━")
    print(f"  Callsign  : {callsign}")
    print(f"  FIPS      : {fips}")
    print(f"  ORG       : {org}")
    print(f"  TZ offset : {tz_offset}  DST: {dst}")
    print(f"  COM3 PIN  : {pin}")
    print(f"  Location keys configured: {len(location_keys)}")
    for k, v in location_keys.items():
        print(f"    Key {k}: {v['name']} → {', '.join(v['fips'])}")
    print()

    confirm = input("  Save this configuration? (Y/n): ").strip().lower()
    if confirm == "n":
        print("  Cancelled — nothing saved.\n")
        return {}

    # Write [station] section
    if not config.has_section("station"):
        config.add_section("station")
    config.set("station", "callsign",  callsign)
    config.set("station", "fips",      fips)
    config.set("station", "org",       org)
    config.set("station", "tz_offset", tz_offset)
    config.set("station", "dst",       dst)

    # Update PIN in [control] section while we have it
    if not config.has_section("control"):
        config.add_section("control")
    config.set("control", "pin", pin)

    # Store location keys: each key gets its own line in [location_keys]
    # Format: 1 = Tompkins County | 036109,036001
    # The pipe separator keeps name and FIPS together in a single value.
    if not config.has_section("location_keys"):
        config.add_section("location_keys")
    for k, v in location_keys.items():
        fips_str = ",".join(v["fips"])
        config.set("location_keys", k, f"{v['name']} | {fips_str}")

    with open(config_path, "w") as f:
        config.write(f)

    print(f"  Saved to {config_path}\n")

    # Return the station dict so callers can use it without re-reading the file
    return {
        "callsign":  callsign,
        "fips":      fips,
        "org":       org,
        "tz_offset": tz_offset,
        "dst":       dst,
        "location_keys": location_keys,
    }


# =============================
# CLI
# =============================

def print_menu():
    print("""
TFT EAS 911 Controller
━━━━━━━━━━━━━━━━━━━━━━
  1  Record voice message
  2  Play voice message
  3  Live patch
  4  Record announcement (manual)
  5  Record announcement (TTS)
  6  Play announcement
  7  Send weekly test
  8  Originate alert
  9  Send EOM
  10 Reboot unit
  s  Setup wizard
  q  Quit
""")


def cli():
    """Interactive CLI for the TFT controller."""
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    try:
        tft = TFTController()
        tft.connect()
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    print("Connected to TFT EAS 911 via COM3.")

    try:
        while True:
            print_menu()
            selection = input("Selection: ").strip().lower()

            if selection == '1':
                input("Press Enter to start recording voice...")
                tft.record_voice()
                input("Recording... press Enter when done.")
                tft.stop()

            elif selection == '2':
                tft.play_voice()
                input("Playing... press Enter when done.")
                tft.stop()

            elif selection == '3':
                tft.live_patch()
                input("Live patch active... press Enter to stop.")
                tft.stop()

            elif selection == '4':
                input("Press Enter to start recording announcement...")
                tft.record_announcement()
                input("Recording... press Enter when done.")
                tft.stop()

            elif selection == '5':
                text = input("Announcement text: ").strip()
                if text:
                    try:
                        tft.record_announcement_tts(text)
                        print("Done — announcement recorded.")
                    except Exception as e:
                        print(f"Error: {e}")

            elif selection == '6':
                tft.play_announcement()
                input("Playing... press Enter when done.")
                tft.stop()

            elif selection == '7':
                tone = input("Attention tone? (y/n, default y): ").strip().lower()
                tft.send_rwt(attention_tone=(tone != 'n'))
                print("RWT sent.")

            elif selection == '8':
                loc_keys = load_location_keys()
                if loc_keys:
                    print("\nConfigured location keys:")
                    for k in sorted(loc_keys, key=lambda x: int(x)):
                        v = loc_keys[k]
                        fips_str = ", ".join(v["fips"]) if v["fips"] else "no FIPS"
                        print(f"  {k} — {v['name']}  ({fips_str})")
                    print()
                else:
                    print("(No location keys configured — run setup wizard first)")
                    print()
                print("Event codes: RWT, DMO, TOR, SVR, FFW, CEM etc.")
                event = input("Event code: ").strip().upper()
                locs  = input("Location keys to include (e.g. 1 or 13 for keys 1 and 3): ").strip()
                dur   = input("Duration (01=15min 02=30min 04=1hr): ").strip()
                use_tts = input("Auto-generate TTS announcement? (Y/n): ").strip().lower()
                try:
                    if use_tts != 'n':
                        text = tft.originate_with_tts(event, locs, dur)
                        print(f"\nAnnouncement: {text}")
                    else:
                        audio = input("Audio (n/p/l, default p): ").strip().lower() or 'p'
                        tft.originate(event, locs, dur, audio)
                    input("Alert sent. Press Enter to send EOM.")
                    tft.send_eom()
                except (ValueError, RuntimeError) as e:
                    print(f"Error: {e}")

            elif selection == '9':
                tft.send_eom()
                print("EOM sent.")

            elif selection == '10':
                confirm = input("Reboot the TFT unit? (y/N): ").strip().lower()
                if confirm == 'y':
                    tft.reboot()
                    print("Reboot command sent.")

            elif selection == 's':
                setup_wizard()

            elif selection == 'q':
                print("Goodbye.")
                break

            else:
                print("Invalid selection.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        tft.disconnect()


if __name__ == "__main__":
    cli()