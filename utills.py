#!/usr/bin/env python3
"""
Shared EAS/SAME utilities.
Used by TFT_Control.py and web.py — no side effects on import.
"""

from datetime import datetime, timezone
import re

try:
    from EAS2Text import EAS2Text
    EAS2TEXT_AVAILABLE = True
except ImportError:
    EAS2TEXT_AVAILABLE = False


# ── SAME header parsing / classification ───────────────────────────────────

_SAME_RE = re.compile(
    r"^ZCZC-(?P<org>[A-Z]{3})-(?P<event>[A-Z0-9]{3})-(?P<locs>[0-9-]+?)"
    r"\+(?P<dur>\d{4})-(?P<time>\d{7})-(?P<sender>[^-]{1,8})-"
)

# Event codes that don't follow the W=warning / A=watch / S=statement suffix rule
_WARNING_CODES = {"TOR", "SVR", "EAN", "CAE", "CEM", "LAE", "TOE", "EVI", "BLU"}
_TEST_CODES    = {"RWT", "RMT", "NPT", "DMO"}
_ADMIN_CODES   = {"ADR", "NMN", "NIC", "EAT"}

CATEGORIES = ["warning", "watch", "statement", "test", "admin", "other"]


def parse_same_header(header: str) -> dict | None:
    """Split a SAME header into its fields. Returns None if it doesn't parse."""
    m = _SAME_RE.match(" ".join(header.split()) if header else "")
    if not m:
        return None
    return {
        "org":       m["org"],
        "event":     m["event"],
        "locations": [l for l in m["locs"].split("-") if len(l) == 6],
        "duration":  m["dur"],
        "time":      m["time"],
        "sender":    m["sender"].strip(),
    }


def classify_event(code: str) -> str:
    """Bucket a SAME event code into warning / watch / statement / test / admin / other."""
    code = (code or "").upper()
    if code in _TEST_CODES:
        return "test"
    if code in _ADMIN_CODES:
        return "admin"
    if code in _WARNING_CODES or code.endswith("W"):
        return "warning"
    if code.endswith("A"):
        return "watch"
    if code.endswith("S"):
        return "statement"
    return "other"


def location_matches(alert_code: str, local_code: str) -> bool:
    """
    True if a SAME location code in an alert covers one of our local codes (PSSCCC).

    000000 = whole US, SS000 = whole state, P=0 = whole county (else 1/9 subdivision).
    """
    if alert_code == "000000":
        return True
    if len(alert_code) != 6 or len(local_code) != 6:
        return False
    if alert_code[1:3] != local_code[1:3]:
        return False
    if alert_code[3:] == "000":
        return True
    if alert_code[3:] != local_code[3:]:
        return False
    return alert_code[0] == "0" or local_code[0] == "0" or alert_code[0] == local_code[0]


def affects_area(alert_codes: list[str], local_codes: list[str]) -> bool:
    """True if any alert location covers any local code. No local codes = everything is local."""
    if not local_codes:
        return True
    return any(location_matches(a, l) for a in alert_codes for l in local_codes)


# Maps TFT duration codes to SAME header HHMM strings
TFT_DUR_TO_SAME = {
    "01": "0015", "02": "0030", "03": "0045",
    "04": "0100", "06": "0130", "08": "0200",
}

# Human-readable labels for duration codes
DUR_LABELS = {
    "01": "15 minutes", "02": "30 minutes", "03": "45 minutes",
    "04": "1 hour",     "06": "1.5 hours",  "08": "2 hours",
}


def build_same_header(event: str, fips_list: list, duration_code: str,
                      org: str = "EAS", callsign: str = "STATION") -> str:
    """
    Construct a SAME-format header string from origination parameters.

    Args:
        event:         EAS event code e.g. 'TOR', 'RWT'
        fips_list:     List of 6-digit FIPS strings e.g. ['036109', '036001']
        duration_code: TFT duration code e.g. '01'=15min '04'=1hr
        org:           SAME originator code: EAS, WXR, CIV, or PEP
        callsign:      Station callsign, max 8 chars
    """
    fips_part  = "-".join(fips_list)
    dur        = TFT_DUR_TO_SAME.get(duration_code, "0100")
    now        = datetime.now(timezone.utc)
    timestamp  = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
    callsign_p = f"{callsign:<8}"[:8]
    return f"ZCZC-{org}-{event}-{fips_part}+{dur}-{timestamp}-{callsign_p}-"


def fips_table() -> dict:
    """
    Return the complete FIPS→name dict from EAS2Text's internal data.
    Keys are 5-digit strings (e.g. '36109'), values are 'County, ST'.
    Returns empty dict if EAS2Text is unavailable.
    """
    if not EAS2TEXT_AVAILABLE:
        return {}
    try:
        import json
        from EAS2Text import EAS2Text as _EAS2Text
        return json.loads(_EAS2Text.__data__).get("SAME", {})
    except Exception:
        return {}


def parse_location_keys(locations: str) -> list[str]:
    """
    Parse a location key string into individual key number strings.

    Accepts comma-separated format ('1,3,10') from the web UI checkboxes,
    or bare-digit format ('13') from manual entry (each digit = one key, 1-9).
    """
    if ',' in locations:
        return [k.strip() for k in locations.split(',') if k.strip()]
    return [c for c in locations if c.isdigit() and c != '0']


def search_fips(query: str, limit: int = 10) -> list:
    """
    Search the EAS2Text FIPS table by partial county or state name.

    Args:
        query: Partial name to search for, e.g. 'tompkins' or 'cook, il'
        limit: Maximum number of results to return.

    Returns:
        List of (fips_6digit, name) tuples, e.g. [('036109', 'Tompkins, NY'), ...]
        The 6-digit FIPS has a leading '0' (whole-county prefix) prepended.
    """
    q = query.lower().strip()
    return [
        (f"0{k}", v)
        for k, v in fips_table().items()
        if q in v.lower()
    ][:limit]


def decode_header(same_string: str, tz_offset: int | None = None) -> str:
    """
    Decode a SAME header to TFT-style human-readable announcement text.

    Args:
        same_string: Full SAME header e.g. 'ZCZC-WXR-TOR-036109+0030-...'
        tz_offset:   UTC offset integer e.g. -5 for Eastern. None = library default.

    Returns:
        Human-readable alert text as the TFT unit would display/speak it.

    Raises:
        RuntimeError: if EAS2Text is not installed.
    """
    if not EAS2TEXT_AVAILABLE:
        raise RuntimeError("EAS2Text not installed — run: pip install EAS2Text-Remastered")
    from EAS2Text import EAS2Text as _EAS2Text
    if tz_offset is not None:
        return _EAS2Text(sameData=same_string, mode="TFT", timeZone=tz_offset).EASText
    return _EAS2Text(sameData=same_string, mode="TFT").EASText
