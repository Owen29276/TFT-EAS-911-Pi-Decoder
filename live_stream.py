#!/usr/bin/env python3
"""
Live audio stream for the TFT EAS 911 logger.

The same arecord that feeds alert capture (audio_capture.Recorder) also hands
every chunk to LiveStream, which pipes it into ffmpeg → MP3 → a local Icecast
server. The stream runs continuously (silence between alerts), so a Sonos /
Apple TV told to play it by Home Assistant can connect instantly.

    USB card in → arecord → PcmRing (alert clips)
                          └→ LiveStream → ffmpeg → icecast://…/tft911.mp3

The recorder must never block on the stream: chunks go through a bounded
queue and are dropped if ffmpeg or Icecast stalls.

Config: [stream] in config.ini, source password in secrets.ini [stream].
Requires [audio] enabled = true (it shares the recorder).
"""

import logging
import queue
import socket
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

logger = logging.getLogger("eas_logger.stream")


@dataclass
class StreamConfig:
    enabled:         bool = False
    icecast_host:    str  = "127.0.0.1"
    icecast_port:    int  = 8000
    mount:           str  = "/tft911.mp3"
    source_user:     str  = "source"
    source_password: str  = ""           # from secrets.ini
    public_url:      str  = ""           # URL speakers open; derived if empty
    bitrate:         str  = "64k"
    out_rate:        int  = 44100        # resample: some speakers reject 16 kHz MP3
    name:            str  = "TFT EAS 911"

    @classmethod
    def from_parser(cls, cp) -> "StreamConfig":
        s, d = "stream", cls()
        if not cp.has_section(s):
            return d
        mount = cp.get(s, "mount", fallback=d.mount).strip() or d.mount
        return cls(
            enabled=cp.getboolean(s, "enabled", fallback=False),
            icecast_host=cp.get(s, "icecast_host", fallback=d.icecast_host).strip() or d.icecast_host,
            icecast_port=cp.getint(s, "icecast_port", fallback=d.icecast_port),
            mount=mount if mount.startswith("/") else "/" + mount,
            source_user=cp.get(s, "source_user", fallback=d.source_user).strip() or d.source_user,
            source_password=cp.get(s, "source_password", fallback=""),
            public_url=cp.get(s, "public_url", fallback="").strip(),
            bitrate=cp.get(s, "bitrate", fallback=d.bitrate).strip() or d.bitrate,
            out_rate=cp.getint(s, "out_rate", fallback=d.out_rate),
            name=cp.get(s, "name", fallback=d.name).strip() or d.name,
        )


def lan_ip(probe_host: str = "192.0.2.1") -> str:
    """This machine's LAN address (no packets are sent — UDP connect just picks a route)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((probe_host, 9))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def listen_url(cfg: StreamConfig, probe_host: str = "192.0.2.1") -> str:
    """The http:// URL Home Assistant hands to the speakers."""
    if cfg.public_url:
        return cfg.public_url
    host = cfg.icecast_host
    if host in ("127.0.0.1", "localhost", "::1", ""):
        host = lan_ip(probe_host)       # speakers can't reach our loopback
        if host.startswith("127.") or host == "::1":
            host = lan_ip()             # probe was itself local (e.g. MQTT on this box)
    return f"http://{host}:{cfg.icecast_port}{cfg.mount}"


def ffmpeg_command(cfg: StreamConfig, in_rate: int) -> list[str]:
    url = (f"icecast://{quote(cfg.source_user, safe='')}:{quote(cfg.source_password, safe='')}"
           f"@{cfg.icecast_host}:{cfg.icecast_port}{cfg.mount}")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "s16le", "-ar", str(in_rate), "-ac", "1", "-i", "pipe:0",
        "-ar", str(cfg.out_rate), "-ac", "1",
        "-c:a", "libmp3lame", "-b:a", cfg.bitrate,
        "-content_type", "audio/mpeg", "-ice_name", cfg.name,
        "-flush_packets", "1", "-f", "mp3", url,
    ]


def redact(cmd: list[str]) -> str:
    return " ".join("icecast://***" if c.startswith("icecast://") else c for c in cmd)


class LiveStream:
    """Feeds raw PCM chunks to ffmpeg, restarting it if it (or Icecast) goes away."""

    QUEUE_CHUNKS: int = 50          # ~5 s of 0.1 s chunks
    RETRY_SECONDS: float = 5

    def __init__(self, cfg: StreamConfig, in_rate: int, popen: Callable = subprocess.Popen):
        self.cfg, self.in_rate, self._popen = cfg, in_rate, popen
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=self.QUEUE_CHUNKS)
        self._stop = threading.Event()
        self._proc = None
        self.running = False
        self.dropped = 0

    # Called from the recorder thread for every chunk — must never block.
    def write(self, chunk: bytes) -> None:
        try:
            self._q.put_nowait(chunk)
        except queue.Full:
            self.dropped += 1

    def start(self) -> None:
        threading.Thread(target=self._run, name="live-stream", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.terminate()
            except Exception:
                pass

    def _drain(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        cmd = ffmpeg_command(self.cfg, self.in_rate)
        while not self._stop.is_set():
            try:
                self._proc = self._popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
            except FileNotFoundError:
                logger.error("ffmpeg not found — install it (sudo apt install ffmpeg). Live stream off.")
                return
            except Exception as e:
                logger.error(f"Live stream: could not start ffmpeg: {e}")
                self._stop.wait(self.RETRY_SECONDS)
                continue
            proc = self._proc
            logger.info(f"Live stream → http://{self.cfg.icecast_host}:{self.cfg.icecast_port}"
                        f"{self.cfg.mount} ({self.cfg.bitrate} MP3)")
            self._drain()           # don't open with stale audio
            self.running = True
            try:
                while not self._stop.is_set():
                    try:
                        chunk = self._q.get(timeout=1)
                    except queue.Empty:
                        if proc.poll() is not None:
                            break
                        continue
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass
            self.running = False
            if self._stop.is_set():
                return
            err = b""
            try:
                proc.wait(timeout=2)
                err = proc.stderr.read() or b""
            except Exception:
                pass
            self._kill()
            msg = err.decode(errors="ignore").strip().splitlines()
            logger.error(f"Live stream ffmpeg exited ({msg[-1] if msg else 'no output'}) — "
                         f"is Icecast running and the source password right? Retrying in "
                         f"{self.RETRY_SECONDS} s")
            self._stop.wait(self.RETRY_SECONDS)


def start_stream(cfg: StreamConfig, in_rate: int) -> LiveStream | None:
    if not cfg.enabled:
        return None
    if not cfg.source_password:
        logger.error("[stream] enabled but no source_password in secrets.ini — live stream off")
        return None
    s = LiveStream(cfg, in_rate)
    s.start()
    return s
