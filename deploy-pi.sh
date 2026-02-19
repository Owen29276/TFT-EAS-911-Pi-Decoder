#!/bin/bash
# TFT911 EAS Logger - Universal Raspberry Pi Deployment Script
# Works on any Raspberry Pi with Raspberry Pi OS

set -e

echo "🚀 TFT911 EAS Logger - Raspberry Pi Deployment"
echo "================================================"
echo ""

# Detect home directory and user
CURRENT_USER=$(whoami)
HOME_DIR=$(eval echo ~$CURRENT_USER)

# Choose installation path
if [ -w "/opt" ]; then
    INSTALL_PATH="/opt/tft911-eas"
else
    INSTALL_PATH="$HOME_DIR/tft911-eas"
fi

echo "Installation path: $INSTALL_PATH"
echo "User: $CURRENT_USER"
echo ""

# 1. Update system
echo "1️⃣  Updating system packages..."
sudo apt update
sudo apt upgrade -y

# 2. Install system dependencies
echo "2️⃣  Installing system dependencies..."
sudo apt install -y python3-pip python3-venv git

# 3. Set up repository
if [ ! -d "$INSTALL_PATH" ]; then
    echo "3️⃣  Setting up repository at $INSTALL_PATH..."
    sudo mkdir -p "$INSTALL_PATH"
    sudo chown $CURRENT_USER:$CURRENT_USER "$INSTALL_PATH"
    cd "$INSTALL_PATH"
    git clone https://github.com/owenschnell/tft911-eas.git . || echo "Note: If not in repo, copy files manually"
else
    echo "3️⃣  Repository already exists, updating..."
    cd "$INSTALL_PATH"
    git pull 2>/dev/null || echo "   (not a git repo)"
fi

# 4. Install Python dependencies
echo "4️⃣  Installing Python dependencies..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt

# 5. Create systemd service
echo "5️⃣  Creating systemd service..."
sudo tee /etc/systemd/system/tft911-eas.service > /dev/null <<EOF
[Unit]
Description=TFT911 EAS Logger
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
EOF

# 6. Enable and start service
echo "6️⃣  Enabling systemd service..."
sudo systemctl daemon-reload
sudo systemctl enable tft911-eas.service
sudo systemctl start tft911-eas.service

# 7. Verify
echo ""
echo "✅ Deployment complete!"
echo ""
echo "Log files:"
echo "  $HOME_DIR/events.log                  # Text log"
echo "  $HOME_DIR/events.jsonl                # JSON records"
echo ""
echo "Service management:"
echo "  sudo systemctl status tft911-eas      # Check status"
echo "  sudo systemctl start tft911-eas       # Start service"
echo "  sudo systemctl stop tft911-eas        # Stop service"
echo "  sudo systemctl restart tft911-eas     # Restart service"
echo "  sudo journalctl -u tft911-eas -f      # View live logs"
echo ""
echo "Configuration:"
echo "  Edit: $INSTALL_PATH/TFT_EAS_911_Pi_logger.py"
echo "  Set PORT, BAUD, NTFY_URL (lines 33-45)"
echo "  Then restart: sudo systemctl restart tft911-eas"
echo ""
echo "Current service status:"
sudo systemctl status tft911-eas --no-pager | head -15
