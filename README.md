# EAS Alert Logger

![Raspberry Pi](https://img.shields.io/badge/platform-Raspberry%20Pi-red)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

**Production EAS (Emergency Alert System) receiver for Raspberry Pi.** Decodes SAME headers, logs alerts, and sends mobile notifications.

> **AI Disclosure:** This project was developed with AI assistance (Claude Haiku 4.5, Claude Sonnet 4.6, and GPT-4) working alongside human direction on design and functionality. Human inputs provided clarifications, requirements, and architectural decisions throughout development. Disclosed per [GitHub's policies on AI-generated content](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service).

## Overview

Production EAS receiver for Raspberry Pi with serial decoder hardware:
- **Receives** EAS alerts via serial decoder board (1200 baud)
- **Decodes** SAME headers to human-readable alerts using EAS2Text
- **Logs** all events to JSONL (machine-readable) + text file
- **Notifies** mobile devices via ntfy.sh (optional)
- **Deduplicates** repeated alerts within a configurable window
- **Displays** formatted console output with full message text
- **Includes** virtual test mode for development without hardware

## Quick Start

```bash
git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git
cd TFT-EAS-911-Pi-Decoder
bash setup.sh
```

`setup.sh` auto-detects whether it's running on a Raspberry Pi or a laptop and does the right thing. See [PI_DEPLOYMENT.md](PI_DEPLOYMENT.md) for detailed Pi setup and configuration.

**On Raspberry Pi** it will:
- Install system dependencies and grant serial port access
- Set up the repository (skips re-clone if already inside the repo)
- Create a Python virtual environment and install dependencies
- Prompt for optional ntfy notifications
- Create a systemd service (auto-starts on reboot)
- Start the logger

**On a laptop** it will:
- Create a Python virtual environment
- Install dependencies
- Print usage instructions for testing

## Usage

**On Raspberry Pi:**
```bash
# Run standalone (without systemd)
python3 TFT_EAS_911_Pi_logger.py
```

Reads from `/dev/ttyUSB0` @ 1200 baud. Logs to `~/eas_logs/alerts/`. The systemd service starts automatically on reboot.

**Development/Testing (laptop):**
```bash
# Run all test scenarios (acts like a serial feed)
python3 virtual_tft.py | python3 TFT_EAS_911_Pi_logger.py

# Run a specific scenario
python3 virtual_tft.py 1 | python3 TFT_EAS_911_Pi_logger.py

# Custom alert
python3 virtual_tft.py custom TOR EAS 053033 60 TEST_STN

# Interactive mode
python3 virtual_tft.py interactive
```

## Features

- SAME header decoding (EAS2Text-Remastered)
- Majority voting across all 3 header copies (FCC § 11.33 compliant)
- Timestamp validation — rejects future-dated and expired alerts
- Automatic deduplication (configurable window)
- Multi-location support (displays all affected counties)
- Mobile notifications via ntfy.sh with delivery confirmation
- JSONL logging with structured SAME fields and ntfy receipt
- Serial/stdin dual-mode (Pi/laptop auto-detection)
- TFT911 filler byte stripping
- Clean formatted console output

## Project Structure

```
TFT-EAS-911-Pi-Decoder/
├── TFT_EAS_911_Pi_logger.py    Main application
├── virtual_tft.py              Test/simulation tool
├── setup.sh                    Universal install (Pi + laptop)
├── requirements.txt            Python dependencies
├── config.ini                  Runtime configuration
├── LICENSE                     MIT License
├── README.md                   This file
├── PI_DEPLOYMENT.md            Detailed Pi setup guide
├── DATA_STRUCTURE.md           Log file layout reference
└── TROUBLESHOOTING.md          Common issues and fixes
```

## Configuration

Edit `config.ini` to customize behaviour:

```ini
[serial]
port = /dev/ttyUSB0
baud = 1200

[logging]
log_dir = ~/eas_logs/logs
log_level = INFO

[alerts]
alerts_dir = ~/eas_logs/alerts
dedupe_window = 120

[notifications]
ntfy_topic = your_topic_name

[hardware]
filler_byte = 0xAB

[advanced]
max_buffer_size = 200000
serial_timeout = 1
```

Leave `ntfy_topic` empty to disable mobile notifications. All settings have sensible defaults if the file is missing.

## Output Example

```
┏━ The National Weather Service has issued a Tornado Warning for Cook County, IL; beginning at 09:48 PM and ending at 10:48 PM. Message from WBBM_EAS.
  Received: 2026-01-15 21:48:08
  Originator: National Weather Service
  Start: 09:48 PM
  End: 10:48 PM
  Duration: 1h
  Repeats: 3 | EOM: True

  Locations:
    • Cook County, IL

  Header: ZCZC-WXR-TOR-017031+0060-0152148-WBBM_EAS-
┗━───────────────────────────────────────────────────────
```

## JSONL Record Structure

Each alert is appended to `events.jsonl` as a single JSON line. See [DATA_STRUCTURE.md](DATA_STRUCTURE.md) for the full field reference.

Key fields:
- `received_utc` / `received_local` — when the alert was received
- `originator_code`, `event_code`, `sender` — parsed SAME fields
- `issued_utc`, `expires_utc` — alert validity window (ISO 8601 UTC)
- `locations_pretty` — human-readable county/area names
- `repeat_count`, `saw_eom` — transmission quality indicators
- `notification` — ntfy.sh delivery outcome

## SAME Header Format

```
ZCZC-ORG-EVT-PSSCCC+TTTT-JJJHHMM-SENDER-
```

- **ORG**: Originator (WXR=NWS, EAS=local, CIV=civil)
- **EVT**: Event type (TOR=tornado, SVR=severe storm, FFW=flash flood, etc.)
- **PSSCCC**: Area codes (state + county FIPS codes)
- **TTTT**: Duration in HHMM format
- **JJJHHMM**: Effective date/time (UTC per FCC § 11.31)
- **SENDER**: Originating station ID

## Dependencies

- **pyserial** — Serial port communication
- **requests** — HTTP for ntfy.sh notifications
- **EAS2Text-Remastered** — SAME header decoding

See `requirements.txt` for version constraints.

## Requirements

**Raspberry Pi:**
- Raspberry Pi 3B+ or newer
- Raspberry Pi OS (Buster or newer)
- Python 3.10+
- TFT911 hardware (serial decoder board) connected via USB

**Development/Testing:**
- Python 3.10+ on any system (Linux/macOS/Windows)

## License

MIT License — See [LICENSE](LICENSE) for details.

## Author

Owen Schnell

## AI Disclosure

This project was developed with AI assistance (Claude Haiku 4.5, Claude Sonnet 4.6, and GPT-4) working alongside human direction on design and functionality. Human inputs provided clarifications, requirements, and architectural decisions throughout development.

Disclosed per [GitHub's policies on AI-generated content](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service).
