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
| **Utilities** | `eas_utils.py` | Shared SAME header builder and EAS2Text TFT-mode decoder |

---

## Quick Start

```bash
git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git
cd TFT-EAS-911-Pi-Decoder
bash setup.sh
```

`setup.sh` auto-detects Raspberry Pi vs laptop. On a Pi it installs dependencies, runs the station setup wizard, creates the systemd services (logger + web dashboard) and starts everything.

**Proxmox / any Debian server:** pass the TFT's USB adapters and sound card through to a VM and run `bash setup.sh --server`. It does the same as the Pi install, plus stable serial names, sound-card setup and local Whisper transcription. See [PROXMOX_DEPLOYMENT.md](PROXMOX_DEPLOYMENT.md).

---

## Hardware

- **TFT EAS 911** ENDEC
- J103 serial output → USB-RS232 adapter → Pi (logger reads alerts at 1200 baud)
- J303 COM3 port → USB-RS232 adapter → Pi (controller sends DTMF commands at 9600 baud)
- Pi audio output wired to TFT CH1 input (for TTS and VoIP announcement recording)

TFT must have **Menu 19 (PC/DTMF Interface)** enabled with a PIN configured.

---

## Web Dashboard

Access at `http://<pi-ip>:5000` after setup and sign in with the dashboard password.

### Security

The dashboard can put a live EAS alert on air, so it is locked down:

- **Password required.** Set it with `python3 web.py --set-password` (setup.sh does this). The web service refuses to start without one. Only a salted hash is stored, in `config.ini` `[web] password_hash`, and it is never sent to or accepted from the Config page.
- **Same-origin only.** POSTs and WebSocket connections from other sites are rejected, and session cookies are `HttpOnly` + `SameSite=Strict`.
- **Brute-force lockout.** 5 failed logins from one IP locks that IP out for 15 minutes.
- **Typed confirmation.** Originating an alert requires typing the event code (e.g. `TOR`); the server enforces this, not just the browser.
- **ERN origination rules** (`[ern]`, GWES procedures §1.4), enforced on the originate form and the RWT button:
  - At most 2 RWTs in any 7 days. Front-panel RWTs the logger hears under your callsign count too.
  - DMO repeats within 10 minutes get a warning.
  - Any other live event must either use the `EAS` originator code or be an ADR. Otherwise you have to name who approved it at Network Operations, or which official agency sent it.
  - Blocked and sent originations are both recorded in the audit log.
  - If you're not on the ERN, set `enforce = false`.
- **Config page keeps comments.** Saving from the dashboard edits only the changed lines of `config.ini`. Section and key names are validated, and the password hash can't be changed from there.
- **Audit log.** Every login, control action, config change, PTT and recording is appended to `alerts/audit.jsonl` with time and IP.

Don't expose port 5000 to the internet. For remote access use a VPN such as Tailscale/WireGuard.

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
python3 virtual_tft.py 1 | python3 TFT_logger.py --stdin
python3 virtual_tft.py interactive

# Run the test suite
pip install pytest && python3 -m pytest tests
```

Input is stdin when something is piped in (`virtual_tft.py | TFT_logger.py`), otherwise the serial port. The same rule applies on a Pi, a VM or a laptop. `--stdin` / `--serial` force one or the other, and the systemd service always uses `--serial`.

Reads J103 serial at 1200 baud, strips TFT preamble bytes, majority-votes the SAME header copies (character-by-character if all three differ), deduplicates within a configurable window (remembered across restarts), and logs to:

- `~/eas_logs/alerts/events.jsonl` — structured JSON, one alert per line
- `~/eas_logs/alerts/events.log` — human-readable text blocks

Optional ntfy.sh push notification on every new alert.

---

## Alert Audio, Transcription and Home Assistant

```
TFT audio out ──▶ USB sound card ──▶ Pi: TFT_logger.py ──WAV──▶ Proxmox: transcriber/server.py (Whisper)
TFT J103 serial ─────────────────▶      │          ◀──text──
                                        └──MQTT──▶ Home Assistant (Mosquitto add-on)
```

All three parts are optional and independent; each is off until enabled in `config.ini`.
If any of them breaks, the logger keeps logging.

- **Audio capture** (`audio_capture.py`, `[audio]`).
  - `arecord` runs continuously into a ~3-minute in-memory ring buffer.
  - When `ZCZC` appears on serial, the clip is taken from just before the header until
    EOM (`NNNN`), 4 s of silence, or 150 s, whichever comes first.
  - It is saved as `alerts/audio/<alert_id>.wav`, and only the newest 200 are kept.
  - Duplicate bursts don't produce audio.
- **Transcription** (`transcribe_client.py`, `[transcribe]`). The WAV is uploaded to a
  small faster-whisper service. On a Proxmox VM, `setup.sh --server` installs it
  alongside the logger on localhost. A Pi 3B+ is too slow, so there it runs on
  another machine. Setup is in [transcriber/README.md](transcriber/README.md). Results go to
  `alerts/transcripts.jsonl`.
- **Home Assistant** (`ha_mqtt.py`, `[mqtt]`). MQTT auto-discovery creates a
  *TFT EAS 911* device with:
  - a local-warning binary sensor
  - last alert, event code and transcript sensors
  - an event entity for automations
  - an availability alarm

  Setup and example automations are in [HOME_ASSISTANT.md](HOME_ASSISTANT.md).
- **Live audio on speakers** (`live_stream.py`, `[stream]`). The capture input is
  streamed as MP3 to a local Icecast server. An *On air* sensor lets Home Assistant
  play it on Sonos or Apple TV while an alert is airing. See
  [HOME_ASSISTANT.md §5](HOME_ASSISTANT.md#5-live-audio-on-sonos--apple-tv).

Credentials (MQTT password, transcription token) go in **`secrets.ini`**, not
`config.ini`. Create it with `cp secrets.ini.example secrets.ini && chmod 600 secrets.ini`.
It is gitignored and never shown in the web dashboard.

### Wiring the audio

Neither the Pi nor the DL360p has a line input, so you need a **USB sound card** with a line or mic input. On Proxmox, pass it through to the VM; `setup.sh --server` finds it and configures it for you.

1. **Tap the receiver audio, not the TFT's output.** Per the TFT manual (Rev C, §2.4):
   - **J101** (XLR, 600 Ω balanced) is the *encoder* output. It only carries audio
     when the 911 transmits or forwards a message, so it would miss alerts that
     aren't forwarded.
   - There is no "monitor out" jack. The speaker is internal.
   - Instead, split the audio from the monitoring receiver (NOAA radio / LP station)
     that feeds **J102 CH1/CH2** and send one leg to the USB card's line input.
     J102 inputs are 10 kΩ bridging (0.35–2 Vp-p), so a Y-split doesn't load the
     signal or change what the TFT hears.
   - If you only care about what the TFT *sends*, J101 works too. It's balanced
     600 Ω at −10 to +10 dBm, so use a balanced-to-unbalanced adapter or isolation
     transformer and keep the capture level low.
2. Feed it into the USB card's input. If you hear hum, put a 600:600 Ω audio isolation
   transformer inline to break the ground loop.
3. Find the device and test a recording:

   ```bash
   arecord -l                                   # card 1, device 0 → plughw:1,0
   arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 10 test.wav
   alsamixer -c 1                               # set capture level: loud but not clipping
   ```
4. Set `[audio] enabled = true` and `device = plughw:1,0`, then
   `sudo systemctl restart tft911-eas`.

The default `mode = forward` assumes the TFT prints the header on J103 when the alert
**starts**. Check this on the first real alert or RWT: if the log shows
`Audio capture started` only after the message was already over, set `mode = lookback`.
That grabs the `lookback_seconds` *before* the header instead.

---

## Project Structure

```
TFT-EAS-911-Pi-Decoder/
├── TFT_logger.py       Logger daemon (J103 serial → SAME decode → log)
├── TFT_Control.py      Controller (J303 COM3 → DTMF commands, setup wizard)
├── web.py              Flask/SocketIO web dashboard
├── web_auth.py         Dashboard login, same-origin checks, audit log
├── audio_capture.py    Alert audio ring buffer + capture (arecord)
├── transcribe_client.py Uploads alert audio to the transcription server
├── ha_mqtt.py          Home Assistant MQTT discovery publisher
├── live_stream.py      Live MP3 stream to Icecast for HA speakers
├── ern_policy.py       GWES ERN §1.4 origination limits for the dashboard
├── ini_edit.py         Comment-preserving config.ini edits (Config page)
├── transcriber/        Whisper transcription server for the Proxmox box
├── eas_utils.py        Shared SAME header builder + EAS2Text TFT decoder
├── virtual_tft.py      Test/simulation tool (replaces serial feed)
├── tests/              pytest suite (logger, audio, transcription, MQTT, dashboard auth)
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
paho-mqtt             Home Assistant MQTT integration
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
