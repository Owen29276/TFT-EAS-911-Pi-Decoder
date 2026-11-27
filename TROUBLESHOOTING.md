# Troubleshooting

Paths below are relative to the install directory: where you cloned the repo, or
`/opt/tft911-eas` (else `~/tft911-eas`) if `setup.sh` cloned it. There are two services:

| Service | Program | Does |
|---|---|---|
| `tft911-eas` | `TFT_logger.py` | Reads J103, logs alerts, audio, transcripts, Home Assistant |
| `tft911-eas-web` | `web.py` | Dashboard and COM3 (J303) control |
| `tft911-transcriber` | `transcriber/server.py` | Optional local Whisper server |

## First checks

```bash
sudo systemctl status tft911-eas tft911-eas-web
sudo journalctl -u tft911-eas -n 50 --no-pager
sudo journalctl -u tft911-eas-web -n 50 --no-pager
tail -f logs/eas_logger.log        # logger's own log
tail -f alerts/events.log          # human-readable alert feed
```

## Serial ports

**`Permission denied: /dev/tft911-data` (or ttyUSB0).** Run the command below, then
log out and back in, and restart the service.

```bash
sudo usermod -aG dialout $USER
```

**Port not found.**
- **Check the adapter is detected:** `ls -l /dev/tft911-* /dev/ttyUSB*` and
  `dmesg | grep -i tty | tail`.
- **`/dev/tft911-data` and `/dev/tft911-cmd` are udev names** made by `setup.sh` step 3b.
  Re-run setup if you swapped adapters.
- **In a Proxmox VM,** check that the USB passthrough survived a host reboot
  (see PROXMOX_DEPLOYMENT.md).

**`port already in use`.** Something else has it open: `sudo lsof /dev/tft911-data`.
Stop the logger before running a manual serial test.

**Watch raw J103 output** (useful during an RWT, to see *when* the header arrives):

```bash
sudo systemctl stop tft911-eas
venv/bin/python -c "import serial,time; s=serial.Serial('/dev/tft911-data',1200)
while 1: d=s.read(s.in_waiting or 1); print(time.strftime('%T'), d)"
```

## Dashboard

- **`No dashboard password set — refusing to start`.** Run
  `venv/bin/python web.py --set-password`, then restart `tft911-eas-web`.
- **`Too many failed logins`.** That IP is locked out for 15 minutes.
- **`COM3 not connected`.** Check `[control] port` in config.ini and the J303 cable.
  Then press **reconnect** on the Control page.
- **Originate returns an ERN §1.4 error.** See README → Security. The audit log
  (`alerts/audit.jsonl`) records each sent and blocked origination.

## Dependencies

```bash
venv/bin/pip install -r requirements.txt
venv/bin/python -c "from EAS2Text import EAS2Text; print('EAS2Text OK')"
```

## Notifications (ntfy)

```bash
curl -d "Test alert" https://ntfy.sh/YOUR_TOPIC     # must arrive on your phone
grep ntfy_topic config.ini
```

## Audio, live stream and Home Assistant

- **Audio, transcription:** see README → Alert Audio and [transcriber/README.md](transcriber/README.md).
- **MQTT, the `On air` sensor, Sonos and Icecast:** see the Troubleshooting section of
  [HOME_ASSISTANT.md](HOME_ASSISTANT.md).
- **Quick capture check:**
  `arecord -D plughw:CARD=<name>,DEV=0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/t.wav`

## Log size

The logger's own log rotates automatically. `alerts/events.jsonl` and `events.log` grow
forever by design (they are the station log), at roughly 1 KB per alert. Back them up
rather than truncating them. On Proxmox, include the VM in a vzdump backup job.

## Testing without hardware

```bash
venv/bin/python virtual_tft.py 1 | venv/bin/python TFT_logger.py --stdin
venv/bin/python virtual_tft.py all | venv/bin/python TFT_logger.py --stdin   # scenarios 1-5
venv/bin/python -m pytest tests -q
```

## Updating

```bash
cd <install dir>
bash setup.sh            # pulls, keeps your config.ini, reinstalls deps, restarts services
git diff config.ini      # shows new options upstream added (your values are kept)
```

## Uninstall

```bash
sudo systemctl disable --now tft911-eas tft911-eas-web tft911-transcriber 2>/dev/null
sudo rm -f /etc/systemd/system/tft911-*.service /etc/udev/rules.d/99-tft911.rules
sudo systemctl daemon-reload
# then delete the install directory (back up alerts/ first)
```
