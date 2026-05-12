# TFT EAS 911 Pi Decoder

![Raspberry Pi](https://img.shields.io/badge/platform-Raspberry%20Pi-red)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

**Full EAS monitoring and remote control system for the TFT EAS 911 ENDEC on Raspberry Pi.**  
Receives, decodes, and logs SAME alerts — and lets you originate your own from any browser.

> **AI Disclosure:** This project was developed with AI assistance (Claude Sonnet 4.6) working alongside human direction on design and functionality. Human inputs provided clarifications, requirements, and architectural decisions. Disclosed per [GitHub's policies on AI-generated content](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service).

---

## Overview

Three components that work together:

| Component | File | Purpose |
|---|---|---|
| **Logger** | `TFT_logger.py` | Reads J103 serial, decodes SAME headers, logs to JSONL + text, sends ntfy alerts |
| **Controller** | `TFT_Control.py` | Drives the J303 COM3 PC/DTMF interface — originate, test, record, PTT |
| **Web Dashboard** | `web.py` | Browser UI: live alert feed, TFT front panel, VoIP PTT, config editor |
| **Utilities** | `utills.py` | Shared SAME header builder and EAS2Text TFT-mode decoder |

---

## Quick Start

```bash
git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git
cd TFT-EAS-911-Pi-Decoder
bash setup.sh
```

`setup.sh` auto-detects Raspberry Pi vs laptop and handles both cases. On a Pi it installs dependencies, runs the station setup wizard, creates two systemd services (logger + web dashboard), and starts everything.

---

## Hardware

- **TFT EAS 911** ENDEC
- J103 serial output → USB-RS232 adapter → Pi (logger reads alerts at 1200 baud)
- J303 COM3 port → USB-RS232 adapter → Pi (controller sends DTMF commands at 9600 baud)
- Pi audio output wired to TFT CH1 input (for TTS and VoIP announcement recording)

TFT must have **Menu 19 (PC/DTMF Interface)** enabled with a PIN configured.

---

## Web Dashboard

Access at `http://<pi-ip>:5000` after setup.

| Page | What it does |
|---|---|
| **Dashboard** | Live incoming alert feed, service status |
| **History** | Searchable log of all past alerts |
| **Panel** | TFT front panel buttons: RWT, EOM, stop, reboot, originate |
| **Control** | TTS announcement recording, VoIP PTT, alert origination |
| **Logs** | Live-streaming journalctl output |
| **Config** | Edit config.ini sections in the browser |

### Originating an Alert

The Control page supports four audio modes:

- **Auto TTS** — Builds a SAME header from your parameters, decodes it with EAS2Text (TFT mode), speaks it via espeak, records it on the TFT, then originates
- **Record via browser mic** — Hold the record button to speak your announcement directly into the browser; audio streams to Pi → `aplay` → TFT CH1 → recorded as announcement
- **Pre-recorded (on TFT)** — Use an announcement already recorded on the unit
- **No audio** — Originate with alert tones only

A "preview text" button shows the decoded announcement text before sending.

---

## CLI Controller

```bash
source venv/bin/activate
python3 TFT_Control.py
```

```
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
```

Option **8** shows your configured location keys by name, then offers auto-TTS origination by default.

---

## Station Setup Wizard

Run once on first deployment (or `s` in the CLI to re-run):

```bash
python3 TFT_Control.py
# select: s
```

Collects:
- Station callsign, FIPS code, ORG code (EAS/WXR/CIV/PEP)
- UTC offset and DST setting (used for EAS2Text timezone)
- COM3 PIN (from TFT Menu 19)
- Location key assignments — name + FIPS codes for each of the TFT's 14 encoder keys

All saved to `config.ini` under `[station]` and `[location_keys]`.

---

## Logger

```bash
# Standalone (without systemd)
python3 TFT_logger.py

# Development testing
python3 virtual_tft.py 1 | python3 TFT_logger.py
python3 virtual_tft.py interactive
```

Reads J103 serial at 1200 baud, strips TFT preamble bytes, majority-votes three SAME copies, deduplicates within a configurable window, and logs to:

- `~/eas_logs/alerts/events.jsonl` — structured JSON, one alert per line
- `~/eas_logs/alerts/events.log` — human-readable text blocks

Optional ntfy.sh push notification on every new alert.

---

## Project Structure

```
TFT-EAS-911-Pi-Decoder/
├── TFT_logger.py       Logger daemon (J103 serial → SAME decode → log)
├── TFT_Control.py      Controller (J303 COM3 → DTMF commands, setup wizard)
├── web.py              Flask/SocketIO web dashboard
├── utills.py           Shared SAME header builder + EAS2Text TFT decoder
├── virtual_tft.py      Test/simulation tool (replaces serial feed)
├── setup.sh            Universal install (Pi + laptop)
├── requirements.txt    Python dependencies
├── config.ini          Runtime configuration
└── TROUBLESHOOTING.md  Common issues and fixes
```

---

## Configuration

`config.ini` is written by the setup wizard and can be edited directly or via the web Config page.

```ini
[serial]
port = /dev/ttyUSB0       # J103 logger port
baud = 1200

[control]
port = /dev/tft911-cmd    # J303 COM3 port (udev symlink)
baud = 9600
pin  = 915                # TFT Menu 19 PIN

[station]
callsign  = WBXX
fips      = 036109
org       = EAS           # EAS | WXR | CIV | PEP
tz_offset = -5            # UTC offset for EAS2Text

[location_keys]
1 = Tompkins County | 036109,036001
2 = Cortland County | 036023

[logging]
log_dir   = ~/eas_logs/logs
log_level = INFO

[alerts]
alerts_dir    = ~/eas_logs/alerts
dedupe_window = 120

[notifications]
ntfy_topic = your_topic_name

[web]
host = 0.0.0.0
port = 5000
```

---

## Dependencies

```
pyserial              Serial port communication
requests              ntfy.sh push notifications
EAS2Text-Remastered   SAME header decoding (TFT emulation mode)
flask                 Web dashboard server
flask-socketio        Real-time WebSocket events
watchdog              Alert file change monitoring
```

**System packages (Pi):** `espeak` (TTS), `alsa-utils` (aplay for VoIP/PTT audio)

---

## Service Management (Pi)

```bash
# Logger
sudo systemctl status  tft911-eas
sudo systemctl restart tft911-eas
sudo journalctl -u tft911-eas -f

# Web dashboard
sudo systemctl status  tft911-eas-web
sudo systemctl restart tft911-eas-web
sudo journalctl -u tft911-eas-web -f
```

---

## SAME Header Format

```
ZCZC-ORG-EVT-PSSCCC+TTTT-JJJHHMM-SENDER-
```

- **ORG**: Originator — `WXR` NWS, `EAS` local, `CIV` civil, `PEP` primary entry point
- **EVT**: Event code — `TOR` tornado, `SVR` severe storm, `RWT` weekly test, etc.
- **PSSCCC**: 6-digit FIPS area codes (state + county)
- **TTTT**: Duration in HHMM format
- **JJJHHMM**: Effective date/time (Julian day + UTC time, per FCC § 11.31)
- **SENDER**: Originating station callsign

---

## Requirements

**Raspberry Pi:**
- Pi 3B+ or newer, Raspberry Pi OS Buster+
- Python 3.10+
- TFT EAS 911 connected via J103 (logger) and J303/COM3 (controller)
- USB-RS232 adapters for both ports

**Development/Testing:**
- Python 3.10+ on any OS (Linux/macOS/Windows)
- No hardware required — `virtual_tft.py` simulates the serial feed

---

## License

MIT — see [LICENSE](LICENSE)

## Author

Owen Schnell
