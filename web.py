#!/usr/bin/env python3
"""
EAS Monitor Web Interface
Runs alongside TFT_EAS_911_Pi_logger.py as a separate process.
Serves a live dashboard fed by the shared events.jsonl file.
"""

import json
import os
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
import configparser

from flask import Flask, render_template_string, jsonify
from flask_socketio import SocketIO
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# =============================
# Config
# =============================

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.ini"
    cfg = {
        'alerts_dir': str(Path(__file__).parent / "alerts"),
        'log_dir':    str(Path(__file__).parent / "logs"),
        'web_port':   5000,
        'web_host':   '0.0.0.0',
    }
    cfg['serial_port'] = '/dev/ttyUSB0'
    if config_path.exists():
        c = configparser.ConfigParser()
        c.read(config_path)
        cfg['alerts_dir']  = c.get('alerts',  'alerts_dir', fallback=cfg['alerts_dir'])
        cfg['log_dir']     = c.get('logging',  'log_dir',   fallback=cfg['log_dir'])
        cfg['web_port']    = c.getint('web',   'port',      fallback=cfg['web_port'])
        cfg['web_host']    = c.get('web',      'host',      fallback=cfg['web_host'])
        cfg['serial_port'] = c.get('serial',   'port',      fallback=cfg['serial_port'])

    def resolve(p):
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else str(Path(__file__).parent / p)
    cfg['alerts_dir'] = resolve(cfg['alerts_dir'])
    cfg['log_dir']    = resolve(cfg['log_dir'])
    return cfg

CONFIG   = load_config()
JSONL    = os.path.join(CONFIG['alerts_dir'], "events.jsonl")
app      = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


# =============================
# Data helpers
# =============================

def read_alerts(limit: int = 100) -> list:
    """Read the most recent alerts from the JSONL file."""
    if not os.path.exists(JSONL):
        return []
    try:
        with open(JSONL, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        alerts = []
        for line in lines[-limit:]:
            try:
                alerts.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return list(reversed(alerts))
    except Exception:
        return []

def _systemctl_active(service: str) -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            timeout=2, capture_output=True
        ).returncode == 0
    except Exception:
        return False

def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False

def logger_running() -> bool:
    """Logger service active (systemd) or logger process running (dev)."""
    if _systemctl_active("tft911-eas"):
        return True
    # Dev fallback: scan process list for the logger script name
    try:
        out = subprocess.run(["pgrep", "-f", "TFT_EAS_911_Pi_logger.py"],
                             capture_output=True, timeout=2).stdout.strip()
        return bool(out)
    except Exception:
        return False

def serial_connected() -> bool:
    """True if the configured serial port device file exists."""
    return os.path.exists(CONFIG['serial_port'])

def icecast_running() -> bool:
    """True if icecast2 service is up or something is listening on port 8000."""
    return _systemctl_active("icecast2") or _port_listening("127.0.0.1", 8000)

def get_stats(alerts: list) -> dict:
    now   = datetime.now(timezone.utc)
    today = now.date()

    today_count = sum(
        1 for a in alerts
        if a.get("received_utc") and
        datetime.strptime(a["received_utc"], "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).date() == today
    )

    last_alert = alerts[0].get("received_local", "None") if alerts else "None"

    last_rwt = next(
        (a.get("received_local", "") for a in alerts if a.get("event_code") == "RWT"),
        "None"
    )

    # Seconds since the most recent alert (for freshness indicator)
    last_utc_str = alerts[0].get("received_utc") if alerts else None
    if last_utc_str:
        last_dt  = datetime.strptime(last_utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        idle_sec = int((now - last_dt).total_seconds())
    else:
        idle_sec = None

    return {
        "today_count": today_count,
        "last_alert":  last_alert,
        "last_rwt":    last_rwt,
        "logger_ok":   logger_running(),
        "serial_ok":   serial_connected(),
        "serial_port": CONFIG['serial_port'],
        "icecast_ok":  icecast_running(),
        "idle_sec":    idle_sec,
        "total":       len(alerts),
    }


# =============================
# Watchdog — detect new alerts
# =============================

class AlertFileHandler(FileSystemEventHandler):
    def __init__(self):
        self._last_size = os.path.getsize(JSONL) if os.path.exists(JSONL) else 0

    def on_modified(self, event):
        if event.src_path != JSONL:
            return
        try:
            current_size = os.path.getsize(JSONL)
            if current_size <= self._last_size:
                return
            # Read only the new bytes appended since last check
            with open(JSONL, encoding="utf-8") as f:
                f.seek(self._last_size)
                new_lines = f.read()
            self._last_size = current_size
            for line in new_lines.strip().splitlines():
                if not line.strip():
                    continue
                try:
                    alert = json.loads(line)
                    socketio.emit("new_alert", alert)
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass


def start_watchdog():
    if not os.path.exists(CONFIG['alerts_dir']):
        return
    handler  = AlertFileHandler()
    observer = Observer()
    observer.schedule(handler, CONFIG['alerts_dir'], recursive=False)
    observer.daemon = True
    observer.start()


# =============================
# Routes
# =============================

@app.route("/")
def index():
    alerts = read_alerts()
    stats  = get_stats(alerts)
    return render_template_string(HTML_TEMPLATE, alerts=alerts, stats=stats)

@app.route("/api/alerts")
def api_alerts():
    return jsonify(read_alerts())

@app.route("/api/stats")
def api_stats():
    alerts = read_alerts()
    return jsonify(get_stats(alerts))

@socketio.on("connect")
def on_connect():
    pass


# =============================
# HTML Template
# =============================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EAS Monitor — ERN/ITH</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0d0d0f;
    --surface:   #141416;
    --surface2:  #1a1a1d;
    --border:    #2a2a2e;
    --border2:   #3a3a3f;
    --text:      #e8e8ea;
    --muted:     #6b6b70;
    --accent:    #4a9eff;
    --warn:      #f0a500;
    --danger:    #e24b4a;
    --success:   #4caf6e;
    --mono:      'IBM Plex Mono', monospace;
    --sans:      'IBM Plex Sans', sans-serif;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
  }

  /* Layout */
  .topbar {
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 10;
  }
  .topbar-title { font-family: var(--mono); font-size: 13px; font-weight: 500; letter-spacing: 0.05em; }
  .topbar-sub { font-size: 11px; color: var(--muted); font-family: var(--mono); margin-top: 1px; }
  .status-pill {
    display: flex; align-items: center; gap: 6px;
    font-size: 11px; font-family: var(--mono);
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 20px; padding: 4px 10px;
  }
  .dot { width: 6px; height: 6px; border-radius: 50%; }
  .dot-green { background: var(--success); box-shadow: 0 0 6px var(--success); }
  .dot-red   { background: var(--danger); }
  .dot-warn  { background: var(--warn); }

  .nav {
    display: flex; gap: 0;
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    overflow-x: auto;
  }
  .nav-item {
    font-size: 12px; font-family: var(--mono);
    padding: 10px 16px; cursor: pointer;
    color: var(--muted); border-bottom: 2px solid transparent;
    white-space: nowrap; transition: color 0.15s;
  }
  .nav-item:hover { color: var(--text); }
  .nav-item.active { color: var(--accent); border-bottom-color: var(--accent); }

  .main { padding: 20px 24px; max-width: 1100px; }

  /* Stats */
  .stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 20px; }
  @media (max-width: 700px) { .stats { grid-template-columns: repeat(2, 1fr); } }
  .stat-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px;
  }
  .stat-label { font-size: 10px; font-family: var(--mono); color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 6px; }
  .stat-value { font-size: 22px; font-family: var(--mono); font-weight: 500; }
  .stat-value.sm { font-size: 13px; padding-top: 4px; }

  /* Two col layout */
  .layout { display: grid; grid-template-columns: 1fr 260px; gap: 16px; }
  @media (max-width: 800px) { .layout { grid-template-columns: 1fr; } }

  /* Alert feed */
  .panel {
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  }
  .panel-header {
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    font-size: 11px; font-family: var(--mono); color: var(--muted);
    letter-spacing: 0.08em; text-transform: uppercase;
    display: flex; align-items: center; justify-content: space-between;
  }
  .panel-body { padding: 0; }

  .alert-item {
    padding: 14px 16px; border-bottom: 1px solid var(--border);
    display: grid; grid-template-columns: 1fr auto;
    gap: 12px; align-items: start;
    animation: slideIn 0.3s ease;
  }
  .alert-item:last-child { border-bottom: none; }
  @keyframes slideIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: translateY(0); } }

  .alert-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
  .badge {
    font-size: 10px; font-family: var(--mono); font-weight: 500;
    padding: 2px 8px; border-radius: 4px; letter-spacing: 0.04em;
  }
  .badge-danger  { background: rgba(226,75,74,0.15);  color: #f07877; border: 1px solid rgba(226,75,74,0.3); }
  .badge-warn    { background: rgba(240,165,0,0.15);  color: #f5c04a; border: 1px solid rgba(240,165,0,0.3); }
  .badge-success { background: rgba(76,175,110,0.15); color: #6dcf8e; border: 1px solid rgba(76,175,110,0.3); }
  .badge-info    { background: rgba(74,158,255,0.15); color: #7ab8ff; border: 1px solid rgba(74,158,255,0.3); }
  .badge-neutral { background: rgba(107,107,112,0.2); color: #9999a0; border: 1px solid rgba(107,107,112,0.3); }

  .event-code { font-family: var(--mono); font-size: 10px; color: var(--muted); }
  .alert-locations { font-size: 12px; color: var(--text); margin-bottom: 3px; }
  .alert-meta { font-size: 11px; color: var(--muted); font-family: var(--mono); }

  /* Countdown */
  .countdown-col { text-align: right; min-width: 90px; }
  .countdown-badge {
    font-size: 10px; font-family: var(--mono); padding: 3px 8px;
    border-radius: 4px; display: inline-block; margin-bottom: 4px;
  }
  .cd-active   { background: rgba(76,175,110,0.12); color: #6dcf8e; border: 1px solid rgba(76,175,110,0.25); }
  .cd-expired  { background: rgba(107,107,112,0.15); color: var(--muted); border: 1px solid var(--border); }
  .cd-indefinite { background: rgba(74,158,255,0.12); color: #7ab8ff; border: 1px solid rgba(74,158,255,0.25); }
  .countdown-time { font-size: 11px; font-family: var(--mono); color: var(--muted); }

  /* Sidebar panels */
  .sidebar { display: flex; flex-direction: column; gap: 12px; }
  .status-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
  .status-row:last-child { border-bottom: none; }
  .status-key { color: var(--muted); font-family: var(--mono); font-size: 11px; }
  .status-val { font-family: var(--mono); font-size: 11px; }
  .ok   { color: var(--success); }
  .err  { color: var(--danger); }
  .warn { color: var(--warn); }

  .action-btn {
    width: 100%; text-align: left; padding: 8px 12px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 12px;
    font-family: var(--mono); cursor: pointer; margin-bottom: 6px;
    transition: border-color 0.15s, background 0.15s;
  }
  .action-btn:last-child { margin-bottom: 0; }
  .action-btn:hover { border-color: var(--border2); background: #202025; }
  .action-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .empty { padding: 32px 16px; text-align: center; color: var(--muted); font-family: var(--mono); font-size: 12px; }

  .live-dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--success);
    display: inline-block; margin-right: 6px;
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

  .page { display: none; }
  .page.active { display: block; }
</style>
</head>
<body>

<div class="topbar">
  <div>
    <div class="topbar-title">ERN/ITH EAS MONITOR</div>
    <div class="topbar-sub">TFT EAS 911 — Ithaca, NY · 036109</div>
  </div>
  <div class="status-pill" id="conn-status">
    <span class="dot dot-warn"></span>connecting
  </div>
</div>

<div class="nav">
  <div class="nav-item active" onclick="showPage('dashboard', this)">Dashboard</div>
  <div class="nav-item" onclick="showPage('history', this)">Alert history</div>
  <div class="nav-item" onclick="showPage('control', this)">Control</div>
</div>

<div class="main">

  <!-- Dashboard -->
  <div id="page-dashboard" class="page active">

    <div class="stats">
      <div class="stat-card">
        <div class="stat-label">Alerts today</div>
        <div class="stat-value" id="stat-today">{{ stats.today_count }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Last alert</div>
        <div class="stat-value sm" id="stat-last">{{ stats.last_alert }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total logged</div>
        <div class="stat-value" id="stat-total">{{ stats.total }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Last RWT</div>
        <div class="stat-value sm" id="stat-rwt">{{ stats.last_rwt }}</div>
      </div>
    </div>

    <div class="layout">

      <div class="panel">
        <div class="panel-header">
          <span><span class="live-dot"></span>live feed</span>
          <span id="feed-count">{{ alerts|length }} alerts</span>
        </div>
        <div class="panel-body" id="alert-feed">
          {% if alerts %}
            {% for alert in alerts %}
            <div class="alert-item" data-expires="{{ alert.expires_utc or '' }}" data-issued="{{ alert.issued_utc or '' }}">
              <div>
                <div class="alert-top">
                  {{ badge(alert) }}
                  <span class="event-code">{{ alert.event_code or '???' }} · {{ alert.originator_code or '???' }}</span>
                </div>
                <div class="alert-locations">{{ alert.locations_pretty[:3]|join(', ') if alert.locations_pretty else 'Unknown location' }}{% if alert.locations_pretty and alert.locations_pretty|length > 3 %} +{{ alert.locations_pretty|length - 3 }} more{% endif %}</div>
                <div class="alert-meta">{{ alert.received_local or '' }} · {{ alert.repeat_count or 1 }} repeat{{ 's' if (alert.repeat_count or 1) != 1 else '' }} · {{ alert.sender or '' }}</div>
              </div>
              <div class="countdown-col">
                <div class="countdown-badge cd-active">active</div>
                <div class="countdown-time">—</div>
              </div>
            </div>
            {% endfor %}
          {% else %}
            <div class="empty">no alerts logged yet</div>
          {% endif %}
        </div>
      </div>

      <div class="sidebar">

        <div class="panel">
          <div class="panel-header">system status</div>
          <div class="panel-body" style="padding: 4px 16px;" id="status-panel">
            <div class="status-row">
              <span class="status-key">logger</span>
              <span class="status-val {{ 'ok' if stats.logger_ok else 'err' }}" id="st-logger">{{ 'running' if stats.logger_ok else 'stopped' }}</span>
            </div>
            <div class="status-row">
              <span class="status-key" id="st-serial-label">{{ stats.serial_port }}</span>
              <span class="status-val {{ 'ok' if stats.serial_ok else 'err' }}" id="st-serial">{{ 'connected' if stats.serial_ok else 'disconnected' }}</span>
            </div>
            <div class="status-row">
              <span class="status-key">icecast</span>
              <span class="status-val {{ 'ok' if stats.icecast_ok else 'warn' }}" id="st-icecast">{{ 'running' if stats.icecast_ok else 'not running' }}</span>
            </div>
            <div class="status-row">
              <span class="status-key">last alert</span>
              <span class="status-val" id="st-idle" style="color: var(--muted)">{{ '—' if stats.idle_sec is none else (stats.idle_sec|string + 's ago') }}</span>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-header">rwt watchdog</div>
          <div class="panel-body" style="padding: 12px 16px;">
            <div style="font-size: 11px; color: var(--muted); font-family: var(--mono); margin-bottom: 4px;">last received</div>
            <div style="font-size: 12px; font-family: var(--mono);">{{ stats.last_rwt }}</div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-header">quick actions</div>
          <div class="panel-body" style="padding: 12px;">
            <button class="action-btn" disabled title="Requires COM3">send weekly test</button>
            <button class="action-btn" disabled title="Requires COM3">reboot TFT unit</button>
            <button class="action-btn" onclick="downloadLog()">download alert log</button>
          </div>
        </div>

      </div>
    </div>
  </div>

  <!-- History -->
  <div id="page-history" class="page">
    <div class="panel">
      <div class="panel-header">
        <span>alert history</span>
        <input type="text" id="search" placeholder="filter by event, location..." oninput="filterHistory()" style="background: var(--surface2); border: 1px solid var(--border); border-radius: 4px; color: var(--text); font-family: var(--mono); font-size: 11px; padding: 3px 8px; width: 200px;">
      </div>
      <div id="history-feed">
        {% if alerts %}
          {% for alert in alerts %}
          <div class="alert-item history-item" data-text="{{ (alert.event_code or '') + ' ' + (alert.locations_pretty|join(' ') if alert.locations_pretty else '') }}">
            <div>
              <div class="alert-top">
                {{ badge(alert) }}
                <span class="event-code">{{ alert.event_code or '???' }} · {{ alert.originator_code or '???' }}</span>
              </div>
              <div class="alert-locations">{{ alert.locations_pretty[:3]|join(', ') if alert.locations_pretty else 'Unknown location' }}</div>
              <div class="alert-meta">{{ alert.received_local or '' }} · {{ alert.repeat_count or 1 }} repeat{{ 's' if (alert.repeat_count or 1) != 1 else '' }} · {{ alert.sender or '' }}</div>
              <div class="alert-meta" style="margin-top: 2px; color: #444; font-size: 10px;">{{ alert.canonical_header or '' }}</div>
            </div>
            <div class="countdown-col">
              <div class="countdown-badge cd-expired">expired</div>
            </div>
          </div>
          {% endfor %}
        {% else %}
          <div class="empty">no alerts logged yet</div>
        {% endif %}
      </div>
    </div>
  </div>

  <!-- Control -->
  <div id="page-control" class="page">
    <div class="panel" style="max-width: 400px;">
      <div class="panel-header">com3 remote control</div>
      <div class="panel-body" style="padding: 16px;">
        <div style="font-size: 12px; color: var(--muted); font-family: var(--mono); margin-bottom: 16px; line-height: 1.8;">
          COM3 remote control requires a USB-RS232 adapter connected to J303 and PC/DTMF enabled in menu 19 on the TFT unit.
        </div>
        <button class="action-btn" disabled>send weekly test (no tone)</button>
        <button class="action-btn" disabled>send weekly test (with tone)</button>
        <button class="action-btn" disabled>send EOM</button>
        <button class="action-btn" disabled>reboot unit</button>
        <button class="action-btn" disabled>live audio patch</button>
      </div>
    </div>
  </div>

</div>

<script>
const socket = io();
const WARNING_EVENTS = ['SVR','SVA','HWW','HWA','FFW','FFA','FLW','FLA','WSW','WSA','BZW','SQW','EWW','DSW','SMW'];
const DANGER_EVENTS  = ['TOR','TOA','HUW','HUA','TSW','TSA','EAN','CEM','CDW','EVI','CAE','LEW','LAE','SPW'];
const TEST_EVENTS    = ['RWT','RMT','NPT','DMO'];

function getBadgeClass(code) {
  if (!code) return 'badge-neutral';
  if (DANGER_EVENTS.includes(code))  return 'badge-danger';
  if (WARNING_EVENTS.includes(code)) return 'badge-warn';
  if (TEST_EVENTS.includes(code))    return 'badge-success';
  return 'badge-info';
}

function getBadgeLabel(code) {
  const labels = {
    TOR:'Tornado Warning', TOA:'Tornado Watch', SVR:'Severe Thunderstorm Warning',
    SVA:'Severe Thunderstorm Watch', FFW:'Flash Flood Warning', FFA:'Flash Flood Watch',
    HUW:'Hurricane Warning', HUA:'Hurricane Watch', TSW:'Tsunami Warning',
    EAN:'Emergency Action Notification', CEM:'Civil Emergency Message',
    RWT:'Required Weekly Test', RMT:'Required Monthly Test', NPT:'National Periodic Test',
    DMO:'Practice/Demo', SPS:'Special Weather Statement', FLW:'Flood Warning',
    WSW:'Winter Storm Warning', WSA:'Winter Storm Watch', BZW:'Blizzard Warning',
    CAE:'Child Abduction Emergency', LEW:'Law Enforcement Warning',
    LAE:'Local Area Emergency', SPW:'Shelter in Place Warning',
    EWW:'Extreme Wind Warning', DSW:'Dust Storm Warning',
  };
  return labels[code] || code || 'Unknown';
}

function formatCountdown(expiresUtc) {
  if (!expiresUtc) return { badge: 'cd-indefinite', label: 'indefinite', time: '' };
  const exp = new Date(expiresUtc.replace('Z', '+00:00'));
  const now = new Date();
  const diff = Math.floor((exp - now) / 1000);
  if (diff <= 0) return { badge: 'cd-expired', label: 'expired', time: '' };
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  const s = diff % 60;
  let time = '';
  if (h > 0) time = h + 'h ' + m + 'm';
  else if (m > 0) time = m + 'm ' + s + 's';
  else time = s + 's';
  return { badge: 'cd-active', label: 'active', time };
}

function updateCountdowns() {
  document.querySelectorAll('.alert-item[data-expires]').forEach(el => {
    const expires = el.dataset.expires;
    const cd = el.querySelector('.countdown-badge');
    const ct = el.querySelector('.countdown-time');
    if (!cd || !ct) return;
    const result = formatCountdown(expires || null);
    cd.className = 'countdown-badge ' + result.badge;
    cd.textContent = result.label;
    ct.textContent = result.time;
  });
}

setInterval(updateCountdowns, 1000);
updateCountdowns();

socket.on('connect', () => {
  const s = document.getElementById('conn-status');
  s.innerHTML = '<span class="dot dot-green"></span>live';
});

socket.on('disconnect', () => {
  const s = document.getElementById('conn-status');
  s.innerHTML = '<span class="dot dot-red"></span>disconnected';
});

socket.on('new_alert', (alert) => {
  prependAlert(alert, document.getElementById('alert-feed'));
  updateFeedCount();
  updateStatToday();
  updateStatLast(alert.received_local || '');
  if (alert.event_code === 'RWT') {
    document.getElementById('stat-rwt').textContent = alert.received_local || '';
  }
});

function prependAlert(alert, container) {
  const empty = container.querySelector('.empty');
  if (empty) empty.remove();

  const code     = alert.event_code || '???';
  const org      = alert.originator_code || '???';
  const locs     = (alert.locations_pretty || []).slice(0, 3).join(', ') || 'Unknown location';
  const more     = (alert.locations_pretty || []).length > 3 ? ` +${alert.locations_pretty.length - 3} more` : '';
  const repeats  = alert.repeat_count || 1;
  const sender   = alert.sender || '';
  const received = alert.received_local || '';
  const expires  = alert.expires_utc || '';
  const bclass   = getBadgeClass(code);
  const blabel   = getBadgeLabel(code);

  const div = document.createElement('div');
  div.className = 'alert-item';
  div.dataset.expires = expires;
  div.innerHTML = `
    <div>
      <div class="alert-top">
        <span class="badge ${bclass}">${blabel}</span>
        <span class="event-code">${code} · ${org}</span>
      </div>
      <div class="alert-locations">${locs}${more}</div>
      <div class="alert-meta">${received} · ${repeats} repeat${repeats !== 1 ? 's' : ''} · ${sender}</div>
    </div>
    <div class="countdown-col">
      <div class="countdown-badge cd-active">active</div>
      <div class="countdown-time"></div>
    </div>`;
  container.insertBefore(div, container.firstChild);
  updateCountdowns();
}

function updateFeedCount() {
  const count = document.querySelectorAll('#alert-feed .alert-item').length;
  document.getElementById('feed-count').textContent = count + ' alerts';
  document.getElementById('stat-total').textContent = count;
}

function updateStatToday() {
  const el = document.getElementById('stat-today');
  el.textContent = parseInt(el.textContent || '0') + 1;
}

function updateStatLast(ts) {
  document.getElementById('stat-last').textContent = ts;
}

function showPage(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  el.classList.add('active');
}

function filterHistory() {
  const q = document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.history-item').forEach(el => {
    const text = (el.dataset.text || '').toLowerCase();
    el.style.display = text.includes(q) ? '' : 'none';
  });
}

function downloadLog() {
  window.location.href = '/api/alerts';
}

// Poll system status every 10 seconds and update sidebar without a page refresh
function refreshStatus() {
  fetch('/api/stats').then(r => r.json()).then(s => {
    const set = (id, text, cls) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      el.className = 'status-val ' + (cls || '');
    };
    set('st-logger', s.logger_ok ? 'running' : 'stopped', s.logger_ok ? 'ok' : 'err');
    set('st-serial', s.serial_ok ? 'connected' : 'disconnected', s.serial_ok ? 'ok' : 'err');
    set('st-icecast', s.icecast_ok ? 'running' : 'not running', s.icecast_ok ? 'ok' : 'warn');
    const lbl = document.getElementById('st-serial-label');
    if (lbl && s.serial_port) lbl.textContent = s.serial_port;
    const idle = document.getElementById('st-idle');
    if (idle) {
      if (s.idle_sec === null || s.idle_sec === undefined) {
        idle.textContent = '—';
      } else if (s.idle_sec < 60) {
        idle.textContent = s.idle_sec + 's ago';
      } else if (s.idle_sec < 3600) {
        idle.textContent = Math.floor(s.idle_sec / 60) + 'm ago';
      } else {
        idle.textContent = Math.floor(s.idle_sec / 3600) + 'h ago';
      }
    }
    document.getElementById('stat-total').textContent = s.total;
  }).catch(() => {});
}

setInterval(refreshStatus, 10000);
</script>
</body>
</html>"""

# Jinja2 helper — inject badge HTML server-side
WARNING_EVENTS = {'SVR','SVA','HWW','HWA','FFW','FFA','FLW','FLA','WSW','WSA','BZW','SQW','EWW','DSW','SMW'}
DANGER_EVENTS  = {'TOR','TOA','HUW','HUA','TSW','TSA','EAN','CEM','CDW','EVI','CAE','LEW','LAE','SPW'}
TEST_EVENTS    = {'RWT','RMT','NPT','DMO'}

BADGE_LABELS = {
    'TOR':'Tornado Warning','TOA':'Tornado Watch','SVR':'Severe Thunderstorm Warning',
    'SVA':'Severe Thunderstorm Watch','FFW':'Flash Flood Warning','FFA':'Flash Flood Watch',
    'HUW':'Hurricane Warning','HUA':'Hurricane Watch','TSW':'Tsunami Warning',
    'EAN':'Emergency Action Notification','CEM':'Civil Emergency Message',
    'RWT':'Required Weekly Test','RMT':'Required Monthly Test','NPT':'National Periodic Test',
    'DMO':'Practice/Demo','SPS':'Special Weather Statement','FLW':'Flood Warning',
    'WSW':'Winter Storm Warning','WSA':'Winter Storm Watch','BZW':'Blizzard Warning',
    'CAE':'Child Abduction Emergency','LEW':'Law Enforcement Warning',
    'LAE':'Local Area Emergency','SPW':'Shelter in Place Warning',
    'EWW':'Extreme Wind Warning','DSW':'Dust Storm Warning',
}

from markupsafe import Markup

def badge(alert):
    code = alert.get('event_code', '')
    if code in DANGER_EVENTS:  cls = 'badge-danger'
    elif code in WARNING_EVENTS: cls = 'badge-warn'
    elif code in TEST_EVENTS:  cls = 'badge-success'
    else: cls = 'badge-info'
    label = BADGE_LABELS.get(code, code or 'Unknown')
    return Markup(f'<span class="badge {cls}">{label}</span>')

app.jinja_env.globals['badge'] = badge


# =============================
# Entry point
# =============================

if __name__ == "__main__":
    os.makedirs(CONFIG['alerts_dir'], exist_ok=True)
    t = threading.Thread(target=start_watchdog, daemon=True)
    t.start()
    print(f"EAS Monitor starting on http://{CONFIG['web_host']}:{CONFIG['web_port']}")
    socketio.run(app, host=CONFIG['web_host'], port=CONFIG['web_port'], debug=False, allow_unsafe_werkzeug=True)
