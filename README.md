# TFT EAS 911 EAS Logger

![Raspberry Pi](https://img.shields.io/badge/platform-Raspberry%20Pi-red)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

**Production EAS (Emergency Alert System) receiver for Raspberry Pi.** Decodes SAME headers from TFT EAS 911 hardware, logs alerts, and sends mobile notifications.

> **Disclosure**: This project was developed with AI assistance (Claude Haiku 4.5 and GPT-4) working alongside human direction on design and functionality. Human inputs provided clarifications, requirements, and architectural decisions throughout development.

## Overview

Production EAS receiver for Raspberry Pi with TFT EAS 911 hardware:
- **Receives** EAS alerts via TFT EAS 911 serial decoder (1200 baud)
- **Decodes** SAME headers to human-readable alerts using EAS2Text
- **Logs** all events to JSONL (machine-readable) + text file
- **Notifies** mobile devices via ntfy.sh webhooks
- **Deduplicates** repeated alerts (120-second window)
- **Displays** formatted console output with full message text
- **Includes** virtual test mode for development (laptop testing only)

## Quick Start

### On Raspberry Pi (Production)

```bash
# One-command deployment
curl -sSL https://raw.githubusercontent.com/Owen29276/TFT-EAS-911-Pi-Decoder/main/deploy-pi.sh | bash
```

Or clone and deploy:
```bash
git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git
cd TFT-EAS-911-Pi-Decoder
bash deploy-pi.sh
```

The script will:
- Install system dependencies
- Set up Python virtual environment
- Install all required packages
- Prompt for optional ntfy notifications
- Create a systemd service (auto-start on reboot)
- Start the logger

See [PI_DEPLOYMENT.md](PI_DEPLOYMENT.md) for detailed setup and configuration.

### For Development/Testing (any system)

```bash
# Generate test alerts
python3 virtual_tft.py

# Or run scenarios
python3 virtual_tft.py 1  # Tornado warning
python3 virtual_tft.py 2  # Severe thunderstorm
python3 virtual_tft.py custom TOR EAS 036109 60 KITH_EAS

# Interactive mode
python3 virtual_tft.py interactive
```

## Usage

**On Raspberry Pi:**
```bash
# One-command deployment (creates systemd service)
bash deploy-pi.sh

# Or run standalone
python3 TFT_EAS_911_Pi_logger.py
```
- Reads from `/dev/ttyUSB0` @ 1200 baud (TFT EAS 911 board)
- Logs to `~/events.jsonl` and `~/events.log`
- Service auto-starts on reboot

**Development/Testing (any system):**
```bash
# Pipe test data through logger
python3 virtual_tft.py 1 | python3 TFT_EAS_911_Pi_logger.py

# Or use interactive mode
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
TFT-EAS-911-Pi-Decoder/
├── TFT_EAS_911_Pi_logger.py    Main application (323 lines)
├── virtual_tft.py              Test/simulation tool (370 lines)
├── deploy-pi.sh                One-command Pi deployment
├── requirements.txt            Python dependencies
├── setup.py                    Package configuration
├── LICENSE                     MIT License
├── README.md                   This file
├── PI_DEPLOYMENT.md            Detailed Pi setup guide
├── DEPLOYMENT.md               General deployment info
├── QUICK_DEPLOY.md             Quick reference
├── TROUBLESHOOTING.md          Common issues and solutions
├── CONTRIBUTING.md             Developer guidelines
└── .gitignore                  Git exclusions
```

## Configuration

Edit these in `TFT_EAS_911_Pi_logger.py`:

```python
PORT = "/dev/ttyUSB0"              # Serial port (Pi only)
BAUD = 1200                        # Serial baud rate
NTFY_URL = ""                      # Mobile alert endpoint (optional)
DEDUPE_WINDOW_SEC = 120            # Duplicate window (seconds)
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
- **EAS2Text-Remastered** (==0.1.25.1) - SAME header decoding

## Platform Detection

Automatically detects Pi vs development mode:
- **Raspberry Pi**: Reads from serial port `/dev/ttyUSB0` for real TFT EAS 911 hardware
- **Development**: Reads from stdin, supports piped input from `virtual_tft.py` (for testing on laptop)

## Requirements

**Raspberry Pi:**
- Raspberry Pi 3B+ or newer
- Raspberry Pi OS (Bookworm recommended)
- Python 3.10+
- TFT EAS 911 hardware (serial decoder board) connected via USB

**For Development/Testing:**
- Python 3.10+ on any system (Linux/macOS)

## License

MIT License - See [LICENSE](LICENSE) file for details

## Support

For troubleshooting, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) or check the logs:
```bash
sudo journalctl -u tft-eas-911 -f
```

---

**Last Updated**: Feb 18, 2026
