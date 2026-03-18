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
| `received_utc` | string | ISO 8601 UTC when alert was received |
| `received_local` | string | Local time when alert was received |
| `canonical_header` | string | Majority-voted SAME header |
| `originator_code` | string | `WXR` (NWS), `EAS` (local), `CIV`, `PEP` |
| `event_code` | string | `TOR`, `SVR`, `FFW`, `RWT`, etc. |
| `sender` | string | Station ID from header |
| `issued_utc` | string | Issue time parsed from JJJHHMM field |
| `expires_utc` | string\|null | Expiry time; `null` for national alerts (+0000) |
| `repeat_count` | int | Header copies received (1–3) |
| `saw_eom` | bool | Whether NNNN end-of-message was received |
| `locations_pretty` | array | Human-readable county/state names |
| `eas2text` | object | Full EAS2Text decode output |
| `raw_burst` | string | Complete raw serial burst |
| `notification` | object | ntfy.sh delivery receipt |

`notification` values:
- `{"attempted": false}` — ntfy not configured
- `{"attempted": true, "sent": true, "http_status": 200}` — delivered
- `{"attempted": true, "sent": false, "http_status": 403}` — HTTP error
- `{"attempted": true, "sent": false, "error": "..."}` — network error

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
