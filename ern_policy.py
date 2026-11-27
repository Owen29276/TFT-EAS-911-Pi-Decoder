#!/usr/bin/env python3
"""
GWES ERN origination rules (Operational Procedures, Oct 2025, §1.4), enforced
server-side before the dashboard sends an alert to the TFT.

  §1.4(1)(a)  RWT: at most 2 originations in any 7-day period
  §1.4(1)(b)  DMO: unlimited, but rapid repeats should be sent off-network (warning only)
  §1.4(3)     Anything else is allowed only if one of these is true:
                (a) approved by a member of Network Operations
                (b) coded as an Administrative Message (ADR)
                (c) originated by an official agency and not sent via EAS
                (d) sent with the "EAS" originator code
              (a) and (c) can't be verified by software, so the operator has to state
              which applies and name the approver/agency. That is recorded in the audit log.

§1.4(2) (official/professional audio only) is a human judgement and isn't checked.

History comes from two places, merged:
  - audit.jsonl "originate_sent" entries (written only after the TFT accepted the command)
  - events.jsonl records whose sender is this station's callsign (catches front-panel originations)
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
BASES = {
    "netops": "§1.4(3)(a) approved by Network Operations",
    "official_agency": "§1.4(3)(c) official agency message not sent via EAS",
}
SAME_ORIGINATION = timedelta(minutes=3)   # logger hears a dashboard send within this


@dataclass
class PolicyConfig:
    enforce:              bool = True
    rwt_max:              int  = 2
    rwt_window_days:      int  = 7
    dmo_repeat_minutes:   int  = 10

    @classmethod
    def from_parser(cls, cp) -> "PolicyConfig":
        s, d = "ern", cls()
        if not cp.has_section(s):
            return d
        return cls(
            enforce=cp.getboolean(s, "enforce", fallback=d.enforce),
            rwt_max=cp.getint(s, "rwt_max", fallback=d.rwt_max),
            rwt_window_days=cp.getint(s, "rwt_window_days", fallback=d.rwt_window_days),
            dmo_repeat_minutes=cp.getint(s, "dmo_repeat_minutes", fallback=d.dmo_repeat_minutes),
        )


@dataclass
class Decision:
    ok: bool
    error: str = ""
    warning: str = ""
    basis: str = ""
    needs_basis: bool = False

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ("", False) or k == "ok"}


def _parse(ts) -> datetime | None:
    try:
        return datetime.strptime(ts, UTC_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _tail(path: str, n: int = 5000) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def load_history(audit_path: str, events_path: str, callsign: str) -> list[tuple[datetime, str]]:
    """
    (time, event_code) of this station's originations, oldest first.

    Every audit "originate_sent" counts. A logger record counts only if no audit
    entry for the same event code sent shortly before it is still unmatched —
    i.e. the logger hearing our own dashboard send isn't counted twice, but two
    quick sends always count as two.
    """
    sent: list[tuple[datetime, str]] = []
    for e in _tail(audit_path):
        if e.get("action") == "originate_sent":
            t = _parse(e.get("time_utc"))
            code = str((e.get("detail") or {}).get("event", "")).upper()
            if t and code:
                sent.append((t, code))
    heard: list[tuple[datetime, str]] = []
    cs = (callsign or "").strip().upper()
    if cs and cs != "STATION":
        for r in _tail(events_path):
            if str(r.get("sender", "")).strip().upper() == cs:
                t = _parse(r.get("received_utc"))
                if t and r.get("event_code"):
                    heard.append((t, str(r["event_code"]).upper()))
    sent.sort()
    matched = [False] * len(sent)
    out = list(sent)
    for t, code in sorted(heard):
        for i, (ts, c) in enumerate(sent):
            if not matched[i] and c == code and timedelta(0) <= t - ts <= SAME_ORIGINATION:
                matched[i] = True
                break
        else:
            out.append((t, code))       # e.g. sent from the TFT front panel
    return sorted(out)


def check(event: str, org: str, basis: str, note: str,
          history: list[tuple[datetime, str]], cfg: PolicyConfig,
          now: datetime | None = None) -> Decision:
    event = (event or "").upper()
    now = now or datetime.now(timezone.utc)
    if not cfg.enforce:
        return Decision(True, basis="ERN rules not enforced ([ern] enforce = false)")

    if event == "RWT":
        window = timedelta(days=cfg.rwt_window_days)
        recent = [t for t, code in history if code == "RWT" and now - t < window]
        if len(recent) >= cfg.rwt_max:
            nxt = min(recent) + window
            return Decision(False, error=(
                f"ERN §1.4(1)(a): RWT already originated {len(recent)} times in the last "
                f"{cfg.rwt_window_days} days. Next allowed after {nxt.strftime('%Y-%m-%d %H:%M')} UTC."))
        return Decision(True, basis=f"§1.4(1)(a) RWT {len(recent) + 1} of {cfg.rwt_max} this week")

    if event == "DMO":
        repeat = timedelta(minutes=cfg.dmo_repeat_minutes)
        if any(code == "DMO" and now - t < repeat for t, code in history):
            return Decision(True, basis="§1.4(1)(b) DMO", warning=(
                "ERN §1.4(1)(b): rapid repeated DMOs should be sent outside the network."))
        return Decision(True, basis="§1.4(1)(b) DMO")

    if event == "ADR":
        return Decision(True, basis="§1.4(3)(b) Administrative Message")

    if (org or "").strip().upper() == "EAS":
        return Decision(True, basis='§1.4(3)(d) sent with the "EAS" originator code')

    basis = (basis or "").strip().lower()
    note = (note or "").strip()
    if basis in BASES and len(note) >= 2:
        return Decision(True, basis=f"{BASES[basis]}: {note[:120]}")
    return Decision(False, needs_basis=True, error=(
        f"ERN §1.4(3): a live {event} from originator {org or '?'} needs Network Operations "
        "approval or an official-agency source. Say which, and name the approver or agency."))
