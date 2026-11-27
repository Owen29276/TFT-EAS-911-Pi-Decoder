#!/usr/bin/env python3
"""
TFT EAS 911 Web Dashboard
Complete browser interface: live alert feed · TFT remote control ·
PTT audio streaming · real-time log tail · config editor.
"""

import json, os, sys, struct, threading, subprocess
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, jsonify, request, session
from flask_socketio import SocketIO
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from markupsafe import Markup
from web_auth import init_auth, get_password_hash, set_password_interactive
from ini_edit import save_ini, IniEditError
from settings import CONFIG_PATH, read_config, resolve_path
from ern_policy import PolicyConfig, check as ern_check, load_history

# Handle --set-password before importing the controller (which opens COM3 on import)
if __name__ == "__main__" and "--set-password" in sys.argv:
    set_password_interactive(Path(__file__).parent / "config.ini")
    sys.exit(0)

from TFT_Control import TFTController, load_location_keys
from eas_utils import build_same_header, decode_header, search_fips, parse_location_keys


# ── config ─────────────────────────────────────────────────────────────────

def _load_web_config() -> dict:
    cfg = {
        'alerts_dir':  str(Path(__file__).parent / "alerts"),
        'log_dir':     str(Path(__file__).parent / "logs"),
        'web_port':    5000,
        'web_host':    '0.0.0.0',
        'serial_port': '/dev/ttyUSB0',
    }
    if CONFIG_PATH.exists():
        c = read_config(CONFIG_PATH)
        cfg['alerts_dir']  = c.get('alerts',  'alerts_dir', fallback=cfg['alerts_dir'])
        cfg['log_dir']     = c.get('logging', 'log_dir',    fallback=cfg['log_dir'])
        cfg['web_port']    = c.getint('web',  'port',       fallback=cfg['web_port'])
        cfg['web_host']    = c.get('web',     'host',       fallback=cfg['web_host'])
        cfg['serial_port'] = c.get('serial',  'port',       fallback=cfg['serial_port'])
    cfg['alerts_dir'] = resolve_path(cfg['alerts_dir'])
    cfg['log_dir']    = resolve_path(cfg['log_dir'])
    return cfg

CONFIG   = _load_web_config()
JSONL    = os.path.join(CONFIG['alerts_dir'], "events.jsonl")
app      = Flask(__name__)
# No cors_allowed_origins → Socket.IO only accepts same-origin connections
socketio = SocketIO(app, async_mode='threading')
audit    = init_auth(app, CONFIG_PATH, os.path.join(CONFIG['alerts_dir'], "audit.jsonl"))

# Config keys that must never be sent to, or overwritten from, the browser
_HIDDEN_CONFIG = {("web", "password_hash")}


# ── TFT controller ─────────────────────────────────────────────────────────

tft             = None
_tft_lk         = threading.Lock()
_orig_lk        = threading.Lock()   # serialises ERN-checked originations
_tft_last_error = ""   # last connection error, exposed via /api/control/status

def _connect_tft():
    global tft, _tft_last_error
    try:
        t = TFTController()
        t.connect()
        tft = t
        _tft_last_error = ""
        print("[web] COM3 connected.")
    except Exception as e:
        tft = None
        _tft_last_error = str(e)
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
    except (ValueError, RuntimeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
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
    c = read_config(CONFIG_PATH)
    return {s: {k: v for k, v in c[s].items() if (s, k) not in _HIDDEN_CONFIG}
            for s in c.sections()}


# ── routes — data ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    alerts = read_alerts()
    return render_template("dashboard.html", alerts=alerts, stats=get_stats(alerts))

@app.route("/api/alerts")
def api_alerts():
    from flask import Response
    data = json.dumps(read_alerts(), ensure_ascii=False, indent=2)
    return Response(data, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=eas-alerts.json"})

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
    ok = tft_ok()
    return jsonify({"connected": ok, "error": "" if ok else _tft_last_error})

@app.route("/api/control/reconnect", methods=["POST"])
def api_reconnect():
    global tft
    with _tft_lk:
        try: tft and tft.disconnect()
        except: pass
    _connect_tft()
    ok = tft_ok()
    return jsonify({"ok": ok, "connected": ok, "error": "" if ok else _tft_last_error})

# ── ERN §1.4 origination gate (shared by every route that puts an alert on air) ──

def _ern_decide(event: str, basis: str = "", note: str = ""):
    """Return (decision, station_cfg). Call with _orig_lk held."""
    station = tft.config if tft is not None else {}
    pc = read_config(CONFIG_PATH)
    decision = ern_check(event, station.get("org", ""), basis, note,
                         load_history(audit.path, JSONL, station.get("callsign", "")),
                         PolicyConfig.from_parser(pc))
    if not decision.ok:
        audit.log("originate_blocked", {"event": event, "reason": decision.error})
    return decision, station


def _ern_sent(event: str, station: dict, decision, **detail) -> None:
    """Record a successful origination — this is what the weekly RWT count reads."""
    audit.log("originate_sent", {"event": event, "org": station.get("org", ""),
                                 "basis": decision.basis, **detail})


@app.route("/api/control/rwt", methods=["POST"])
def api_rwt():
    tone = (request.json or {}).get("tone", True)
    with _orig_lk:
        decision, station = _ern_decide("RWT")
        if not decision.ok:
            return jsonify({"ok": False, **decision.as_dict()}), 403
        resp = _tft_call(lambda: tft.send_rwt(attention_tone=tone))  # type: ignore[union-attr]
        if not isinstance(resp, tuple):
            _ern_sent("RWT", station, decision, mode="rwt_button", tone=bool(tone))
        return resp

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
    d         = request.json or {}
    event     = d.get("event", "").upper()
    locations = d.get("locations", "")
    duration  = d.get("duration", "01")
    if not event:
        return jsonify({"ok": False, "error": "event code required"}), 400
    if not locations:
        return jsonify({"ok": False, "error": "location keys required"}), 400
    # Typed confirmation: the client must echo the event code back
    if str(d.get("confirm", "")).strip().upper() != event:
        return jsonify({"ok": False, "error": "Confirmation did not match event code — not sent"}), 400

    # Check + send + record as one step, so two quick RWTs can't both pass the weekly limit
    with _orig_lk:
        decision, station = _ern_decide(event, d.get("ern_basis", ""), d.get("ern_note", ""))
        if not decision.ok:
            return jsonify({"ok": False, **decision.as_dict()}), 403

        def sent(extra=None):
            _ern_sent(event, station, decision, locations=locations, duration=duration,
                      mode="tts" if d.get("tts") else d.get("audio", "p"))
            return jsonify({"ok": True, "basis": decision.basis, "warning": decision.warning,
                            **(extra or {})})

        if d.get("tts"):
            try:
                with _tft_lk:
                    text = tft.originate_with_tts(event, locations, duration)
                return sent({"text": text})
            except (ValueError, RuntimeError) as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        resp = _tft_call(lambda: tft.originate(event, locations, duration, d.get("audio", "p")))
        return sent() if not isinstance(resp, tuple) else resp

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
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "expected an object of sections"}), 400
    try:
        # Edits only the changed lines, so config.ini keeps its comments
        save_ini(CONFIG_PATH, data, replace_sections=("location_keys",),
                 protected=_HIDDEN_CONFIG)
        return jsonify({"ok": True})
    except IniEditError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── websocket handlers ─────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    # Reject WebSocket connections without a logged-in session (blocks PTT/rec audio)
    if not session.get("auth"):
        return False

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
    audit.log("ptt_start")
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
    audit.log("rec_start")
    if not tft_ok():
        socketio.emit('rec_error', {'error': 'COM3 not connected'})
        return
    try:
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
# Cache-buster so browsers pick up a new dashboard.js / .css after an update
app.jinja_env.globals['asset_v'] = int(max(
    os.path.getmtime(os.path.join(app.static_folder or "static", f))
    for f in ("dashboard.js", "dashboard.css")))


# ── entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not get_password_hash(CONFIG_PATH):
        print("[web] No dashboard password set — refusing to start an unauthenticated control panel.\n"
              "      Run: python3 web.py --set-password", file=sys.stderr)
        sys.exit(1)
    os.makedirs(CONFIG['alerts_dir'], exist_ok=True)
    threading.Thread(target=start_watchdog,  daemon=True).start()
    threading.Thread(target=start_log_stream, daemon=True).start()
    print(f"EAS Monitor starting on http://{CONFIG['web_host']}:{CONFIG['web_port']}")
    socketio.run(app, host=CONFIG['web_host'], port=CONFIG['web_port'], debug=False)
