"""Tests for transcribe_client.py (Pi side) and transcriber/server.py (Proxmox side)."""

import io
import json
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "transcriber"))
import transcribe_client as C   # noqa: E402
import server as S              # noqa: E402

TOKEN = "t" * 32
REC = {"alert_id": "20261124T155745Z-abcd1234", "event_code": "TOR",
       "canonical_header": "ZCZC-WXR-TOR-036109+0030-3281415-KBGM/NWS-",
       "received_utc": "2026-11-24T15:57:45Z"}


def make_wav(path=None, seconds=1.0, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    data = buf.getvalue()
    if path:
        Path(path).write_bytes(data)
    return data


# ── client ─────────────────────────────────────────────────────────────────

class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, json.dumps(body or {})
    def json(self):
        return self._body


def client(tmp_path, responses, **cfg):
    calls = []
    def post(url, **kw):
        calls.append((url, kw))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    c = C.TranscribeClient(C.TranscribeConfig(url="http://srv:8765", token=TOKEN, backoff=0, **cfg),
                           str(tmp_path / "transcripts.jsonl"), post=post, sleep=lambda s: None)
    return c, calls


def test_client_success(tmp_path):
    wav = tmp_path / "a.wav"; make_wav(wav)
    c, calls = client(tmp_path, [Resp(200, {"text": "hello", "model": "small.en", "duration": 1.0})])
    assert c.transcribe(REC, str(wav))["text"] == "hello"
    url, kw = calls[0]
    assert url == "http://srv:8765/transcribe"
    assert kw["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kw["data"]["alert_id"] == REC["alert_id"]


def test_client_retries_then_succeeds(tmp_path):
    wav = tmp_path / "a.wav"; make_wav(wav)
    c, calls = client(tmp_path, [ConnectionError("down"), Resp(503), Resp(200, {"text": "ok"})])
    assert c.transcribe(REC, str(wav))["text"] == "ok"
    assert len(calls) == 3


def test_client_gives_up(tmp_path):
    wav = tmp_path / "a.wav"; make_wav(wav)
    c, calls = client(tmp_path, [Resp(500)] * 3)
    assert c.transcribe(REC, str(wav)) is None and len(calls) == 3


def test_client_no_retry_on_auth_error(tmp_path):
    wav = tmp_path / "a.wav"; make_wav(wav)
    c, calls = client(tmp_path, [Resp(401, {"error": "unauthorized"})])
    assert c.transcribe(REC, str(wav)) is None and len(calls) == 1


def test_client_worker_saves_and_calls_back(tmp_path):
    wav = tmp_path / "a.wav"; make_wav(wav)
    got = []
    c, _ = client(tmp_path, [Resp(200, {"text": "Tornado warning for Tompkins", "model": "small.en",
                                        "duration": 1.0})])
    c.on_transcript = lambda rec, res: got.append((rec["alert_id"], res["text"]))
    c.start()
    c.submit(REC, str(wav))
    import time
    for _ in range(50):
        if got: break
        time.sleep(0.05)
    assert got == [(REC["alert_id"], "Tornado warning for Tompkins")]
    line = json.loads((tmp_path / "transcripts.jsonl").read_text().splitlines()[0])
    assert line["alert_id"] == REC["alert_id"] and line["audio_file"] == "a.wav"


def test_client_disabled_without_url(tmp_path):
    c = C.TranscribeClient(C.TranscribeConfig(url=""), str(tmp_path / "t.jsonl"))
    c.start()
    c.submit(REC, "nope.wav")          # silently ignored
    assert c._thread is None


# ── server ─────────────────────────────────────────────────────────────────

class FakeEngine:
    model_name, loaded = "fake", True
    def __init__(self): self.calls = []
    def transcribe(self, path):
        self.calls.append(path)
        return {"text": "test transcript", "language": "en", "duration": 1.0,
                "segments": [], "model": "fake"}


@pytest.fixture
def srv():
    eng = FakeEngine()
    app = S.create_app(eng, TOKEN, max_seconds=5)
    app.config["TESTING"] = True
    return app.test_client(), eng


def upload(c, data, token=TOKEN):
    return c.post("/transcribe", headers={"Authorization": f"Bearer {token}"},
                  data={"audio": (io.BytesIO(data), "a.wav"), "alert_id": "x1"},
                  content_type="multipart/form-data")


def test_server_health_no_auth(srv):
    c, _ = srv
    assert c.get("/health").get_json() == {"ok": True, "model": "fake", "loaded": True}


def test_server_rejects_bad_token(srv):
    c, eng = srv
    assert upload(c, make_wav(), token="wrong").status_code == 401
    r = c.post("/transcribe", data={"audio": (io.BytesIO(make_wav()), "a.wav")},
               content_type="multipart/form-data")
    assert r.status_code == 401 and eng.calls == []


def test_server_rejects_non_wav(srv):
    c, eng = srv
    r = upload(c, b"definitely not audio" * 100)
    assert r.status_code == 400 and eng.calls == []


def test_server_rejects_too_long(srv):
    c, eng = srv
    assert upload(c, make_wav(seconds=6)).status_code == 413 and eng.calls == []


def test_server_transcribes_and_cleans_up(srv):
    c, eng = srv
    r = upload(c, make_wav())
    body = r.get_json()
    assert r.status_code == 200 and body["text"] == "test transcript" and body["alert_id"] == "x1"
    assert not Path(eng.calls[0]).exists()            # temp file deleted


def test_server_requires_token_to_build():
    with pytest.raises(ValueError):
        S.create_app(FakeEngine(), "")


def test_wav_seconds(tmp_path):
    p = tmp_path / "a.wav"; make_wav(p, seconds=2.5)
    assert S.wav_seconds(str(p)) == pytest.approx(2.5)
