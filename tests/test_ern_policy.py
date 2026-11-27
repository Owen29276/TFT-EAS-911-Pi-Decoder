"""Tests for ern_policy.py — GWES ERN §1.4 origination rules."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ern_policy as P  # noqa: E402

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
CFG = P.PolicyConfig()


def ago(**kw):
    return NOW - timedelta(**kw)


def test_rwt_limited_to_two_per_seven_days():
    assert P.check("RWT", "EAS", "", "", [], CFG, NOW).ok
    one = [(ago(days=2), "RWT")]
    d = P.check("RWT", "EAS", "", "", one, CFG, NOW)
    assert d.ok and "2 of 2" in d.basis
    two = [(ago(days=6, hours=23), "RWT"), (ago(days=1), "RWT")]
    d = P.check("RWT", "EAS", "", "", two, CFG, NOW)
    assert not d.ok and "§1.4(1)(a)" in d.error
    assert "2026-11-24 19:00" in d.error          # oldest one + 7 days
    old = [(ago(days=7, minutes=1), "RWT"), (ago(days=1), "RWT")]
    assert P.check("RWT", "EAS", "", "", old, CFG, NOW).ok   # rolling window


def test_rwt_limit_applies_whatever_the_org_or_basis():
    two = [(ago(days=1), "RWT"), (ago(days=2), "RWT")]
    assert not P.check("rwt", "CIV", "netops", "Jane", two, CFG, NOW).ok


def test_dmo_unlimited_but_warns_on_rapid_repeat():
    d = P.check("DMO", "CIV", "", "", [(ago(minutes=3), "DMO")] * 5, CFG, NOW)
    assert d.ok and "§1.4(1)(b)" in d.warning
    assert P.check("DMO", "CIV", "", "", [(ago(hours=1), "DMO")], CFG, NOW).warning == ""


def test_adr_and_eas_org_allowed():
    assert P.check("ADR", "CIV", "", "", [], CFG, NOW).basis.startswith("§1.4(3)(b)")
    d = P.check("TOR", "eas", "", "", [], CFG, NOW)
    assert d.ok and "§1.4(3)(d)" in d.basis


def test_live_event_from_other_org_needs_stated_basis():
    d = P.check("TOR", "CIV", "", "", [], CFG, NOW)
    assert not d.ok and d.needs_basis and "§1.4(3)" in d.error
    assert not P.check("TOR", "CIV", "netops", "", [], CFG, NOW).ok          # no name
    assert not P.check("TOR", "CIV", "because", "me", [], CFG, NOW).ok       # unknown basis
    d = P.check("TOR", "CIV", "netops", "J. Smith (NetOps)", [], CFG, NOW)
    assert d.ok and "§1.4(3)(a)" in d.basis and "J. Smith" in d.basis
    d = P.check("CEM", "CIV", "official_agency", "Tompkins County EM", [], CFG, NOW)
    assert d.ok and "§1.4(3)(c)" in d.basis


def test_enforce_off():
    two = [(ago(days=1), "RWT"), (ago(days=2), "RWT")]
    assert P.check("RWT", "", "", "", two, P.PolicyConfig(enforce=False), NOW).ok


def test_load_history_merges_audit_and_logged_own_alerts(tmp_path):
    audit = tmp_path / "audit.jsonl"
    events = tmp_path / "events.jsonl"
    audit.write_text("\n".join(json.dumps(e) for e in [
        {"time_utc": "2026-11-23T17:00:00Z", "action": "/api/control/originate", "detail": {"event": "RWT"}},
        {"time_utc": "2026-11-23T17:00:05Z", "action": "originate_sent", "detail": {"event": "RWT"}},
        {"time_utc": "2026-11-23T18:00:00Z", "action": "originate_blocked", "detail": {"event": "RWT"}},
    ]) + "\nnot json\n")
    events.write_text("\n".join(json.dumps(r) for r in [
        # the logger hears our own RWT a minute later → same origination
        {"received_utc": "2026-11-23T17:01:00Z", "sender": "KE2HPG  ", "event_code": "RWT"},
        # front-panel RWT the dashboard never saw
        {"received_utc": "2026-11-20T15:00:00Z", "sender": "ke2hpg", "event_code": "RWT"},
        # someone else's RWT doesn't count
        {"received_utc": "2026-11-22T15:00:00Z", "sender": "KBGM/NWS", "event_code": "RWT"},
    ]) + "\n")
    h = P.load_history(str(audit), str(events), "KE2HPG")
    assert [(t.strftime("%m-%d %H:%M"), c) for t, c in h] == [("11-20 15:00", "RWT"), ("11-23 17:00", "RWT")]
    assert not P.check("RWT", "EAS", "", "", h, CFG, NOW).ok
    # Unconfigured callsign: only the audit log counts
    assert len(P.load_history(str(audit), str(events), "STATION")) == 1


def test_two_quick_sends_count_twice_but_logger_echo_once(tmp_path):
    audit = tmp_path / "audit.jsonl"
    events = tmp_path / "events.jsonl"
    audit.write_text("".join(json.dumps({"time_utc": t, "action": "originate_sent",
                                         "detail": {"event": "RWT"}}) + "\n"
                             for t in ("2026-11-24T17:00:00Z", "2026-11-24T17:00:30Z")))
    events.write_text("".join(json.dumps({"received_utc": t, "sender": "KE2HPG", "event_code": "RWT"}) + "\n"
                              for t in ("2026-11-24T17:00:20Z", "2026-11-24T17:00:50Z")))
    h = P.load_history(str(audit), str(events), "KE2HPG")
    assert len(h) == 2
    assert not P.check("RWT", "EAS", "", "", h, CFG, NOW).ok
