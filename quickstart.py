#!/usr/bin/env python3
"""
Quick start guide - Run this to test the system
"""

import subprocess
import sys

def main():
    print("=" * 60)
    print("TFT911 EAS Logger - Quick Start Test")
    print("=" * 60)
    print()
    
    # Test 1: Check Python version
    print("1. Checking Python version...")
    version = sys.version_info
    if version.major == 3 and version.minor >= 10:
        print(f"   ✓ Python {version.major}.{version.minor} (OK)")
    else:
        print(f"   ✗ Python {version.major}.{version.minor} (Need 3.10+)")
        return False
    print()
    
    # Test 2: Check imports
    print("2. Checking dependencies...")
    try:
        import serial
        print("   ✓ pyserial installed")
    except ImportError:
        print("   ✗ pyserial not installed")
        return False
    
    try:
        import requests
        print("   ✓ requests installed")
    except ImportError:
        print("   ✗ requests not installed")
        return False
    
    try:
        from EAS2Text import EAS2Text
        print("   ✓ EAS2Text-Remastered installed")
    except ImportError:
        print("   ✗ EAS2Text-Remastered not installed")
        return False
    print()
    
    # Test 3: Try a simple decode
    print("3. Testing EAS2Text decoding...")
    try:
        from EAS2Text import EAS2Text
        test_header = "ZCZC-EAS-TOR-036109+0060-0492145-KITH_EAS-"
        oof = EAS2Text(test_header)
        message = getattr(oof, "EASText", None)
        if message:
            print(f"   ✓ Decoded: {message.split(chr(10))[0]}")
        else:
            print("   ✗ Decode failed")
            return False
    except Exception as e:
        print(f"   ✗ Error: {e}")
        return False
    print()
    
    # Test 4: Run a test scenario
    print("4. Running test scenario...")
    print()
    try:
        result = subprocess.run(
            ["python3", "virtual_tft.py", "1"],
            capture_output=True,
            timeout=5,
            text=True
        )
        if result.returncode == 0:
            print("   ✓ Test scenario generated successfully")
            # Show first few lines
            lines = result.stdout.split('\n')[:3]
            for line in lines:
                if line:
                    print(f"      {line[:60]}")
        else:
            print(f"   ✗ Test scenario failed")
            return False
    except Exception as e:
        print(f"   ✗ Error running test: {e}")
        return False
    print()
    
    print("=" * 60)
    print("✅ All checks passed! System is ready.")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  - Run: python3 virtual_tft.py 1 | python3 TFT_EAS_911_Pi_logger.py")
    print("  - Try: python3 virtual_tft.py interactive")
    print("  - Read: README.md for full documentation")
    print()
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
