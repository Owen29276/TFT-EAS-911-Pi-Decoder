#!/usr/bin/env python3
"""
Alert audio capture for the TFT EAS 911 logger.

A background thread runs `arecord` continuously into an in-memory ring buffer
(16 kHz mono = ~32 KB/s, so 3 minutes is ~6 MB — fine on a Pi 3B+). When the
logger sees "ZCZC" on the serial line it calls trigger(); the capture ends on
EOM (NNNN), on sustained silence, or at max_seconds, and the slice is written
as a WAV file. Nothing is saved unless the logger attaches a (non-duplicate)
alert record to the capture.

Modes (config.ini [audio] mode):
  forward   — the TFT prints the header when the alert *starts* (default).
              Audio is taken from just before the header until it ends.
  lookback  — the TFT prints the header only after the message ends.
              Audio is the lookback_seconds *before* the header.
"""

import array
import logging
import math
import os
import subprocess
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger("eas_logger.audio")


@dataclass
class AudioConfig:
    enabled:           bool  = False
    device:            str   = "default"
    sample_rate:       int   = 16000
    mode:              str   = "forward"    # forward | lookback
    pre_seconds:       float = 2.0
    min_seconds:       float = 8.0
    max_seconds:       float = 150.0
    silence_seconds:   float = 4.0
    silence_threshold: int   = 300          # RMS of int16 samples (~ -40 dBFS)
    post_eom_seconds:  float = 1.0
    lookback_seconds:  float = 150.0
    attach_timeout:    float = 30.0         # wait this long for the logger to attach a record
    audio_dir:         str   = "alerts/audio"
    keep_files:        int   = 200

    @classmethod
    def from_parser(cls, cp, base_dir: Path) -> "AudioConfig":
        s = "audio"
        if not cp.has_section(s):
            return cls()
        d = cls()
        cfg = cls(
            enabled=cp.getboolean(s, "enabled", fallback=d.enabled),
            device=cp.get(s, "device", fallback=d.device),
            sample_rate=cp.getint(s, "sample_rate", fallback=d.sample_rate),
            mode=cp.get(s, "mode", fallback=d.mode).strip().lower(),
            pre_seconds=cp.getfloat(s, "pre_seconds", fallback=d.pre_seconds),
            min_seconds=cp.getfloat(s, "min_seconds", fallback=d.min_seconds),
            max_seconds=cp.getfloat(s, "max_seconds", fallback=d.max_seconds),
            silence_seconds=cp.getfloat(s, "silence_seconds", fallback=d.silence_seconds),
            silence_threshold=cp.getint(s, "silence_threshold", fallback=d.silence_threshold),
            post_eom_seconds=cp.getfloat(s, "post_eom_seconds", fallback=d.post_eom_seconds),
            lookback_seconds=cp.getfloat(s, "lookback_seconds", fallback=d.lookback_seconds),
            attach_timeout=cp.getfloat(s, "attach_timeout", fallback=d.attach_timeout),
            audio_dir=cp.get(s, "audio_dir", fallback=d.audio_dir),
            keep_files=cp.getint(s, "keep_files", fallback=d.keep_files),
        )
        p = os.path.expanduser(cfg.audio_dir)
        cfg.audio_dir = p if os.path.isabs(p) else str(base_dir / p)
        if cfg.mode not in ("forward", "lookback"):
            logger.warning(f"Unknown [audio] mode '{cfg.mode}' — using forward")
            cfg.mode = "forward"
        return cfg

    @property
    def ring_seconds(self) -> float:
        # Enough history for a full lookback or pre-roll + the longest forward capture
        return max(self.lookback_seconds, self.pre_seconds + self.max_seconds) + 10


# ── ring buffer ────────────────────────────────────────────────────────────

class PcmRing:
    """
    Fixed-duration ring of 16-bit mono PCM chunks, each stamped with the
    monotonic time its *last* sample arrived. Thread-safe.
    """

    def __init__(self, sample_rate: int, seconds: float):
        self.rate      = sample_rate
        self.max_bytes = int(sample_rate * 2 * seconds)
        self._chunks: deque[tuple[float, bytes]] = deque()
        self._bytes = 0
        self._lk = threading.Lock()

    def append(self, data: bytes, t: float) -> None:
        if len(data) % 2:
            data = data[:-1]
        if not data:
            return
        with self._lk:
            self._chunks.append((t, data))
            self._bytes += len(data)
            while self._bytes > self.max_bytes and self._chunks:
                _, old = self._chunks.popleft()
                self._bytes -= len(old)

    def slice(self, t0: float, t1: float) -> bytes:
        """PCM covering [t0, t1], at chunk granularity."""
        out = bytearray()
        with self._lk:
            for t_end, data in self._chunks:
                t_start = t_end - len(data) / (2 * self.rate)
                if t_end >= t0 and t_start <= t1:
                    out += data
        return bytes(out)

    def rms(self, t0: float, t1: float) -> float:
        pcm = self.slice(t0, t1)
        if len(pcm) < 2:
            return 0.0
        # Every 4th sample is plenty for a loudness estimate and keeps a Pi 3B+ idle
        samples = array.array("h", pcm)[::4]
        return math.sqrt(sum(s * s for s in samples) / len(samples))

    def latest_time(self) -> float | None:
        with self._lk:
            return self._chunks[-1][0] if self._chunks else None


# ── recorder thread ────────────────────────────────────────────────────────

class Recorder:
    """Runs arecord forever (restarting if the USB card drops) and feeds a PcmRing."""

    CHUNK = 3200  # 0.1 s at 16 kHz mono 16-bit

    def __init__(self, cfg: AudioConfig, ring: PcmRing,
                 popen: Callable = subprocess.Popen, clock: Callable[[], float] = time.monotonic,
                 sinks: list[Callable[[bytes], None]] | None = None):
        self.cfg, self.ring, self._popen, self._clock = cfg, ring, popen, clock
        self.sinks = list(sinks or [])      # e.g. LiveStream.write — must not block
        self._stop = threading.Event()
        self._proc = None
        self.running = False

    def command(self) -> list[str]:
        return ["arecord", "-q", "-D", self.cfg.device, "-f", "S16_LE",
                "-c", "1", "-r", str(self.cfg.sample_rate), "-t", "raw"]

    def start(self) -> None:
        threading.Thread(target=self._run, name="audio-recorder", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._proc = self._popen(self.command(), stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
                logger.info(f"Audio recording from ALSA device '{self.cfg.device}' "
                            f"@ {self.cfg.sample_rate} Hz")
                self.running = True
                while not self._stop.is_set():
                    data = self._proc.stdout.read(self.CHUNK)
                    if not data:
                        break
                    self.ring.append(data, self._clock())
                    for sink in self.sinks:
                        try:
                            sink(data)
                        except Exception:
                            logger.exception("Audio sink failed")
                err = b""
                try: err = self._proc.stderr.read() or b""
                except Exception: pass
                if not self._stop.is_set():
                    logger.error(f"arecord exited: {err.decode(errors='ignore').strip() or 'no output'}"
                                 " — retrying in 5 s")
            except FileNotFoundError:
                logger.error("arecord not found — install alsa-utils. Audio capture disabled.")
                self.running = False
                return
            except Exception as e:
                logger.error(f"Audio recorder error: {e} — retrying in 5 s")
            self.running = False
            self._stop.wait(5)


# ── WAV helpers ────────────────────────────────────────────────────────────

def write_wav(path: str, pcm: bytes, sample_rate: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    os.replace(tmp, path)


def prune_dir(directory: str, keep: int, suffix: str = ".wav") -> int:
    """Delete the oldest files beyond `keep`. Returns how many were removed."""
    try:
        files = sorted((p for p in Path(directory).iterdir() if p.suffix == suffix),
                       key=lambda p: p.stat().st_mtime)
    except FileNotFoundError:
        return 0
    extra = files[:-keep] if keep > 0 else files
    for p in extra:
        try: p.unlink()
        except OSError: pass
    return len(extra)


# ── capture controller ─────────────────────────────────────────────────────

class AlertAudio:
    """
    Coordinates one capture at a time.

        trigger()      — "ZCZC" seen on serial; starts a capture if none is running
        go_live(hdr)   — first complete header of this capture is new (not a repeat)
        note_eom()     — "NNNN" seen on serial
        attach(rec)    — the logger saved a new alert; the capture belongs to it

    When a capture finishes and has a record attached, on_ready(record, wav_path)
    is called from the capture thread.

    on_air(True, header) / on_air(False, None) bracket the live part of the
    message (forward mode only), so Home Assistant can play the live stream.
    """

    def __init__(self, cfg: AudioConfig, ring: PcmRing,
                 on_ready: Callable[[dict, str], None],
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 on_air: Callable[[bool, str | None], None] | None = None):
        self.cfg, self.ring, self.on_ready = cfg, ring, on_ready
        self.on_air = on_air
        self._live = False
        self._clock, self._sleep = clock, sleep
        self._lk = threading.Lock()
        self._active = False
        self._t0 = 0.0
        self._eom_t: float | None = None
        self._record: dict | None = None
        self._discard = False
        self._record_evt = threading.Event()

    @property
    def active(self) -> bool:
        return self._active

    def trigger(self) -> bool:
        with self._lk:
            if self._active:
                return False
            self._active = True
            self._t0 = self._clock()
            self._eom_t = None
            self._record = None
            self._discard = False
            self._record_evt.clear()
        logger.info(f"Audio capture started ({self.cfg.mode} mode)")
        threading.Thread(target=self._run_capture, name="audio-capture", daemon=True).start()
        return True

    def go_live(self, header: str) -> bool:
        """Signal on-air for the running capture. Only meaningful in forward mode."""
        with self._lk:
            if not self._active or self._live or self.cfg.mode != "forward" or not self.on_air:
                return False
            self._live = True
        self._notify_air(True, header)
        return True

    def _notify_air(self, on: bool, header: str | None) -> None:
        try:
            self.on_air(on, header)     # type: ignore[misc]
        except Exception:
            logger.exception("on_air callback failed")

    def _end_live(self) -> None:
        with self._lk:
            was, self._live = self._live, False
        if was:
            self._notify_air(False, None)

    def note_eom(self) -> None:
        with self._lk:
            if self._active and self._eom_t is None:
                self._eom_t = self._clock()

    def attach(self, record: dict) -> None:
        with self._lk:
            if self._active and self._record is None:
                self._record = record
                self._record_evt.set()

    def discard(self) -> None:
        """The burst for this capture was a duplicate / unparseable — drop the audio now."""
        with self._lk:
            if self._active and self._record is None:
                self._discard = True
                self._record_evt.set()

    def _wait_for_record(self) -> dict | None:
        """
        Wait for the logger to attach the alert record.

        In forward mode the record is only created when NNNN arrives at the *end*
        of the message, which can be well after a silence-ended capture — so wait
        until max_seconds after the header (plus slack), not just attach_timeout.
        """
        if self.cfg.mode == "forward":
            deadline = self._t0 + self.cfg.max_seconds + self.cfg.attach_timeout
        else:
            deadline = self._clock() + self.cfg.attach_timeout
        while not self._record_evt.is_set() and self._clock() < deadline:
            self._sleep(0.25)
        return None if self._discard else self._record

    # -- capture thread --

    def _capture_window(self) -> tuple[float, float]:
        """Block until the capture should end; return (t_start, t_end) in monotonic time."""
        c, t0 = self.cfg, self._t0
        if c.mode == "lookback":
            return t0 - c.lookback_seconds, t0 + c.post_eom_seconds
        while True:
            now = self._clock()
            elapsed = now - t0
            if elapsed >= c.max_seconds:
                logger.info("Audio capture hit max_seconds")
                return t0 - c.pre_seconds, now
            if elapsed >= c.min_seconds:
                eom = self._eom_t
                if eom is not None and now >= eom + c.post_eom_seconds:
                    logger.info("Audio capture ended on EOM")
                    return t0 - c.pre_seconds, now
                if self.ring.rms(now - c.silence_seconds, now) < c.silence_threshold:
                    logger.info("Audio capture ended on silence")
                    return t0 - c.pre_seconds, now - c.silence_seconds + 0.5
            self._sleep(0.25)

    def _run_capture(self) -> None:
        try:
            if self.cfg.mode == "lookback":
                self._sleep(self.cfg.post_eom_seconds)
            t_start, t_end = self._capture_window()
            self._end_live()
            pcm = self.ring.slice(t_start, t_end)

            record = self._wait_for_record()
            if record is None:
                logger.info("Audio capture discarded — no new alert was logged (duplicate or bad burst)")
                return
            if not pcm:
                logger.warning("Audio capture empty — is the recorder running / device wired?")
                return
            secs = len(pcm) / (2 * self.cfg.sample_rate)
            path = os.path.join(self.cfg.audio_dir, f"{record['alert_id']}.wav")
            write_wav(path, pcm, self.cfg.sample_rate)
            removed = prune_dir(self.cfg.audio_dir, self.cfg.keep_files)
            logger.info(f"Saved alert audio {os.path.basename(path)} ({secs:.1f} s)"
                        + (f", pruned {removed} old file(s)" if removed else ""))
            self.on_ready(record, path)
        except Exception:
            logger.exception("Audio capture failed")
        finally:
            self._end_live()
            with self._lk:
                self._active = False


def start_audio(cfg: AudioConfig, on_ready: Callable[[dict, str], None],
                sinks: list[Callable[[bytes], None]] | None = None,
                on_air: Callable[[bool, str | None], None] | None = None) -> AlertAudio | None:
    """Build and start the recorder + capture controller. Returns None if disabled."""
    if not cfg.enabled:
        return None
    ring = PcmRing(cfg.sample_rate, cfg.ring_seconds)
    Recorder(cfg, ring, sinks=sinks).start()
    return AlertAudio(cfg, ring, on_ready, on_air=on_air)
