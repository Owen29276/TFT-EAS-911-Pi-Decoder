#!/usr/bin/env python3
"""
TFT EAS 911 transcription server — runs on the Proxmox box, not the Pi.

The Pi uploads each captured alert WAV; this transcribes it with faster-whisper
(CPU, int8) and returns the text. Stateless: the Pi stores the transcript and
publishes it to Home Assistant.

Environment (see tft911-transcriber.env.example):
  TRANSCRIBE_TOKEN     required — shared secret, same value as [transcribe] token on the Pi
  WHISPER_MODEL        default small.en  (tiny.en / base.en / small.en / medium.en)
  WHISPER_THREADS      default 0 = all cores given to the container
  WHISPER_PROMPT       vocabulary hint passed as initial_prompt
  TRANSCRIBE_BIND      default 0.0.0.0
  TRANSCRIBE_PORT      default 8765
  MAX_AUDIO_SECONDS    default 600

API:
  GET  /health        → {"ok": true, "model": ..., "loaded": bool}
  POST /transcribe    multipart: audio=<wav file>, optional alert_id, event_code
                      header:    Authorization: Bearer <TRANSCRIBE_TOKEN>
                      → {"text", "language", "duration", "model", "processing_seconds", "segments"}
"""

import hmac
import logging
import os
import sys
import tempfile
import threading
import time
import wave

from flask import Flask, jsonify, request

log = logging.getLogger("transcriber")

DEFAULT_PROMPT = (
    "This is an Emergency Alert System message. The National Weather Service in "
    "Binghamton has issued a Tornado Warning, Severe Thunderstorm Warning, Flash Flood "
    "Warning for Tompkins County, Cortland County, Cayuga County, Ithaca, New York."
)
MAX_UPLOAD_BYTES = 40 * 1024 * 1024   # 10 min of 16 kHz mono is ~19 MB


def load_wav_16k(path: str):
    """
    Decode a 16-bit PCM WAV to the float32 16 kHz mono array Whisper expects.

    Done with the stdlib + numpy instead of faster-whisper's PyAV/FFmpeg path:
    it avoids PyAV version breakage (faster-whisper 1.2.1 + av 19 crash) and keeps
    FFmpeg's decoders away from uploaded files.
    """
    import numpy as np
    with wave.open(path, "rb") as w:
        channels, rate = w.getnchannels(), w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    audio = pcm.astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio[: len(audio) - len(audio) % channels].reshape(-1, channels).mean(axis=1)
    if rate != 16000 and len(audio):
        n_out = int(round(len(audio) * 16000 / rate))
        audio = np.interp(np.linspace(0, len(audio) - 1, n_out),
                          np.arange(len(audio)), audio).astype(np.float32)
    return audio


class WhisperEngine:
    """Lazy-loads faster-whisper once; transcriptions run one at a time."""

    def __init__(self, model: str, threads: int = 0, prompt: str = DEFAULT_PROMPT):
        self.model_name, self.threads, self.prompt = model, threads, prompt
        self._model = None
        self._lk = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            t = time.time()
            log.info(f"Loading Whisper model {self.model_name} (first run downloads it)…")
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                       cpu_threads=self.threads)
            log.info(f"Model loaded in {time.time() - t:.1f} s")
        return self._model

    def transcribe(self, path: str) -> dict:
        audio = load_wav_16k(path)
        with self._lk:
            model = self.load()
            segments, info = model.transcribe(
                audio,
                language="en",
                beam_size=5,
                vad_filter=True,        # skip SAME bursts / attention tone / silence
                initial_prompt=self.prompt or None,
                condition_on_previous_text=False,   # limits repetition loops on noisy audio
            )
            segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                    for s in segments]
        return {
            "text":     " ".join(s["text"] for s in segs).strip(),
            "language": info.language,
            "duration": round(info.duration, 2),
            "segments": segs,
            "model":    self.model_name,
        }


def wav_seconds(path: str) -> float:
    """Validate the upload is a real PCM WAV and return its length. Raises ValueError."""
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() not in (1, 2):
                raise ValueError("expected 16-bit mono/stereo PCM WAV")
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a valid WAV file: {e}") from e


def create_app(engine, token: str, max_seconds: float = 600) -> Flask:
    if not token:
        raise ValueError("token required")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    def authorized() -> bool:
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        return hmac.compare_digest(supplied.encode(), token.encode())

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "model": engine.model_name, "loaded": engine.loaded})

    @app.post("/transcribe")
    def transcribe():
        if not authorized():
            log.warning(f"Rejected unauthorized request from {request.remote_addr}")
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        f = request.files.get("audio")
        if f is None:
            return jsonify({"ok": False, "error": "missing 'audio' file"}), 400
        alert_id = (request.form.get("alert_id") or "")[:64]

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="tft911-")
        os.close(fd)
        try:
            f.save(path)
            try:
                secs = wav_seconds(path)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
            if secs > max_seconds:
                return jsonify({"ok": False, "error": f"audio too long ({secs:.0f} s > {max_seconds:.0f} s)"}), 413
            t = time.time()
            result = engine.transcribe(path)
            result["processing_seconds"] = round(time.time() - t, 2)
            result["alert_id"] = alert_id
            result["ok"] = True
            log.info(f"{alert_id or '?'}: {secs:.1f} s audio → {result['processing_seconds']} s, "
                     f"{len(result['text'])} chars")
            return jsonify(result)
        except Exception as e:
            log.exception("Transcription failed")
            return jsonify({"ok": False, "error": f"transcription failed: {e}"}), 500
        finally:
            try: os.unlink(path)
            except OSError: pass

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"ok": False, "error": "upload too large"}), 413

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("TRANSCRIBE_TOKEN", "").strip()
    if len(token) < 16:
        sys.exit("TRANSCRIBE_TOKEN must be set (16+ chars). Generate one with: "
                 "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
    engine = WhisperEngine(
        model=os.environ.get("WHISPER_MODEL", "small.en"),
        threads=int(os.environ.get("WHISPER_THREADS", "0")),
        prompt=os.environ.get("WHISPER_PROMPT", DEFAULT_PROMPT),
    )
    engine.load()   # fail fast (and download the model) at startup, not on the first alert
    app = create_app(engine, token, float(os.environ.get("MAX_AUDIO_SECONDS", "600")))

    from waitress import serve
    host = os.environ.get("TRANSCRIBE_BIND", "0.0.0.0")
    port = int(os.environ.get("TRANSCRIBE_PORT", "8765"))
    log.info(f"Listening on {host}:{port}")
    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
