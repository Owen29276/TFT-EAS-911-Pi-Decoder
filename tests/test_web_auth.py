"""Tests for dashboard authentication, origin checks and audit logging."""

import json
import sys
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import web          # noqa: E402
import web_auth     # noqa: E402

PW     = "correct horse battery"
ORIGIN = {"Origin": "http://localhost"}
EVIL   = {"Origin": "http://evil.example"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.ini"
    cfg.write_text("[web]\nport = 5000\n[control]\npin = 915\n")
    web_auth.write_password_hash(cfg, generate_password_hash(PW))
    monkeypatch.setattr(web, "CONFIG_PATH", cfg)
    # init_auth bound the real config path; point the hash lookup at the temp file instead
    monkeypatch.setattr(web_auth, "get_password_hash", lambda _p: _read_hash(cfg))
    monkeypatch.setattr(web.audit, "path", str(tmp_path / "audit.jsonl"))
    web_auth._fails.clear()
    web.app.config["TESTING"] = True
    return web.app.test_client()


def _read_hash(cfg):
    import configparser
    c = configparser.ConfigParser(interpolation=None)
    c.read(cfg)
    return c.get("web", "password_hash", fallback="")


def login(client, pw=PW):
    return client.post("/login", data={"password": pw, "next": "/"}, headers=ORIGIN)


# ── unauthenticated access ─────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/alerts", "/api/stats", "/api/config", "/api/location_keys"])
def test_api_get_requires_login(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/api/control/rwt", "/api/control/originate",
                                  "/api/control/reboot", "/api/config"])
def test_api_post_requires_login(client, path):
    assert client.post(path, json={}, headers=ORIGIN).status_code == 401


def test_index_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")


def test_socketio_rejects_unauthenticated(client):
    sio = web.socketio.test_client(web.app, flask_test_client=client)
    assert not sio.is_connected()


# ── login ──────────────────────────────────────────────────────────────────

def test_wrong_password(client):
    assert login(client, "nope").status_code == 401
    assert client.get("/api/alerts").status_code == 401


def test_correct_password(client):
    r = login(client)
    assert r.status_code == 302
    assert client.get("/api/alerts").status_code == 200


def test_dashboard_renders_after_login(client):
    login(client)
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'href="/logout"' in html and "confirmOriginate(" in html


def test_socketio_accepts_after_login(client):
    login(client)
    sio = web.socketio.test_client(web.app, flask_test_client=client)
    assert sio.is_connected()
    sio.disconnect()


def test_lockout_after_repeated_failures(client):
    for _ in range(web_auth.MAX_FAILS):
        login(client, "wrong")
    # Even the right password is refused while locked out
    assert login(client).status_code == 429


def test_open_redirect_blocked(client):
    r = client.post("/login", data={"password": PW, "next": "//evil.example/x"}, headers=ORIGIN)
    assert r.headers["Location"] == "/"


def test_logout(client):
    login(client)
    client.get("/logout")
    assert client.get("/api/alerts").status_code == 401


# ── cross-origin protection ────────────────────────────────────────────────

def test_cross_origin_post_blocked(client):
    login(client)
    assert client.post("/api/control/rwt", json={}, headers=EVIL).status_code == 403


def test_post_without_origin_blocked(client):
    login(client)
    assert client.post("/api/control/rwt", json={}).status_code == 403


def test_cross_origin_login_blocked(client):
    r = client.post("/login", data={"password": PW}, headers=EVIL)
    assert r.status_code == 403


# ── secrets + originate confirmation + audit ───────────────────────────────

def test_config_get_hides_password_hash(client):
    login(client)
    data = client.get("/api/config").get_json()
    assert "password_hash" not in data.get("web", {})


def test_config_post_cannot_overwrite_hash(client):
    login(client)
    before = _read_hash(web.CONFIG_PATH)
    client.post("/api/config", json={"web": {"password_hash": "pwned"}}, headers=ORIGIN)
    assert _read_hash(web.CONFIG_PATH) == before


def test_originate_requires_typed_confirmation(client, monkeypatch):
    login(client)
    monkeypatch.setattr(web, "tft_ok", lambda: True)
    called = []
    monkeypatch.setattr(web, "_tft_call", lambda fn: called.append(fn) or web.jsonify({"ok": True}))
    body = {"event": "TOR", "locations": "1", "duration": "01", "audio": "n"}
    r = client.post("/api/control/originate", json=body, headers=ORIGIN)
    assert r.status_code == 400 and not called
    r = client.post("/api/control/originate", json={**body, "confirm": "RWT"}, headers=ORIGIN)
    assert r.status_code == 400 and not called
    r = client.post("/api/control/originate", json={**body, "confirm": "tor"}, headers=ORIGIN)
    assert r.status_code == 200 and len(called) == 1


def test_control_actions_are_audited(client, monkeypatch):
    login(client)
    monkeypatch.setattr(web, "tft_ok", lambda: False)
    client.post("/api/control/rwt", json={"tone": True}, headers=ORIGIN)
    entries = [json.loads(l) for l in Path(web.audit.path).read_text().splitlines()]
    actions = [e["action"] for e in entries]
    assert "login_ok" in actions
    assert any(e["action"] == "/api/control/rwt" and e["detail"] == {"tone": True} for e in entries)
