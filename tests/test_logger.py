"""Tests for TFT_logger.py parsing, voting, expiry, dedupe and save-before-notify."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import TFT_logger as L  # noqa: E402

HDR = "ZCZC-WXR-TOR-036109+0030-3281415-KBGM/NWS-"


# ── issue / expiry times ───────────────────────────────────────────────────

def test_expiry_normal():
    recv = datetime(2026, 11, 24, 14, 16, tzinfo=timezone.utc)
    assert L.issued_utc(HDR, recv) == "2026-11-24T14:15:00Z"      # day 328 of 2026
    assert L.compute_expires_utc(HDR, recv) == "2026-11-24T14:45:00Z"


def test_expiry_year_rollover():
    # Issued Dec 31 23:50, decoded just after midnight Jan 1
    hdr  = "ZCZC-WXR-SVR-036109+0100-3652350-KBGM/NWS-"
    recv = datetime(2027, 1, 1, 0, 5, tzinfo=timezone.utc)
    assert L.issued_utc(hdr, recv) == "2026-12-31T23:50:00Z"
    assert L.compute_expires_utc(hdr, recv) == "2027-01-01T00:50:00Z"


def test_expiry_none_for_zero_duration():
    hdr = "ZCZC-PEP-EAN-000000+0000-3281415-WHITEHSE-"
    assert L.compute_expires_utc(hdr) is None


def test_expiry_garbage_header():
    assert L.compute_expires_utc("ZCZC-garbage-") is None
    assert L.issued_utc("ZCZC-WXR-TOR-036109+0030-9991415-X-") is None   # day 999


# ── majority vote ──────────────────────────────────────────────────────────

def test_vote_all_agree():
    assert L.vote_header([HDR, HDR, HDR]) == (HDR, 3)


def test_vote_two_of_three():
    bad = HDR.replace("TOR", "TQR")
    assert L.vote_header([bad, HDR, HDR]) == (HDR, 3)


def test_vote_per_character_when_all_differ():
    a = HDR.replace("TOR", "TQR")          # error in event code
    b = HDR.replace("036109", "036X09")    # error in FIPS
    c = HDR.replace("KBGM", "KBGN")        # error in sender
    assert L.vote_header([a, b, c]) == (HDR, 3)


def test_vote_single_copy():
    assert L.vote_header([HDR]) == (HDR, 1)


def test_vote_empty_raises():
    with pytest.raises(ValueError):
        L.vote_header([])


# ── burst extraction ───────────────────────────────────────────────────────

def test_extract_complete_burst():
    bursts, rest = L.extract_bursts("noise" + HDR * 3 + "NNNN" + "tail")
    assert bursts == [HDR * 3 + "NNNN"]
    assert rest == "ail"   # only a short tail is kept when there's no ZCZC


def test_extract_partial_burst_kept():
    bursts, rest = L.extract_bursts("junk" + HDR)
    assert bursts == [] and rest == HDR


def test_extract_split_across_reads():
    data = HDR * 3 + "NNNN"
    bursts, buf = L.extract_bursts(data[:20])
    assert bursts == []
    bursts, buf = L.extract_bursts(buf + data[20:])
    assert bursts == [data]


def test_noise_buffer_does_not_grow():
    _, rest = L.extract_bursts("x" * 10_000)
    assert len(rest) <= 3


def test_headers_in_burst():
    assert L.headers_in_burst(HDR * 3 + "NNNN") == [HDR, HDR, HDR]


# ── dedupe persistence ─────────────────────────────────────────────────────

def test_load_recent_fingerprints(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(
        json.dumps({"received_utc": "2026-11-24T14:15:00Z", "canonical_header": HDR,
                    "alert_id": "a1"}) + "\n" +
        json.dumps({"received_utc": "2026-11-24T10:00:00Z", "canonical_header": "ZCZC-OLD-"}) + "\n" +
        "not json\n"
    )
    now = datetime(2026, 11, 24, 14, 16, tzinfo=timezone.utc).timestamp()
    seen = L.load_recent_fingerprints(str(f), window=120, now=now)
    assert list(seen) == [L.event_key(HDR)]
    until, alert_id = seen[L.event_key(HDR)]
    assert alert_id == "a1"
    # HDR is issued 14:15 for +0030 → repeats suppressed until it expires at 14:45
    assert until == datetime(2026, 11, 24, 14, 45, tzinfo=timezone.utc).timestamp()


# ── relay-aware dedupe (NWR on CH1 + ERN relay on CH2) ─────────────────────

def test_event_key_ignores_sender():
    relayed = HDR.replace("KBGM/NWS", "ERNOWEN ")
    assert L.header_sender(HDR) == "KBGM/NWS"
    assert L.header_sender(relayed) == "ERNOWEN"
    assert L.event_key(HDR) == L.event_key(relayed)
    assert L.fingerprint(HDR) != L.fingerprint(relayed)
    # A different event, area, duration or issue time is a different alert
    for other in (HDR.replace("TOR", "SVR"), HDR.replace("036109", "036023"),
                  HDR.replace("+0030", "+0100"), HDR.replace("3281415", "3281420")):
        assert L.event_key(other) != L.event_key(HDR)


def test_suppress_until():
    recv = datetime(2026, 11, 24, 14, 16, tzinfo=timezone.utc)
    t = recv.timestamp()
    assert L.suppress_until(HDR, recv, 120) == t + 29 * 60          # until expiry (14:45)
    long = HDR.replace("+0030", "+9900")                              # 99 h duration
    assert L.suppress_until(long, recv, 120) == t + L.MAX_SUPPRESS   # capped at 6 h
    ean = "ZCZC-PEP-EAN-000000+0000-3281415-WHITEHSE-"
    assert L.suppress_until(ean, recv, 120) == t + 120                # no expiry → window


def test_relayed_copy_is_not_logged_twice(isolated, monkeypatch):
    monkeypatch.setattr(L, "NTFY_URL", "")
    seen = {}
    first = L.handle_burst(HDR * 3 + "NNNN", seen)
    assert first and first["sender"] == "KBGM/NWS"
    relayed = HDR.replace("KBGM/NWS", "ERNRELAY")
    assert L.handle_burst(relayed * 3 + "NNNN", seen) is None         # ERN copy on CH2
    assert len((isolated / "events.jsonl").read_text().splitlines()) == 1
    # Once the suppression window has passed, a new alert with that key logs again
    seen[L.event_key(HDR)] = (0.0, first["alert_id"])
    assert L.handle_burst(relayed * 3 + "NNNN", seen) is not None


def test_load_recent_fingerprints_missing_file(tmp_path):
    assert L.load_recent_fingerprints(str(tmp_path / "nope.jsonl"), 120) == {}


# ── handle_burst ───────────────────────────────────────────────────────────

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "JSONL_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(L, "TEXT_FILE",  str(tmp_path / "events.log"))
    return tmp_path


def test_handle_burst_writes_record_and_dedupes(isolated, monkeypatch):
    monkeypatch.setattr(L, "NTFY_URL", "")
    seen = {}
    rec = L.handle_burst(HDR * 3 + "NNNN", seen)
    assert rec["canonical_header"] == HDR
    assert rec["repeat_count"] == 3
    assert rec["raw_burst"].endswith("NNNN")
    assert rec["notification"] == {"attempted": False}
    lines = (isolated / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    # Same burst again inside the window is skipped
    assert L.handle_burst(HDR * 3 + "NNNN", seen) is None
    assert len((isolated / "events.jsonl").read_text().splitlines()) == 1


def test_record_saved_before_notification(isolated, monkeypatch):
    """The JSONL line must already exist when the ntfy push starts."""
    monkeypatch.setattr(L, "NTFY_URL", "https://ntfy.invalid/x")
    monkeypatch.setattr(L, "requests", object())
    seen_on_disk = []
    monkeypatch.setattr(L, "_notify_async",
        lambda title, body: seen_on_disk.append((isolated / "events.jsonl").exists()))
    L.handle_burst(HDR * 3 + "NNNN", {})
    assert seen_on_disk == [True]


def test_handle_burst_no_headers(isolated):
    assert L.handle_burst("ZCZC\x00\x01NNNN", {}) is None


# ── CLI ────────────────────────────────────────────────────────────────────

def test_parse_args_flags():
    assert L.parse_args(["--stdin"]).stdin
    assert L.parse_args(["--serial"]).serial
    with pytest.raises(SystemExit):
        L.parse_args(["--stdin", "--serial"])


def test_stdin_is_piped(tmp_path):
    """Input auto-detect must not depend on the hardware (Pi vs Proxmox VM)."""
    import os
    r, w = os.pipe()
    try:
        assert L.stdin_is_piped(r)                     # virtual_tft.py | TFT_logger.py
    finally:
        os.close(r); os.close(w)
    f = tmp_path / "feed.txt"; f.write_text("x")
    with open(f) as fh:
        assert L.stdin_is_piped(fh.fileno())           # TFT_logger.py < feed.txt
    with open(os.devnull) as fh:
        assert not L.stdin_is_piped(fh.fileno())       # systemd: stdin is /dev/null → serial
