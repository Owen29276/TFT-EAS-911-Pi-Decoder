#!/usr/bin/env python3
"""
Authentication, cross-origin protection and audit logging for web.py.

- Single shared dashboard password, stored as a salted hash in
  config.ini [web] password_hash (set with: python3 web.py --set-password)
- Session cookie: HttpOnly, SameSite=Strict, 12 h lifetime
- State-changing requests must come from the dashboard's own origin
- Brute-force throttle: 5 failed logins per IP per 15 minutes
- Every control / config action is appended to alerts/audit.jsonl
"""

import getpass
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (has_request_context, jsonify, redirect,
                   render_template_string, request, session)
from werkzeug.security import check_password_hash, generate_password_hash

from ini_edit import save_ini
from settings import read_config

BASE_DIR    = Path(__file__).parent
SECRET_FILE = BASE_DIR / ".flask_secret"

MAX_FAILS   = 5
FAIL_WINDOW = 15 * 60          # seconds
MIN_PW_LEN  = 8

# Paths that never require a session
PUBLIC_ENDPOINTS = {"login", "logout"}
# POSTs to these prefixes are written to the audit log
AUDITED_PREFIXES = ("/api/control/", "/api/config")

_fail_lk = threading.Lock()
_fails: dict[str, list[float]] = {}   # ip -> failure timestamps
_audit_lk = threading.Lock()


# ── secrets / password storage ─────────────────────────────────────────────

def load_secret_key(path: Path = SECRET_FILE) -> bytes:
    """Load the Flask session signing key, creating it (mode 0600) on first run."""
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def get_password_hash(config_path: Path) -> str:
    return read_config(config_path).get("web", "password_hash", fallback="").strip()


def write_password_hash(config_path: Path, pw_hash: str) -> None:
    # Edits just this one line, so config.ini keeps its comments
    save_ini(config_path, {"web": {"password_hash": pw_hash}})


def set_password_interactive(config_path: Path) -> None:
    """Prompt for a new dashboard password and save its hash to config.ini."""
    while True:
        pw = getpass.getpass("New dashboard password: ")
        if len(pw) < MIN_PW_LEN:
            print(f"  Must be at least {MIN_PW_LEN} characters.")
            continue
        if getpass.getpass("Confirm password: ") != pw:
            print("  Passwords don't match.")
            continue
        break
    write_password_hash(config_path, generate_password_hash(pw))
    print(f"Password saved to {config_path} [web] password_hash.")
    print("Restart the web service for it to take effect: sudo systemctl restart tft911-eas-web")


# ── throttle ───────────────────────────────────────────────────────────────

def _recent_fails(ip: str, now: float) -> list[float]:
    fails = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    _fails[ip] = fails
    return fails


def is_locked_out(ip: str) -> bool:
    with _fail_lk:
        return len(_recent_fails(ip, time.time())) >= MAX_FAILS


def record_failure(ip: str) -> None:
    with _fail_lk:
        now = time.time()
        _recent_fails(ip, now).append(now)


def clear_failures(ip: str) -> None:
    with _fail_lk:
        _fails.pop(ip, None)


# ── origin check ───────────────────────────────────────────────────────────

def same_origin() -> bool:
    """True if the request's Origin (or Referer) host matches the Host header."""
    src = request.headers.get("Origin") or request.headers.get("Referer")
    if not src:
        return False
    return urlparse(src).netloc == request.host


# ── audit log ──────────────────────────────────────────────────────────────

class Auditor:
    def __init__(self, path: str):
        self.path = path

    def log(self, action: str, detail=None) -> None:
        entry = {
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ip":       request.remote_addr if has_request_context() else None,
            "action":   action,
            "detail":   detail,
        }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with _audit_lk, open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[web] audit log write failed: {e}")


# ── login page ─────────────────────────────────────────────────────────────

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EAS Monitor — Sign in</title>
<style>
  body { background:#0d1117; color:#e6edf3; font-family:-apple-system,system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }
  form { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:28px; width:300px; }
  h1 { font-size:15px; font-family:monospace; letter-spacing:.05em; margin:0 0 18px; }
  input { width:100%; box-sizing:border-box; padding:9px 11px; background:#0d1117; color:#e6edf3;
          border:1px solid #30363d; border-radius:6px; font-size:14px; margin-bottom:12px; }
  button { width:100%; padding:9px; background:#238636; color:#fff; border:0; border-radius:6px;
           font-size:14px; cursor:pointer; }
  .err { color:#f85149; font-size:13px; margin-bottom:12px; }
</style></head><body>
<form method="post" action="/login">
  <h1>EAS MONITOR</h1>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <input type="password" name="password" placeholder="Password" autofocus required>
  <input type="hidden" name="next" value="{{ next }}">
  <button type="submit">Sign in</button>
</form></body></html>"""


def _safe_next(target: str) -> str:
    # Only allow local paths, never //evil.com or absolute URLs
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


# ── wiring ─────────────────────────────────────────────────────────────────

def init_auth(app, config_path: Path, audit_path: str) -> Auditor:
    """Attach login routes, the auth guard and audit logging to a Flask app."""
    app.secret_key = load_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    auditor = Auditor(audit_path)

    @app.before_request
    def _guard():
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin():
            return jsonify({"ok": False, "error": "Cross-origin request blocked"}), 403
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not session.get("auth"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Not logged in"}), 401
            return redirect(f"/login?next={request.path}")
        if request.method == "POST" and request.path.startswith(AUDITED_PREFIXES):
            body = request.get_json(silent=True)
            if request.path == "/api/config" and isinstance(body, dict):
                body = {"sections": sorted(body.keys())}   # don't copy secrets into the log
            auditor.log(request.path, body)
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        nxt = _safe_next(request.values.get("next", "/"))
        if request.method == "GET":
            return render_template_string(LOGIN_HTML, error=None, next=nxt)
        ip = request.remote_addr or "?"
        if is_locked_out(ip):
            auditor.log("login_locked_out")
            return render_template_string(LOGIN_HTML, next=nxt,
                error="Too many failed attempts. Try again in 15 minutes."), 429
        pw_hash = get_password_hash(config_path)
        if pw_hash and check_password_hash(pw_hash, request.form.get("password", "")):
            clear_failures(ip)
            session.clear()
            session["auth"] = True
            session.permanent = True
            auditor.log("login_ok")
            return redirect(nxt)
        record_failure(ip)
        auditor.log("login_failed")
        return render_template_string(LOGIN_HTML, error="Wrong password.", next=nxt), 401

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    return auditor
