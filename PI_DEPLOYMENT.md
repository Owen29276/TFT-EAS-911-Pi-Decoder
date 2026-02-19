# Raspberry Pi Deployment Guide

Quick start: Run one command to set everything up!

## One-Command Installation

```bash
curl -sSL https://raw.githubusercontent.com/Owen29276/TFT-EAS-911-Pi-Decoder/main/deploy-pi.sh | bash
```

Or if you cloned the repo:

```bash
cd ~/tft911-eas
bash deploy-pi.sh
```

## What the Script Does

1. ✅ Updates system packages
2. ✅ Installs Python 3 and dependencies
3. ✅ Clones repository to `/opt/tft911-eas`
4. ✅ Creates Python virtual environment
5. ✅ Installs all Python dependencies
6. ✅ Creates systemd service (auto-starts on reboot)
7. ✅ Starts the logger service

**Total time: ~5-10 minutes**

## Manual Installation (if you prefer)

### Step 1: Update system
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git
```

### Step 2: Clone repository
```bash
cd /opt
sudo mkdir tft911-eas
sudo chown $USER:$USER tft911-eas
cd tft911-eas
git clone . . # or clone from GitHub
```

### Step 3: Create virtual environment and install
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 4: Test it works
```bash
python3 TFT_EAS_911_Pi_logger.py
# Wait for messages from TFT EAS 911 hardware or Ctrl+C to exit
```

### Step 5: Create systemd service
Create `/etc/systemd/system/tft911-eas.service`:

```bash
sudo nano /etc/systemd/system/tft911-eas.service
```

Paste this content:

```ini
[Unit]
Description=TFT EAS 911 EAS Logger
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/tft911-eas
Environment="PATH=/opt/tft911-eas/venv/bin"
ExecStart=/opt/tft911-eas/venv/bin/python3 TFT_EAS_911_Pi_logger.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Step 6: Enable and start service
```bash
sudo systemctl daemon-reload
sudo systemctl enable tft911-eas.service
sudo systemctl start tft911-eas.service
```

## Managing the Service

```bash
# Check status
sudo systemctl status tft911-eas

# View live logs
sudo journalctl -u tft911-eas -f

# Start/stop/restart
sudo systemctl start tft911-eas
sudo systemctl stop tft911-eas
sudo systemctl restart tft911-eas

# View logs from last hour
sudo journalctl -u tft911-eas -n 100 --since "1 hour ago"

# Save logs to file
sudo journalctl -u tft911-eas > tft911-logs.txt
```

## Configuration

Edit the logger config:

```bash
sudo nano /opt/tft911-eas/TFT_EAS_911_Pi_logger.py
```

Key settings (lines 33-47):

```python
PORT = "/dev/ttyUSB0"                    # Serial port (change if different)
BAUD = 1200                              # Baud rate (usually 1200)
NTFY_URL = "https://ntfy.sh/owen_tft911" # Phone notifications (optional)
DEDUPE_WINDOW_SEC = 120                  # Duplicate alert window
```

After editing, restart the service:

```bash
sudo systemctl restart tft911-eas
```

## Hardware Setup

### Serial Port Detection

Check if your TFT EAS 911 board is connected:

```bash
# List serial devices
ls -la /dev/tty*

# Check for USB serial adapters
dmesg | grep -i tty | tail -20
```

You should see something like `/dev/ttyUSB0`

### Permissions

If you get "permission denied" errors:

```bash
# Add pi user to dialout group
sudo usermod -a -G dialout pi

# Log out and back in, then test:
python3 TFT_EAS_911_Pi_logger.py
```

## Troubleshooting

### Service won't start

```bash
# Check systemd errors
sudo journalctl -u tft911-eas -n 50

# Verify Python can import modules
source /opt/tft911-eas/venv/bin/activate
python3 -c "from EAS2Text import EAS2Text; print('OK')"
```

### Serial port not found

```bash
# Check if device is connected
ls /dev/ttyUSB*

# If you see /dev/ttyUSB0, good!
# If not, check dmesg
dmesg | tail -20
```

### High CPU usage

- This is normal if processing many alerts
- Check logs: `sudo journalctl -u tft911-eas -f`
- Alert file size can be controlled by archiving old logs

### Mobile notifications not working

- Check `NTFY_URL` is set correctly in config
- Test manually: `curl -d "Test alert" https://ntfy.sh/YOUR_TOPIC`
- On your phone, subscribe to the topic in the Ntfy app

## Log Files

Located in your home directory:

```bash
ls -lh ~/events.*

# View live updates
tail -f ~/events.log

# Check JSON records
head -5 ~/events.jsonl
```

## Auto-Update

To keep up with repository changes:

```bash
cd /opt/tft911-eas
git pull
sudo systemctl restart tft911-eas
```

## Uninstall

If you need to remove the service:

```bash
sudo systemctl stop tft911-eas
sudo systemctl disable tft911-eas
sudo rm /etc/systemd/system/tft911-eas.service
sudo systemctl daemon-reload
sudo rm -rf /opt/tft911-eas
```

## Support

For issues or questions:
- Check logs: `sudo journalctl -u tft911-eas -f`
- Read README.md for full documentation
- Check DEPLOYMENT.md for general info

---

**Last Updated**: Feb 18, 2026
