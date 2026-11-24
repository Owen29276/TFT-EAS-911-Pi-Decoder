# Proxmox Deployment (DL360p VM)

Runs everything (logger, audio capture, web dashboard/C2, Whisper transcription) in
one Debian VM. The TFT's two USB-serial adapters and the USB sound card are passed
through to the VM, and the Pi is retired.

```
TFT EAS 911 ── J103 ──▶ USB-serial #1 ─┐
            ── J303 ──▶ USB-serial #2 ─┤ USB passthrough   ┌─ VM "tft911" (Debian 12) ────────────┐
            ── audio out ▶ USB sound ──┤ ─────────────────▶│ tft911-eas          logger + audio   │
            ◀─ CH1 in ──── card out ───┘                   │ tft911-eas-web      dashboard :5000  │
                                                           │ tft911-transcriber  Whisper, 127.0.0.1│
                                                           └──────────── MQTT ──▶ Home Assistant VM
```

## 1. Cabling

Same connections as on the Pi, but now into the server's USB ports:

| TFT | → | USB device |
|---|---|---|
| J103 (logger serial out) | | USB-RS232 adapter #1 |
| J303 COM3 (PC/DTMF control) | | USB-RS232 adapter #2 |
| Monitoring receiver audio (Y-split from the feed into J102 CH1) | | USB sound card **line/mic in** (alert capture) |
| CH1 audio input | | USB sound card **headphone/line out** (TTS, browser PTT, VoIP) |

Per the TFT manual, J101 (XLR) is the *encoder* output and only carries audio when the
911 transmits or forwards. Capture from the receiver feed instead so you record every
alert, forwarded or not. J102 is 10 kΩ bridging, so the split is harmless.

Label the two serial adapters. If they're identical cheap adapters (same vendor,
product and serial number), keep each one in the **same physical port** from now on,
because that's the only way to tell them apart.

## 2. Find the devices on the Proxmox host

```bash
lsusb          # e.g. 0403:6001 Future Technology Devices FT232 ... / 0d8c:0014 C-Media USB Audio
lsusb -t       # shows bus-port paths, e.g. Bus 001 → Port 1 → Port 2  ==  1-1.2
```

## 3. Create the VM

In the Proxmox web UI, **Create VM**:

| Setting | Value | Why |
|---|---|---|
| OS | Debian 12 netinst ISO | |
| Machine | q35 | |
| **CPU type** | **host** | The default x86-64-v2 type hides AVX from the guest, so Whisper would fall back to slow generic code. (x86-64-v3 won't boot: Ivy Bridge has no AVX2.) |
| Cores / RAM | 4 cores / 6 GB | Logger + dashboard are tiny; Whisper `small.en` uses ~1–2 GB while transcribing |
| Disk | 32 GB | Models ~0.5 GB, alert audio ~1–4 MB each (newest 200 kept) |
| QEMU Guest Agent | enabled | |

Then go to **Options**, set **Start at boot = Yes**, and set **Startup order** to start it early.

### USB passthrough

**Hardware → Add → USB Device**, once per device:

- **Use USB Vendor/Device ID** if every device has a different ID (e.g. FTDI
  adapter + Prolific adapter + C-Media card). The device then follows the hardware
  to any port.
- **Use USB Port** if the two serial adapters have the **same** vendor:device ID.
  Passing through by ID can't tell identical adapters apart.

CLI equivalent (VM 120 as an example):

```bash
qm set 120 -usb0 host=1-1.1      # J103 adapter, by port
qm set 120 -usb1 host=1-1.2      # J303 adapter, by port
qm set 120 -usb2 host=0d8c:0014  # USB sound card, by ID
```

USB passthrough is hot-pluggable, so unplugging and replugging re-attaches the device
to the running VM.

## 4. Move your data off the Pi (optional, do this first)

On the **Pi**, stop the services so two loggers don't both publish to Home Assistant:

```bash
sudo systemctl disable --now tft911-eas tft911-eas-web
```

In the **VM**, get the repo (push your latest changes to GitHub first, or `scp -r` the
project folder over instead of cloning), then copy the Pi's config, secrets and history:

```bash
git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git ~/tft911-eas
cd ~/tft911-eas
rsync -av pi@<pi-ip>:~/TFT-EAS-911-Pi-Decoder/{config.ini,secrets.ini} .
rsync -av pi@<pi-ip>:~/TFT-EAS-911-Pi-Decoder/alerts/ alerts/
```

Your station settings, location keys, dashboard password hash, ntfy topic and MQTT
login carry over. Home Assistant keeps the same device and entity history, because
the MQTT node ID doesn't change.

## 5. Install

```bash
cd ~/tft911-eas
bash setup.sh --server
```

On a VM, `--server` does everything the Pi install did, plus:

- **Groups.** Adds you to `dialout` (serial), `audio` (sound card) and
  `systemd-journal` (dashboard log view). Pi OS gave its default user these for free;
  a fresh Debian user has none of them.
- **Serial names.** Lists the passed-through adapters, asks which one is J103 and which
  is J303, and writes `/etc/udev/rules.d/99-tft911.rules`, so `/dev/tft911-data` and
  `/dev/tft911-cmd` survive reboots. It matches on the adapter's serial number when
  that's unique, otherwise on the USB port.
- **Sound card.**
  - Detects the USB card and sets `[audio] device = plughw:CARD=<name>,DEV=0`.
  - Makes the card the default playback device (`/etc/asound.conf`) so TTS, PTT and
    VoIP go out to TFT CH1.
  - Offers to turn on audio capture.
- **Whisper.**
  - Installs faster-whisper in its own venv.
  - Generates a random token and writes it to both `/etc/tft911-transcriber.env`
    (root-only) and `secrets.ini`.
  - Binds the service to **127.0.0.1**, so it's never reachable from the network.
- **Services.**
  - `tft911-eas` (always run with `--serial`), `tft911-eas-web` and `tft911-transcriber`.
  - Installs `qemu-guest-agent`.

Log out and back in afterwards so your shell picks up the new groups. The services
already have them.

## 6. Verify

```bash
ls -l /dev/tft911-*                         # both symlinks present
arecord -l; aplay -l                        # USB card listed for capture and playback
curl -s 127.0.0.1:8765/health               # {"loaded":true,"model":"small.en","ok":true}
sudo journalctl -u tft911-eas -n 30         # "Opened /dev/tft911-data @ 1200 baud",
                                            # "Audio recording from ALSA device ...", "MQTT connected"
```

Then open `http://<vm-ip>:5000`, log in, and check that the control status shows
COM3 connected. Send a test RWT from the Panel page, or wait for the next weekly
test, and confirm all of these:

- the alert is logged
- `alerts/audio/<id>.wav` exists
- `alerts/transcripts.jsonl` has the text
- Home Assistant updated

## Operations notes

- **Reboots take minutes.** A DL360p Gen8 takes several minutes to POST. While it's
  down, alerts aren't logged (the TFT still works standalone). Schedule Proxmox host
  updates, and put the server on a UPS.
- **Monitoring.** The Home Assistant entities go `unavailable` whenever the logger
  isn't running. Use the "logger offline" automation in HOME_ASSISTANT.md, but run it
  on a machine other than this server so you hear about server outages too.
- **Backups.** Include the VM in your Proxmox backup job (vzdump). Alert history,
  audio and transcripts all live in `~/tft911-eas/alerts/`.
- **Firewall.** Only port 5000 (dashboard) and SSH need to be reachable, from the LAN
  or Tailscale only. Whisper listens on localhost. Outbound MQTT goes to the HA VM.
  In Proxmox, **VM → Firewall** can enforce this.
