#!/bin/bash
# TFT EAS 911 EAS Logger - Installation Script

set -e

echo "🚀 TFT EAS 911 EAS Logger - Installation"
echo "===================================="

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Python $PYTHON_VERSION detected"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
else
    echo "✓ Virtual environment already exists"
fi

# Activate virtual environment
source venv/bin/activate
echo "✓ Virtual environment activated"

# Install dependencies
echo "📥 Installing dependencies..."
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt

echo ""
echo "✅ Installation complete!"
echo ""
echo "Usage:"
echo "  source venv/bin/activate          # Activate environment"
echo "  python3 TFT_EAS_911_Pi_logger.py  # Run on Pi (serial mode)"
echo "  python3 virtual_tft.py 1 | python3 TFT_EAS_911_Pi_logger.py  # Test mode"
echo "  python3 virtual_tft.py interactive # Interactive testing"
echo ""
echo "For more details, see README.md"
