# Transcription server

> **Running the logger in a Proxmox VM?** You don't need this page:
> `bash setup.sh --server` installs this service in the same VM, bound to 127.0.0.1,
> and wires up the token for you (see [PROXMOX_DEPLOYMENT.md](../PROXMOX_DEPLOYMENT.md)).
> The steps below are for running it on a *separate* machine, e.g. when the logger
> stays on a Pi.

The Pi 3B+ is too slow for Whisper, so the Pi uploads each alert's audio to this
small service on the DL360p. It transcribes the audio with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) on the CPU and returns
the text. The Pi then saves the transcript and publishes it to Home Assistant.

```
Pi (TFT_logger.py) ── WAV over HTTP + bearer token ──▶ LXC "tft911-transcriber" :8765
                   ◀── {"text": "...", "duration": ...} ──
```

## 1. Create the LXC

In Proxmox, create an unprivileged **Debian 12** container:

- 4 cores, 4 GB RAM, 16 GB disk (`small.en` uses ~1 GB of RAM while transcribing)
- Static IP on the LAN, e.g. `192.168.1.60`

## 2. Install

```bash
apt update && apt install -y python3-venv git
useradd --system --home /opt/tft911-transcriber --shell /usr/sbin/nologin tft911
mkdir -p /opt/tft911-transcriber/models
cd /opt/tft911-transcriber

# Copy server.py, requirements.txt, and the .service/.env.example files from this folder
# (scp from your laptop, or git clone the repo and copy transcriber/*)
python3 -m venv venv
venv/bin/pip install -r requirements.txt
chown -R tft911:tft911 /opt/tft911-transcriber

cp tft911-transcriber.env.example /etc/tft911-transcriber.env
chmod 600 /etc/tft911-transcriber.env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # paste into TRANSCRIBE_TOKEN
nano /etc/tft911-transcriber.env

cp tft911-transcriber.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tft911-transcriber
journalctl -u tft911-transcriber -f     # first start downloads the model (~500 MB for small.en)
```

Check it is up:

```bash
curl http://192.168.1.60:8765/health
# {"loaded":true,"model":"small.en","ok":true}
```

## 3. Point the Pi at it

`config.ini` on the Pi:

```ini
[transcribe]
url = http://192.168.1.60:8765
```

Put the token in `secrets.ini` on the Pi (`[transcribe] token = ...`), then run
`sudo systemctl restart tft911-eas`.

## Notes

- **Xeon E5 v2 (Ivy Bridge) has AVX but no AVX2.** CTranslate2 detects the CPU at
  runtime and works on AVX-only CPUs. If it crashes with `Illegal instruction`,
  uncomment `CT2_FORCE_CPU_ISA=AVX` in the env file.
- **Audio decoding** uses the Python `wave` module plus numpy, not PyAV/FFmpeg. Only
  16-bit PCM WAV is accepted. This avoids the faster-whisper 1.2.1 + PyAV 19
  incompatibility (`unexpected keyword argument 'metadata_errors'`). It also keeps
  FFmpeg's decoders away from uploaded files.
- The **VAD filter** removes the SAME data bursts, the attention tone, and silence
  before transcription. In testing it cut 17 s of tones from a 40 s clip and produced
  no junk text.
- **Security:**
  - Every upload needs the bearer token.
  - Uploads are capped at 40 MB and 10 minutes of audio.
  - Files are deleted after transcription.
  - The service runs as an unprivileged user with systemd hardening.
  - Keep port 8765 LAN-only: don't port-forward it.
- **Model choice** (CPU, int8):
  - `base.en` is fast and good on clean audio. On an Apple M4 it transcribed a 40 s
    alert in 1.7 s.
  - `small.en` is noticeably more accurate on noisy NOAA Weather Radio audio.
  - `medium.en` is the most accurate, but expect several times slower.
