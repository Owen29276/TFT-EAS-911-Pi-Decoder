# TFT EAS 911 Pi Decoder — Project Context
# For Claude Code — read this before doing anything

## What this project is
A Raspberry Pi-based EAS (Emergency Alert System) monitoring and control system
built around a TFT EAS 911 hardware ENDEC (Encoder/Decoder). The Pi logs all
decoded EAS alerts, controls the TFT remotely via COM3, and serves a web dashboard.

## Hardware
- **TFT EAS 911** — commercial EAS ENDEC, rack mount, manufactured 2002
  - Firmware: V.840H
  - Options installed: voice recorder, 4-port COM expander, 4-channel audio input expander
  - Serial number: 10194789
- **Raspberry Pi** (hostname: TFT, IP: 192.168.1.127, user: owen)
- **Sangean CL-100** weather radio → J102 CH1 → TFT monitors NWR
- **ERN Icecast stream** playing via Pi → J102 CH2 (GWES ERN monitoring requirement)
- **USB-RS232 adapter** on /dev/tft911-data → J103 (data output, 1200 baud)
- **USB-RS232 adapter** on /dev/tft911-cmd → J303 COM3 (remote control, 9600 baud)
- **Corsair HS65** USB audio adapter → J101 XLR audio output (for Icecast stream capture)

## udev rules
Both serial adapters have persistent names via /etc/udev/rules.d/99-tft911.rules:
- /dev/tft911-data → ttyUSB0 (EDBTb11A920) — J103 logger
- /dev/tft911-cmd  → ttyUSB1 (CFBHb153609) — J303 COM3 control

## Repo
https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder
Install path on Pi: ~/TFT-EAS-911-Pi-Decoder

## Files in the repo
- TFT_EAS_911_Pi_logger.py — main logger daemon (reads J103, decodes, logs, notifies)
- tft_control.py           — TFTController class + CLI (controls TFT via COM3)
- web.py                   — Flask web dashboard with live alerts and control panel
- virtual_tft.py           — test harness, simulates TFT serial output via stdin
- config.ini               — shared configuration
- setup.sh                 — universal setup script (Pi + laptop detection)
- requirements.txt         — Python dependencies
- systemd services:
  - tft911-eas.service     — logger daemon
  - tft911-eas-web.service — web dashboard

## config.ini structure
```ini
[serial]
port = /dev/tft911-data
baud = 1200

[control]
port = /dev/tft911-cmd
baud = 9600
pin = 915

[logging]
log_dir = logs
log_level = INFO

[alerts]
alerts_dir = alerts
dedupe_window = 120

[notifications]
ntfy_topic =

[hardware]
filler_byte = 0xAB

[web]
host = 0.0.0.0
port = 5000

[advanced]
max_buffer_size = 200000
buffer_trim_size = 100000
serial_timeout = 1
serial_retry_delay = 1
notification_timeout = 5
```

## TFT EAS 911 COM3 protocol
Commands are DTMF strings: *{PIN}{COMMAND_CODE}#
PIN is configured in menu 19 on the TFT front panel. Owen's PIN is 915.

| Command | Sends |
|---------|-------|
| RWT with tone | *91531# |
| RWT no tone | *91530# |
| Record announcement (CH1) | *91521# |
| Play announcement | *91522# |
| Record voice | *91509# |
| Play voice | *91511# |
| Live patch | *91520# |
| Stop/end operation | # |
| Originate no audio | *91540# |
| Originate pre-recorded | *91541# |
| Send EOM | *91543# |
| Reboot | *91591# |

Originate sequence:
1. *91541# (or *91540#)
2. *{event_number}#
3. *{location_keys}#
4. *{duration_code}#

Event numbers: RWT=34, DMO=31, TOR=42, SVR=36, FFW=15, CEM=7 etc (see TFT_EVENTS dict)
Location keys: front panel key numbers (1-14), multiple = consecutive e.g. *12# for keys 1 and 2
Duration codes: 01=15min, 02=30min, 03=45min, 04=1hr, 06=1.5hr, 08=2hr

## TFT setup menu items (front panel only, cannot be set via COM3)
1.  Set current date/time (local time)
2.  Set station time zone (Owen's: +05 for Eastern Standard, DST handled by item 3)
3.  Daylight saving (ENABLE)
4.  Set station ORG code (EAS)
5.  Set station FIPS code (036109 = Tompkins County NY)
6.  Set station ID code (KITH_EAS, max 8 chars)
7.  Set attention signal duration (seconds)
8.  Change primary password
9.  Change setup password
10. Select events to auto forward
11. Add locations to auto forward
12. Verify/delete locations to auto forward
13. Assign encoder event keys (11 keys)
14. Assign encoder location keys (14 keys, each can hold 31 FIPS codes)
15. Verify/edit encoder location keys
16. Voice recorder installed (YES)
17. Set remote sign protocol
18. Enable char gen interface
19. Remote interface definition (PC/DTMF — Owen's PIN: 915)
20. Set LCD contrast
21. Record voice announcement
22. Verify voice announcement
23. Enable remote control/status module interface
24. Enable one-button weekly test
25. Set alert timeout
26. Set one-button manual forward

## Current project task
Owen is writing tft_control.py from scratch as a CIS 213 final project (due May 15).
The goal is a TFTController class that can be imported by web.py and called from a CLI.

### What Owen is building (student writes this):
A setup_wizard() function that:
- Checks if config.ini has [station] section filled in
- If not: walks user through TFT configuration
  - Asks for callsign, FIPS, ORG code, timezone, DST
  - Walks through location key assignments (name + FIPS codes per key)
  - Walks through event key assignments
  - Saves everything to config.ini
- Stores the TFT's configuration so the software knows the machine

### Coding style rules (IMPORTANT)
- Owen learns by understanding, not by rote — explain concepts when introducing them
- Keep code clean and readable — this is explicitly what his instructor grades on
- Comments explain WHY not just what
- Docstrings on every function (required by rubric)
- No unnecessary complexity — if it can be simple, keep it simple
- Do NOT write everything for him — guide, review, help when stuck
- He should understand every line before moving on
- The completed tft_control.py should read naturally to someone who knows Python

### Rubric (100 points)
1. Functionality (20) — runs correctly, clear problem-solving
2. Functions/modularity (10) — well-designed, no repetition
3. Docstrings (10) — all functions documented with purpose/params/returns
4. Comments (10) — clear comments on major logic
5. Data structures (10) — lists, dicts, JSON used appropriately
6. File I/O (10) — reads/writes files meaningfully
7. API usage (5) — optional bonus
8. Output quality (10) — readable, user-friendly
9. Execution quality (5) — runs without modification
10. AI usage reflection (10) — thorough explanation of AI assistance

### AI reflection story (Owen should write this himself)
The honest story: started with tftcmd (someone else's code) which worked but was
unusable headless. Owen understood WHY it was unusable. He redesigned it as a class
that separates concerns — connection, commands, configuration, CLI. He used AI to
understand the TFT protocol, review code, and explain concepts — but drove the
design decisions himself. The basic_logger.py he wrote himself was cleaner than the
AI-generated version, which taught him something real about AI-assisted development.

## ERN (GWES EAS Relay Network) goals
Owen is working toward rejoining ERN as station ERN/ITH. Requirements:
- Commercial EAS equipment (TFT 911 ✓)
- Monitor at least one internet stream (ERN stream on CH2 ✓)
- Stream to Icecast server (needs darkice configured — blocked by audio adapter issue)
- 60% monthly Icecast uptime

## Known issues / in progress
- Icecast stream not yet configured (need TRRS adapter or $8 USB audio adapter for J101 capture)
- TFT clock timestamp encoding was wrong (fixed: set to +05 UTC offset, DST enabled)
- NWS Buffalo transmitter has misconfigured clock — alerts arrive with wrong timestamps
  (fixed in logger: timestamp_suspect flag instead of dropping alerts)
- web.py was lost and regenerated — Owen should push latest version to GitHub

## Dependencies (requirements.txt)
- pyserial>=3.5
- requests>=2.31.0
- EAS2Text-Remastered>=0.1.23
- flask
- flask-socketio
- watchdog
