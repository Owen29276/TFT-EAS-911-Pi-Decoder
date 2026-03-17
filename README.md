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
- **Notifies** mobile devices via ntfy.sh webhooks
- **Deduplicates** repeated alerts (120-second window)
- **Displays** formatted console output with full message text
- **Includes** virtual test mode for development (laptop testing only)

## Quick Start

```bash
git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git
cd TFT-EAS-911-Pi-Decoder
bash setup.sh
```

`setup.sh` auto-detects whether it's running on a Raspberry Pi or a laptop and does the right thing. See [PI_DEPLOYMENT.md](PI_DEPLOYMENT.md) for detailed Pi setup and configuration.

**On Raspberry Pi** it will:
- Install system dependencies
- Clone and set up the repository
- Create a Python virtual environment
- Prompt for optional ntfy notifications
- Create a systemd service (auto-starts on reboot)
- Start the logger

**On a laptop** it will:
- Create a Python virtual environment
- Install dependencies
- Print usage instructions for testing

### Usage

**On Raspberry Pi:**
```bash
# Run standalone (without systemd)
python3 TFT_EAS_911_Pi_logger.py
```
- Reads from `/dev/ttyUSB0` @ 1200 baud (TFT911 board)
- Logs to `~/eas_logs/alerts/events.jsonl` and `~/eas_logs/alerts/events.log`
- Service auto-starts on reboot

**Development/Testing (laptop):**
```bash
# Scenario 1: Tornado warning
python3 virtual_tft.py 1 | python3 TFT_EAS_911_Pi_logger.py

# Scenario 2: Severe thunderstorm
python3 virtual_tft.py 2 | python3 TFT_EAS_911_Pi_logger.py

# Custom alert
python3 virtual_tft.py custom TOR EAS 001001 60 TEST_STN

# Interactive mode
python3 virtual_tft.py interactive
```

## Features

- ✅ SAME header decoding (EAS2Text-Remastered)
- ✅ Majority voting across all 3 header copies (FCC § 11.33 compliant)
- ✅ Timestamp validation — rejects future-dated and expired alerts
- ✅ Automatic deduplication (120-second window)
- ✅ Multi-location support (displays all affected counties)
- ✅ Mobile notifications (ntfy.sh optional) with delivery confirmation
- ✅ JSONL logging (machine-readable events) including ntfy receipt
- ✅ Serial/stdin dual-mode (Pi/laptop auto-detection)
- ✅ TFT911 filler byte stripping (0xAB)
- ✅ Clean formatted output with box drawing
- ✅ Full message text (not just event type)

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

### Using config.ini (Recommended)

The logger comes with `config.ini` pre-configured with sensible defaults. Customize as needed:

```bash
# Edit config.ini with your settings
nano config.ini
```

The logger will automatically load these settings on startup:

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

### Environment Variables (Override config.ini)

```bash
export EAS_PORT=/dev/ttyUSB0
export EAS_BAUD=1200
python3 TFT_EAS_911_Pi_logger.py
```

### Defaults (if config.ini is missing)

```python
port = /dev/ttyUSB0              # Serial port (Pi only)
baud = 1200                      # Serial baud rate
ntfy_topic = ""                  # ntfy.sh topic name (empty = disabled)
dedupe_window = 120              # Duplicate window (seconds)
filler_byte = 0xAB               # Serial decoder padding byte
```

## Output Example

```
┏━ The National Weather Service has issued a Tornado Warning for Cook County, IL; beginning at 09:48 PM and ending at 10:48 PM. Message from WBBM_EAS.
  Received: 2026-01-15 21:48:08
  Originator: National Weather Service
  Start: 09:48 PM
  End: 10:48 PM
  Repeats: 3 | EOM: True

  Locations:
    • Cook County, IL

  Header: ZCZC-WXR-TOR-017031+0060-0152148-WBBM_EAS-
┗━───────────────────────────────────────────────────────
```

## JSONL Record Structure

Each alert is appended to `events.jsonl` as a single JSON line. The `notification` field records the ntfy.sh delivery outcome:

```json
{
  "received_utc": "2026-01-15T21:48:08Z",
  "received_local": "2026-01-15 15:48:08",
  "canonical_header": "ZCZC-WXR-TOR-017031+0060-0152148-WBBM_EAS-",
  "originator_code": "WXR",
  "event_code": "TOR",
  "sender": "WBBM_EAS",
  "issued_utc": "2026-01-15T21:48:00Z",
  "expires_utc": "2026-01-15T22:48:00Z",
  "repeat_count": 3,
  "saw_eom": true,
  "locations_pretty": ["Cook County, IL"],
  "eas2text": { "evntText": "Tornado Warning", "orgText": "An EAS Participant", "..." : "..." },
  "raw_burst": "...",
  "notification": {"attempted": true, "sent": true, "http_status": 200}
}
```

`notification` values:
- `{"attempted": false}` — ntfy not configured
- `{"attempted": true, "sent": true, "http_status": 200}` — delivered
- `{"attempted": true, "sent": false, "http_status": 403}` — HTTP error
- `{"attempted": true, "sent": false, "error": "..."}` — network/timeout error

`expires_utc` is `null` for national/presidential alerts (duration `+0000`).

## SAME Header Format

```
ZCZC-ORG-EVT-PSSCCC+TTTT-JJJHHMM-SENDER-
```

- **ORG**: Originator (WXR=NWS, EAS=local, CIV=civil)
- **EVT**: Event type (TOR=tornado, SVR=severe storm, FFW=flash flood, etc.)
- **PSSCCC**: Area codes (state + county FIPS codes)
- **TTTT**: Duration in HHMM format (e.g., 0130 = 1 hour 30 minutes)
- **JJJHHMM**: Effective date/time
- **SENDER**: Originating station ID

Example alert types: TOR, SVR, FFW, RWT, CEM, EVI, HLS, AWW, etc.

## Dependencies

- **pyserial** (≥3.5) - Serial port communication
- **requests** (≥2.31.0) - HTTP for ntfy.sh
- **EAS2Text-Remastered** (≥0.1.23) - SAME header decoding

## Platform Detection

Automatically detects Pi vs development mode:
- **Raspberry Pi**: Reads from serial port `/dev/ttyUSB0` for real TFT911 hardware
- **Development**: Reads from stdin, supports piped input from `virtual_tft.py` (for testing on laptop)

## Requirements

**Raspberry Pi:**
- Raspberry Pi 3B+ or newer
- Raspberry Pi OS (Buster or newer)
- Python 3.10+
- TFT911 hardware (serial decoder board) connected via USB

**For Development/Testing:**
- Python 3.10+ on any system (Linux/macOS)

## License

MIT License - See [LICENSE](LICENSE) file for details

## Author

Owen Schnell

## AI Disclosure

This project was developed with AI assistance (Claude Haiku 4.5, Claude Sonnet 4.6, and GPT-4) working alongside human direction on design and functionality. Human inputs provided clarifications, requirements, and architectural decisions throughout development.

This disclosure is provided in the spirit of transparency per [GitHub's policies on AI-generated content](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service).

---

**Last Updated**: Mar 17, 2026
