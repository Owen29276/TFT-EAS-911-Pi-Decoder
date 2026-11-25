"""Tests for live_stream.py and the On air signal (audio_capture, ha_mqtt, logger)."""

import configparser
import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import live_stream as S  # noqa: E402
import audio_capture as A  # noqa: E402
import ha_mqtt as H  # noqa: E402

TOR = "ZCZC-WXR-TOR-036109-036023+0030-3281415-KBGM/NWS-"


# ── config / command ───────────────────────────────────────────────────────

def test_config_from_parser_reads_secret():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string("[stream]\nenabled = true\nmount = eas.mp3\nsource_password = p@ss:w/rd\n")
    c = S.StreamConfig.from_parser(cp)
    assert c.enabled and c.mount == "/eas.mp3" and c.source_password == "p@ss:w/rd"
    assert S.StreamConfig.from_parser(configparser.ConfigParser()).enabled is False


def test_ffmpeg_command_escapes_password_and_redacts():
    c = S.StreamConfig(source_password="p@ss:w/rd")
    cmd = S.ffmpeg_command(c, 16000)
    url = cmd[-1]
    assert url == "icecast://source:p%40ss%3Aw%2Frd@127.0.0.1:8000/tft911.mp3"
    assert cmd[cmd.index("-ar") + 1] == "16000"           # input rate
    assert "44100" in cmd and "libmp3lame" in cmd
    assert "p%40ss" not in S.redact(cmd)


def test_listen_url():
    assert S.listen_url(S.StreamConfig(public_url="http://eas.lan/x.mp3")) == "http://eas.lan/x.mp3"
    assert S.listen_url(S.StreamConfig(icecast_host="10.0.0.5")) == "http://10.0.0.5:8000/tft911.mp3"
    # loopback Icecast → speakers get this machine's LAN address, never 127.0.0.1
    for probe in ("192.0.2.1", "127.0.0.1"):      # MQTT host may itself be local
        url = S.listen_url(S.StreamConfig(), probe_host=probe)
        assert url.endswith(":8000/tft911.mp3") and "127.0.0.1" not in url


def test_start_stream_requires_password():
    assert S.start_stream(S.StreamConfig(enabled=False), 16000) is None
    assert S.start_stream(S.StreamConfig(enabled=True, source_password=""), 16000) is None


# ── LiveStream with a fake ffmpeg ──────────────────────────────────────────

class FakeProc:
    def __init__(self, fail_after=None):
        self.written = bytearray()
        self.fail_after = fail_after
        self.returncode = None
        outer = self

        class In:
            def write(self, b):
                if outer.fail_after is not None and len(outer.written) >= outer.fail_after:
                    outer.returncode = 1
                    raise BrokenPipeError
                outer.written += b
            def flush(self): pass
            def close(self): pass
        self.stdin = In()
        self.stderr = io.BytesIO(b"Connection refused\n")

    def poll(self): return self.returncode
    def wait(self, timeout=None): return self.returncode
    def terminate(self): self.returncode = -15


def wait_until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_live_stream_pipes_chunks_and_restarts():
    procs = []
    def popen(cmd, **kw):
        procs.append(FakeProc(fail_after=4 if not procs else None))
        return procs[-1]
    ls = S.LiveStream(S.StreamConfig(source_password="x"), 16000, popen=popen)
    ls.RETRY_SECONDS = 0.01
    ls.start()
    assert wait_until(lambda: ls.running)
    for _ in range(3):
        ls.write(b"ab")
    assert wait_until(lambda: len(procs) >= 2 and procs[1] is not None)   # 1st died → restarted
    assert wait_until(lambda: ls.running)
    ls.write(b"zz")
    assert wait_until(lambda: b"zz" in procs[1].written)
    ls.stop()


def test_write_never_blocks_when_ffmpeg_stalls():
    ls = S.LiveStream(S.StreamConfig(source_password="x"), 16000, popen=lambda *a, **k: None)
    t = time.time()
    for _ in range(ls.QUEUE_CHUNKS + 25):
        ls.write(b"\0" * 3200)          # nothing is consuming
    assert time.time() - t < 0.5
    assert ls.dropped == 25


def test_ffmpeg_missing_stops_quietly():
    def popen(*a, **k):
        raise FileNotFoundError
    ls = S.LiveStream(S.StreamConfig(source_password="x"), 16000, popen=popen)
    th = threading.Thread(target=ls._run)
    th.start(); th.join(2)
    assert not th.is_alive() and not ls.running


# ── recorder tee ───────────────────────────────────────────────────────────

def test_recorder_feeds_sinks():
    cfg = A.AudioConfig(enabled=True)
    ring = A.PcmRing(16000, 5)
    got = []
    class P:
        def __init__(self, *a, **k):
            self.stdout = io.BytesIO(b"\1\0" * 3200)
            self.stderr = io.BytesIO(b"")
    rec = A.Recorder(cfg, ring, popen=P, sinks=[got.append, lambda b: 1 / 0])
    rec._stop.wait = lambda s: rec._stop.set()      # one pass, then exit
    rec._run()
    assert b"".join(got) == b"\1\0" * 3200           # failing sink didn't break the good one
    assert ring.latest_time() is not None


# ── On air bracketing in AlertAudio ────────────────────────────────────────

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


def make_audio(mode="forward"):
    clock = Clock()
    cfg = A.AudioConfig(enabled=True, mode=mode, min_seconds=1, max_seconds=3,
                        attach_timeout=0.01, audio_dir="/nonexistent")
    ring = A.PcmRing(16000, cfg.ring_seconds)
    events = []
    aa = A.AlertAudio(cfg, ring, on_ready=lambda r, p: None, clock=clock, sleep=clock.sleep,
                      on_air=lambda on, h: events.append((on, h)))
    return aa, events


def test_on_air_brackets_live_capture(monkeypatch):
    aa, events = make_audio()
    started = threading.Event()
    real = A.threading.Thread
    class Held:     # hold the capture thread until go_live has been called
        def __init__(self, target, **kw): self.target = target
        def start(self): real(target=lambda: (started.wait(2), self.target())).start()
    monkeypatch.setattr(A.threading, "Thread", Held)
    assert aa.trigger()
    assert aa.go_live(TOR) is True
    assert aa.go_live(TOR) is False                  # once per capture
    started.set()
    assert wait_until(lambda: not aa.active)
    assert events == [(True, TOR), (False, None)]


def test_go_live_ignored_when_idle_or_lookback():
    aa, events = make_audio()
    assert aa.go_live(TOR) is False                  # no capture running
    aa2, ev2 = make_audio("lookback")
    aa2._active = True
    assert aa2.go_live(TOR) is False and ev2 == []


# ── Home Assistant messages ────────────────────────────────────────────────

def test_on_air_discovery_and_messages():
    cfg = H.MQTTConfig(local_fips=["036109"])
    disc = H.discovery_configs(cfg)
    oa = disc["homeassistant/binary_sensor/tft911/on_air/config"]
    assert oa["state_topic"] == "tft911/on_air/state" and oa["device_class"] == "sound"

    msgs = dict((t, p) for t, p, _ in H.on_air_messages(cfg, True, TOR, "http://10.0.0.5:8000/tft911.mp3"))
    attrs = json.loads(msgs["tft911/on_air/attributes"])
    assert msgs["tft911/on_air/state"] == "ON"
    assert attrs["event_code"] == "TOR" and attrs["category"] == "warning" and attrs["local"] is True
    assert attrs["sender"] == "KBGM/NWS" and attrs["stream_url"].endswith("/tft911.mp3")
    named = dict((t, p) for t, p, _ in H.on_air_messages(cfg, True, TOR, event_text="a Tornado Warning"))
    assert json.loads(named["tft911/on_air/attributes"])["event_name"] == "Tornado Warning"
    off = H.on_air_messages(cfg, False, None)
    assert off == [("tft911/on_air/state", "OFF", True)]   # keeps last attributes


def test_header_sender_single_location():
    assert H.header_sender("ZCZC-WXR-RWT-036109+0015-3281415-KBGM/NWS-") == "KBGM/NWS"
    assert H.header_sender("garbage") == ""


# ── logger: early header + repeat check ────────────────────────────────────

@pytest.fixture
def L(tmp_path, monkeypatch):
    import TFT_logger as L
    return L


def test_first_complete_header(L):
    assert L.first_complete_header("ZCZC-WXR-TOR-036109+0030-3281415-KBG") is None   # still arriving
    assert L.first_complete_header("ZCZC-WXR-TOR-036109+0030-3281415-KBGM/NWS-ZCZC-WX") == \
        "ZCZC-WXR-TOR-036109+0030-3281415-KBGM/NWS-"
    assert L.first_complete_header("ZCZC-WXR-TOR-036109-036023+0030-3281415-KBGM/NWS-") == TOR


def test_is_recent_repeat(L):
    seen = {L.event_key(TOR): (2000.0, "a1")}
    relayed = TOR.replace("KBGM/NWS", "ERNRELAY")
    assert L.is_recent_repeat(relayed, seen, now=1999.0)
    assert not L.is_recent_repeat(relayed, seen, now=2001.0)          # suppression over
    assert not L.is_recent_repeat(TOR.replace("TOR", "SVR"), seen, now=1999.0)


def test_header_event_text(L):
    if not L.EAS2TEXT_AVAILABLE:
        pytest.skip("EAS2Text not installed")
    assert "Tornado Warning" in L.header_event_text(TOR)
    assert L.header_event_text("ZCZC-garbage-") == ""
