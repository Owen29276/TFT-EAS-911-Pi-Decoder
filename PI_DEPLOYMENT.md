# Raspberry Pi Deployment Guide

## Installation

```bash
git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git
cd TFT-EAS-911-Pi-Decoder
bash setup.sh
```

`setup.sh` handles everything automatically:
- System packages and Python dependencies
- Serial port permissions (dialout group)
- Timezone and NTP check
- Python virtual environment
- Optional ntfy mobile notifications
- systemd service (auto-starts on reboot)

## Managing the Service

```bash
sudo systemctl status tft911-eas
sudo systemctl restart tft911-eas
sudo systemctl stop tft911-eas

# Live logs
sudo journalctl -u tft911-eas -f

# Last 50 lines
sudo journalctl -u tft911-eas -n 50 --no-pager
```

## Updating

```bash
cd ~/TFT-EAS-911-Pi-Decoder
git stash          # save local config changes
git pull
git stash pop      # restore local config
sudo systemctl restart tft911-eas
```

## Configuration

```bash
nano ~/TFT-EAS-911-Pi-Decoder/config.ini
sudo systemctl restart tft911-eas
```

Key settings:

```ini
[serial]
port = /dev/ttyUSB0
baud = 1200

[alerts]
dedupe_window = 120

[notifications]
ntfy_topic = your_topic_name
```

## Log Files

Located inside the repo at `~/TFT-EAS-911-Pi-Decoder/`:

```bash
# Live alert feed
tail -f ~/TFT-EAS-911-Pi-Decoder/alerts/events.log

# JSON records
cat ~/TFT-EAS-911-Pi-Decoder/alerts/events.jsonl

# Application logs
tail -f ~/TFT-EAS-911-Pi-Decoder/logs/eas_logger.log
```

## Hardware

Check serial port is detected:
```bash
ls /dev/ttyUSB*
dmesg | grep -i tty | tail -10
```

If permission denied on serial port:
```bash
sudo usermod -aG dialout $USER
sudo systemctl restart tft911-eas
```

## Troubleshooting

```bash
# Check for errors
sudo journalctl -u tft911-eas -n 50 --no-pager

# Test Python imports manually
cd ~/TFT-EAS-911-Pi-Decoder
source venv/bin/activate
python3 -c "from EAS2Text import EAS2Text; print('OK')"
```

## Uninstall

```bash
sudo systemctl stop tft911-eas
sudo systemctl disable tft911-eas
sudo rm /etc/systemd/system/tft911-eas.service
sudo systemctl daemon-reload
rm -rf ~/TFT-EAS-911-Pi-Decoder
```
