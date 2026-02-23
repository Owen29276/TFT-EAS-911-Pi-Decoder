# EAS Alert Logger

![Raspberry Pi](https://img.shields.io/badge/platform-Raspberry%20Pi-red)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

**Production EAS (Emergency Alert System) receiver for Raspberry Pi.** Decodes SAME headers, logs alerts, and sends mobile notifications.

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

### On Raspberry Pi (Production)

```bash
# Clone and deploy
git clone https://github.com/yourusername/eas-logger.git
cd eas-logger
bash deploy-pi.sh
```

The script will:
- Install system dependencies
- Set up Python virtual environment
- Install all required packages
- Create a systemd service (auto-start on reboot)
- Start the logger

See [PI_DEPLOYMENT.md](PI_DEPLOYMENT.md) for detailed setup and configuration.

### For Development/Testing (any system)

### Usage

**On Raspberry Pi:**
```bash
# One-command deployment (creates systemd service)
bash deploy-pi.sh

# Or run standalone
python3 TFT_EAS_911_Pi_logger.py
```
- Reads from `/dev/ttyUSB0` @ 1200 baud (TFT911 board)
- Logs to `~/events.jsonl` and `~/events.log`
- Service auto-starts on reboot

**Development/Testing (any system):**
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
- ✅ Automatic deduplication (120-second window)
- ✅ Multi-location support (displays all affected counties)
- ✅ Mobile notifications (ntfy.sh optional)
- ✅ JSONL logging (machine-readable events)
- ✅ Serial/stdin dual-mode (Pi/laptop auto-detection)
- ✅ Clean formatted output with box drawing
- ✅ Full message text (not just event type)

## Project Structure

```
tft911-eas/
├── TFT_EAS_911_Pi_logger.py    Main application (290 lines)
├── virtual_tft.py              Test/simulation tool
├── requirements.txt            Python dependencies
├── setup.py                    Package configuration
├── install.sh                  Installation script
├── LICENSE                     MIT License
├── README.md                   This file
└── .gitignore                  Git exclusions
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
ntfy_url = https://ntfy.sh/your_topic

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
ntfy_url = ""                    # Mobile alert endpoint (empty = disabled)
dedupe_window = 120              # Duplicate window (seconds)
filler_byte = 0xAB               # Serial decoder padding byte
```

## Output Example

```
┏━ An EAS Participant has issued a Tornado Warning for Tompkins County, NY; beginning at 09:48 PM and ending at 10:48 PM. Message from KITH_EAS.
  Received: 2026-02-18 21:48:08
  Originator: An EAS Participant
  Start: 09:48 PM
  End: 10:48 PM
  Repeats: 3 | EOM: True
  
  Locations:
    • Tompkins County, NY
  
  Header: ZCZC-EAS-TOR-036109+0060-0492148-KITH_EAS-
┗━───────────────────────────────────────────────────────
```

## SAME Header Format

```
ZCZC-ORG-EVT-PSSCCC+TTTT-JJJHHMM-SENDER-
```

- **ORG**: Originator (WXR=NWS, EAS=local, CIV=civil)
- **EVT**: Event type (TOR=tornado, SVR=severe storm, FFW=flash flood, etc.)
- **PSSCCC**: Area codes (state + county FIPS codes)
- **TTTT**: Duration (minutes)
- **JJJHHMM**: Effective date/time
- **SENDER**: Originating station ID

Example alert types: TOR, SVR, FFW, RWT, CEM, EVI, HLS, AWW, etc.

## Dependencies

- **pyserial** (≥3.5) - Serial port communication
- **requests** (≥2.31.0) - HTTP for ntfy.sh
- **EAS2Text-Remastered** (≥1.0.0) - SAME header decoding

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

---

**Last Updated**: Feb 18, 2026
