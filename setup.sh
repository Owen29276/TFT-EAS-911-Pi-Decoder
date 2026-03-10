#!/bin/bash
# TFT EAS 911 - Universal Setup Script
# Auto-detects Raspberry Pi vs laptop and runs the appropriate setup

set -e

# =============================================
# Platform Detection
# =============================================

IS_PI=false
if [ -f "/proc/device-tree/model" ] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
fi

if $IS_PI; then
    echo "TFT EAS 911 - Raspberry Pi Deployment"
    echo "======================================"
else
    echo "TFT EAS 911 - Development Install"
    echo "=================================="
fi
echo ""

# =============================================
# Shared: Python version check
# =============================================

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || [ "$PYTHON_MINOR" -lt 10 ]; then
    echo "Error: Python 3.10 or newer is required (found $PYTHON_VERSION)"
    exit 1
fi
echo "Python $PYTHON_VERSION - OK"

# =============================================
# Raspberry Pi Path
# =============================================

if $IS_PI; then
    CURRENT_USER=$(whoami)
    HOME_DIR=$(eval echo ~$CURRENT_USER)

    # Choose installation directory
    if [ -w "/opt" ]; then
        INSTALL_PATH="/opt/tft911-eas"
    else
        INSTALL_PATH="$HOME_DIR/tft911-eas"
    fi

    echo "Install path: $INSTALL_PATH"
    echo "User: $CURRENT_USER"
    echo ""

    # 1. Update system packages
    echo "1. Updating system packages..."
    sudo apt update
    sudo apt upgrade -y

    # 2. Install system dependencies
    echo "2. Installing system dependencies..."
    sudo apt install -y python3-pip python3-venv git

    # 3. Clone or update repo
    if [ ! -d "$INSTALL_PATH" ]; then
        echo "3. Cloning repository to $INSTALL_PATH..."
        sudo mkdir -p "$INSTALL_PATH"
        sudo chown $CURRENT_USER:$CURRENT_USER "$INSTALL_PATH"
        cd "$INSTALL_PATH"
        git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git .
    else
        echo "3. Repository already exists, updating..."
        cd "$INSTALL_PATH"
        git pull 2>/dev/null || echo "   (not a git repo, skipping pull)"
    fi

    # 4. Create venv and install Python dependencies
    echo "4. Installing Python dependencies..."
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip > /dev/null 2>&1
    pip install -r requirements.txt

    # 5. Configure ntfy notifications (optional)
    echo ""
    echo "5. Configure push notifications (optional)"
    echo "   The logger can send alerts to your phone via ntfy.sh"
    echo "   Example: https://ntfy.sh/my_alerts"
    echo ""
    read -p "   Enter ntfy URL (or press Enter to skip): " NTFY_URL

    if [ -n "$NTFY_URL" ]; then
        sed -i "s|ntfy_url = .*|ntfy_url = $NTFY_URL|g" config.ini
        echo "   ntfy configured: $NTFY_URL"
    else
        echo "   ntfy disabled (edit config.ini to enable later)"
    fi

    # 6. Create systemd service
    echo ""
    echo "6. Creating systemd service..."
    sudo tee /etc/systemd/system/tft911-eas.service > /dev/null <<SVCEOF
[Unit]
Description=TFT EAS 911 EAS Logger
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$INSTALL_PATH
Environment="PATH=$INSTALL_PATH/venv/bin"
ExecStart=$INSTALL_PATH/venv/bin/python3 TFT_EAS_911_Pi_logger.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

    # 7. Enable and start service
    echo "7. Enabling and starting service..."
    sudo systemctl daemon-reload
    sudo systemctl enable tft911-eas.service
    sudo systemctl start tft911-eas.service

    echo ""
    echo "Done! Logger is running as a systemd service."
    echo ""
    echo "Service management:"
    echo "  sudo systemctl status tft911-eas      # Check status"
    echo "  sudo systemctl restart tft911-eas     # Restart"
    echo "  sudo journalctl -u tft911-eas -f      # Live logs"
    echo ""
    echo "Configuration:"
    echo "  nano $INSTALL_PATH/config.ini"
    echo "  sudo systemctl restart tft911-eas"
    echo ""
    echo "Current status:"
    sudo systemctl status tft911-eas --no-pager | head -10

# =============================================
# Laptop / Development Path
# =============================================

else
    # Create venv
    if [ ! -d "venv" ]; then
        echo "Creating virtual environment..."
        python3 -m venv venv
    else
        echo "Virtual environment already exists"
    fi

    # Install dependencies
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
fi
