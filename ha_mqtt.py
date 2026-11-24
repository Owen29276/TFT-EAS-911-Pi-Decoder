#!/usr/bin/env python3
"""
Home Assistant integration over MQTT with auto-discovery.

Creates one HA device ("TFT EAS 911") with these entities:

  sensor.tft_eas_911_last_alert          "Tornado Warning"   (+ full record as attributes)
  sensor.tft_eas_911_last_event_code     "TOR"
  sensor.tft_eas_911_last_alert_time     timestamp
  sensor.tft_eas_911_active_alerts       count of unexpired local, non-test alerts
  binary_sensor.tft_eas_911_local_warning ON while a warning covers your area
  sensor.tft_eas_911_transcript          Whisper transcript (first 250 chars; full text in attributes)
  sensor.tft_eas_911_last_weekly_test    timestamp of the last RWT
  event.tft_eas_911_alert                fires on every new alert (event_type = category)

Availability uses an MQTT last-will, so HA shows the device unavailable if the
logger or the Pi dies — useful as a health alarm on its own.
"""

import json
import logging
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from utills import CATEGORIES, affects_area, classify_event, parse_same_header

try:
    import paho.mqtt.client as mqtt
    PAHO_AVAILABLE = True
except ImportError:
    mqtt = None
    PAHO_AVAILABLE = False

logger = logging.getLogger("eas_logger.mqtt")

STATE_MAX  = 250                    # HA rejects sensor states over 255 chars
NO_EXPIRY_ACTIVE = timedelta(hours=1)   # how long +0000-duration alerts count as active


@dataclass
class MQTTConfig:
    enabled:          bool = False
    host:             str  = ""
    port:             int  = 1883
    username:         str  = ""
    password:         str  = ""
    discovery_prefix: str  = "homeassistant"
    base_topic:       str  = "tft911"
    node_id:          str  = "tft911"
    local_fips:       list = field(default_factory=list)
    refresh_seconds:  int  = 30

    @classmethod
    def from_parser(cls, cp) -> "MQTTConfig":
        s, d = "mqtt", cls()
        local = cp.get(s, "local_fips", fallback="").strip() or cp.get("station", "fips", fallback="")
        return cls(
            enabled=cp.getboolean(s, "enabled", fallback=False),
            host=cp.get(s, "host", fallback="").strip(),
            port=cp.getint(s, "port", fallback=d.port),
            username=cp.get(s, "username", fallback="").strip(),
            password=cp.get(s, "password", fallback=""),
            discovery_prefix=cp.get(s, "discovery_prefix", fallback=d.discovery_prefix).strip(),
            base_topic=cp.get(s, "base_topic", fallback=d.base_topic).strip().rstrip("/"),
            node_id=cp.get(s, "node_id", fallback=d.node_id).strip(),
            local_fips=[f.strip() for f in local.split(",") if f.strip()],
            refresh_seconds=cp.getint(s, "refresh_seconds", fallback=d.refresh_seconds),
        )


# ── pure helpers (unit-tested) ─────────────────────────────────────────────

def _iso(ts: str | None) -> str | None:
    """'2026-11-24T15:57:45Z' → '2026-11-24T15:57:45+00:00' (what HA timestamp sensors want)."""
    if not ts:
        return None
    return ts.replace("Z", "+00:00") if ts.endswith("Z") else ts


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def event_name(record: dict) -> str:
    """'a Tornado Warning' → 'Tornado Warning'."""
    text = (record.get("event_text") or "").strip()
    for art in ("a ", "an ", "the "):
        if text.lower().startswith(art):
            text = text[len(art):]
            break
    return text or record.get("event_code") or "EAS Alert"


def enrich(record: dict, local_fips: list[str]) -> dict:
    """Add category / location codes / local flag to a logger record."""
    hdr = parse_same_header(record.get("canonical_header", "")) or {}
    code = record.get("event_code") or hdr.get("event", "")
    locs = hdr.get("locations", [])
    return {
        **record,
        "event_code":     code,
        "event_name":     event_name({**record, "event_code": code}),
        "category":       classify_event(code),
        "location_codes": locs,
        "local":          affects_area(locs, local_fips),
    }


def is_active(rec: dict, now: datetime) -> bool:
    if rec.get("category") == "test":
        return False
    exp = _parse_utc(rec.get("expires_utc"))
    if exp is None:
        recv = _parse_utc(rec.get("received_utc"))
        exp = recv + NO_EXPIRY_ACTIVE if recv else None
    return exp is not None and exp > now


def active_summary(records: list[dict], now: datetime) -> dict:
    """State for the active_alerts sensor and local_warning binary_sensor."""
    active = [r for r in records if r.get("local") and is_active(r, now)]
    active.sort(key=lambda r: r.get("received_utc", ""), reverse=True)
    return {
        "count":   len(active),
        "warning": any(r.get("category") == "warning" for r in active),
        "alerts":  [{"event_code": r.get("event_code"), "event_name": r.get("event_name"),
                     "category": r.get("category"), "expires": _iso(r.get("expires_utc")),
                     "alert_id": r.get("alert_id")} for r in active],
    }


def discovery_configs(cfg: MQTTConfig) -> dict[str, dict]:
    """Map of discovery topic → config payload for every entity."""
    base, node = cfg.base_topic, cfg.node_id
    device = {
        "identifiers":  [f"{node}_endec"],
        "name":         "TFT EAS 911",
        "manufacturer": "TFT Inc.",
        "model":        "EAS 911 ENDEC",
    }
    common = {
        "availability_topic": f"{base}/status",
        "device": device,
        "origin": {"name": "TFT-EAS-911-Pi-Decoder",
                   "url": "https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder"},
    }

    def ent(component, key, **extra):
        topic = f"{cfg.discovery_prefix}/{component}/{node}/{key}/config"
        return topic, {**common, "unique_id": f"{node}_{key}", **extra}

    entities = [
        ent("sensor", "last_alert", name="Last alert", icon="mdi:alert",
            state_topic=f"{base}/last_alert/state",
            json_attributes_topic=f"{base}/last_alert/attributes"),
        ent("sensor", "last_event_code", name="Last event code", icon="mdi:code-tags",
            state_topic=f"{base}/last_event_code/state"),
        ent("sensor", "last_alert_time", name="Last alert time", device_class="timestamp",
            state_topic=f"{base}/last_alert_time/state"),
        ent("sensor", "active_alerts", name="Active alerts", icon="mdi:alert-circle",
            state_class="measurement",
            state_topic=f"{base}/active/count",
            json_attributes_topic=f"{base}/active/attributes"),
        ent("binary_sensor", "local_warning", name="Local warning", device_class="safety",
            payload_on="ON", payload_off="OFF",
            state_topic=f"{base}/active/warning",
            json_attributes_topic=f"{base}/active/attributes"),
        ent("sensor", "transcript", name="Transcript", icon="mdi:text-to-speech",
            state_topic=f"{base}/transcript/state",
            json_attributes_topic=f"{base}/transcript/attributes"),
        ent("sensor", "last_weekly_test", name="Last weekly test", device_class="timestamp",
            state_topic=f"{base}/last_rwt/state"),
        ent("event", "alert", name="Alert", icon="mdi:bullhorn",
            event_types=CATEGORIES, state_topic=f"{base}/event"),
    ]
    return dict(entities)


def alert_messages(cfg: MQTTConfig, rec: dict) -> list[tuple[str, str, bool]]:
    """(topic, payload, retain) for a new, enriched alert record."""
    base = cfg.base_topic
    attrs = {k: rec.get(k) for k in (
        "alert_id", "event_code", "event_name", "category", "local", "originator_code",
        "org_text", "sender", "eas_text", "locations_pretty", "location_codes",
        "canonical_header", "received_utc", "issued_utc", "expires_utc")}
    attrs["expires"] = _iso(rec.get("expires_utc"))
    msgs = [
        (f"{base}/last_alert/attributes", json.dumps(attrs), True),
        (f"{base}/last_alert/state",      rec["event_name"][:STATE_MAX], True),
        (f"{base}/last_event_code/state", rec.get("event_code") or "", True),
        (f"{base}/last_alert_time/state", _iso(rec.get("received_utc")) or "", True),
        # The event entity is a trigger, not state — never retain it or HA re-fires on reconnect
        (f"{base}/event", json.dumps({"event_type": rec["category"], **attrs}), False),
    ]
    if rec.get("event_code") == "RWT":
        msgs.append((f"{base}/last_rwt/state", _iso(rec.get("received_utc")) or "", True))
    return msgs


def transcript_messages(cfg: MQTTConfig, rec: dict, result: dict) -> list[tuple[str, str, bool]]:
    base = cfg.base_topic
    text = (result.get("text") or "").strip()
    state = text if len(text) <= STATE_MAX else text[:STATE_MAX - 1] + "…"
    attrs = {"text": text, "alert_id": rec.get("alert_id"), "event_code": rec.get("event_code"),
             "event_name": rec.get("event_name"), "received_utc": rec.get("received_utc"),
             "model": result.get("model"), "audio_seconds": result.get("duration")}
    return [
        (f"{base}/transcript/attributes", json.dumps(attrs), True),
        (f"{base}/transcript/state", state or "(no speech detected)", True),
    ]


def active_messages(cfg: MQTTConfig, summary: dict) -> list[tuple[str, str, bool]]:
    base = cfg.base_topic
    return [
        (f"{base}/active/attributes", json.dumps({"alerts": summary["alerts"]}), True),
        (f"{base}/active/count",   str(summary["count"]), True),
        (f"{base}/active/warning", "ON" if summary["warning"] else "OFF", True),
    ]


# ── publisher ──────────────────────────────────────────────────────────────

class HAPublisher:
    def __init__(self, cfg: MQTTConfig, client=None):
        self.cfg = cfg
        self._client = client
        self._lk = threading.Lock()
        self._records: list[dict] = []          # recent enriched records for active tracking
        self._retained: dict[str, str] = {}     # last retained payload per topic (re-sent on reconnect)
        self._stop = threading.Event()
        self.connected = False

    # -- lifecycle --

    def start(self, recent_records: list[dict] | None = None) -> bool:
        if not self.cfg.enabled:
            return False
        if not self.cfg.host:
            logger.error("[mqtt] enabled but host is empty — Home Assistant integration off")
            return False
        if self._client is None:
            if not PAHO_AVAILABLE:
                logger.error("paho-mqtt not installed — run: pip install paho-mqtt")
                return False
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,  # type: ignore[union-attr]
                                       client_id=f"{self.cfg.node_id}-{socket.gethostname()}")
        c = self._client
        if self.cfg.username:
            c.username_pw_set(self.cfg.username, self.cfg.password)
        c.will_set(f"{self.cfg.base_topic}/status", "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.reconnect_delay_set(1, 60)

        for r in recent_records or []:
            self._records.append(enrich(r, self.cfg.local_fips))
        if self._records:
            self._remember(alert_messages(self.cfg, self._records[-1]))
            rwt = next((r for r in reversed(self._records) if r.get("event_code") == "RWT"), None)
            if rwt:
                self._remember([(f"{self.cfg.base_topic}/last_rwt/state",
                                 _iso(rwt.get("received_utc")) or "", True)])
        self._remember(active_messages(self.cfg, self._summary()))

        c.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        c.loop_start()
        threading.Thread(target=self._refresh_loop, name="mqtt-refresh", daemon=True).start()
        logger.info(f"MQTT → {self.cfg.host}:{self.cfg.port} (Home Assistant discovery prefix "
                    f"'{self.cfg.discovery_prefix}', local FIPS {self.cfg.local_fips or 'ALL'})")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._client and self.connected:
            self._client.publish(f"{self.cfg.base_topic}/status", "offline", qos=1, retain=True)
            self._client.disconnect()
        if self._client:
            self._client.loop_stop()

    # -- public API --

    def publish_alert(self, record: dict) -> None:
        rec = enrich(record, self.cfg.local_fips)
        with self._lk:
            self._records.append(rec)
            self._records = self._records[-200:]
        self._send(alert_messages(self.cfg, rec))
        self._send(active_messages(self.cfg, self._summary()))

    def publish_transcript(self, record: dict, result: dict) -> None:
        rec = enrich(record, self.cfg.local_fips)
        self._send(transcript_messages(self.cfg, rec, result))

    # -- internals --

    def _summary(self) -> dict:
        with self._lk:
            return active_summary(list(self._records), datetime.now(timezone.utc))

    def _remember(self, msgs) -> None:
        for topic, payload, retain in msgs:
            if retain:
                self._retained[topic] = payload

    def _send(self, msgs) -> None:
        self._remember(msgs)
        if not self._client or not self.connected:
            logger.debug("MQTT not connected — state kept for next connect")
            return
        for topic, payload, retain in msgs:
            self._client.publish(topic, payload, qos=1, retain=retain)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            logger.error(f"MQTT connect refused: {reason_code}")
            return
        self.connected = True
        logger.info("MQTT connected — publishing Home Assistant discovery")
        for topic, payload in discovery_configs(self.cfg).items():
            client.publish(topic, json.dumps(payload), qos=1, retain=True)
        client.publish(f"{self.cfg.base_topic}/status", "online", qos=1, retain=True)
        for topic, payload in list(self._retained.items()):
            client.publish(topic, payload, qos=1, retain=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        if not self._stop.is_set():
            logger.warning(f"MQTT disconnected ({reason_code}) — reconnecting")

    def _refresh_loop(self) -> None:
        """Re-evaluate expiries so local_warning turns OFF when the warning expires."""
        last = None
        while not self._stop.wait(self.cfg.refresh_seconds):
            msgs = active_messages(self.cfg, self._summary())
            if msgs != last:
                self._send(msgs)
                last = msgs
