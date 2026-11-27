"""Tests for ini_edit.py — comment-preserving config.ini edits."""

import configparser
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ini_edit as E  # noqa: E402

SAMPLE = """\
# EAS logger config
[serial]
port = /dev/tft911-data
# 1200 for the TFT data port
baud = 1200

[web]
port = 5000
password_hash = scrypt:32768:8:1$salt$abc

# ── Audio ──
[audio]
enabled = false
# ALSA device — arecord -l
device = plughw:1,0

[location_keys]
# key = name|fips,...
1 = Tompkins|036109
2 = Cayuga|036011
"""


def parsed(text):
    c = configparser.ConfigParser(interpolation=None)
    c.read_string(text)
    return c


def test_changes_value_and_keeps_every_comment():
    out = E.update_ini_text(SAMPLE, {"audio": {"enabled": "true"}, "serial": {"baud": "9600"}})
    assert out.replace("enabled = true", "enabled = false").replace("baud = 9600", "baud = 1200") == SAMPLE
    c = parsed(out)
    assert c.getboolean("audio", "enabled") and c.get("serial", "baud") == "9600"


def test_unchanged_full_resave_is_byte_identical():
    c = parsed(SAMPLE)
    everything = {s: dict(c[s]) for s in c.sections()}
    everything["web"].pop("password_hash")        # the dashboard never sees it
    out = E.update_ini_text(SAMPLE, everything, replace_sections=("location_keys",),
                            protected={("web", "password_hash")})
    assert out == SAMPLE


def test_new_key_goes_inside_its_section_before_next_sections_comment():
    out = E.update_ini_text(SAMPLE, {"web": {"host": "0.0.0.0"}})
    lines = out.splitlines()
    assert lines.index("host = 0.0.0.0") == lines.index("password_hash = scrypt:32768:8:1$salt$abc") + 1
    assert parsed(out).get("web", "host") == "0.0.0.0"


def test_empty_value_removes_key_new_section_appended():
    out = E.update_ini_text(SAMPLE, {"audio": {"device": ""}, "mqtt": {"host": "10.0.0.2", "port": ""}})
    c = parsed(out)
    assert not c.has_option("audio", "device")
    assert "# ALSA device — arecord -l" in out            # its comment stays
    assert dict(c["mqtt"]) == {"host": "10.0.0.2"}


def test_replace_section_rewrites_location_keys_keeps_comment():
    out = E.update_ini_text(SAMPLE, {"location_keys": {"1": "Tompkins|036109", "3": "Tioga|036107"}},
                            replace_sections=("location_keys",))
    c = parsed(out)
    assert dict(c["location_keys"]) == {"1": "Tompkins|036109", "3": "Tioga|036107"}
    assert "# key = name|fips,..." in out


def test_protected_key_cannot_be_changed_even_with_other_case():
    for key in ("password_hash", "Password_Hash", "PASSWORD_HASH"):
        out = E.update_ini_text(SAMPLE, {"web": {key: "pwned"}, "WEB": {key: ""}},
                                protected={("web", "password_hash")})
        assert parsed(out).get("web", "password_hash") == "scrypt:32768:8:1$salt$abc"


def test_newlines_in_values_cannot_inject_keys_or_sections():
    out = E.update_ini_text(SAMPLE, {"audio": {"device": "x\n[web]\npassword_hash = pwned"}},
                            protected={("web", "password_hash")})
    c = parsed(out)
    assert c.get("web", "password_hash") == "scrypt:32768:8:1$salt$abc"
    assert c.get("audio", "device") == "x [web] password_hash = pwned"


@pytest.mark.parametrize("bad", [{"we b": {"a": "1"}}, {"web]": {"a": "1"}},
                                 {"web": {"a=b": "1"}}, {"web": {"": "1"}}, {"web": "notadict"}])
def test_bad_names_rejected(bad):
    with pytest.raises(E.IniEditError):
        E.update_ini_text(SAMPLE, bad)


def test_continuation_lines_are_replaced_together():
    text = "[a]\nlist = one\n    two\n    three\n# after\nx = 1\n"
    out = E.update_ini_text(text, {"a": {"list": "single"}})
    assert out == "[a]\nlist = single\n# after\nx = 1\n"


def test_save_ini_is_atomic_and_keeps_permissions(tmp_path):
    f = tmp_path / "config.ini"
    f.write_text(SAMPLE)
    os.chmod(f, 0o600)
    E.save_ini(f, {"audio": {"enabled": "true"}})
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert "# ── Audio ──" in f.read_text()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".config-")]


def test_empty_placeholders_survive_resave_but_cleared_values_go():
    text = "[mqtt]\n# your HA IP\nhost =\nport = 1883\n"
    assert E.update_ini_text(text, {"mqtt": {"host": "", "port": "1883"}}) == text
    out = E.update_ini_text(text, {"mqtt": {"port": ""}})
    assert out == "[mqtt]\n# your HA IP\nhost =\n"


def test_real_config_full_resave_is_identical():
    import configparser
    path = Path(__file__).resolve().parent.parent / "config.example.ini"
    text = path.read_text(encoding="utf-8")
    c = configparser.ConfigParser(interpolation=None)
    c.read_string(text)
    data = {s: {k: v for k, v in c[s].items() if (s, k) != ("web", "password_hash")} for s in c.sections()}
    assert E.update_ini_text(text, data, replace_sections=("location_keys",),
                             protected={("web", "password_hash")}) == text
