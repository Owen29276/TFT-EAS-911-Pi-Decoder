#!/usr/bin/env python3
"""
TFT EAS 911 Web Dashboard
Complete browser interface: live alert feed · TFT remote control ·
PTT audio streaming · real-time log tail · config editor.
"""

import json, os, struct, threading, subprocess, configparser
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from markupsafe import Markup
from TFT_Control import TFTController, load_location_keys
from utills import build_same_header, decode_header, search_fips, parse_location_keys


# ── config ─────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.ini"

def _load_web_config() -> dict:
    cfg = {
        'alerts_dir':  str(Path(__file__).parent / "alerts"),
        'log_dir':     str(Path(__file__).parent / "logs"),
        'web_port':    5000,
        'web_host':    '0.0.0.0',
        'serial_port': '/dev/ttyUSB0',
    }
    if CONFIG_PATH.exists():
        c = configparser.ConfigParser()
        c.read(CONFIG_PATH)
        cfg['alerts_dir']  = c.get('alerts',  'alerts_dir', fallback=cfg['alerts_dir'])
        cfg['log_dir']     = c.get('logging', 'log_dir',    fallback=cfg['log_dir'])
        cfg['web_port']    = c.getint('web',  'port',       fallback=cfg['web_port'])
        cfg['web_host']    = c.get('web',     'host',       fallback=cfg['web_host'])
        cfg['serial_port'] = c.get('serial',  'port',       fallback=cfg['serial_port'])
    def resolve(p):
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else str(Path(__file__).parent / p)
    cfg['alerts_dir'] = resolve(cfg['alerts_dir'])
    cfg['log_dir']    = resolve(cfg['log_dir'])
    return cfg

CONFIG   = _load_web_config()
JSONL    = os.path.join(CONFIG['alerts_dir'], "events.jsonl")
app      = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


# ── TFT controller ─────────────────────────────────────────────────────────

tft     = None
_tft_lk = threading.Lock()

def _connect_tft():
    global tft
    try:
        t = TFTController()
        t.connect()
        tft = t
        print("[web] COM3 connected.")
    except Exception as e:
        tft = None
        print(f"[web] COM3 unavailable: {e}")

_connect_tft()

def tft_ok() -> bool:
    return tft is not None and getattr(getattr(tft, 'ser', None), 'is_open', False)

def _tft_call(fn):
    """Call zero-arg fn() under the TFT lock, return JSON result."""
    if not tft_ok():
        return jsonify({"ok": False, "error": "COM3 not connected"}), 503
    try:
        with _tft_lk:
            fn()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── PTT state ──────────────────────────────────────────────────────────────

_ptt_lk   = threading.Lock()
_ptt_proc = None   # aplay subprocess while PTT is active


# ── data helpers ───────────────────────────────────────────────────────────

def read_alerts(limit: int = 200) -> list:
    if not os.path.exists(JSONL):
        return []
    try:
        with open(JSONL, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        out = []
        for line in lines[-limit:]:
            try: out.append(json.loads(line))
            except: pass
        return list(reversed(out))
    except Exception:
        return []

def logger_running() -> bool:
    try:
        return subprocess.run(
            ['systemctl', 'is-active', 'tft911-eas'],
            capture_output=True
        ).returncode == 0
    except Exception:
        return False

def serial_connected() -> bool:
    return os.path.exists(CONFIG['serial_port'])

def get_stats(alerts: list) -> dict:
    today = datetime.now(timezone.utc).date()
    today_count = sum(
        1 for a in alerts
        if a.get("received_utc") and
        datetime.strptime(a["received_utc"], "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).date() == today
    )
    return {
        "today_count": today_count,
        "last_alert":  alerts[0].get("received_local", "None") if alerts else "None",
        "last_rwt":    next((a.get("received_local","") for a in alerts
                             if a.get("event_code") == "RWT"), "None"),
        "logger_ok":   logger_running(),
        "serial_ok":   serial_connected(),
        "control_ok":  tft_ok(),
        "total":       len(alerts),
    }


# ── watchdog ───────────────────────────────────────────────────────────────

class AlertFileHandler(FileSystemEventHandler):
    def __init__(self):
        self._last_size = os.path.getsize(JSONL) if os.path.exists(JSONL) else 0

    def on_modified(self, event):
        if event.src_path != JSONL:
            return
        try:
            sz = os.path.getsize(JSONL)
            if sz <= self._last_size:
                return
            with open(JSONL, encoding="utf-8") as f:
                f.seek(self._last_size)
                new = f.read()
            self._last_size = sz
            for line in new.strip().splitlines():
                if line.strip():
                    try: socketio.emit("new_alert", json.loads(line))
                    except: pass
        except Exception:
            pass

def start_watchdog():
    if not os.path.exists(CONFIG['alerts_dir']):
        return
    ob = Observer()
    ob.schedule(AlertFileHandler(), CONFIG['alerts_dir'], recursive=False)
    ob.daemon = True
    ob.start()


# ── log streaming ──────────────────────────────────────────────────────────

def start_log_stream():
    """Tail journalctl in a background thread and push each line via WebSocket."""
    try:
        proc = subprocess.Popen(
            ['journalctl', '-u', 'tft911-eas', '-f', '-n', '50', '--output=cat'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        for line in proc.stdout:
            socketio.emit('log_line', {'line': line.rstrip()})
    except Exception:
        pass


# ── config helpers ─────────────────────────────────────────────────────────

def _read_config_dict() -> dict:
    c = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        c.read(CONFIG_PATH)
    return {s: dict(c[s]) for s in c.sections()}


# ── routes — data ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    alerts = read_alerts()
    return render_template_string(HTML, alerts=alerts, stats=get_stats(alerts))

@app.route("/api/alerts")
def api_alerts():
    return jsonify(read_alerts())

@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats(read_alerts()))

@app.route("/api/logs")
def api_logs():
    try:
        r = subprocess.run(
            ["journalctl", "-u", "tft911-eas", "-n", "100", "--no-pager", "-o", "short"],
            capture_output=True, text=True, timeout=5
        )
        return jsonify({"lines": r.stdout.splitlines()})
    except Exception as e:
        return jsonify({"lines": [str(e)]})


# ── routes — control ───────────────────────────────────────────────────────

@app.route("/api/control/status")
def api_control_status():
    return jsonify({"connected": tft_ok()})

@app.route("/api/control/reconnect", methods=["POST"])
def api_reconnect():
    global tft
    with _tft_lk:
        try: tft and tft.disconnect()
        except: pass
    _connect_tft()
    return jsonify({"ok": tft_ok(), "connected": tft_ok()})

@app.route("/api/control/rwt", methods=["POST"])
def api_rwt():
    tone = (request.json or {}).get("tone", True)
    return _tft_call(lambda: tft.send_rwt(attention_tone=tone))

@app.route("/api/control/eom", methods=["POST"])
def api_eom():
    return _tft_call(lambda: tft.send_eom())

@app.route("/api/control/stop", methods=["POST"])
def api_stop():
    return _tft_call(lambda: tft.stop())

@app.route("/api/control/reboot", methods=["POST"])
def api_reboot():
    return _tft_call(lambda: tft.reboot())

@app.route("/api/control/voice/record", methods=["POST"])
def api_voice_record():
    return _tft_call(lambda: tft.record_voice())

@app.route("/api/control/voice/play", methods=["POST"])
def api_voice_play():
    return _tft_call(lambda: tft.play_voice())

@app.route("/api/control/announcement/record", methods=["POST"])
def api_ann_record():
    return _tft_call(lambda: tft.record_announcement())

@app.route("/api/control/announcement/play", methods=["POST"])
def api_ann_play():
    return _tft_call(lambda: tft.play_announcement())

@app.route("/api/control/patch", methods=["POST"])
def api_patch():
    return _tft_call(lambda: tft.live_patch())

@app.route("/api/control/announce", methods=["POST"])
def api_announce():
    if not tft_ok():
        return jsonify({"ok": False, "error": "COM3 not connected"}), 503
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No text provided"}), 400
    return _tft_call(lambda: tft.record_announcement_tts(text))

@app.route("/api/control/originate", methods=["POST"])
def api_originate():
    if not tft_ok():
        return jsonify({"ok": False, "error": "COM3 not connected"}), 503
    d        = request.json or {}
    event    = d.get("event", "").upper()
    locations = d.get("locations", "")
    duration = d.get("duration", "01")
    try:
        if d.get("tts"):
            if tft is None:
                return jsonify({"ok": False, "error": "COM3 not connected"}), 503
            with _tft_lk:
                text = tft.originate_with_tts(event, locations, duration)
            return jsonify({"ok": True, "text": text})
        return _tft_call(lambda: tft.originate(
            event, locations, duration, d.get("audio", "p")
        ))
    except (ValueError, RuntimeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/decode", methods=["POST"])
def api_decode():
    d         = request.json or {}
    event     = d.get("event", "").upper()
    locations = d.get("locations", "")
    duration  = d.get("duration", "01")
    if not event or not locations:
        return jsonify({"ok": False, "error": "event and locations required"}), 400
    loc_keys  = load_location_keys()
    fips_list = []
    for k in parse_location_keys(locations):
        if k in loc_keys:
            fips_list.extend(loc_keys[k]["fips"])
    if not fips_list:
        return jsonify({"ok": False, "error": f"No FIPS codes for keys {locations!r}"}), 400
    cfg  = tft.config if tft is not None else {}
    same = build_same_header(event, fips_list, duration,
                             org=cfg.get("org", "EAS"),
                             callsign=cfg.get("callsign", "STATION"))
    try:
        text = decode_header(same, cfg.get("tz_offset"))
        return jsonify({"ok": True, "text": text, "same": same})
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/location_keys", methods=["GET"])
def api_location_keys():
    return jsonify(load_location_keys())

@app.route("/api/fips/search")
def api_fips_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    return jsonify(search_fips(q, limit=8))

# kept for backwards compat with existing JS
@app.route("/api/control/play_announcement", methods=["POST"])
def api_play_announcement():
    return _tft_call(lambda: tft.play_announcement())


# ── routes — config ────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_cfg_get():
    return jsonify(_read_config_dict())

@app.route("/api/config", methods=["POST"])
def api_cfg_post():
    data = request.json or {}
    c = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        c.read(CONFIG_PATH)
    for section, keys in data.items():
        if not c.has_section(section):
            c.add_section(section)
        if section == "location_keys":
            for opt in list(c.options(section)):
                c.remove_option(section, opt)
        for k, v in keys.items():
            if v or section != "location_keys":
                c.set(section, k, str(v))
    try:
        with open(CONFIG_PATH, 'w') as f:
            c.write(f)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── websocket handlers ─────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    pass

@socketio.on("disconnect")
def on_disconnect():
    """Clean up PTT and VoIP recording if browser disconnects mid-transmission."""
    global _ptt_proc, _rec_proc
    with _ptt_lk:
        if _ptt_proc:
            try: _ptt_proc.stdin.close()
            except: pass
            _ptt_proc = None
    with _rec_lk:
        if _rec_proc:
            try: _rec_proc.stdin.close()
            except: pass
            _rec_proc = None
    if tft_ok():
        try:
            with _tft_lk: tft.stop()
        except: pass

@socketio.on("ptt_start")
def on_ptt_start():
    global _ptt_proc
    if not tft_ok():
        socketio.emit('ptt_error', {'error': 'COM3 not connected'})
        return
    try:
        with _tft_lk:
            tft.live_patch()
    except Exception as e:
        socketio.emit('ptt_error', {'error': str(e)})
        return
    with _ptt_lk:
        try:
            _ptt_proc = subprocess.Popen(
                ['aplay', '-r', '44100', '-f', 'S16_LE', '-c', '1', '-'],
                stdin=subprocess.PIPE
            )
        except FileNotFoundError:
            socketio.emit('ptt_error', {'error': 'aplay not found — install alsa-utils'})
        except Exception as e:
            socketio.emit('ptt_error', {'error': str(e)})

@socketio.on("ptt_chunk")
def on_ptt_chunk(samples):
    """Receive Int16 PCM samples from browser, write to aplay stdin."""
    with _ptt_lk:
        if _ptt_proc and _ptt_proc.stdin:
            try:
                _ptt_proc.stdin.write(struct.pack(f'{len(samples)}h', *samples))
                _ptt_proc.stdin.flush()
            except Exception:
                pass

@socketio.on("ptt_stop")
def on_ptt_stop():
    global _ptt_proc
    with _ptt_lk:
        if _ptt_proc:
            try: _ptt_proc.stdin.close()
            except: pass
            _ptt_proc = None
    if tft_ok():
        try:
            with _tft_lk: tft.stop()
        except: pass


# ── VoIP announcement recording ────────────────────────────────────────────

_rec_lk   = threading.Lock()
_rec_proc = None   # aplay subprocess while browser is recording announcement

@socketio.on("rec_start")
def on_rec_start():
    global _rec_proc
    if not tft_ok():
        socketio.emit('rec_error', {'error': 'COM3 not connected'})
        return
    try:
        if tft is None:
            socketio.emit('rec_error', {'error': 'COM3 not connected'})
            return
        with _tft_lk:
            tft.record_announcement()
    except Exception as e:
        socketio.emit('rec_error', {'error': str(e)})
        return
    with _rec_lk:
        try:
            _rec_proc = subprocess.Popen(
                ['aplay', '-r', '44100', '-f', 'S16_LE', '-c', '1', '-'],
                stdin=subprocess.PIPE
            )
            socketio.emit('rec_ready')
        except FileNotFoundError:
            socketio.emit('rec_error', {'error': 'aplay not found — install alsa-utils'})
        except Exception as e:
            socketio.emit('rec_error', {'error': str(e)})

@socketio.on("rec_chunk")
def on_rec_chunk(samples):
    """Receive Int16 PCM samples from browser, pipe to TFT CH1 via aplay."""
    with _rec_lk:
        if _rec_proc and _rec_proc.stdin:
            try:
                _rec_proc.stdin.write(struct.pack(f'{len(samples)}h', *samples))
                _rec_proc.stdin.flush()
            except Exception:
                pass

@socketio.on("rec_stop")
def on_rec_stop():
    global _rec_proc
    with _rec_lk:
        if _rec_proc:
            try: _rec_proc.stdin.close()
            except: pass
            _rec_proc = None
    if tft_ok():
        try:
            with _tft_lk: tft.stop()
        except: pass
    socketio.emit('rec_done')


# ── HTML template ──────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
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
    --bg: #0d0d0f; --surface: #141416; --surface2: #1a1a1d;
    --border: #2a2a2e; --border2: #3a3a3f;
    --text: #e8e8ea; --muted: #6b6b70;
    --accent: #4a9eff; --warn: #f0a500; --danger: #e24b4a; --success: #4caf6e;
    --mono: 'IBM Plex Mono', monospace; --sans: 'IBM Plex Sans', sans-serif;
  }
  body { background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; line-height: 1.6; }

  /* ── topbar ── */
  .topbar { border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; background: var(--bg); z-index: 10; }
  .topbar-title { font-family: var(--mono); font-size: 13px; font-weight: 500; letter-spacing: 0.05em; }
  .topbar-sub { font-size: 11px; color: var(--muted); font-family: var(--mono); margin-top: 1px; }
  .status-pill { display: flex; align-items: center; gap: 6px; font-size: 11px; font-family: var(--mono); background: var(--surface2); border: 1px solid var(--border); border-radius: 20px; padding: 4px 10px; }
  .dot { width: 6px; height: 6px; border-radius: 50%; }
  .dot-green { background: var(--success); box-shadow: 0 0 6px var(--success); }
  .dot-red   { background: var(--danger); }
  .dot-warn  { background: var(--warn); }

  /* ── nav ── */
  .nav { display: flex; border-bottom: 1px solid var(--border); padding: 0 24px; overflow-x: auto; }
  .nav-item { font-size: 12px; font-family: var(--mono); padding: 10px 14px; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; white-space: nowrap; transition: color 0.15s; }
  .nav-item:hover { color: var(--text); }
  .nav-item.active { color: var(--accent); border-bottom-color: var(--accent); }

  /* ── layout ── */
  .main { padding: 20px 24px; max-width: 1200px; }
  .page { display: none; }
  .page.active { display: block; }

  /* ── stats ── */
  .stats { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 10px; margin-bottom: 20px; }
  @media (max-width: 700px) { .stats { grid-template-columns: repeat(2,1fr); } }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
  .stat-label { font-size: 10px; font-family: var(--mono); color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 6px; }
  .stat-value { font-size: 22px; font-family: var(--mono); font-weight: 500; }
  .stat-value.sm { font-size: 13px; padding-top: 4px; }

  /* ── panel + sidebar ── */
  .layout { display: grid; grid-template-columns: 1fr 260px; gap: 16px; }
  @media (max-width: 800px) { .layout { grid-template-columns: 1fr; } }
  .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .panel-header { padding: 12px 16px; border-bottom: 1px solid var(--border); font-size: 11px; font-family: var(--mono); color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; display: flex; align-items: center; justify-content: space-between; }
  .sidebar { display: flex; flex-direction: column; gap: 12px; }
  .status-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
  .status-row:last-child { border-bottom: none; }
  .status-key { color: var(--muted); font-family: var(--mono); font-size: 11px; }
  .status-val { font-family: var(--mono); font-size: 11px; }
  .ok   { color: var(--success); }
  .err  { color: var(--danger); }
  .warn { color: var(--warn); }

  /* ── alert items ── */
  .alert-item { padding: 14px 16px; border-bottom: 1px solid var(--border); display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: start; animation: slideIn 0.3s ease; }
  .alert-item:last-child { border-bottom: none; }
  @keyframes slideIn { from { opacity:0; transform:translateY(-6px); } to { opacity:1; transform:translateY(0); } }
  .alert-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
  .badge { font-size: 10px; font-family: var(--mono); font-weight: 500; padding: 2px 8px; border-radius: 4px; letter-spacing: 0.04em; }
  .badge-danger  { background: rgba(226,75,74,.15);  color: #f07877; border: 1px solid rgba(226,75,74,.3); }
  .badge-warn    { background: rgba(240,165,0,.15);  color: #f5c04a; border: 1px solid rgba(240,165,0,.3); }
  .badge-success { background: rgba(76,175,110,.15); color: #6dcf8e; border: 1px solid rgba(76,175,110,.3); }
  .badge-info    { background: rgba(74,158,255,.15); color: #7ab8ff; border: 1px solid rgba(74,158,255,.3); }
  .event-code { font-family: var(--mono); font-size: 10px; color: var(--muted); }
  .alert-locations { font-size: 12px; color: var(--text); margin-bottom: 3px; }
  .alert-meta { font-size: 11px; color: var(--muted); font-family: var(--mono); }
  .countdown-col { text-align: right; min-width: 90px; }
  .countdown-badge { font-size: 10px; font-family: var(--mono); padding: 3px 8px; border-radius: 4px; display: inline-block; margin-bottom: 4px; }
  .cd-active     { background: rgba(76,175,110,.12); color: #6dcf8e; border: 1px solid rgba(76,175,110,.25); }
  .cd-expired    { background: rgba(107,107,112,.15); color: var(--muted); border: 1px solid var(--border); }
  .cd-indefinite { background: rgba(74,158,255,.12); color: #7ab8ff; border: 1px solid rgba(74,158,255,.25); }
  .countdown-time { font-size: 11px; font-family: var(--mono); color: var(--muted); }
  .empty { padding: 32px 16px; text-align: center; color: var(--muted); font-family: var(--mono); font-size: 12px; }
  .live-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--success); display: inline-block; margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

  /* ── action buttons (sidebar) ── */
  .action-btn { width: 100%; text-align: left; padding: 8px 12px; background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 12px; font-family: var(--mono); cursor: pointer; margin-bottom: 6px; transition: border-color .15s, background .15s; }
  .action-btn:last-child { margin-bottom: 0; }
  .action-btn:hover:not(:disabled) { border-color: var(--border2); background: #202025; }
  .action-btn:disabled { opacity: .4; cursor: not-allowed; }
  .action-btn.danger:hover:not(:disabled) { border-color: var(--danger); color: #f07877; }

  /* ── search ── */
  .search-input { background: var(--surface2); border: 1px solid var(--border); border-radius: 4px; color: var(--text); font-family: var(--mono); font-size: 11px; padding: 3px 8px; width: 200px; }
  .search-input:focus { outline: none; border-color: var(--accent); }

  /* ── control / originate ── */
  .control-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
  .control-section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .control-section h3 { font-family: var(--mono); font-size: 11px; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 12px; }
  .tts-input { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-family: var(--mono); font-size: 12px; padding: 8px 10px; resize: vertical; min-height: 60px; margin-bottom: 8px; }
  .tts-input:focus { outline: none; border-color: var(--accent); }
  .originate-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px; }
  .originate-input { background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-family: var(--mono); font-size: 12px; padding: 6px 10px; width: 100%; }
  .originate-input:focus { outline: none; border-color: var(--accent); }
  select.originate-input option { background: var(--surface2); }

  /* ── TFT panel ── */
  .panel-page-grid { display: grid; grid-template-columns: 1fr 320px; gap: 20px; }
  @media (max-width: 900px) { .panel-page-grid { grid-template-columns: 1fr; } }
  .panel-sep { font-size: 10px; font-family: var(--mono); color: var(--muted); letter-spacing: 0.1em; text-transform: uppercase; padding: 14px 0 8px; border-top: 1px solid var(--border); margin-top: 10px; }
  .panel-sep:first-child { border-top: none; padding-top: 0; }
  .btn-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px,1fr)); gap: 8px; }
  .panel-btn { padding: 14px 10px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-family: var(--mono); font-size: 11px; cursor: pointer; text-align: center; transition: all .12s; display: flex; flex-direction: column; align-items: center; gap: 5px; }
  .panel-btn .icon { font-size: 18px; line-height: 1; }
  .panel-btn .lbl  { font-size: 10px; color: var(--muted); letter-spacing: .05em; }
  .panel-btn:hover:not(:disabled) { border-color: var(--border2); background: #1e1e22; }
  .panel-btn:active:not(:disabled) { transform: scale(.96); }
  .panel-btn.w:hover:not(:disabled) { border-color: var(--warn); color: var(--warn); }
  .panel-btn.d:hover:not(:disabled) { border-color: var(--danger); color: #f07877; }
  .panel-btn.s:hover:not(:disabled) { border-color: var(--success); color: #6dcf8e; }
  .panel-btn:disabled { opacity: .3; cursor: not-allowed; }

  /* ── PTT ── */
  .ptt-wrap { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 8px 0; }
  .ptt-btn { width: 160px; height: 160px; border-radius: 50%; background: var(--surface2); border: 3px solid var(--border); color: var(--muted); font-family: var(--mono); font-size: 13px; cursor: pointer; user-select: none; transition: all .1s; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; }
  .ptt-btn .ptt-icon { font-size: 32px; }
  .ptt-btn:hover { border-color: var(--border2); color: var(--text); }
  .ptt-btn.active { border-color: var(--danger); background: rgba(226,75,74,.12); color: #f07877; box-shadow: 0 0 24px rgba(226,75,74,.25); }
  .ptt-status { font-size: 11px; font-family: var(--mono); color: var(--muted); }

  /* ── log viewer ── */
  .log-box { background: #0a0a0c; border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-family: var(--mono); font-size: 11px; color: #8a8a90; height: 450px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
  .log-line-info    { color: #8a8a90; }
  .log-line-warning { color: #f5c04a; }
  .log-line-error   { color: #f07877; }

  /* ── config editor ── */
  .cfg-section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 12px; }
  .cfg-section h3 { font-family: var(--mono); font-size: 10px; color: var(--accent); letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 14px; }
  .cfg-row { display: grid; grid-template-columns: 200px 1fr; gap: 10px; align-items: center; margin-bottom: 8px; }
  .cfg-key { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .cfg-val { background: var(--surface2); border: 1px solid var(--border); border-radius: 4px; color: var(--text); font-family: var(--mono); font-size: 12px; padding: 5px 8px; width: 100%; }
  .cfg-val:focus { outline: none; border-color: var(--accent); }
  .cfg-save { background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 10px 24px; font-family: var(--mono); font-size: 12px; cursor: pointer; margin-top: 8px; }
  .cfg-save:hover { opacity: .85; }

  /* ── settings page ── */
  .settings-section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 12px; }
  .settings-section h3 { font-family: var(--mono); font-size: 10px; color: var(--accent); letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 14px; }
  .settings-grid { display: grid; grid-template-columns: 180px 1fr; gap: 8px 12px; align-items: center; }
  .settings-label { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .settings-hint { font-size: 10px; color: var(--muted); font-family: var(--mono); margin-top: 2px; grid-column: 2; }
  .lk-row { display: grid; grid-template-columns: 26px 150px 1fr; gap: 8px; align-items: start; padding: 8px 0; border-bottom: 1px solid var(--border); }
  .lk-row:last-child { border-bottom: none; }
  .lk-key-num { font-family: var(--mono); font-size: 11px; color: var(--muted); padding-top: 7px; }
  .lk-fips-area { display: flex; flex-direction: column; gap: 4px; }
  .lk-chips { display: flex; flex-wrap: wrap; gap: 4px; min-height: 24px; }
  .lk-fips-chip { display: inline-flex; align-items: center; gap: 2px; background: var(--surface2); border: 1px solid var(--border2); border-radius: 4px; font-family: var(--mono); font-size: 10px; padding: 2px 6px; color: var(--text); }
  .lk-chip-rm { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 13px; padding: 0 0 0 2px; line-height: 1; }
  .lk-chip-rm:hover { color: var(--danger); }
  .lk-search-wrap { position: relative; }
  .lk-search-results { position: absolute; top: 100%; left: 0; right: 0; background: var(--surface); border: 1px solid var(--border2); border-radius: 4px; z-index: 20; max-height: 180px; overflow-y: auto; box-shadow: 0 4px 12px rgba(0,0,0,.4); }
  .lk-search-result { padding: 6px 10px; font-family: var(--mono); font-size: 11px; cursor: pointer; border-bottom: 1px solid var(--border); }
  .lk-search-result:last-child { border-bottom: none; }
  .lk-search-result:hover { background: var(--surface2); }

  /* ── toast ── */
  .toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; font-family: var(--mono); font-size: 12px; z-index: 100; opacity: 0; transition: opacity .3s; pointer-events: none; }
  .toast.show { opacity: 1; }
  .toast.ok   { border-color: var(--success); color: #6dcf8e; }
  .toast.fail { border-color: var(--danger);  color: #f07877; }
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
  <div class="nav-item active" onclick="showPage('dashboard',this)">Dashboard</div>
  <div class="nav-item" onclick="showPage('history',this)">History</div>
  <div class="nav-item" onclick="showPage('panel',this); refreshPanelStatus()">Panel</div>
  <div class="nav-item" onclick="showPage('control',this)">Control</div>
  <div class="nav-item" onclick="showPage('logs',this)">Logs</div>
  <div class="nav-item" onclick="showPage('config',this); loadSettings()">Settings</div>
</div>

<div class="main">

<!-- ═══════════════════════════════ DASHBOARD ═══════════════════════════════ -->
<div id="page-dashboard" class="page active">
  <div class="stats">
    <div class="stat-card"><div class="stat-label">Alerts today</div><div class="stat-value" id="stat-today">{{ stats.today_count }}</div></div>
    <div class="stat-card"><div class="stat-label">Last alert</div><div class="stat-value sm" id="stat-last">{{ stats.last_alert }}</div></div>
    <div class="stat-card"><div class="stat-label">Total logged</div><div class="stat-value" id="stat-total">{{ stats.total }}</div></div>
    <div class="stat-card"><div class="stat-label">Last RWT</div><div class="stat-value sm" id="stat-rwt">{{ stats.last_rwt }}</div></div>
  </div>
  <div class="layout">
    <div class="panel">
      <div class="panel-header">
        <span><span class="live-dot"></span>live feed</span>
        <span id="feed-count">{{ alerts|length }} alerts</span>
      </div>
      <div id="alert-feed">
        {% if alerts %}
          {% for alert in alerts %}
          <div class="alert-item" data-expires="{{ alert.expires_utc or '' }}">
            <div>
              <div class="alert-top">{{ badge(alert) }}<span class="event-code">{{ alert.event_code or '???' }} · {{ alert.originator_code or '???' }}</span></div>
              <div class="alert-locations">{{ alert.locations_pretty[:3]|join(', ') if alert.locations_pretty else 'Unknown location' }}{% if alert.locations_pretty and alert.locations_pretty|length > 3 %} +{{ alert.locations_pretty|length - 3 }} more{% endif %}</div>
              <div class="alert-meta">{{ alert.received_local or '' }} · {{ alert.sender or '' }}</div>
            </div>
            <div class="countdown-col"><div class="countdown-badge cd-active">active</div><div class="countdown-time">—</div></div>
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
        <div style="padding:4px 16px">
          <div class="status-row"><span class="status-key">logger</span><span class="status-val {{ 'ok' if stats.logger_ok else 'err' }}">{{ 'running' if stats.logger_ok else 'stopped' }}</span></div>
          <div class="status-row"><span class="status-key">serial J103</span><span class="status-val {{ 'ok' if stats.serial_ok else 'err' }}">{{ 'connected' if stats.serial_ok else 'disconnected' }}</span></div>
          <div class="status-row"><span class="status-key">com3 control</span><span class="status-val {{ 'ok' if stats.control_ok else 'warn' }}">{{ 'connected' if stats.control_ok else 'not connected' }}</span></div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-header">quick actions</div>
        <div style="padding:12px">
          <button class="action-btn" onclick="sendRWT(true)">send weekly test</button>
          <button class="action-btn" onclick="sendEOM()">send EOM</button>
          <button class="action-btn" onclick="downloadLog()">download alert log</button>
          <button class="action-btn danger" onclick="confirmReboot()">reboot TFT unit</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════ HISTORY ═════════════════════════════════ -->
<div id="page-history" class="page">
  <div class="panel">
    <div class="panel-header">
      <span>alert history</span>
      <input type="text" class="search-input" id="search" placeholder="filter..." oninput="filterHistory()">
    </div>
    <div id="history-feed">
      {% if alerts %}
        {% for alert in alerts %}
        <div class="alert-item history-item" data-text="{{ (alert.event_code or '') + ' ' + (alert.locations_pretty|join(' ') if alert.locations_pretty else '') }}">
          <div>
            <div class="alert-top">{{ badge(alert) }}<span class="event-code">{{ alert.event_code or '???' }} · {{ alert.originator_code or '???' }}</span></div>
            <div class="alert-locations">{{ alert.locations_pretty[:3]|join(', ') if alert.locations_pretty else 'Unknown location' }}</div>
            <div class="alert-meta">{{ alert.received_local or '' }} · {{ alert.sender or '' }}</div>
            <div class="alert-meta" style="margin-top:2px;font-size:10px;color:#444">{{ alert.canonical_header or '' }}</div>
          </div>
          <div class="countdown-col"><div class="countdown-badge cd-expired">expired</div></div>
        </div>
        {% endfor %}
      {% else %}
        <div class="empty">no alerts logged yet</div>
      {% endif %}
    </div>
  </div>
</div>

<!-- ═══════════════════════════════ PANEL ═══════════════════════════════════ -->
<div id="page-panel" class="page">
  <div class="panel-page-grid">
    <div>
      <div class="panel-sep">Audio</div>
      <div class="btn-grid">
        <button class="panel-btn" onclick="panelCall('/api/control/voice/record','Recording voice…')"><span class="icon">🎙</span><span>Record Voice</span><span class="lbl">09# · Mon1 → unit</span></button>
        <button class="panel-btn" onclick="panelCall('/api/control/voice/play','Playing voice…')"><span class="icon">▶</span><span>Play Voice</span><span class="lbl">11#</span></button>
        <button class="panel-btn" onclick="panelCall('/api/control/announcement/record','Recording announcement…')"><span class="icon">📢</span><span>Rec Announcement</span><span class="lbl">21# · Mon1 → unit</span></button>
        <button class="panel-btn" onclick="panelCall('/api/control/announcement/play','Playing announcement…')"><span class="icon">▶</span><span>Play Announcement</span><span class="lbl">22#</span></button>
        <button class="panel-btn" onclick="panelCall('/api/control/patch','Live patch active…')"><span class="icon">🔗</span><span>Live Patch</span><span class="lbl">20# · Mon1 → main out</span></button>
        <button class="panel-btn w" onclick="panelCall('/api/control/stop','Stop sent.')"><span class="icon">⏹</span><span>Stop</span><span class="lbl"># · end operation</span></button>
      </div>

      <div class="panel-sep">Alerts</div>
      <div class="btn-grid">
        <button class="panel-btn s" onclick="sendRWT(true)"><span class="icon">📡</span><span>RWT + Tone</span><span class="lbl">31# · weekly test</span></button>
        <button class="panel-btn s" onclick="sendRWT(false)"><span class="icon">📡</span><span>RWT No Tone</span><span class="lbl">30# · weekly test</span></button>
        <button class="panel-btn" onclick="sendEOM()"><span class="icon">⏺</span><span>Send EOM</span><span class="lbl">43# · end of message</span></button>
      </div>

      <div class="panel-sep">Originate Alert</div>
      <div class="control-section" style="border:none;padding:0;background:none">
        <div class="originate-row">
          <input class="originate-input" id="p-orig-event" placeholder="Event code (e.g. DMO, TOR)">
          <select class="originate-input" id="p-orig-dur">
            <option value="01">15 minutes</option>
            <option value="02">30 minutes</option>
            <option value="03">45 minutes</option>
            <option value="04">1 hour</option>
            <option value="06">1.5 hours</option>
            <option value="08">2 hours</option>
          </select>
        </div>
        <div style="margin-bottom:8px">
          <div style="font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:4px">Location keys:</div>
          <div id="p-orig-locs-checks" style="display:flex;flex-wrap:wrap;gap:6px">
            <span style="font-size:11px;color:var(--muted);font-family:var(--mono)">loading…</span>
          </div>
          <input class="originate-input" id="p-orig-locs" placeholder="Or type manually (e.g. 1,3)" style="margin-top:6px">
        </div>
        <select class="originate-input" id="p-orig-audio" style="margin-bottom:8px">
          <option value="p">Pre-recorded audio</option>
          <option value="n">No audio</option>
          <option value="l">Live audio</option>
        </select>
        <button class="action-btn" onclick="panelOriginate()">originate alert</button>
      </div>

      <div class="panel-sep">System</div>
      <div class="btn-grid">
        <button class="panel-btn d" onclick="confirmReboot()"><span class="icon">🔄</span><span>Reboot Unit</span><span class="lbl">91#</span></button>
        <button class="panel-btn" onclick="reconnectCOM3()"><span class="icon">🔌</span><span>Reconnect COM3</span><span class="lbl">re-open serial</span></button>
      </div>
    </div>

    <div class="sidebar">
      <div class="panel">
        <div class="panel-header">com3 status</div>
        <div style="padding:4px 16px">
          <div class="status-row"><span class="status-key">connection</span><span class="status-val" id="panel-com3-status">checking…</span></div>
          <div class="status-row"><span class="status-key">logger</span><span class="status-val {{ 'ok' if stats.logger_ok else 'err' }}">{{ 'running' if stats.logger_ok else 'stopped' }}</span></div>
          <div class="status-row"><span class="status-key">serial J103</span><span class="status-val {{ 'ok' if stats.serial_ok else 'err' }}">{{ 'connected' if stats.serial_ok else 'disconnected' }}</span></div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-header">event codes</div>
        <div style="padding:10px 16px;font-family:var(--mono);font-size:10px;color:var(--muted);line-height:1.8">
          ADR AVA AVW BZW CAE CDW CEM CFA CFW DSW EQW EVI FRW FFA FFW FFS FLA FLS FLW HMW HWA HWW HUA HUW HLS LEW LAE NMN TOE NUW DMO RHW RMT <span style="color:var(--success)">RWT</span> SVA SVR SVS SPW SMW SPS TOA <span style="color:var(--danger)">TOR</span> TRA TRW TSA TSW VOA VOW WSA WSW
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════ CONTROL ══════════════════════════════════ -->
<div id="page-control" class="page">

  <!-- ── Originate Alert (full-width) ── -->
  <div class="control-section" style="margin-bottom:16px">
    <h3>Originate Alert</h3>
    <div class="originate-row" style="margin-bottom:8px">
      <select class="originate-input" id="orig-event">
        <option value="">— select event —</option>
        <optgroup label="Tornado / Severe">
          <option value="TOR">TOR — Tornado Warning</option>
          <option value="TOA">TOA — Tornado Watch</option>
          <option value="TRW">TRW — Tropical Storm Warning</option>
          <option value="TRA">TRA — Tropical Storm Watch</option>
          <option value="SVR">SVR — Severe Thunderstorm Warning</option>
          <option value="SVA">SVA — Severe Thunderstorm Watch</option>
          <option value="SVS">SVS — Severe Weather Statement</option>
          <option value="SPS">SPS — Special Weather Statement</option>
        </optgroup>
        <optgroup label="Flood">
          <option value="FFW">FFW — Flash Flood Warning</option>
          <option value="FFA">FFA — Flash Flood Watch</option>
          <option value="FFS">FFS — Flash Flood Statement</option>
          <option value="FLW">FLW — Flood Warning</option>
          <option value="FLA">FLA — Flood Watch</option>
          <option value="FLS">FLS — Flood Statement</option>
          <option value="CFA">CFA — Coastal Flood Watch</option>
          <option value="CFW">CFW — Coastal Flood Warning</option>
        </optgroup>
        <optgroup label="Winter / Wind">
          <option value="WSW">WSW — Winter Storm Warning</option>
          <option value="WSA">WSA — Winter Storm Watch</option>
          <option value="BZW">BZW — Blizzard Warning</option>
          <option value="HWW">HWW — High Wind Warning</option>
          <option value="HWA">HWA — High Wind Watch</option>
          <option value="DSW">DSW — Dust Storm Warning</option>
          <option value="SMW">SMW — Special Marine Warning</option>
          <option value="SPW">SPW — Shelter In Place Warning</option>
        </optgroup>
        <optgroup label="Hurricane">
          <option value="HUW">HUW — Hurricane Warning</option>
          <option value="HUA">HUA — Hurricane Watch</option>
          <option value="HLS">HLS — Hurricane Statement</option>
        </optgroup>
        <optgroup label="Fire / Hazmat">
          <option value="FRW">FRW — Fire Warning</option>
          <option value="HMW">HMW — Hazardous Materials Warning</option>
          <option value="RHW">RHW — Radiological Hazard Warning</option>
          <option value="CDW">CDW — Civil Danger Warning</option>
          <option value="NUW">NUW — Nuclear Power Plant Warning</option>
        </optgroup>
        <optgroup label="Tsunami / Seismic / Volcano">
          <option value="TSW">TSW — Tsunami Warning</option>
          <option value="TSA">TSA — Tsunami Watch</option>
          <option value="EQW">EQW — Earthquake Warning</option>
          <option value="VOW">VOW — Volcano Warning</option>
          <option value="VOA">VOA — Volcano Watch</option>
        </optgroup>
        <optgroup label="Avalanche">
          <option value="AVW">AVW — Avalanche Warning</option>
          <option value="AVA">AVA — Avalanche Watch</option>
        </optgroup>
        <optgroup label="Civil / Emergency">
          <option value="CEM">CEM — Civil Emergency Message</option>
          <option value="CAE">CAE — Child Abduction Emergency</option>
          <option value="LEW">LEW — Law Enforcement Warning</option>
          <option value="EVI">EVI — Evacuation Immediate</option>
          <option value="ADR">ADR — Administrative Message</option>
          <option value="LAE">LAE — Local Area Emergency</option>
          <option value="TOE">TOE — 911 Telephone Outage Emergency</option>
          <option value="NMN">NMN — Network Message Notification</option>
        </optgroup>
        <optgroup label="Test">
          <option value="RWT">RWT — Required Weekly Test</option>
          <option value="RMT">RMT — Required Monthly Test</option>
          <option value="DMO">DMO — Practice / Demo Warning</option>
        </optgroup>
      </select>
      <select class="originate-input" id="orig-dur">
        <option value="01">15 minutes</option>
        <option value="02">30 minutes</option>
        <option value="03">45 minutes</option>
        <option value="04" selected>1 hour</option>
        <option value="06">1.5 hours</option>
        <option value="08">2 hours</option>
      </select>
    </div>
    <div style="margin-bottom:8px">
      <div style="font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:4px">Location keys — select one or more:</div>
      <div id="orig-locs-checks" style="display:flex;flex-wrap:wrap;gap:6px">
        <span style="font-size:11px;color:var(--muted);font-family:var(--mono)">loading…</span>
      </div>
      <input class="originate-input" id="orig-locs" placeholder="Or enter keys manually (e.g. 1,3)" style="margin-top:6px">
    </div>
    <select class="originate-input" id="orig-audio" style="margin-bottom:8px;max-width:320px" onchange="onOrigAudioChange()">
      <option value="tts">Auto TTS announcement</option>
      <option value="voip">Record via browser mic</option>
      <option value="p">Pre-recorded (on TFT)</option>
      <option value="n">No audio</option>
      <option value="l">Live audio</option>
    </select>
    <div id="orig-tts-panel" style="margin-bottom:8px">
      <button class="action-btn" style="width:auto;padding:4px 12px;margin-bottom:6px;font-size:11px" onclick="previewAnnouncement()">preview text</button>
      <div id="orig-preview-text" style="display:none;font-size:11px;font-family:var(--mono);color:var(--muted);padding:8px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:4px"></div>
    </div>
    <div id="orig-voip-panel" style="display:none;margin-bottom:8px">
      <div style="font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:6px">Click to start/stop recording into TFT CH1:</div>
      <div style="display:flex;align-items:center;gap:10px">
        <button class="action-btn" id="orig-rec-btn" style="width:auto;padding:6px 18px;margin:0" onclick="toggleVoipRec()">⏺ Start Recording</button>
        <span id="orig-rec-status" style="font-size:11px;font-family:var(--mono);color:var(--muted)">Idle</span>
      </div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="action-btn" onclick="originateAlert()">originate alert</button>
      <button class="action-btn" onclick="sendEOM()">send EOM</button>
    </div>
  </div>

  <!-- ── bottom 3-col row ── -->
  <div class="control-grid">

    <div class="control-section">
      <h3>TTS Announcement</h3>
      <textarea class="tts-input" id="tts-text" placeholder="Type announcement text — hit Record to generate and store it on the TFT unit via espeak…"></textarea>
      <button class="action-btn" onclick="recordAnnouncement()">record announcement (TTS)</button>
      <button class="action-btn" onclick="panelCall('/api/control/announcement/play','Playing announcement…')">play announcement</button>
      <button class="action-btn" onclick="panelCall('/api/control/stop','Stop sent.')">stop</button>
    </div>

    <div class="control-section" style="display:flex;flex-direction:column;align-items:center;justify-content:center">
      <h3 style="width:100%">Live PTT</h3>
      <p style="font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:16px;width:100%">Hold to transmit mic audio into CH1.</p>
      <div class="ptt-wrap">
        <button class="ptt-btn" id="ptt-btn"
          onmousedown="startPTT()" onmouseup="stopPTT()" onmouseleave="stopPTT()"
          ontouchstart="startPTT()" ontouchend="stopPTT()">
          <span class="ptt-icon">🎤</span>
          <span>PTT</span>
        </button>
        <div class="ptt-status" id="ptt-status">Idle — hold to talk</div>
      </div>
    </div>

    <div class="control-section">
      <h3>Unit Control</h3>
      <div id="control-status" style="font-size:11px;font-family:var(--mono);color:var(--muted);margin-bottom:12px">checking…</div>
      <button class="action-btn" onclick="reconnectCOM3()">reconnect COM3</button>
      <button class="action-btn danger" onclick="confirmReboot()">reboot TFT unit</button>
    </div>

  </div>
</div>

<!-- ═══════════════════════════════ LOGS ════════════════════════════════════ -->
<div id="page-logs" class="page">
  <div class="panel">
    <div class="panel-header">
      <span>logger output — live stream</span>
      <button class="action-btn" style="width:auto;margin:0;padding:4px 10px" onclick="clearLogs()">clear</button>
    </div>
    <div class="log-box" id="log-box"><span class="log-line-info">Waiting for log lines…</span></div>
  </div>
</div>

<!-- ═══════════════════════════════ SETTINGS ════════════════════════════════ -->
<div id="page-config" class="page">
  <div id="settings-loading" style="font-family:var(--mono);font-size:12px;color:var(--muted);padding:20px 0">Loading settings…</div>
  <div id="settings-content" style="display:none">

    <div class="settings-section">
      <h3>Station Identity</h3>
      <div class="settings-grid">
        <span class="settings-label">Callsign</span>
        <input id="s-callsign" class="cfg-val" maxlength="8" placeholder="WBXX" style="width:120px">
        <span class="settings-label">Home FIPS</span>
        <input id="s-fips" class="cfg-val" maxlength="6" placeholder="036109" style="width:120px">
        <span class="settings-label">Originator</span>
        <select id="s-org" class="cfg-val" style="width:220px">
          <option value="EAS">EAS — local station</option>
          <option value="WXR">WXR — NWS</option>
          <option value="CIV">CIV — civil authority</option>
          <option value="PEP">PEP — primary entry point</option>
        </select>
        <span class="settings-label">UTC offset</span>
        <input id="s-tz" class="cfg-val" type="number" min="-12" max="14" placeholder="-5" style="width:80px">
      </div>
    </div>

    <div class="settings-section">
      <h3>COM3 Interface (J303)</h3>
      <div class="settings-grid">
        <span class="settings-label">Serial port</span>
        <input id="s-ctrl-port" class="cfg-val" placeholder="/dev/tft911-cmd">
        <span class="settings-label">Baud rate</span>
        <input id="s-ctrl-baud" class="cfg-val" type="number" placeholder="9600" style="width:100px">
        <span class="settings-label">PIN</span>
        <input id="s-ctrl-pin" class="cfg-val" placeholder="Menu 19 PIN" style="width:130px">
      </div>
    </div>

    <div class="settings-section">
      <h3>Logger Serial (J103)</h3>
      <div class="settings-grid">
        <span class="settings-label">Serial port</span>
        <input id="s-ser-port" class="cfg-val" placeholder="/dev/ttyUSB0">
        <span class="settings-label">Baud rate</span>
        <input id="s-ser-baud" class="cfg-val" type="number" placeholder="1200" style="width:100px">
      </div>
    </div>

    <div class="settings-section">
      <h3>Location Keys</h3>
      <div style="font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:12px">14 TFT encoder location keys. Search by county name to add FIPS codes to each key.</div>
      <div id="lk-rows"></div>
    </div>

    <div class="settings-section">
      <h3>Push Notifications</h3>
      <div class="settings-grid">
        <span class="settings-label">ntfy.sh topic</span>
        <input id="s-ntfy" class="cfg-val" placeholder="my_eas_alerts">
        <span class="settings-hint">Leave blank to disable — alerts post to ntfy.sh/&lt;topic&gt;</span>
      </div>
    </div>

    <div class="settings-section">
      <h3>Web Dashboard</h3>
      <div class="settings-grid">
        <span class="settings-label">Host</span>
        <input id="s-web-host" class="cfg-val" placeholder="0.0.0.0">
        <span class="settings-label">Port</span>
        <input id="s-web-port" class="cfg-val" type="number" placeholder="5000" style="width:100px">
      </div>
    </div>

    <div class="settings-section">
      <h3>Advanced</h3>
      <div class="settings-grid">
        <span class="settings-label">Dedupe window (s)</span>
        <input id="s-dedupe" class="cfg-val" type="number" placeholder="120" style="width:100px">
        <span class="settings-label">Alerts dir</span>
        <input id="s-alerts-dir" class="cfg-val" placeholder="~/eas_logs/alerts">
        <span class="settings-label">Log dir</span>
        <input id="s-log-dir" class="cfg-val" placeholder="~/eas_logs/logs">
      </div>
    </div>

    <div style="margin-top:4px">
      <button class="cfg-save" onclick="saveAllSettings()">Save settings</button>
      <div style="font-size:11px;color:var(--muted);font-family:var(--mono);margin-top:8px">Restart services to apply: <code>sudo systemctl restart tft911-eas tft911-eas-web</code></div>
    </div>
  </div>
</div>

</div><!-- .main -->

<div class="toast" id="toast"></div>

<script>
const socket = io();

// ── badge helpers ──────────────────────────────────────────────────────────
const DANGER_EVENTS  = new Set(['TOR','TOA','HUW','HUA','TSW','TSA','EAN','CEM','CDW','EVI','CAE','LEW','LAE','SPW']);
const WARNING_EVENTS = new Set(['SVR','SVA','HWW','HWA','FFW','FFA','FLW','FLA','WSW','WSA','BZW','SQW','EWW','DSW','SMW']);
const TEST_EVENTS    = new Set(['RWT','RMT','NPT','DMO']);
const BADGE_LABELS   = {
  TOR:'Tornado Warning',TOA:'Tornado Watch',SVR:'Severe Thunderstorm Warning',
  SVA:'Severe Thunderstorm Watch',FFW:'Flash Flood Warning',FFA:'Flash Flood Watch',
  HUW:'Hurricane Warning',HUA:'Hurricane Watch',TSW:'Tsunami Warning',
  EAN:'Emergency Action Notification',CEM:'Civil Emergency Message',
  RWT:'Required Weekly Test',RMT:'Required Monthly Test',NPT:'National Periodic Test',
  DMO:'Practice/Demo',SPS:'Special Weather Statement',FLW:'Flood Warning',
  WSW:'Winter Storm Warning',WSA:'Winter Storm Watch',BZW:'Blizzard Warning',
  CAE:'Child Abduction Emergency',LEW:'Law Enforcement Warning',
  LAE:'Local Area Emergency',SPW:'Shelter in Place Warning',
  EWW:'Extreme Wind Warning',DSW:'Dust Storm Warning',
};
function badgeClass(c) { return DANGER_EVENTS.has(c)?'badge-danger':WARNING_EVENTS.has(c)?'badge-warn':TEST_EVENTS.has(c)?'badge-success':'badge-info'; }
function badgeLabel(c) { return BADGE_LABELS[c] || c || 'Unknown'; }

// ── countdowns ─────────────────────────────────────────────────────────────
function formatCountdown(utc) {
  if (!utc) return {badge:'cd-indefinite',label:'indefinite',time:''};
  const diff = Math.floor((new Date(utc.replace('Z','+00:00')) - new Date()) / 1000);
  if (diff <= 0) return {badge:'cd-expired',label:'expired',time:''};
  const h=Math.floor(diff/3600), m=Math.floor((diff%3600)/60), s=diff%60;
  return {badge:'cd-active',label:'active',time:h?`${h}h ${m}m`:m?`${m}m ${s}s`:`${s}s`};
}
function updateCountdowns() {
  document.querySelectorAll('.alert-item[data-expires]').forEach(el => {
    const cb = el.querySelector('.countdown-badge'), ct = el.querySelector('.countdown-time');
    if (!cb || !ct) return;
    const r = formatCountdown(el.dataset.expires || null);
    cb.className = 'countdown-badge ' + r.badge;
    cb.textContent = r.label;
    ct.textContent = r.time;
  });
}
setInterval(updateCountdowns, 1000);
updateCountdowns();

// ── socket ─────────────────────────────────────────────────────────────────
socket.on('connect', () => {
  document.getElementById('conn-status').innerHTML = '<span class="dot dot-green"></span>live';
});
socket.on('disconnect', () => {
  document.getElementById('conn-status').innerHTML = '<span class="dot dot-red"></span>disconnected';
});
socket.on('new_alert', alert => {
  prependAlert(alert, document.getElementById('alert-feed'));
  updateFeedCount();
  document.getElementById('stat-today').textContent  = +document.getElementById('stat-today').textContent + 1;
  document.getElementById('stat-last').textContent   = alert.received_local || '';
  document.getElementById('stat-total').textContent  = +document.getElementById('stat-total').textContent + 1;
  if (alert.event_code === 'RWT') document.getElementById('stat-rwt').textContent = alert.received_local || '';
});
socket.on('ptt_error', ({error}) => {
  toast('PTT: ' + error, false);
  pttCleanup();
});

// ── log streaming ──────────────────────────────────────────────────────────
socket.on('log_line', ({line}) => {
  const box = document.getElementById('log-box');
  if (!box) return;
  // Clear placeholder text on first real line
  if (box.firstChild && box.firstChild.textContent === 'Waiting for log lines…') box.innerHTML = '';
  const span = document.createElement('span');
  span.className = (line.includes('ERROR')||line.includes('CRIT')) ? 'log-line-error'
                 : line.includes('WARN') ? 'log-line-warning' : 'log-line-info';
  span.textContent = line;
  box.appendChild(span);
  box.appendChild(document.createTextNode('\n'));
  while (box.childNodes.length > 800) box.removeChild(box.firstChild);
  if (document.getElementById('page-logs').classList.contains('active'))
    box.scrollTop = box.scrollHeight;
});

// ── alert rendering ────────────────────────────────────────────────────────
function prependAlert(alert, container) {
  const empty = container.querySelector('.empty');
  if (empty) empty.remove();
  const code = alert.event_code || '???';
  const locs = (alert.locations_pretty||[]).slice(0,3).join(', ') || 'Unknown location';
  const more = (alert.locations_pretty||[]).length > 3 ? ` +${alert.locations_pretty.length-3} more` : '';
  const div  = document.createElement('div');
  div.className = 'alert-item';
  div.dataset.expires = alert.expires_utc || '';
  div.innerHTML = `
    <div>
      <div class="alert-top"><span class="badge ${badgeClass(code)}">${badgeLabel(code)}</span><span class="event-code">${code} · ${alert.originator_code||'???'}</span></div>
      <div class="alert-locations">${locs}${more}</div>
      <div class="alert-meta">${alert.received_local||''} · ${alert.sender||''}</div>
    </div>
    <div class="countdown-col"><div class="countdown-badge cd-active">active</div><div class="countdown-time"></div></div>`;
  container.insertBefore(div, container.firstChild);
  updateCountdowns();
}
function updateFeedCount() {
  document.getElementById('feed-count').textContent = document.querySelectorAll('#alert-feed .alert-item').length + ' alerts';
}

// ── page navigation ────────────────────────────────────────────────────────
function showPage(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  el.classList.add('active');
  if (name === 'control') checkControlStatus();
  if (name === 'logs') document.getElementById('log-box').scrollTop = document.getElementById('log-box').scrollHeight;
}
function filterHistory() {
  const q = document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('.history-item').forEach(el => {
    el.style.display = (el.dataset.text||'').toLowerCase().includes(q) ? '' : 'none';
  });
}
function downloadLog() { window.location.href = '/api/alerts'; }
function clearLogs() { document.getElementById('log-box').innerHTML = ''; }

// ── toast ──────────────────────────────────────────────────────────────────
function toast(msg, ok=true) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast show ${ok?'ok':'fail'}`;
  setTimeout(() => el.className = 'toast', 3500);
}

// ── API helpers ────────────────────────────────────────────────────────────
async function post(url, body={}) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  return r.json();
}
async function panelCall(url, successMsg) {
  const r = await post(url);
  toast(r.ok ? successMsg : r.error, r.ok);
}

// ── control actions ────────────────────────────────────────────────────────
async function sendRWT(tone=true) {
  const r = await post('/api/control/rwt', {tone});
  toast(r.ok ? (tone?'RWT sent with tone':'RWT sent without tone') : r.error, r.ok);
}
async function sendEOM() {
  const r = await post('/api/control/eom');
  toast(r.ok ? 'EOM sent' : r.error, r.ok);
}
async function confirmReboot() {
  if (!confirm('Reboot the TFT unit?')) return;
  const r = await post('/api/control/reboot');
  toast(r.ok ? 'Reboot command sent' : r.error, r.ok);
}
async function reconnectCOM3() {
  toast('Reconnecting COM3…', true);
  const r = await post('/api/control/reconnect');
  toast(r.connected ? 'COM3 reconnected' : 'COM3 still unavailable', r.connected);
  checkControlStatus();
  refreshPanelStatus();
}
async function recordAnnouncement() {
  const text = document.getElementById('tts-text').value.trim();
  if (!text) { toast('Enter announcement text first', false); return; }
  toast('Recording TTS announcement…', true);
  const r = await post('/api/control/announce', {text});
  toast(r.ok ? 'Announcement recorded' : r.error, r.ok);
}
function _getCheckedLocs(checksId, manualId) {
  const checked = [...document.querySelectorAll(`#${checksId} input[type=checkbox]:checked`)].map(cb => cb.value);
  if (checked.length) return checked.join(',');
  return document.getElementById(manualId).value.trim();
}
async function loadLocationKeys() {
  const r = await fetch('/api/location_keys');
  const keys = await r.json();
  ['orig-locs-checks', 'p-orig-locs-checks'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const entries = Object.entries(keys).sort((a,b) => parseInt(a[0])-parseInt(b[0]));
    if (!entries.length) {
      el.innerHTML = '<span style="font-size:11px;color:var(--muted);font-family:var(--mono)">No keys configured — run setup wizard</span>';
      return;
    }
    el.innerHTML = entries.map(([k,v]) =>
      `<label style="display:flex;align-items:center;gap:4px;font-size:11px;font-family:var(--mono);color:var(--text);cursor:pointer;background:var(--surface2);padding:3px 8px;border-radius:4px;border:1px solid var(--border)">` +
      `<input type="checkbox" value="${k}" style="accent-color:var(--accent)"> ${k} — ${v.name}</label>`
    ).join('');
  });
}
function onOrigAudioChange() {
  const mode = document.getElementById('orig-audio').value;
  document.getElementById('orig-tts-panel').style.display  = mode === 'tts'  ? 'block' : 'none';
  document.getElementById('orig-voip-panel').style.display = mode === 'voip' ? 'block' : 'none';
}
async function previewAnnouncement() {
  const event = (document.getElementById('orig-event').value || '').trim().toUpperCase();
  const locs  = _getCheckedLocs('orig-locs-checks', 'orig-locs');
  const dur   = document.getElementById('orig-dur').value;
  if (!event || !locs) { toast('Select an event and location key(s) to preview', false); return; }
  const r = await post('/api/decode', {event, locations:locs, duration:dur});
  const el = document.getElementById('orig-preview-text');
  if (r.ok && el) { el.textContent = r.text; el.style.display = 'block'; }
  else toast(r.error || 'Preview failed', false);
}

// ── VoIP announcement recording ──────────────────────────────────────────
let _recCtx = null, _recStream = null, _recProc = null, _recActive = false, _recTimer = null;
function toggleVoipRec() { if (_recActive) stopVoipRec(); else startVoipRec(); }
async function startVoipRec() {
  if (_recActive) return;
  try {
    _recStream = await navigator.mediaDevices.getUserMedia({audio: true});
    _recCtx    = new AudioContext({sampleRate: 44100});
    const src  = _recCtx.createMediaStreamSource(_recStream);
    _recProc   = _recCtx.createScriptProcessor(4096, 1, 1);
    _recProc.onaudioprocess = e => {
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32767));
      socket.emit('rec_chunk', Array.from(i16));
    };
    src.connect(_recProc);
    _recProc.connect(_recCtx.destination);
    socket.emit('rec_start');
    _recActive = true;
    const btn = document.getElementById('orig-rec-btn');
    const sts = document.getElementById('orig-rec-status');
    if (btn) { btn.style.background = 'var(--danger)'; btn.textContent = '⏹ Stop Recording'; }
    let _recSecs = 0;
    if (sts) { sts.textContent = '● REC 0s'; sts.style.color = 'var(--danger)'; }
    _recTimer = setInterval(() => {
      _recSecs++;
      const s = document.getElementById('orig-rec-status');
      if (s) s.textContent = `● REC ${_recSecs}s`;
    }, 1000);
  } catch(e) { toast('Microphone: ' + e.message, false); }
}
function stopVoipRec() {
  if (!_recActive) return;
  if (_recTimer)  { clearInterval(_recTimer); _recTimer = null; }
  if (_recProc)   { _recProc.disconnect();    _recProc  = null; }
  if (_recCtx)    { _recCtx.close();          _recCtx   = null; }
  if (_recStream) { _recStream.getTracks().forEach(t=>t.stop()); _recStream = null; }
  socket.emit('rec_stop');
  _recActive = false;
  const btn = document.getElementById('orig-rec-btn');
  const sts = document.getElementById('orig-rec-status');
  if (btn) { btn.style.background = ''; btn.textContent = '⏺ Start Recording'; }
  if (sts) { sts.textContent = '✓ Ready — click Originate to send'; sts.style.color = 'var(--success)'; }
}
socket.on('rec_error', d => { stopVoipRec(); toast(d.error, false); });

async function originateAlert() {
  const event = document.getElementById('orig-event').value.trim().toUpperCase();
  const locs  = _getCheckedLocs('orig-locs-checks', 'orig-locs');
  const dur   = document.getElementById('orig-dur').value;
  const mode  = document.getElementById('orig-audio').value;
  if (!event || !locs) { toast('Enter event code and select/enter location keys', false); return; }
  if (mode === 'tts') {
    toast('Generating TTS and originating…', true);
    const r = await post('/api/control/originate', {event, locations:locs, duration:dur, tts:true});
    if (r.ok) {
      const el = document.getElementById('orig-preview-text');
      if (el) { el.textContent = r.text; el.style.display = 'block'; }
      toast(`${event} originated with TTS`, true);
    } else { toast(r.error, false); }
  } else if (mode === 'voip') {
    const sts = document.getElementById('orig-rec-status');
    if (!sts || !sts.textContent.startsWith('✓')) { toast('Record your announcement first', false); return; }
    const r = await post('/api/control/originate', {event, locations:locs, duration:dur, audio:'p'});
    toast(r.ok ? `${event} originated with VoIP recording` : r.error, r.ok);
    if (r.ok && sts) { sts.textContent = 'Idle'; sts.style.color = 'var(--muted)'; }
  } else {
    const r = await post('/api/control/originate', {event, locations:locs, duration:dur, audio:mode});
    toast(r.ok ? `${event} originated` : r.error, r.ok);
  }
}
async function panelOriginate() {
  const event = document.getElementById('p-orig-event').value.trim().toUpperCase();
  const locs  = _getCheckedLocs('p-orig-locs-checks', 'p-orig-locs');
  const dur   = document.getElementById('p-orig-dur').value;
  const audio = document.getElementById('p-orig-audio').value;
  if (!event || !locs) { toast('Enter event code and select/enter location keys', false); return; }
  const r = await post('/api/control/originate', {event, locations:locs, duration:dur, audio});
  toast(r.ok ? `${event} originated` : r.error, r.ok);
}
async function checkControlStatus() {
  const r = await fetch('/api/control/status');
  const d = await r.json();
  const el = document.getElementById('control-status');
  if (el) el.innerHTML = d.connected
    ? '<span style="color:var(--success)">● COM3 connected</span>'
    : '<span style="color:var(--warn)">● COM3 not connected</span>';
}
async function refreshPanelStatus() {
  const r = await fetch('/api/control/status');
  const d = await r.json();
  const el = document.getElementById('panel-com3-status');
  if (el) { el.textContent = d.connected ? 'connected' : 'not connected'; el.className = 'status-val ' + (d.connected?'ok':'warn'); }
}
setInterval(checkControlStatus, 30000);

// ── PTT ────────────────────────────────────────────────────────────────────
let _pttActive = false, _pttCtx = null, _pttStream = null, _pttProc = null;

async function startPTT() {
  if (_pttActive) return;
  try {
    _pttStream = await navigator.mediaDevices.getUserMedia({audio:true, video:false});
    _pttCtx    = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 44100});
    const src  = _pttCtx.createMediaStreamSource(_pttStream);
    _pttProc   = _pttCtx.createScriptProcessor(2048, 1, 1);
    _pttProc.onaudioprocess = e => {
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++)
        i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32768 | 0));
      socket.emit('ptt_chunk', Array.from(i16));
    };
    src.connect(_pttProc);
    _pttProc.connect(_pttCtx.destination);
    socket.emit('ptt_start');
    _pttActive = true;
    document.getElementById('ptt-btn').classList.add('active');
    document.getElementById('ptt-status').textContent = '● TRANSMITTING';
    document.getElementById('ptt-status').style.color = 'var(--danger)';
  } catch(e) {
    toast('Microphone: ' + e.message, false);
  }
}
function stopPTT() {
  if (!_pttActive) return;
  pttCleanup();
  socket.emit('ptt_stop');
}
function pttCleanup() {
  if (_pttProc)   { _pttProc.disconnect();  _pttProc = null; }
  if (_pttCtx)    { _pttCtx.close();        _pttCtx  = null; }
  if (_pttStream) { _pttStream.getTracks().forEach(t=>t.stop()); _pttStream = null; }
  _pttActive = false;
  const btn = document.getElementById('ptt-btn');
  const sts = document.getElementById('ptt-status');
  if (btn) btn.classList.remove('active');
  if (sts) { sts.textContent = 'Idle — hold to talk'; sts.style.color = ''; }
}

// ── settings page ─────────────────────────────────────────────────────────
let _settingsLoaded = false;
async function loadSettings() {
  if (_settingsLoaded) return;
  const r    = await fetch('/api/config');
  const data = await r.json();
  const st = data.station       || {};
  const ct = data.control       || {};
  const se = data.serial        || {};
  const no = data.notifications || {};
  const we = data.web           || {};
  const al = data.alerts        || {};
  const lo = data.logging       || {};

  document.getElementById('s-callsign').value   = st.callsign  || '';
  document.getElementById('s-fips').value       = st.fips      || '';
  document.getElementById('s-org').value        = st.org       || 'EAS';
  document.getElementById('s-tz').value         = st.tz_offset !== undefined ? st.tz_offset : '';
  document.getElementById('s-ctrl-port').value  = ct.port || '/dev/tft911-cmd';
  document.getElementById('s-ctrl-baud').value  = ct.baud || '9600';
  document.getElementById('s-ctrl-pin').value   = ct.pin  || '';
  document.getElementById('s-ser-port').value   = se.port || '/dev/ttyUSB0';
  document.getElementById('s-ser-baud').value   = se.baud || '1200';
  document.getElementById('s-ntfy').value       = no.ntfy_topic || '';
  document.getElementById('s-web-host').value   = we.host || '0.0.0.0';
  document.getElementById('s-web-port').value   = we.port || '5000';
  document.getElementById('s-dedupe').value     = al.dedupe_window || '120';
  document.getElementById('s-alerts-dir').value = al.alerts_dir   || '';
  document.getElementById('s-log-dir').value    = lo.log_dir      || '';

  renderLKRows(data.location_keys || {});
  document.getElementById('settings-loading').style.display = 'none';
  document.getElementById('settings-content').style.display = 'block';
  _settingsLoaded = true;
}

async function saveAllSettings() {
  const lkData = {};
  document.querySelectorAll('.lk-row').forEach(row => {
    const key  = row.dataset.key;
    const name = row.querySelector('.lk-name').value.trim();
    const fips = [...row.querySelectorAll('.lk-fips-chip')].map(c => c.dataset.fips).join(',');
    if (name && fips) lkData[key] = `${name} | ${fips}`;
    else if (name)    lkData[key] = name;
  });
  const payload = {
    station:       { callsign: document.getElementById('s-callsign').value, fips: document.getElementById('s-fips').value, org: document.getElementById('s-org').value, tz_offset: document.getElementById('s-tz').value },
    control:       { port: document.getElementById('s-ctrl-port').value, baud: document.getElementById('s-ctrl-baud').value, pin: document.getElementById('s-ctrl-pin').value },
    serial:        { port: document.getElementById('s-ser-port').value, baud: document.getElementById('s-ser-baud').value },
    notifications: { ntfy_topic: document.getElementById('s-ntfy').value },
    web:           { host: document.getElementById('s-web-host').value, port: document.getElementById('s-web-port').value },
    alerts:        { dedupe_window: document.getElementById('s-dedupe').value, alerts_dir: document.getElementById('s-alerts-dir').value },
    logging:       { log_dir: document.getElementById('s-log-dir').value },
    location_keys: lkData,
  };
  const r   = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const res = await r.json();
  toast(res.ok ? 'Settings saved — restart services to apply' : res.error, res.ok);
  if (res.ok) { _settingsLoaded = false; loadLocationKeys(); }
}

function renderLKRows(lkData) {
  const container = document.getElementById('lk-rows');
  container.innerHTML = '';
  for (let i = 1; i <= 14; i++) {
    const key = String(i);
    const val = lkData[key] || '';
    let name = '', fipsList = [];
    if (val.includes('|')) {
      const [n, f] = val.split('|');
      name     = n.trim();
      fipsList = f.trim().split(',').map(s => s.trim()).filter(Boolean);
    } else {
      name = val.trim();
    }
    container.appendChild(buildLKRow(key, name, fipsList));
  }
}

function buildLKRow(key, name, fipsList) {
  const row = document.createElement('div');
  row.className = 'lk-row';
  row.dataset.key = key;
  const chips = fipsList.map(f =>
    `<span class="lk-fips-chip" data-fips="${f}">${f}<button class="lk-chip-rm" onclick="removeFipsChip(this)">×</button></span>`
  ).join('');
  row.innerHTML =
    `<span class="lk-key-num">${key}</span>` +
    `<input class="lk-name cfg-val" placeholder="Name" value="${name.replace(/"/g,'&quot;')}">` +
    `<div class="lk-fips-area">` +
      `<div class="lk-chips">${chips}</div>` +
      `<div class="lk-search-wrap">` +
        `<input class="lk-search cfg-val" placeholder="Search county to add…" oninput="fipsSearchInput(this)" autocomplete="off">` +
        `<div class="lk-search-results" style="display:none"></div>` +
      `</div>` +
    `</div>`;
  return row;
}

function removeFipsChip(btn) { btn.parentElement.remove(); }

let _fipsTimer = null;
function fipsSearchInput(inp) {
  clearTimeout(_fipsTimer);
  const q   = inp.value.trim();
  const res = inp.nextElementSibling;
  if (q.length < 2) { res.style.display = 'none'; return; }
  _fipsTimer = setTimeout(async () => {
    const r     = await fetch(`/api/fips/search?q=${encodeURIComponent(q)}`);
    const items = await r.json();
    if (!items.length) { res.style.display = 'none'; return; }
    res.innerHTML = items.map(([fips, cname]) =>
      `<div class="lk-search-result" data-fips="${fips}" data-name="${cname.replace(/"/g,'&quot;')}">${cname} <span style="color:var(--muted)">${fips}</span></div>`
    ).join('');
    res.querySelectorAll('.lk-search-result').forEach(el => {
      el.addEventListener('mousedown', ev => {
        ev.preventDefault();
        const row   = el.closest('.lk-row');
        const chips = row.querySelector('.lk-chips');
        const fips  = el.dataset.fips;
        const cn    = el.dataset.name;
        if (!row.querySelector(`.lk-fips-chip[data-fips="${fips}"]`)) {
          const chip = document.createElement('span');
          chip.className = 'lk-fips-chip';
          chip.dataset.fips = fips;
          chip.innerHTML = `${fips} ${cn}<button class="lk-chip-rm" onclick="removeFipsChip(this)">×</button>`;
          chips.appendChild(chip);
          const ni = row.querySelector('.lk-name');
          if (!ni.value) ni.value = cn;
        }
        inp.value = '';
        res.style.display = 'none';
      });
    });
    res.style.display = 'block';
  }, 250);
}
document.addEventListener('click', () => {
  document.querySelectorAll('.lk-search-results').forEach(d => d.style.display = 'none');
});

loadLocationKeys();
</script>
</body>
</html>"""


# ── badge helper (Jinja2 global) ───────────────────────────────────────────

WARNING_EVENTS = {'SVR','SVA','HWW','HWA','FFW','FFA','FLW','FLA','WSW','WSA','BZW','SQW','EWW','DSW','SMW'}
DANGER_EVENTS  = {'TOR','TOA','HUW','HUA','TSW','TSA','EAN','CEM','CDW','EVI','CAE','LEW','LAE','SPW'}
TEST_EVENTS    = {'RWT','RMT','NPT','DMO'}
BADGE_LABELS   = {
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

def badge(alert):
    code = alert.get('event_code', '')
    if code in DANGER_EVENTS:   cls = 'badge-danger'
    elif code in WARNING_EVENTS: cls = 'badge-warn'
    elif code in TEST_EVENTS:    cls = 'badge-success'
    else:                        cls = 'badge-info'
    label = BADGE_LABELS.get(code, code or 'Unknown')
    return Markup(f'<span class="badge {cls}">{label}</span>')

app.jinja_env.globals['badge'] = badge


# ── entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(CONFIG['alerts_dir'], exist_ok=True)
    threading.Thread(target=start_watchdog,  daemon=True).start()
    threading.Thread(target=start_log_stream, daemon=True).start()
    print(f"EAS Monitor starting on http://{CONFIG['web_host']}:{CONFIG['web_port']}")
    socketio.run(app, host=CONFIG['web_host'], port=CONFIG['web_port'], debug=False)
