#!/bin/bash
# TFT EAS 911 - Universal Setup Script
# Auto-detects Raspberry Pi vs laptop and runs the appropriate setup

set -e

# macOS requires sed -i '', Linux requires sed -i (no suffix)
SED_INPLACE=(-i)
[[ "$OSTYPE" == "darwin"* ]] && SED_INPLACE=(-i '')

# ─────────────────────────────────────────────
# Colors & helpers
# ─────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✔${RESET}  $1"; }
info() { echo -e "  ${CYAN}→${RESET}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
err()  { echo -e "  ${RED}✘${RESET}  $1"; }
step() { echo -e "\n${BOLD}[$1]${RESET} $2"; }

# ─────────────────────────────────────────────
# Platform Detection
# ─────────────────────────────────────────────
IS_PI=false
if [ -f "/proc/device-tree/model" ] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${RESET}"
if $IS_PI; then
    echo -e "${BOLD}║     TFT EAS 911 — Pi Deployment      ║${RESET}"
else
    echo -e "${BOLD}║   TFT EAS 911 — Development Install  ║${RESET}"
fi
echo -e "${BOLD}╚══════════════════════════════════════╝${RESET}"
echo ""

# ─────────────────────────────────────────────
# Shared: Python version check
# ─────────────────────────────────────────────
step "0" "Checking Python version"
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || [ "$PYTHON_MINOR" -lt 10 ]; then
    err "Python 3.10+ required (found $PYTHON_VERSION)"
    exit 1
fi
ok "Python $PYTHON_VERSION"

# ─────────────────────────────────────────────
# Raspberry Pi Path
# ─────────────────────────────────────────────
if $IS_PI; then
    CURRENT_USER=$(whoami)
    HOME_DIR=$(eval echo ~$CURRENT_USER)

    # If setup.sh is already running from inside the repo, use that directory
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if git -C "$SCRIPT_DIR" rev-parse --git-dir > /dev/null 2>&1; then
        INSTALL_PATH="$SCRIPT_DIR"
    elif [ -w "/opt" ]; then
        INSTALL_PATH="/opt/tft911-eas"
    else
        INSTALL_PATH="$HOME_DIR/tft911-eas"
    fi

    info "Install path: $INSTALL_PATH"
    info "User:         $CURRENT_USER"

    step "1" "Checking date, time and timezone"
    CURRENT_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "Unknown")
    NTP_SYNC=$(timedatectl show --property=NTPSynchronized --value 2>/dev/null || echo "unknown")
    info "Timezone: $CURRENT_TZ"
    info "Time:     $(date)"
    if [ "$NTP_SYNC" = "yes" ]; then
        ok "NTP synchronized"
    else
        warn "NTP not synchronized — clock may be wrong, EAS timestamps could be off"
        sudo timedatectl set-ntp true 2>/dev/null && info "NTP enabled" || true
    fi
    echo ""
    read -rp "  $(echo -e "${CYAN}→${RESET}  Enter timezone (e.g. America/New_York) or press Enter to keep [$CURRENT_TZ]: ")" INPUT_TZ
    if [ -n "$INPUT_TZ" ]; then
        if sudo timedatectl set-timezone "$INPUT_TZ" 2>/dev/null; then
            ok "Timezone set to $INPUT_TZ"
        else
            warn "Invalid timezone '$INPUT_TZ' — keeping $CURRENT_TZ"
        fi
    else
        ok "Keeping timezone: $CURRENT_TZ"
    fi

    step "1b" "Updating system packages"
    sudo apt update -qq
    sudo apt upgrade -y -qq
    ok "System packages updated"

    step "2" "Installing system dependencies"
    sudo apt install -y python3-pip python3-venv git espeak alsa-utils -qq
    ok "Dependencies installed"
    sudo usermod -a -G dialout $CURRENT_USER
    ok "User $CURRENT_USER added to dialout group (serial port access)"

    step "2b" "Serial port permissions"
    if ! groups "$CURRENT_USER" | grep -qw dialout; then
        sudo usermod -aG dialout "$CURRENT_USER"
        ok "Added $CURRENT_USER to dialout group (serial port access)"
    else
        ok "$CURRENT_USER already in dialout group"
    fi

    step "3" "Setting up repository"
    if [ "$SCRIPT_DIR" = "$INSTALL_PATH" ]; then
        info "Running from repo — skipping clone."
        cd "$INSTALL_PATH"
        ok "Using existing repo at $INSTALL_PATH"
    elif [ ! -d "$INSTALL_PATH" ]; then
        info "Cloning to $INSTALL_PATH..."
        sudo mkdir -p "$INSTALL_PATH"
        sudo chown $CURRENT_USER:$CURRENT_USER "$INSTALL_PATH"
        cd "$INSTALL_PATH"
        git clone https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder.git .
        ok "Repository cloned"
    else
        info "Repository already exists, pulling latest..."
        cd "$INSTALL_PATH"
        git checkout -- config.ini 2>/dev/null || true
        git pull 2>/dev/null && ok "Repository updated" || warn "Could not pull latest — continuing with existing version"
    fi

    step "4" "Installing Python dependencies"
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip > /dev/null 2>&1
    pip install -r requirements.txt
    ok "Python dependencies installed"

    step "4b" "Configuring serial ports"
    echo ""
    info "Scanning for USB serial adapters..."
    SERIAL_DEVS=($(ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyS0 2>/dev/null))
    if [ ${#SERIAL_DEVS[@]} -eq 0 ]; then
        warn "No serial devices found — plug in both USB adapters and re-run setup, or edit config.ini manually."
    else
        echo ""
        echo "  Found serial devices:"
        for i in "${!SERIAL_DEVS[@]}"; do
            echo "    [$i] ${SERIAL_DEVS[$i]}"
        done
        echo ""

        # J103 data port (1200 baud, receives SAME headers)
        CURRENT_DATA=$(grep -m1 "^port" "$INSTALL_PATH/config.ini" 2>/dev/null | head -1 | sed 's/.*= *//' || echo "/dev/ttyUSB0")
        read -rp "  $(echo -e "${CYAN}→${RESET}  J103 data port (SAME headers, 1200 baud) [${CURRENT_DATA}]: ")" INPUT_DATA
        DATA_PORT="${INPUT_DATA:-$CURRENT_DATA}"

        # J303 control port (9600 baud, DTMF commands)
        CURRENT_CTRL=$(grep -m1 "^port" <(awk '/\[control\]/,/\[/' "$INSTALL_PATH/config.ini" 2>/dev/null) | sed 's/.*= *//' || echo "/dev/ttyUSB1")
        read -rp "  $(echo -e "${CYAN}→${RESET}  J303 control port (COM3, DTMF, 9600 baud) [${CURRENT_CTRL}]: ")" INPUT_CTRL
        CTRL_PORT="${INPUT_CTRL:-$CURRENT_CTRL}"

        # Write into config.ini using Python (handles ini sections cleanly)
        source venv/bin/activate
        python3 - <<PYEOF
import configparser, pathlib
p = pathlib.Path("$INSTALL_PATH/config.ini")
c = configparser.ConfigParser()
if p.exists():
    c.read(p)
if not c.has_section("serial"):
    c.add_section("serial")
if not c.has_section("control"):
    c.add_section("control")
c.set("serial",  "port", "$DATA_PORT")
c.set("control", "port", "$CTRL_PORT")
with open(p, "w") as f:
    c.write(f)
print(f"  Saved: J103={c.get('serial','port')}  J303={c.get('control','port')}")
PYEOF
        ok "Serial ports written to config.ini"
    fi
    echo ""

    step "5" "Configure push notifications (optional)"
    echo ""
    info "The logger can push alerts to your phone via ntfy.sh"
    info "Enter just the topic name — e.g. my_eas_alerts"
    echo ""
    read -p "  ntfy topic (Enter to skip): " NTFY_TOPIC

    if [ -n "$NTFY_TOPIC" ]; then
        sed "${SED_INPLACE[@]}" "s|ntfy_[a-z_]*\s*=.*|ntfy_topic = $NTFY_TOPIC|g" "$INSTALL_PATH/config.ini"
        if grep -q "ntfy_topic = $NTFY_TOPIC" "$INSTALL_PATH/config.ini"; then
            ok "ntfy topic saved to config.ini"
        else
            warn "Could not write to config.ini — set ntfy_topic manually"
        fi
    else
        info "Skipped — edit config.ini to enable notifications later"
    fi

    step "5b" "Station setup wizard"
    echo ""
    info "The setup wizard configures your station identity, COM3 PIN,"
    info "timezone, and location keys for alert origination."
    echo ""
    read -rp "  $(echo -e "${CYAN}→${RESET}  Run station setup wizard now? (Y/n): ")" RUN_WIZARD
    if [ "${RUN_WIZARD,,}" != "n" ]; then
        source venv/bin/activate
        python3 -c "from TFT_Control import setup_wizard; setup_wizard()"
    else
        info "Skipped — run 'python3 TFT_Control.py' and select 's' to configure later"
    fi

    step "6" "Creating systemd services"
    sudo tee /etc/systemd/system/tft911-eas.service > /dev/null <<SVCEOF
[Unit]
Description=TFT EAS 911 Logger
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$INSTALL_PATH
Environment="PATH=$INSTALL_PATH/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$INSTALL_PATH/venv/bin/python3 TFT_logger.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF
    ok "Logger service file written"

    sudo tee /etc/systemd/system/tft911-eas-web.service > /dev/null <<SVCEOF
[Unit]
Description=TFT EAS 911 Web Dashboard
After=network.target tft911-eas.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$INSTALL_PATH
Environment="PATH=$INSTALL_PATH/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$INSTALL_PATH/venv/bin/python3 web.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF
    ok "Web dashboard service file written"

    step "7" "Enabling and starting services"
    sudo systemctl daemon-reload
    sudo systemctl enable tft911-eas.service tft911-eas-web.service
    sudo systemctl start  tft911-eas.service tft911-eas-web.service
    ok "Services enabled and started"

    PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<pi-ip>")

    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${GREEN}${BOLD}  Setup complete — all services running${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  ${BOLD}Web dashboard${RESET}"
    echo "    http://$PI_IP:5000"
    echo ""
    echo -e "  ${BOLD}Log files${RESET}"
    echo "    $HOME_DIR/eas_logs/alerts/events.log"
    echo "    $HOME_DIR/eas_logs/alerts/events.jsonl"
    echo ""
    echo -e "  ${BOLD}Service management${RESET}"
    echo "    sudo systemctl status  tft911-eas tft911-eas-web"
    echo "    sudo systemctl restart tft911-eas tft911-eas-web"
    echo "    sudo journalctl -u tft911-eas -f"
    echo "    sudo journalctl -u tft911-eas-web -f"
    echo ""
    echo -e "  ${BOLD}Configuration${RESET}"
    echo "    nano $INSTALL_PATH/config.ini"
    echo "    sudo systemctl restart tft911-eas tft911-eas-web"
    echo ""
    echo -e "  ${BOLD}Current status${RESET}"
    sudo systemctl status tft911-eas tft911-eas-web --no-pager | head -20

# ─────────────────────────────────────────────
# Laptop / Development Path
# ─────────────────────────────────────────────
else
    step "1" "Setting up virtual environment"
    if [ ! -d "venv" ]; then
        python3 -m venv venv
        ok "Virtual environment created"
    else
        ok "Virtual environment already exists"
    fi

    step "2" "Installing dependencies"
    source venv/bin/activate
    pip install --upgrade pip > /dev/null 2>&1
    pip install -r requirements.txt
    ok "Dependencies installed"

    step "3" "Configure push notifications (optional)"
    echo ""
    info "The logger can push alerts to your phone via ntfy.sh"
    info "Enter just the topic name — e.g. my_eas_alerts"
    echo ""
    read -p "  ntfy topic (Enter to skip): " NTFY_TOPIC

    if [ -n "$NTFY_TOPIC" ]; then
        sed "${SED_INPLACE[@]}" "s|ntfy_[a-z_]*\s*=.*|ntfy_topic = $NTFY_TOPIC|g" config.ini
        if grep -q "ntfy_topic = $NTFY_TOPIC" config.ini; then
            ok "ntfy topic saved to config.ini"
        else
            warn "Could not write to config.ini — set ntfy_topic manually"
        fi
    else
        info "Skipped — edit config.ini to enable notifications later"
    fi

    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${GREEN}${BOLD}  Setup complete${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  ${BOLD}Activate environment${RESET}"
    echo "    source venv/bin/activate"
    echo ""
    echo -e "  ${BOLD}Run logger with simulated alerts${RESET}"
    echo "    python3 virtual_tft.py 1 | python3 TFT_logger.py"
    echo ""
    echo -e "  ${BOLD}Run web dashboard${RESET}"
    echo "    python3 web.py"
    echo "    # then open http://localhost:5000"
    echo ""
    echo -e "  ${BOLD}Run station setup wizard${RESET}"
    echo "    python3 TFT_Control.py  # select s"
    echo ""
    echo -e "  ${BOLD}Interactive test mode${RESET}"
    echo "    python3 virtual_tft.py interactive"
    echo ""
    echo "  See README.md for full documentation."
    echo ""
fi
