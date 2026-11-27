"""Tests for settings.py and the config read/write paths that use it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import settings  # noqa: E402
import web_auth  # noqa: E402

CFG = """\
# station notes
[notifications]
# %20 must not crash anything
ntfy_topic = https://ntfy.sh/eas%20alerts

[web]
# dashboard
port = 5000
"""


def test_percent_in_value_is_literal(tmp_path):
    f = tmp_path / "config.ini"
    f.write_text(CFG)
    c = settings.read_config(f)
    assert c.get("notifications", "ntfy_topic") == "https://ntfy.sh/eas%20alerts"


def test_secrets_only_when_asked(tmp_path, monkeypatch):
    f = tmp_path / "config.ini"
    f.write_text(CFG)
    sec = tmp_path / "secrets.ini"
    sec.write_text("[mqtt]\npassword = hunter2%\n")
    monkeypatch.setattr(settings, "SECRETS_PATH", sec)
    assert not settings.read_config(f).has_section("mqtt")
    assert settings.read_config(f, with_secrets=True).get("mqtt", "password") == "hunter2%"


def test_missing_file_is_empty(tmp_path):
    assert settings.read_config(tmp_path / "nope.ini").sections() == []


def test_resolve_path():
    assert settings.resolve_path("/abs/x") == "/abs/x"
    assert settings.resolve_path("alerts") == str(settings.APP_DIR / "alerts")


def test_set_password_keeps_comments(tmp_path):
    f = tmp_path / "config.ini"
    f.write_text(CFG)
    web_auth.write_password_hash(f, "scrypt:32768:8:1$salt$hash")
    text = f.read_text()
    assert "# station notes" in text and "# dashboard" in text and "# %20 must not crash" in text
    assert web_auth.get_password_hash(f) == "scrypt:32768:8:1$salt$hash"
    web_auth.write_password_hash(f, "scrypt:new")
    assert f.read_text().count("password_hash") == 1 and web_auth.get_password_hash(f) == "scrypt:new"


def test_controller_config_survives_percent(tmp_path, monkeypatch):
    import TFT_Control
    f = tmp_path / "config.ini"
    f.write_text(CFG + "[station]\ncallsign = KE2HPG\n")
    monkeypatch.setattr(TFT_Control, "CONFIG_PATH", f)
    assert TFT_Control.load_config()["callsign"] == "KE2HPG"
