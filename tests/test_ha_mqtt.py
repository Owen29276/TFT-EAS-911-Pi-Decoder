"""Tests for SAME parsing/classification (utills.py) and the HA MQTT publisher (ha_mqtt.py)."""

import configparser
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ha_mqtt as H                                   # noqa: E402
from utills import (affects_area, classify_event,     # noqa: E402
                    location_matches, parse_same_header)

TOR = "ZCZC-WXR-TOR-036109-036023+0030-3281415-KBGM/NWS-"


# ── SAME helpers ───────────────────────────────────────────────────────────

def test_parse_same_header():
    h = parse_same_header(TOR)
    assert h == {"org": "WXR", "event": "TOR", "locations": ["036109", "036023"],
                 "duration": "0030", "time": "3281415", "sender": "KBGM/NWS"}
    assert parse_same_header("garbage") is None
    assert parse_same_header("") is None


def test_classify_event():
    assert classify_event("TOR") == "warning"   # warnings that don't end in W
    assert classify_event("SVR") == "warning"
    assert classify_event("FFW") == "warning"
    assert classify_event("CEM") == "warning"
    assert classify_event("EAN") == "warning"
    assert classify_event("TOA") == "watch"
    assert classify_event("SVS") == "statement"
    assert classify_event("RWT") == "test"
    assert classify_event("ADR") == "admin"
    assert classify_event("XYZ") == "other"
    assert classify_event("") == "other"


def test_location_matching():
    assert location_matches("036109", "036109")
    assert location_matches("000000", "036109")          # national
    assert location_matches("036000", "036109")          # whole state
    assert location_matches("036109", "136109")          # whole county covers NW part
    assert location_matches("136109", "036109")          # part of our county
    assert not location_matches("136109", "236109")      # different parts
    assert not location_matches("036023", "036109")
    assert not location_matches("042109", "036109")      # different state
    assert affects_area(["036023"], [])                  # no local list → everything local
    assert affects_area(["036023", "036109"], ["036109"])
    assert not affects_area(["036023"], ["036109"])


# ── enrichment / state ─────────────────────────────────────────────────────

def rec(code="TOR", header=TOR, received="2026-11-24T14:16:00Z", expires="2026-11-24T14:45:00Z", **kw):
    return {"alert_id": f"id-{code}", "event_code": code, "event_text": "a Tornado Warning",
            "canonical_header": header, "received_utc": received, "expires_utc": expires, **kw}


def test_enrich():
    e = H.enrich(rec(), ["036109"])
    assert e["event_name"] == "Tornado Warning"
    assert e["category"] == "warning" and e["local"] is True
    assert e["location_codes"] == ["036109", "036023"]
    assert H.enrich(rec(), ["036001"])["local"] is False


def test_enrich_undecoded_record_uses_header():
    e = H.enrich({"canonical_header": TOR, "received_utc": "2026-11-24T14:16:00Z"}, [])
    assert e["event_code"] == "TOR" and e["event_name"] == "TOR" and e["category"] == "warning"


def test_active_summary():
    now = datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc)
    recs = [
        H.enrich(rec(), ["036109"]),                                            # active local warning
        H.enrich(rec("TOA", TOR.replace("TOR", "TOA")), ["036109"]),            # active local watch
        H.enrich(rec("RWT", TOR.replace("TOR", "RWT")), ["036109"]),            # tests never active
        H.enrich(rec(expires="2026-11-24T14:00:00Z"), ["036109"]),              # expired
        H.enrich(rec(header=TOR.replace("036109-036023", "036001")), ["036109"]),  # not local
    ]
    s = H.active_summary(recs, now)
    assert s["count"] == 2 and s["warning"] is True
    assert {a["event_code"] for a in s["alerts"]} == {"TOR", "TOA"}
    later = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    assert H.active_summary(recs, later) == {"count": 0, "warning": False, "alerts": []}


def test_zero_duration_alert_active_for_an_hour():
    r = H.enrich(rec("EAN", TOR.replace("TOR", "EAN"), expires=None), [])
    assert H.is_active(r, datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc))
    assert not H.is_active(r, datetime(2026, 11, 24, 15, 30, tzinfo=timezone.utc))


# ── discovery + messages ───────────────────────────────────────────────────

CFG = H.MQTTConfig(enabled=True, host="ha.local", local_fips=["036109"])


def test_discovery_configs():
    d = H.discovery_configs(CFG)
    assert "homeassistant/binary_sensor/tft911/local_warning/config" in d
    assert "homeassistant/event/tft911/alert/config" in d
    ids = [c["unique_id"] for c in d.values()]
    assert len(ids) == len(set(ids)) == 9
    for c in d.values():
        assert c["availability_topic"] == "tft911/status"
        assert c["device"]["identifiers"] == ["tft911_endec"]
        assert c["state_topic"].startswith("tft911/")
    assert d["homeassistant/event/tft911/alert/config"]["event_types"] == H.CATEGORIES


def test_alert_messages():
    msgs = H.alert_messages(CFG, H.enrich(rec(), CFG.local_fips))
    by_topic = {t: (p, r) for t, p, r in msgs}
    assert by_topic["tft911/last_alert/state"] == ("Tornado Warning", True)
    assert by_topic["tft911/last_event_code/state"] == ("TOR", True)
    assert by_topic["tft911/last_alert_time/state"] == ("2026-11-24T14:16:00+00:00", True)
    payload, retain = by_topic["tft911/event"]
    assert retain is False                                  # triggers must not be retained
    ev = json.loads(payload)
    assert ev["event_type"] == "warning" and ev["event_code"] == "TOR" and ev["local"] is True
    assert "tft911/last_rwt/state" not in by_topic


def test_rwt_updates_last_weekly_test():
    r = H.enrich(rec("RWT", TOR.replace("TOR", "RWT")), [])
    topics = [t for t, _, _ in H.alert_messages(CFG, r)]
    assert "tft911/last_rwt/state" in topics


def test_transcript_truncated_for_state():
    long = "word " * 200
    msgs = dict((t, p) for t, p, _ in H.transcript_messages(CFG, H.enrich(rec(), []), {"text": long}))
    assert len(msgs["tft911/transcript/state"]) <= 250
    assert json.loads(msgs["tft911/transcript/attributes"])["text"] == long.strip()
    empty = dict((t, p) for t, p, _ in H.transcript_messages(CFG, H.enrich(rec(), []), {"text": ""}))
    assert empty["tft911/transcript/state"] == "(no speech detected)"


def test_config_from_parser_falls_back_to_station_fips():
    cp = configparser.ConfigParser()
    cp.read_string("[station]\nfips = 036109\n[mqtt]\nenabled = true\nhost = 10.0.0.5\n")
    c = H.MQTTConfig.from_parser(cp)
    assert c.enabled and c.host == "10.0.0.5" and c.local_fips == ["036109"]
    cp.read_string("[mqtt]\nlocal_fips = 036109, 036023\n")
    assert H.MQTTConfig.from_parser(cp).local_fips == ["036109", "036023"]


# ── publisher with a fake paho client ──────────────────────────────────────

class FakeClient:
    def __init__(self):
        self.published, self.will, self.connected_to = [], None, None
        self.on_connect = self.on_disconnect = None
    def username_pw_set(self, u, p): self.auth = (u, p)
    def will_set(self, topic, payload, qos=0, retain=False): self.will = (topic, payload, retain)
    def reconnect_delay_set(self, *a): pass
    def connect_async(self, host, port, keepalive=60): self.connected_to = (host, port)
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


class RC:
    is_failure = False


def test_publisher_lifecycle():
    fc = FakeClient()
    cfg = H.MQTTConfig(enabled=True, host="ha.local", username="tft911", password="pw",
                       local_fips=["036109"], refresh_seconds=3600)
    pub = H.HAPublisher(cfg, client=fc)
    assert pub.start(recent_records=[rec()])
    assert fc.will == ("tft911/status", "offline", True)
    assert fc.auth == ("tft911", "pw") and fc.connected_to == ("ha.local", 1883)

    # Alert arrives while disconnected → kept, replayed on connect
    pub.publish_alert(rec("SVR", TOR.replace("TOR", "SVR")))
    assert fc.published == []
    fc.on_connect(fc, None, None, RC())
    topics = [t for t, _, _ in fc.published]
    assert sum(t.startswith("homeassistant/") for t in topics) == 9
    assert ("tft911/status", "online", True) in fc.published
    assert ("tft911/last_event_code/state", "SVR", True) in fc.published
    assert not any(t == "tft911/event" for t in topics)      # no stale trigger replay

    # Connected: alert published immediately, including the event trigger
    fc.published.clear()
    pub.publish_alert(rec())
    assert any(t == "tft911/event" for t, _, _ in fc.published)
    pub.publish_transcript(rec(), {"text": "The National Weather Service has issued", "model": "small.en"})
    assert ("tft911/transcript/state", "The National Weather Service has issued", True) in fc.published

    pub.stop()
    assert fc.published[-1] == ("tft911/status", "offline", True)


def test_publisher_disabled_or_no_host():
    assert H.HAPublisher(H.MQTTConfig(enabled=False)).start() is False
    assert H.HAPublisher(H.MQTTConfig(enabled=True, host="")).start() is False


def test_header_sender_and_strip_sender_shared():
    import utills as U
    h = "ZCZC-WXR-TOR-036109-036023+0030-3281415-KBGM/NWS-"
    assert U.header_sender(h) == "KBGM/NWS" and U.header_sender("junk") == ""
    assert U.strip_sender(h) == "ZCZC-WXR-TOR-036109-036023+0030-3281415--"
    assert U.strip_sender(h) == U.strip_sender(h.replace("KBGM/NWS", "ERN RLY "))
    assert U.strip_sender("junk-ABC-") == "junk-ABC-"         # malformed: untouched
    assert H.header_sender is U.header_sender                 # one implementation
