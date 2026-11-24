"""Tests for audio_capture.py — ring buffer, capture windows, WAV output."""

import array
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import audio_capture as A  # noqa: E402

RATE = 16000
CHUNK_S = 0.1


def pcm(amplitude: int, seconds: float) -> bytes:
    n = int(RATE * seconds)
    return array.array("h", [amplitude if i % 2 else -amplitude for i in range(n)]).tobytes()


def fill(ring: A.PcmRing, t_from: float, t_to: float, amplitude: int) -> None:
    """Append 0.1 s chunks covering (t_from, t_to], stamped with each chunk's end time."""
    t = t_from
    while t < t_to - 1e-9:
        t += CHUNK_S
        ring.append(pcm(amplitude, CHUNK_S), round(t, 3))


class FakeClock:
    def __init__(self, t: float):
        self.t = t
        self.on_sleep = []          # callables run on the first sleep (simulate the logger)

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        pending, self.on_sleep = self.on_sleep, []   # callbacks may re-queue for the next sleep
        for fn in pending:
            fn()
        self.t += s


@pytest.fixture
def sync_threads(monkeypatch):
    """Run AlertAudio's capture thread inline so tests are deterministic."""
    class InlineThread:
        def __init__(self, target, **_kw):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(A.threading, "Thread", InlineThread)


def make(tmp_path, clock, **over):
    cfg = A.AudioConfig(**{"enabled": True, "sample_rate": RATE, "audio_dir": str(tmp_path / "audio"),
                           "attach_timeout": 0.01, **over})
    ring = A.PcmRing(RATE, cfg.ring_seconds)
    ready = []
    audio = A.AlertAudio(cfg, ring, lambda rec, path: ready.append((rec, path)),
                         clock=clock, sleep=clock.sleep)
    return cfg, ring, audio, ready


def wav_len(path) -> float:
    with wave.open(path) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return w.getnframes() / RATE


# ── PcmRing ────────────────────────────────────────────────────────────────

def test_ring_slice_and_rms():
    ring = A.PcmRing(RATE, 60)
    fill(ring, 0, 10, 1000)
    fill(ring, 10, 20, 0)
    assert len(ring.slice(2, 4)) == pytest.approx(RATE * 2 * 2.1, rel=0.06)
    assert ring.rms(5, 9) == pytest.approx(1000, rel=0.01)
    assert ring.rms(15, 19) == 0


def test_ring_drops_old_audio():
    ring = A.PcmRing(RATE, 5)
    fill(ring, 0, 20, 500)
    assert ring.slice(0, 10) == b""
    assert len(ring.slice(15, 20)) <= RATE * 2 * 5


def test_ring_odd_bytes_trimmed():
    ring = A.PcmRing(RATE, 5)
    ring.append(b"\x01\x02\x03", 1.0)
    assert ring.slice(0, 2) == b"\x01\x02"


# ── capture windows ────────────────────────────────────────────────────────

REC = {"alert_id": "20261124T155745Z-abcd1234", "event_code": "TOR"}


def test_forward_ends_on_silence(tmp_path, sync_threads):
    clock = FakeClock(100.0)
    cfg, ring, audio, ready = make(tmp_path, clock)
    fill(ring, 90, 130, 2000)     # alert audio 90–130 s
    fill(ring, 130, 200, 0)       # then silence (ring holds ~162 s)
    clock.on_sleep.append(lambda: audio.attach(REC))
    audio.trigger()
    assert len(ready) == 1
    rec, path = ready[0]
    assert rec is REC and path.endswith(f"{REC['alert_id']}.wav")
    # pre_seconds (2) before trigger through ~0.5 s after the audio stopped
    # (chunk granularity: 97.9 s → 130.8 s)
    assert wav_len(path) == pytest.approx(130.8 - 97.9, abs=0.3)
    assert not audio.active


def test_forward_ends_on_eom(tmp_path, sync_threads):
    clock = FakeClock(100.0)
    cfg, ring, audio, ready = make(tmp_path, clock)
    fill(ring, 90, 200, 2000)     # never silent
    def logger_side():
        audio.attach(REC)
        clock.t = 140.0
        audio.note_eom()
    clock.on_sleep.append(logger_side)
    audio.trigger()
    _, path = ready[0]
    assert wav_len(path) == pytest.approx(141.25 - 98, abs=0.4)


def test_forward_hits_max_seconds(tmp_path, sync_threads):
    clock = FakeClock(100.0)
    cfg, ring, audio, ready = make(tmp_path, clock, max_seconds=20)
    fill(ring, 90, 200, 2000)
    clock.on_sleep.append(lambda: audio.attach(REC))
    audio.trigger()
    assert wav_len(ready[0][1]) == pytest.approx(22, abs=0.4)


def test_silence_ignored_before_min_seconds(tmp_path, sync_threads):
    clock = FakeClock(100.0)
    cfg, ring, audio, ready = make(tmp_path, clock, min_seconds=8)
    fill(ring, 90, 200, 0)        # dead air from the start
    clock.on_sleep.append(lambda: audio.attach(REC))
    audio.trigger()
    # Can't end before 8 s even though it's silent the whole time
    assert wav_len(ready[0][1]) >= 8 - 4


def test_lookback_mode(tmp_path, sync_threads):
    clock = FakeClock(200.0)
    cfg, ring, audio, ready = make(tmp_path, clock, mode="lookback", lookback_seconds=60)
    fill(ring, 0, 205, 1500)
    clock.on_sleep.append(lambda: audio.attach(REC))
    audio.trigger()
    assert wav_len(ready[0][1]) == pytest.approx(61, abs=0.4)


def test_record_arriving_long_after_silence_is_kept(tmp_path, sync_threads):
    """Regression: TFT prints NNNN (→ record) ~2 min after the header; audio must not be dropped."""
    clock = FakeClock(100.0)
    cfg, ring, audio, ready = make(tmp_path, clock, attach_timeout=5)
    fill(ring, 90, 130, 2000)
    fill(ring, 130, 200, 0)
    def tick():
        if clock.t >= 230:            # 96 s after the capture ended on silence
            audio.attach(REC)
        else:
            clock.on_sleep.append(tick)
    clock.on_sleep.append(tick)
    audio.trigger()
    assert len(ready) == 1


def test_discard_frees_capture_immediately(tmp_path, sync_threads):
    clock = FakeClock(100.0)
    cfg, ring, audio, ready = make(tmp_path, clock)
    fill(ring, 90, 200, 0)
    clock.on_sleep.append(audio.discard)    # logger: burst was a duplicate
    audio.trigger()
    assert ready == [] and not audio.active
    assert clock.t < 120                    # didn't sit out the full max_seconds window


def test_discarded_without_attach(tmp_path, sync_threads):
    clock = FakeClock(100.0)
    cfg, ring, audio, ready = make(tmp_path, clock)
    fill(ring, 90, 200, 0)
    audio.trigger()               # duplicate burst: logger never attaches a record
    assert ready == []
    assert not (tmp_path / "audio").exists()
    assert not audio.active


def test_trigger_idempotent_while_active():
    clock = FakeClock(0)
    cfg = A.AudioConfig(enabled=True)
    audio = A.AlertAudio(cfg, A.PcmRing(RATE, 10), lambda *_: None, clock=clock, sleep=clock.sleep)
    audio._active = True
    assert audio.trigger() is False


def test_empty_ring_saves_nothing(tmp_path, sync_threads):
    clock = FakeClock(100.0)
    cfg, ring, audio, ready = make(tmp_path, clock)
    clock.on_sleep.append(lambda: audio.attach(REC))
    audio.trigger()
    assert ready == []


# ── helpers ────────────────────────────────────────────────────────────────

def test_prune_dir_keeps_newest(tmp_path):
    import os, time
    for i in range(5):
        p = tmp_path / f"{i}.wav"
        p.write_bytes(b"x")
        os.utime(p, (time.time() + i, time.time() + i))
    (tmp_path / "keep.txt").write_text("not audio")
    assert A.prune_dir(str(tmp_path), keep=2) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.wav", "4.wav", "keep.txt"]


def test_config_from_parser(tmp_path):
    import configparser
    cp = configparser.ConfigParser()
    cp.read_string("[audio]\nenabled = yes\ndevice = plughw:1,0\nmode = LOOKBACK\naudio_dir = rel/dir\n")
    cfg = A.AudioConfig.from_parser(cp, tmp_path)
    assert cfg.enabled and cfg.device == "plughw:1,0" and cfg.mode == "lookback"
    assert cfg.audio_dir == str(tmp_path / "rel/dir")
    cp.read_string("[audio]\nmode = bogus\n")
    assert A.AudioConfig.from_parser(cp, tmp_path).mode == "forward"


def test_recorder_feeds_ring_and_handles_missing_arecord():
    ring = A.PcmRing(RATE, 10)
    cfg = A.AudioConfig(enabled=True)

    class FakeProc:
        def __init__(self):
            import io
            self.stdout = io.BytesIO(pcm(100, 0.3))
            self.stderr = io.BytesIO(b"")
    calls = []
    def popen(cmd, **_kw):
        calls.append(cmd)
        if len(calls) > 1:
            raise FileNotFoundError
        return FakeProc()
    rec = A.Recorder(cfg, ring, popen=popen, clock=lambda: 5.0)
    rec._stop.wait = lambda _t: False     # don't actually sleep between retries
    rec._run()
    assert calls[0][0] == "arecord" and "-r" in calls[0]
    assert len(ring.slice(0, 10)) == len(pcm(100, 0.3))
    assert rec.running is False
