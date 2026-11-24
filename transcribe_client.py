#!/usr/bin/env python3
"""
Uploads captured alert audio to the transcription server (transcriber/server.py,
running on the Proxmox box) and hands the transcript back to the logger.

Runs a single worker thread with a queue so uploads never block the serial loop.
Transcripts are appended to alerts/transcripts.jsonl, keyed by alert_id.
"""

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("eas_logger.transcribe")


@dataclass
class TranscribeConfig:
    url:      str   = ""          # e.g. http://192.168.1.50:8765
    token:    str   = ""          # from secrets.ini
    timeout:  float = 600.0
    retries:  int   = 3
    backoff:  float = 15.0        # seconds, doubles each retry

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip())

    @classmethod
    def from_parser(cls, cp) -> "TranscribeConfig":
        s, d = "transcribe", cls()
        return cls(
            url=cp.get(s, "url", fallback=d.url).strip().rstrip("/"),
            token=cp.get(s, "token", fallback=d.token).strip(),
            timeout=cp.getfloat(s, "timeout", fallback=d.timeout),
            retries=cp.getint(s, "retries", fallback=d.retries),
            backoff=cp.getfloat(s, "backoff", fallback=d.backoff),
        )


class TranscribeClient:
    def __init__(self, cfg: TranscribeConfig, transcripts_path: str,
                 on_transcript: Callable[[dict, dict], None] | None = None,
                 post: Callable | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.transcripts_path = transcripts_path
        self.on_transcript = on_transcript
        self._post  = post or (requests.post if requests else None)
        self._sleep = sleep
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        if self._post is None:
            logger.error("requests not installed — transcription disabled")
            return
        if not self.cfg.token:
            logger.warning("[transcribe] token is empty — the server will reject uploads")
        self._thread = threading.Thread(target=self._worker, name="transcribe", daemon=True)
        self._thread.start()
        logger.info(f"Transcription enabled → {self.cfg.url}")

    def submit(self, record: dict, wav_path: str) -> None:
        if self._thread:
            self._q.put((record, wav_path))

    # -- worker --

    def _worker(self) -> None:
        while True:
            record, wav_path = self._q.get()
            try:
                result = self.transcribe(record, wav_path)
                if result is not None:
                    self._save(record, wav_path, result)
                    if self.on_transcript:
                        self.on_transcript(record, result)
            except Exception:
                logger.exception("Transcription worker error")

    def transcribe(self, record: dict, wav_path: str) -> dict | None:
        """POST the WAV with retries. Returns the server's JSON or None."""
        url = f"{self.cfg.url}/transcribe"
        headers = {"Authorization": f"Bearer {self.cfg.token}"}
        post = self._post
        if post is None:
            return None
        delay = self.cfg.backoff
        for attempt in range(1, self.cfg.retries + 1):
            try:
                with open(wav_path, "rb") as f:
                    r = post(url, headers=headers, timeout=self.cfg.timeout,
                                   files={"audio": (os.path.basename(wav_path), f, "audio/wav")},
                                   data={"alert_id": record.get("alert_id", ""),
                                         "event_code": record.get("event_code", "")})
                if r.status_code == 200:
                    result = r.json()
                    logger.info(f"Transcribed {record.get('alert_id')} "
                                f"({result.get('processing_seconds', '?')} s, model {result.get('model', '?')})")
                    return result
                if r.status_code in (400, 401, 403, 413):
                    # Won't get better by retrying
                    logger.error(f"Transcription rejected (HTTP {r.status_code}): {r.text[:200]}")
                    return None
                logger.warning(f"Transcription attempt {attempt} failed: HTTP {r.status_code}")
            except Exception as e:
                logger.warning(f"Transcription attempt {attempt} failed: {e}")
            if attempt < self.cfg.retries:
                self._sleep(delay)
                delay *= 2
        logger.error(f"Giving up on transcription for {record.get('alert_id')} — audio kept at {wav_path}")
        return None

    def _save(self, record: dict, wav_path: str, result: dict) -> None:
        entry = {
            "alert_id":         record.get("alert_id"),
            "canonical_header": record.get("canonical_header"),
            "event_code":       record.get("event_code"),
            "received_utc":     record.get("received_utc"),
            "transcribed_utc":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "audio_file":       os.path.basename(wav_path),
            "text":             result.get("text", ""),
            "language":         result.get("language"),
            "audio_seconds":    result.get("duration"),
            "model":            result.get("model"),
        }
        os.makedirs(os.path.dirname(self.transcripts_path) or ".", exist_ok=True)
        with open(self.transcripts_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
