# Troubleshooting Guide

## Common Issues

### Serial Port Problems

**Error: "Permission denied /dev/ttyUSB0"**
```bash
# Add your user to the dialout group
sudo usermod -a -G dialout $USER
# Log out and back in, then restart the service
sudo systemctl restart tft-eas-911
```

**Error: "No such file or directory /dev/ttyUSB0"**
- Check if hardware is connected: `ls /dev/tty*`
- Verify USB adapter is recognized: `dmesg | grep -i usb | tail -10`
- Try different USB port

**Error: "port already in use"**
- Kill other processes: `sudo lsof /dev/ttyUSB0`
- Or change PORT in config and restart

### Dependency Issues

**Error: "ModuleNotFoundError: No module named 'EAS2Text'"**
```bash
# Verify venv is active and reinstall
source /opt/tft-eas-911/venv/bin/activate
pip install --force-reinstall EAS2Text-Remastered==0.1.25.1
```

**Error: "No module named 'serial'"**
```bash
pip install pyserial>=3.5
```

**Error: "No module named 'requests'"**
```bash
pip install requests>=2.31.0
```

### Service Management

**Service won't start**
```bash
# Check logs for errors
sudo journalctl -u tft-eas-911 -n 50

# Verify Python environment
source /opt/tft-eas-911/venv/bin/activate
python3 -c "from EAS2Text import EAS2Text; print('OK')"
```

**High CPU usage**
- Check active alerts: `tail -f ~/events.log`
- Normal during high alert volume
- Consider archiving old logs to reduce I/O

**Service keeps restarting**
```bash
# Check for errors
sudo journalctl -u tft-eas-911 -f
# Most common: serial port not available
```

### Notifications Not Working

**ntfy.sh alerts not arriving**
```bash
# Test your ntfy topic manually
curl -d "Test alert" https://ntfy.sh/YOUR_TOPIC

# On phone, subscribe to the topic in ntfy app
# Verify NTFY_URL is set in config:
grep NTFY_URL /opt/tft-eas-911/TFT_EAS_911_Pi_logger.py
```

**Network timeout errors**
- Check internet connection: `ping 8.8.8.8`
- Verify ntfy.sh is reachable: `curl https://ntfy.sh`
- Check firewall rules

### Log Issues

**Can't find log files**
```bash
# Check home directory
ls -lh ~/*.{log,jsonl} 2>/dev/null

# Check actual location
grep 'JSONL_FILE\|TEXT_FILE' /opt/tft-eas-911/TFT_EAS_911_Pi_logger.py
```

**Logs growing too large**
```bash
# Archive old logs
cd ~
gzip events.log
mv events.log.gz events.log.$(date +%Y%m%d).gz

# Start fresh logs
touch events.log events.jsonl
sudo systemctl restart tft-eas-911
```

## Diagnostic Commands

```bash
# Check service status
sudo systemctl status tft-eas-911

# View live logs (follow mode)
sudo journalctl -u tft-eas-911 -f

# See last 100 lines
sudo journalctl -u tft-eas-911 -n 100

# Search for errors
sudo journalctl -u tft-eas-911 | grep -i error

# Check service configuration
sudo systemctl cat tft-eas-911

# Verify Python path
which python3
python3 --version

# Test serial connection
cat /dev/ttyUSB0 &  # Press Ctrl+C to stop
```

## Testing Without Hardware

```bash
# Generate test alerts
python3 virtual_tft.py

# Or use interactive mode
python3 virtual_tft.py interactive

# Pipe test data to logger (test mode)
echo "ZCZC-WEA-ALL-063001+0015-1180609-KEAO/NWS-" | python3 TFT_EAS_911_Pi_logger.py
```

## Getting Help

1. **Check logs first**: `sudo journalctl -u tft-eas-911 -f`
2. **Verify hardware**: `ls -la /dev/tty*`
3. **Test dependencies**: `python3 quickstart.py`
4. **Read README.md** for general info
5. **Check GitHub issues**: https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder/issues

## System Requirements

- **OS**: Raspberry Pi OS (Bookworm recommended)
- **Python**: 3.10+
- **Free disk**: 100MB minimum
- **Memory**: 256MB minimum
- **Network**: For ntfy notifications (optional)

---

**Last Updated**: Feb 18, 2026
