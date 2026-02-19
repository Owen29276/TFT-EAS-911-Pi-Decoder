#!/usr/bin/env python3
"""
Virtual TFT Generator - Simulate EAS alerts and feed to logger
Generates SAME headers and processes them like the main logger would
"""

import subprocess
import json
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

# Import logger functions
try:
    from TFT_EAS_911_Pi_logger import (
        HEADER_RE, normalize, fingerprint, now_utc, now_local, append_line
    )
except ImportError:
    # Fallback functions
    HEADER_RE = re.compile(r"(ZCZC-[\s\S]*?-)(?=ZCZC|NNNN|$)")
    
    def normalize(s):
        return " ".join(s.split())
    
    def fingerprint(s):
        import hashlib
        return hashlib.sha256(normalize(s).encode("utf-8")).hexdigest()
    
    def now_utc():
        return datetime.now(timezone.utc).isoformat()
    
    def now_local():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    def append_line(path, line):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ============================================================================
# SAME Header Generator
# ============================================================================

class SAMEHeaderGenerator:
    """Generate valid SAME headers for testing."""
    
    EVENTS = {
        "RWT": "Required Weekly Test",
        "RMT": "Required Monthly Test",
        "TOR": "Tornado Warning",
        "SVR": "Severe Thunderstorm Warning",
        "FFW": "Flash Flood Warning",
        "FLW": "Flash Flood Watch",
        "SPS": "Special Weather Statement",
        "CEM": "Civil Emergency Message",
    }
    
    ORIGINATORS = {
        "WXR": "National Weather Service",
        "EAS": "EAS Participant",
        "CIV": "Civil Authorities",
        "PEP": "Primary Entry Point",
    }
    
    LOCATIONS = {
        "036001": "New York (all)",
        "036109": "Albany County, NY",
        "017031": "Cook County, IL",
        "017043": "DuPage County, IL",
        "017197": "Will County, IL",
        "036003": "Allegany County, NY",
        "036005": "Bronx County, NY",
    }
    
    @staticmethod
    def generate(
        originator="WXR",
        event="TOR",
        locations=None,
        duration_minutes=60,
        sender="KITH_EAS"
    ) -> str:
        """Generate a SAME header."""
        if not locations:
            locations = ["036109"]
        
        # Locations string (PSSCCC+TTTT format)
        loc_str = "+".join(locations)
        
        # Duration in TTTT format (minutes from 0000 to 1260)
        duration = min(duration_minutes * 60 // 60, 1260)  # Convert to minutes, cap at 1260
        tttt = f"{duration:04d}"
        
        # Build timestamp JJJHHMM
        now = datetime.now()
        jjj = now.strftime("%j")  # Day of year
        hhmm = now.strftime("%H%M")  # Hour and minute
        
        # Sender (max 8 chars, right-padded)
        sender = (sender[:8]).ljust(8, " ")
        
        # Build header: ZCZC-ORG-EVT-PSSCCC+TTTT-JJJHHMM-SENDER-
        header = f"ZCZC-{originator}-{event}-{loc_str}+{tttt}-{jjj}{hhmm}-{sender}-"
        return header
    
    @staticmethod
    def create_burst(header, repetitions=3):
        """Create a burst with repeated headers + NNNN end marker."""
        return (header * repetitions) + "NNNN"


# ============================================================================
# Logger Simulation
# ============================================================================

def output_burst_for_serial(header, repetitions=3):
    """
    Output a burst exactly as the TFT decoder would send it to serial.
    This is what the main logger expects to read.
    """
    burst = (header * repetitions) + "NNNN"
    # Optionally add TFT911 filler byte (0xAB) like real hardware does
    # For now, just output the raw burst
    print(burst)


def simulate_logger_processing(header_list):
    """
    Simulate what the main logger does when it receives SAME headers.
    Mimics the serial -> parsing -> decoding -> output pipeline.
    """
    for header in header_list:
        # Create burst with 3x repetition + NNNN end marker
        burst = header + header + header + "NNNN"
        
        # Extract headers using regex
        headers = [h for h in HEADER_RE.findall(burst) if h.startswith("ZCZC-")]
        canonical = headers[0] if headers else burst
        repeat_count = len(headers)
        saw_eom = "NNNN" in burst
        
        received_utc = now_utc()
        received_local = now_local()
        
        # Try to decode with EAS2Text
        eas2text_output = decode_header_with_eas2text(canonical)
        
        # Get event name from header
        event_code = canonical.split("-")[2]
        event_name = SAMEHeaderGenerator.EVENTS.get(event_code, event_code)
        
        # Parse locations
        location_str = canonical.split("-")[3].split("+")[0]
        location_name = SAMEHeaderGenerator.LOCATIONS.get(location_str, f"Location {location_str}")
        
        # Create fingerprint
        fp = fingerprint(canonical)
        
        # === CONSOLE OUTPUT ===
        print(f"[{received_local}] {event_name} | {location_name}")
        
        # === JSON RECORD (events.jsonl) ===
        record = {
            "received_utc": received_utc,
            "received_local": received_local,
            "canonical_header": canonical,
            "repeat_count": repeat_count,
            "saw_eom": saw_eom,
            "locations": [location_name],
            "eas2text": eas2text_output,
            "fingerprint": fp[:16]
        }
        print(json.dumps(record, separators=(",", ":")))
        
        # === TEXT LOG (events.log) ===
        text_entry = f"""{event_name}
Alert Received: {received_local}

Locations: {location_name}
Repeats: {repeat_count} | EOM: {saw_eom}

eas2text:
{eas2text_output}

SAME Header:
{canonical}
"""
        print(text_entry)
        print("-" * 60)


# ============================================================================
# Test Scenarios
# ============================================================================

def test_scenario_1_kith_eas_tornado():
    """Test 1: KITH_EAS tornado warning - output as serial burst."""
    header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="TOR",
        locations=["036109"],
        duration_minutes=60,
        sender="KITH_EAS"
    )
    output_burst_for_serial(header)


def test_scenario_2_kith_eas_severe():
    """Test 2: KITH_EAS severe thunderstorm warning."""
    header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="SVR",
        locations=["017031"],
        duration_minutes=60,
        sender="KITH_EAS"
    )
    output_burst_for_serial(header)


def test_scenario_3_kith_eas_test():
    """Test 3: KITH_EAS required weekly test."""
    header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="RWT",
        locations=["036001"],
        duration_minutes=0,
        sender="KITH_EAS"
    )
    output_burst_for_serial(header)


def test_scenario_4_nws_vs_kith():
    """Test 4: Compare NWS vs KITH_EAS headers."""
    # NWS (official) tornado warning
    nws_header = SAMEHeaderGenerator.generate(
        originator="WXR",
        event="TOR",
        locations=["036109"],
        duration_minutes=60,
        sender="ALBANY  "
    )
    
    # KITH_EAS (local) tornado warning
    eas_header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="TOR",
        locations=["036109"],
        duration_minutes=60,
        sender="KITH_EAS"
    )
    
    # Output both as serial bursts
    print("# NWS Alert")
    output_burst_for_serial(nws_header)
    print("# KITH_EAS Alert")
    output_burst_for_serial(eas_header)


def test_scenario_5_emergency():
    """Test 5: KITH_EAS civil emergency message."""
    header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="CEM",
        locations=["036001"],
        duration_minutes=30,
        sender="KITH_EAS"
    )
    output_burst_for_serial(header)


def test_interactive_generator():
    """Interactive header generator - flexible for any alert type."""
    print("\nEvent codes:")
    for code, name in SAMEHeaderGenerator.EVENTS.items():
        print(f"  {code}: {name}")
    
    print("\nOriginator codes:")
    for code, name in SAMEHeaderGenerator.ORIGINATORS.items():
        print(f"  {code}: {name}")
    
    print("\nSample locations:")
    for code, name in list(SAMEHeaderGenerator.LOCATIONS.items())[:6]:
        print(f"  {code}: {name}")
    
    event = input("\nEvent code (TOR/SVR/RWT/CEM/etc): ").strip().upper() or "TOR"
    originator = input("Originator (WXR/EAS/CIV/PEP): ").strip().upper() or "EAS"
    location = input("Location code (e.g., 036109): ").strip() or "036109"
    duration = int(input("Duration (minutes, 0 for tests): ").strip() or "60")
    sender = input("Sender (max 8 chars): ").strip() or "KITH_EAS"
    
    header = SAMEHeaderGenerator.generate(
        originator=originator,
        event=event,
        locations=[location],
        duration_minutes=duration,
        sender=sender
    )
    
    output_burst_for_serial(header)


def test_custom(event="TOR", originator="EAS", location="036109", duration=60, sender="KITH_EAS"):
    """Flexible test function for any alert combination."""
    header = SAMEHeaderGenerator.generate(
        originator=originator,
        event=event,
        locations=[location],
        duration_minutes=duration,
        sender=sender
    )
    output_burst_for_serial(header)


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "1":
            test_scenario_1_kith_eas_tornado()
        elif sys.argv[1] == "2":
            test_scenario_2_kith_eas_severe()
        elif sys.argv[1] == "3":
            test_scenario_3_kith_eas_test()
        elif sys.argv[1] == "4":
            test_scenario_4_nws_vs_kith()
        elif sys.argv[1] == "5":
            test_scenario_5_emergency()
        elif sys.argv[1] == "all":
            test_scenario_1_kith_eas_tornado()
            test_scenario_2_kith_eas_severe()
            test_scenario_3_kith_eas_test()
            test_scenario_4_nws_vs_kith()
            test_scenario_5_emergency()
        elif sys.argv[1] == "interactive":
            test_interactive_generator()
        elif sys.argv[1] == "custom":
            # Custom: python3 virtual_tft.py custom TOR EAS 036109 60 KITH_EAS
            event = sys.argv[2] if len(sys.argv) > 2 else "TOR"
            originator = sys.argv[3] if len(sys.argv) > 3 else "EAS"
            location = sys.argv[4] if len(sys.argv) > 4 else "036109"
            duration = int(sys.argv[5]) if len(sys.argv) > 5 else 60
            sender = sys.argv[6] if len(sys.argv) > 6 else "KITH_EAS"
            test_custom(event, originator, location, duration, sender)
        else:
            print(f"Usage: {sys.argv[0]} [1|2|3|4|5|all|interactive|custom]")
    else:
        print("\n" + "="*70)
        print("Virtual TFT - EAS Alert Simulator (Serial Output)")
        print("="*70)
        print("\nQUICK START:")
        print(f"  python3 {sys.argv[0]} 1 | python3 TFT_EAS_911_Pi_logger.py")
        print("\nSCENARIOS (Preset Tests):")
        print(f"  {sys.argv[0]} 1              Tornado warning (KITH_EAS)")
        print(f"  {sys.argv[0]} 2              Severe weather (KITH_EAS)")
        print(f"  {sys.argv[0]} 3              Weekly test (KITH_EAS)")
        print(f"  {sys.argv[0]} 4              NWS vs KITH_EAS tornado")
        print(f"  {sys.argv[0]} 5              Emergency message (KITH_EAS)")
        print(f"  {sys.argv[0]} all            All scenarios")
        print("\nCUSTOM ALERTS:")
        print(f"  {sys.argv[0]} interactive    Interactive mode (choose your own)")
        print(f"  {sys.argv[0]} custom EVENT ORG LOCATION DURATION SENDER")
        print("\n  Example: {sys.argv[0]} custom FFW WXR 036001 120 ALBANY")
        print("    EVENT: TOR, SVR, FFW, RWT, CEM, etc.")
        print("    ORG: WXR (NWS), EAS (local), CIV (civil), PEP (entry point)")
        print("    LOCATION: 6-digit PSSCCC code (e.g., 036109 = Albany County, NY)")
        print("    DURATION: Minutes (0 for tests)")
        print("    SENDER: Originating station (max 8 chars)")
