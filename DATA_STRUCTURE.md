# EAS Logger Data Structure

## Directory Layout

All output files are written inside the repo directory:

```
TFT-EAS-911-Pi-Decoder/
├── logs/
│   ├── eas_logger.log       # Current application log
│   ├── eas_logger.log.1     # Rotated backup (10MB each, 5 kept)
│   └── ...
└── alerts/
    ├── events.jsonl         # Machine-readable alert records (JSONL)
    └── events.log           # Human-readable alert archive
```

Both directories are created automatically on first run and excluded from git.

## Files

### `logs/eas_logger.log`
Operational log from the Python `logging` module. Rotates at 10MB, keeps 5 backups. Use for troubleshooting and monitoring service health.

### `alerts/events.jsonl`
One JSON object per line. Permanent record of every processed alert.

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `alert_id` | string | Unique ID `YYYYMMDDTHHMMSSZ-<hash8>`; names the audio file and links transcripts |
| `received_utc` | string | ISO 8601 UTC when alert was received |
| `received_local` | string | Local time when alert was received |
| `canonical_header` | string | Majority-voted SAME header |
| `repeat_count` | int | Header copies found in the burst (normally 3) |
| `issued_utc` | string\|null | Issue time from the JJJHHMM field (year inferred, Dec 31 → Jan 1 safe) |
| `expires_utc` | string\|null | Issue time + duration; `null` for +0000 (e.g. national alerts) |
| `raw_burst` | string | Complete raw ZCZC…NNNN burst as received |
| `event_code` | string | `TOR`, `SVR`, `FFW`, `RWT`, etc. |
| `originator_code` | string | `WXR` (NWS), `EAS` (local), `CIV`, `PEP` |
| `sender` | string | Sender text from EAS2Text (may be empty) |
| `event_text` | string | EAS2Text event description, e.g. "a Tornado Warning" |
| `org_text` | string | EAS2Text originator description |
| `eas_text` | string | Full EAS2Text decoded sentence |
| `locations_pretty` | array | Human-readable county/state names |
| `notification` | object | `{"attempted": true}` if an ntfy push was queued |
| `decode_error` | string | Only present when EAS2Text is missing or fails (decoded fields are then absent) |

The record is written to disk **before** the ntfy push is sent (in a background thread), so a slow or failed push can't lose an alert. Delivery results go to `logs/eas_logger.log`.

### `alerts/audio/<alert_id>.wav`
Captured alert audio (16 kHz mono 16-bit PCM, ~32 KB/s). Only written when `[audio] enabled = true`; the newest `keep_files` (default 200) are kept.

### `alerts/transcripts.jsonl`
One line per transcribed alert, appended when the transcription server replies:
`alert_id`, `canonical_header`, `event_code`, `received_utc`, `transcribed_utc`, `audio_file`, `text`, `language`, `audio_seconds`, `model`. Join to `events.jsonl` on `alert_id`.

### `alerts/audit.jsonl`
Written by the web dashboard: one line per login attempt, control action, config save, PTT and recording, with `time_utc`, `ip`, `action` and `detail`.

### `alerts/events.log`
Human-readable formatted text blocks, one per alert. Same content as the console receipt output.

## Configuration

Paths can be changed in `config.ini`:

```ini
[logging]
log_dir = logs

[alerts]
alerts_dir = alerts
```

Relative paths resolve to the script directory. Absolute paths and `~` are also supported.

## Maintenance

```bash
# Watch live alerts
tail -f ~/TFT-EAS-911-Pi-Decoder/alerts/events.log

# Query JSON records
jq '.' ~/TFT-EAS-911-Pi-Decoder/alerts/events.jsonl | less

# Archive
tar -czf alerts_backup.tar.gz ~/TFT-EAS-911-Pi-Decoder/alerts/

# Clear human-readable log (keeps JSONL data)
rm ~/TFT-EAS-911-Pi-Decoder/alerts/events.log
```
