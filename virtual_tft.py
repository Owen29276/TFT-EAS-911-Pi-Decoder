#!/usr/bin/env python3
"""
Virtual TFT Generator - Simulate EAS alerts and feed to logger
Generates SAME headers and processes them like the main logger would
"""

import json
import sys
import re
from datetime import datetime, timezone

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
        "001001": "County A",
        "001002": "County B",
        "001003": "County C",
        "002001": "County D",
        "002002": "County E",
        "003001": "County F",
        "003002": "County G",
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
        
        # Duration in HHMM format (e.g., 90 minutes = "0130" = 1 hour 30 minutes)
        hours = min(duration_minutes // 60, 99)
        minutes = duration_minutes % 60
        tttt = f"{hours:02d}{minutes:02d}"
        
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


# ============================================================================
# Test Scenarios
# ============================================================================

def test_scenario_1_generic_eas_tornado():
    """Test 1: Generic EAS tornado warning - output as serial burst."""
    header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="TOR",
        locations=["001001"],
        duration_minutes=60,
        sender="EAS_TEST"
    )
    output_burst_for_serial(header)


def test_scenario_2_generic_eas_severe():
    """Test 2: Generic EAS severe thunderstorm warning."""
    header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="SVR",
        locations=["002001"],
        duration_minutes=60,
        sender="EAS_TEST"
    )
    output_burst_for_serial(header)


def test_scenario_3_generic_eas_test():
    """Test 3: Generic EAS required weekly test."""
    header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="RWT",
        locations=["001002"],
        duration_minutes=0,
        sender="EAS_TEST"
    )
    output_burst_for_serial(header)


def test_scenario_4_nws_vs_eas():
    """Test 4: Compare NWS vs EAS headers."""
    # NWS (official) tornado warning
    nws_header = SAMEHeaderGenerator.generate(
        originator="WXR",
        event="TOR",
        locations=["001001"],
        duration_minutes=60,
        sender="NWS_OFC"
    )
    
    # EAS (local) tornado warning
    eas_header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="TOR",
        locations=["001001"],
        duration_minutes=60,
        sender="EAS_TEST"
    )
    
    # Output both as serial bursts
    print("# NWS Alert")
    output_burst_for_serial(nws_header)
    print("# KITH_EAS Alert")
    output_burst_for_serial(eas_header)


def test_scenario_5_emergency():
    """Test 5: Generic EAS civil emergency message."""
    header = SAMEHeaderGenerator.generate(
        originator="EAS",
        event="CEM",
        locations=["003001"],
        duration_minutes=30,
        sender="EAS_TEST"
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
            test_scenario_1_generic_eas_tornado()
        elif sys.argv[1] == "2":
            test_scenario_2_generic_eas_severe()
        elif sys.argv[1] == "3":
            test_scenario_3_generic_eas_test()
        elif sys.argv[1] == "4":
            test_scenario_4_nws_vs_eas()
        elif sys.argv[1] == "5":
            test_scenario_5_emergency()
        elif sys.argv[1] == "all":
            test_scenario_1_generic_eas_tornado()
            test_scenario_2_generic_eas_severe()
            test_scenario_3_generic_eas_test()
            test_scenario_4_nws_vs_eas()
            test_scenario_5_emergency()
        elif sys.argv[1] == "interactive":
            test_interactive_generator()
        elif sys.argv[1] == "custom":
            # Custom: python3 virtual_tft.py custom TOR EAS 001001 60 TEST_STN
            event = sys.argv[2] if len(sys.argv) > 2 else "TOR"
            originator = sys.argv[3] if len(sys.argv) > 3 else "EAS"
            location = sys.argv[4] if len(sys.argv) > 4 else "001001"
            duration = int(sys.argv[5]) if len(sys.argv) > 5 else 60
            sender = sys.argv[6] if len(sys.argv) > 6 else "TEST_STN"
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
        print(f"  {sys.argv[0]} 1              Tornado warning (generic)")
        print(f"  {sys.argv[0]} 2              Severe weather (generic)")
        print(f"  {sys.argv[0]} 3              Weekly test (generic)")
        print(f"  {sys.argv[0]} 4              NWS vs EAS comparison")
        print(f"  {sys.argv[0]} 5              Emergency message (generic)")
        print(f"  {sys.argv[0]} all            All scenarios")
        print("\nCUSTOM ALERTS:")
        print(f"  {sys.argv[0]} interactive    Interactive mode (choose your own)")
        print(f"  {sys.argv[0]} custom EVENT ORG LOCATION DURATION SENDER")
        print(f"\n  Example: {sys.argv[0]} custom FFW WXR 001001 120 TEST_STN")
        print("    EVENT: TOR, SVR, FFW, RWT, CEM, etc.")
        print("    ORG: WXR (NWS), EAS (local), CIV (civil), PEP (entry point)")
        print("    LOCATION: 6-digit PSSCCC code (e.g., 001001 = County A)")
        print("    DURATION: Minutes (0 for tests)")
        print("    SENDER: Originating station (max 8 chars)")
