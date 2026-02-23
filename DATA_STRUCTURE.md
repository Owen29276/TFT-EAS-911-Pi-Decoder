# EAS Logger Data Structure

The logger organizes all output files in a structured directory hierarchy for better organization and management.

## Directory Layout

```
~/eas_logs/
├── logs/                  # Application logs (rotating, debug info)
│   ├── eas_logger.log     # Current application log
│   ├── eas_logger.log.1   # Rotated backup (10MB each, 5 kept)
│   ├── eas_logger.log.2
│   └── ...
│
└── alerts/                # EAS alert data (persistent)
    ├── events.jsonl       # Machine-readable alert records (JSONL format)
    └── events.log         # Human-readable alert logs
```

## Files Explained

### Application Logs (`logs/eas_logger.log`)
- **Purpose**: Debug and operational information about the logger itself
- **Content**: Timestamps, log levels, function names, debug messages
- **Retention**: Automatic rotation at 10MB with 5 backup files kept
- **Usage**: Monitor system health, troubleshoot issues
- **Log Levels**:
  - DEBUG: Detailed technical info (headers, decoded messages)
  - INFO: Important events (startup, alerts received, connections)
  - WARNING: Issues that don't stop execution
  - ERROR: Serious problems (decode failures, serial errors)

### Alert Records (`alerts/events.jsonl`)
- **Purpose**: Machine-readable permanent record of all EAS alerts
- **Format**: JSONL (JSON Lines - one JSON object per line)
- **Content**: Complete alert data including:
  - UTC and local timestamps
  - SAME header (canonical format)
  - Decoded event info (locations, times, originator)
  - EAS2Text decode data
  - Fingerprint for deduplication
- **Retention**: Indefinite (your choice - archive or delete as needed)
- **Usage**: Data analysis, backup, external systems

### Alert Log (`alerts/events.log`)
- **Purpose**: Human-readable archive of all EAS alerts
- **Format**: Formatted text blocks with box drawing
- **Content**: Alert details including:
  - Received time
  - Originator and sender
  - Affected locations
  - Start/end times
  - Full SAME header
- **Retention**: Indefinite (your choice)
- **Usage**: Manual review, quick reference

## Configuration

To change the data directory, edit `config.ini` or use the command-line:

```ini
[logging]
log_dir = ~/eas_logs/logs

[alerts]
alerts_dir = ~/eas_logs/alerts
```

On Raspberry Pi, you might want to use:
- `/var/log/eas_logs/` - System logs directory
- `/home/pi/eas_logs/` - Home directory (recommended)
- External storage path - For long-term archival

## Log Rotation

Application logs rotate automatically:
- **Trigger**: When log file reaches 10MB
- **Backups**: 5 previous logs kept (eas_logger.log.1 through .5)
- **Oldest**: Deleted automatically when limit reached

Example sequence:
```
eas_logger.log (current, growing)
eas_logger.log.1 (most recent backup)
eas_logger.log.2
eas_logger.log.3
eas_logger.log.4
eas_logger.log.5 (oldest, will be deleted on next rotation)
```

## Maintenance

### Viewing Logs
```bash
# Watch live application logs (tail -f)
tail -f ~/eas_logs/logs/eas_logger.log

# View all alerts
cat ~/eas_logs/alerts/events.log

# Analyze alert data (JSONL format)
jq '.' ~/eas_logs/alerts/events.jsonl | less
```

### Archiving
```bash
# Archive old alert data
tar -czf alerts_backup_2026-02.tar.gz ~/eas_logs/alerts/

# Archive logs
tar -czf logs_backup_2026-02.tar.gz ~/eas_logs/logs/
```

### Cleanup
```bash
# Clear alert logs (keep JSONL for data)
rm ~/eas_logs/alerts/events.log

# Remove old backups (keep current log only)
rm ~/eas_logs/logs/eas_logger.log.[2-5]
```

## Systemd Integration

When running as a systemd service on Raspberry Pi, logs are:
- **Application logs**: `/home/pi/eas_logs/logs/` (rotating)
- **Alert logs**: `/home/pi/eas_logs/alerts/` (persistent)
- **Systemd journal**: Also captured via `sudo journalctl -u eas-logger`

## Quick Reference

| File | Type | Size | Keep? | Rotate? |
|------|------|------|-------|---------|
| eas_logger.log | Debug | ~10MB | Temporary | Yes, auto |
| events.jsonl | Data | Growing | Yes | No |
| events.log | Reference | Growing | Yes | No |

---

**Last Updated**: February 19, 2026
