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
    assert 'href="/logout"' in html and "/static/dashboard.js?v=" in html
    js = client.get("/static/dashboard.js")
    assert js.status_code == 200 and "confirmOriginate(" in js.get_data(as_text=True)
    assert client.get("/static/dashboard.css").status_code == 200


def test_static_assets_require_login(client):
    r = client.get("/static/dashboard.js")
    assert r.status_code in (302, 401)


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


class FakeTFT:
    def __init__(self, org="EAS", callsign="KE2HPG"):
        self.config = {"org": org, "callsign": callsign}
        self.sent = []
    def send_rwt(self, attention_tone=True): self.sent.append("RWT")
    def originate(self, event, locations, duration, audio="p"): self.sent.append(event)


def test_originate_requires_typed_confirmation(client, monkeypatch):
    login(client)
    monkeypatch.setattr(web, "tft_ok", lambda: True)
    monkeypatch.setattr(web, "tft", FakeTFT())
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


# ── ERN §1.4 enforcement through the routes ────────────────────────────────

@pytest.fixture
def station(client, monkeypatch, tmp_path):
    login(client)
    fake = FakeTFT()
    monkeypatch.setattr(web, "tft", fake)
    monkeypatch.setattr(web, "tft_ok", lambda: True)
    monkeypatch.setattr(web, "JSONL", str(tmp_path / "events.jsonl"))
    def tft_call(fn):
        fn()
        return web.jsonify({"ok": True})
    monkeypatch.setattr(web, "_tft_call", tft_call)
    return fake


def audit_actions():
    return [json.loads(l)["action"] for l in Path(web.audit.path).read_text().splitlines()]


def test_third_rwt_in_a_week_blocked_across_both_routes(client, station):
    fake = station
    assert client.post("/api/control/rwt", json={"tone": True}, headers=ORIGIN).status_code == 200
    body = {"event": "RWT", "locations": "1", "duration": "00", "audio": "n", "confirm": "RWT"}
    assert client.post("/api/control/originate", json=body, headers=ORIGIN).status_code == 200
    r = client.post("/api/control/rwt", json={"tone": True}, headers=ORIGIN)
    assert r.status_code == 403 and "§1.4(1)(a)" in r.get_json()["error"]
    r = client.post("/api/control/originate", json=body, headers=ORIGIN)
    assert r.status_code == 403
    assert fake.sent == ["RWT", "RWT"]                        # blocked ones never reached the TFT
    acts = audit_actions()
    assert acts.count("originate_sent") == 2 and acts.count("originate_blocked") == 2


def test_failed_send_is_not_counted(client, station, monkeypatch):
    monkeypatch.setattr(web, "_tft_call", lambda fn: (web.jsonify({"ok": False}), 500))
    for _ in range(3):
        client.post("/api/control/rwt", json={}, headers=ORIGIN)
    assert "originate_sent" not in audit_actions()


def test_live_alert_from_non_eas_org_needs_basis(client, station, monkeypatch):
    fake = station
    fake.config["org"] = "CIV"
    body = {"event": "CEM", "locations": "1", "duration": "01", "audio": "n", "confirm": "CEM"}
    r = client.post("/api/control/originate", json=body, headers=ORIGIN)
    assert r.status_code == 403 and r.get_json()["needs_basis"] is True and fake.sent == []
    r = client.post("/api/control/originate", headers=ORIGIN,
                    json={**body, "ern_basis": "official_agency", "ern_note": "Tompkins County EM"})
    j = r.get_json()
    assert r.status_code == 200 and "§1.4(3)(c)" in j["basis"] and fake.sent == ["CEM"]


def test_config_post_case_variant_cannot_overwrite_hash(client):
    login(client)
    before = _read_hash(web.CONFIG_PATH)
    r = client.post("/api/config", json={"web": {"Password_Hash": "pwned"}}, headers=ORIGIN)
    assert r.status_code == 200 and _read_hash(web.CONFIG_PATH) == before


def test_config_post_keeps_comments_and_rejects_bad_names(client):
    login(client)
    cfg = web.CONFIG_PATH
    cfg.write_text("# my notes\n" + cfg.read_text() + "\n# audio notes\n[audio]\nenabled = false\n")
    r = client.post("/api/config", json={"audio": {"enabled": "true"}}, headers=ORIGIN)
    assert r.status_code == 200
    text = cfg.read_text()
    assert "# my notes" in text and "# audio notes" in text and "enabled = true" in text
    r = client.post("/api/config", json={"audio]": {"x": "1"}}, headers=ORIGIN)
    assert r.status_code == 400
