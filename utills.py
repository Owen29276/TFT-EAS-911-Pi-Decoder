#!/usr/bin/env python3
"""
Shared EAS/SAME utilities.
Used by TFT_Control.py and web.py — no side effects on import.
"""

from datetime import datetime, timezone

try:
    from EAS2Text import EAS2Text
    EAS2TEXT_AVAILABLE = True
except ImportError:
    EAS2TEXT_AVAILABLE = False


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
