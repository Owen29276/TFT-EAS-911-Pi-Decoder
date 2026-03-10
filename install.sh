#!/bin/bash
# TFT EAS 911 - Laptop/Development Install Script

set -e

echo "TFT EAS 911 - Development Install"
echo "=================================="
echo ""

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || [ "$PYTHON_MINOR" -lt 10 ]; then
    echo "Error: Python 3.10 or newer is required (found $PYTHON_VERSION)"
    exit 1
fi
echo "Python $PYTHON_VERSION - OK"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
else
    echo "Virtual environment already exists"
fi

# Activate and install
source venv/bin/activate
echo "Installing dependencies..."
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt

echo ""
echo "Done! To get started:"
echo ""
echo "  source venv/bin/activate"
echo ""
echo "  # Run a test scenario"
echo "  python3 virtual_tft.py 1 | python3 TFT_EAS_911_Pi_logger.py"
echo ""
echo "  # Interactive mode"
echo "  python3 virtual_tft.py interactive"
echo ""
echo "See README.md for full documentation."
