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
    return cfg


# =============================
# Event code lookup
# =============================

# Maps EAS event codes to TFT front panel key numbers
TFT_EVENTS = {
    "EAN": None, "EAT": None, "NIC": None, "NPT": None,
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

        logging.basicConfig(
            level=getattr(logging, self.config['log_level'].upper(), logging.INFO),
            format='[%(asctime)s] %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
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
            raise ValueError(f"Unknown or non-originatable event code: {event!r}")
        originate_cmd = '41' if audio == 'p' else '40'
        self._send(f'*{self.pin}{originate_cmd}#')
        self._send(f'*{code}#')
        self._send(f'*{locations}#')
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
  q  Quit
""")


def cli():
    """Interactive CLI for the TFT controller."""
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
                print("Event codes: RWT, DMO, TOR, SVR, FFW, CEM etc.")
                event = input("Event code: ").strip().upper()
                locs  = input("Location keys (e.g. 1 or 12): ").strip()
                dur   = input("Duration (01=15min 02=30min 04=1hr): ").strip()
                audio = input("Audio (n/p/l, default p): ").strip().lower() or 'p'
                try:
                    tft.originate(event, locs, dur, audio)
                    if audio != 'l':
                        input("Alert sent. Press Enter to send EOM.")
                        tft.send_eom()
                except ValueError as e:
                    print(f"Error: {e}")

            elif selection == '9':
                tft.send_eom()
                print("EOM sent.")

            elif selection == '10':
                confirm = input("Reboot the TFT unit? (y/N): ").strip().lower()
                if confirm == 'y':
                    tft.reboot()
                    print("Reboot command sent.")

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