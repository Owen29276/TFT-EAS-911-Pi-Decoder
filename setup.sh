#!/bin/bash
# TFT EAS 911 - Universal Setup Script
#
#   bash setup.sh            Raspberry Pi → full deployment; anything else → dev install
#   bash setup.sh --server   Full deployment on any Debian/Ubuntu box (e.g. a Proxmox VM
#                            with the TFT's USB adapters passed through)

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
ask()  { read -rp "  $(echo -e "${CYAN}→${RESET}  $1")" "$2"; }

# Set "key = value" inside one [section] of an ini file without touching comments
ini_set() {  # file section key value
    sed "${SED_INPLACE[@]}" "/^\[$2\]/,/^\[/ s|^$3 *=.*|$3 = $4|" "$1"
}

# ─────────────────────────────────────────────
# Platform Detection
# ─────────────────────────────────────────────
IS_PI=false
if [ -f "/proc/device-tree/model" ] && grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
fi
SERVER_FLAG=false
RERUN="bash setup.sh"
if [ "${1:-}" = "--server" ]; then SERVER_FLAG=true; RERUN="bash setup.sh --server"; fi

DEPLOY=false
if $IS_PI || $SERVER_FLAG; then
    DEPLOY=true
fi
VIRT=$(systemd-detect-virt 2>/dev/null || true)
VIRT=${VIRT:-none}

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${RESET}"
if $IS_PI; then
    echo -e "${BOLD}║     TFT EAS 911 — Pi Deployment      ║${RESET}"
elif $DEPLOY; then
    echo -e "${BOLD}║   TFT EAS 911 — Server Deployment    ║${RESET}"
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
# Deployment helpers
# ─────────────────────────────────────────────

# Give the two USB-serial adapters stable names (/dev/tft911-data, /dev/tft911-cmd).
# ttyUSB0/1 numbering can swap on reboot; these names can't.
setup_serial_names() {
    local rules=${TFT_UDEV_RULES:-/etc/udev/rules.d/99-tft911.rules}   # override for testing
    local ports=()
    for p in ${TFT_SERIAL_GLOB:-/dev/ttyUSB* /dev/ttyACM*}; do [ -e "$p" ] && ports+=("$p"); done
    if [ ${#ports[@]} -eq 0 ]; then
        warn "No USB serial adapters found."
        warn "Connect (or pass through) both adapters, then re-run: $RERUN"
        return
    fi
    if [ -f "$rules" ]; then
        info "Existing $rules:"
        sed 's/^/      /' "$rules"
        ask "Keep it? (Y/n): " KEEP_RULES
        if [ "${KEEP_RULES,,}" != "n" ]; then ok "Keeping existing serial names"; return; fi
    fi

    local serials=() paths=()
    info "USB serial adapters found:"
    for i in "${!ports[@]}"; do
        local props; props=$(udevadm info -q property -n "${ports[$i]}" 2>/dev/null || true)
        serials[$i]=$(echo "$props" | sed -n 's/^ID_SERIAL=//p')
        paths[$i]=$(echo "$props" | sed -n 's/^ID_PATH=//p')
        echo "      $((i+1))) ${ports[$i]}   id=${serials[$i]:-?}   port=${paths[$i]:-?}"
    done
    info "Can't tell them apart? Unplug one adapter and run: ls /dev/ttyUSB*"
    ask "Number of the J103 adapter (logger output, 1200 baud) [Enter = skip]: " DATA_N
    ask "Number of the J303/COM3 adapter (control, 9600 baud) [Enter = skip]: " CMD_N

    # Match on the adapter's serial number when it's unique; otherwise on the USB port
    rule_for() {  # index name
        local idx=$1 name=$2 dupes=0
        for s in "${serials[@]}"; do [ "$s" = "${serials[$idx]}" ] && dupes=$((dupes+1)); done
        if [ -n "${serials[$idx]}" ] && [ "$dupes" -eq 1 ]; then
            echo "SUBSYSTEM==\"tty\", ENV{ID_SERIAL}==\"${serials[$idx]}\", SYMLINK+=\"$name\", GROUP=\"dialout\", MODE=\"0660\""
        else
            echo "# Adapters share a serial number — matching on USB port; keep this one in the same port"
            echo "SUBSYSTEM==\"tty\", ENV{ID_PATH}==\"${paths[$idx]}\", SYMLINK+=\"$name\", GROUP=\"dialout\", MODE=\"0660\""
        fi
    }
    local out="# TFT EAS 911 serial adapters — written by setup.sh"
    if [[ "$DATA_N" =~ ^[0-9]+$ ]] && [ "$DATA_N" -ge 1 ] && [ "$DATA_N" -le ${#ports[@]} ]; then
        out+=$'\n'"$(rule_for $((DATA_N-1)) tft911-data)"
    fi
    if [[ "$CMD_N" =~ ^[0-9]+$ ]] && [ "$CMD_N" -ge 1 ] && [ "$CMD_N" -le ${#ports[@]} ]; then
        out+=$'\n'"$(rule_for $((CMD_N-1)) tft911-cmd)"
    fi
    if [ "$(echo "$out" | wc -l)" -le 1 ]; then
        info "Skipped — config.ini expects /dev/tft911-data and /dev/tft911-cmd"
        return
    fi
    echo "$out" | sudo tee "$rules" > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=tty --action=add
    sleep 1
    ls -l /dev/tft911-* 2>/dev/null | sed 's/^/      /' || true
    ok "Serial names written to $rules"
}

# Point audio capture (and, on a server, default playback) at the USB sound card
setup_audio_device() {
    local card
    card=$(sed -n 's/^ *[0-9]* \[\([^ ]*\) *\]: USB-Audio.*/\1/p' /proc/asound/cards 2>/dev/null | head -1)
    if [ -z "$card" ]; then
        warn "No USB sound card found — alert audio capture stays off."
        info "Connect (or pass through) the card, then set [audio] device in config.ini"
        return
    fi
    ok "USB sound card: $card"
    ini_set "$INSTALL_PATH/config.ini" audio device "plughw:CARD=$card,DEV=0"
    ok "config.ini [audio] device = plughw:CARD=$card,DEV=0"

    if ! $IS_PI; then
        # A VM has no other sound device: make the USB card the default so TTS,
        # browser PTT and VoIP recordings (aplay) go out to TFT CH1.
        printf 'defaults.pcm.card %s\ndefaults.ctl.card %s\n' "$card" "$card" | sudo tee /etc/asound.conf > /dev/null
        ok "Default playback → $card (/etc/asound.conf)"
    fi

    ask "Enable alert audio capture now? (Y/n): " EN_AUDIO
    if [ "${EN_AUDIO,,}" != "n" ]; then
        ini_set "$INSTALL_PATH/config.ini" audio enabled true
        ok "Audio capture enabled"
    fi
}

# Whisper on the same machine, bound to localhost, token generated and wired to both ends
install_local_transcriber() {
    local default=$1   # Y or N
    info "Whisper transcribes each alert's audio (needs ~2 GB RAM; too slow on a Pi)."
    ask "Install the transcription service on this machine? ($( [ "$default" = Y ] && echo "Y/n" || echo "y/N" )): " DO_TX
    DO_TX=${DO_TX:-$default}
    if [ "${DO_TX,,}" != "y" ]; then
        info "Skipped — see transcriber/README.md to run it elsewhere"
        return
    fi

    local tdir="$INSTALL_PATH/transcriber"
    python3 -m venv "$tdir/venv"
    "$tdir/venv/bin/pip" install --upgrade pip > /dev/null 2>&1
    "$tdir/venv/bin/pip" install -r "$tdir/requirements.txt"
    mkdir -p "$tdir/models"
    ok "faster-whisper installed"

    local envf=/etc/tft911-transcriber.env token
    if sudo test -f "$envf"; then
        token=$(sudo sed -n 's/^TRANSCRIBE_TOKEN=//p' "$envf")
        ok "Reusing token from $envf"
    else
        token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
        sudo install -m 600 -o root -g root /dev/null "$envf"
        sudo tee "$envf" > /dev/null <<ENVEOF
TRANSCRIBE_TOKEN=$token
WHISPER_MODEL=small.en
WHISPER_THREADS=0
# Local only — the logger on this machine is the only client
TRANSCRIBE_BIND=127.0.0.1
TRANSCRIBE_PORT=8765
HF_HOME=$tdir/models
# Uncomment only if CTranslate2 dies with "Illegal instruction"
# CT2_FORCE_CPU_ISA=AVX
ENVEOF
        ok "Wrote $envf (root-only) with a new token"
    fi
    ini_set "$INSTALL_PATH/config.ini" transcribe url "http://127.0.0.1:8765"
    ini_set "$INSTALL_PATH/secrets.ini" transcribe token "$token"
    ok "Logger pointed at http://127.0.0.1:8765 (token in secrets.ini)"

    sudo tee /etc/systemd/system/tft911-transcriber.service > /dev/null <<SVCEOF
[Unit]
Description=TFT EAS 911 Whisper transcription server
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$tdir
EnvironmentFile=$envf
ExecStart=$tdir/venv/bin/python server.py
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=$tdir/models
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF
    TRANSCRIBER_INSTALLED=true
    ok "Transcription service file written (first start downloads the model, ~500 MB)"
}

# ─────────────────────────────────────────────
# Deployment Path (Raspberry Pi, or --server e.g. Proxmox VM)
# ─────────────────────────────────────────────
if $DEPLOY; then
    CURRENT_USER=$(whoami)
    HOME_DIR=$(eval echo ~$CURRENT_USER)
    TRANSCRIBER_INSTALLED=false

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
    [ "$VIRT" != "none" ] && info "Virtualized:  $VIRT"

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
    ask "Enter timezone (e.g. America/New_York) or press Enter to keep [$CURRENT_TZ]: " INPUT_TZ
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
    sudo apt install -y python3-pip python3-venv git espeak alsa-utils usbutils -qq
    if [ "$VIRT" = "kvm" ] || [ "$VIRT" = "qemu" ]; then
        sudo apt install -y qemu-guest-agent -qq && sudo systemctl enable --now qemu-guest-agent 2>/dev/null || true
    fi
    ok "Dependencies installed"

    step "2b" "Permissions"
    # dialout = serial ports, audio = sound card, systemd-journal = dashboard log view.
    # Pi OS gives its default user these; a fresh Debian VM user has none of them.
    for grp in dialout audio systemd-journal; do
        if id -nG "$CURRENT_USER" | grep -qw "$grp"; then
            ok "$CURRENT_USER already in $grp"
        else
            sudo usermod -aG "$grp" "$CURRENT_USER"
            ok "Added $CURRENT_USER to $grp"
        fi
    done

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

    step "3b" "Serial port names"
    setup_serial_names

    step "4" "Installing Python dependencies"
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip > /dev/null 2>&1
    pip install -r requirements.txt
    ok "Python dependencies installed"

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

    if [ ! -f "$INSTALL_PATH/secrets.ini" ]; then
        cp "$INSTALL_PATH/secrets.ini.example" "$INSTALL_PATH/secrets.ini"
        chmod 600 "$INSTALL_PATH/secrets.ini"
        ok "Created secrets.ini (chmod 600) — add MQTT / transcription credentials there"
    fi

    step "5a" "Alert audio (USB sound card)"
    setup_audio_device

    step "5b" "Station setup wizard"
    echo ""
    info "The setup wizard configures your station identity, COM3 PIN,"
    info "timezone, and location keys for alert origination."
    echo ""
    ask "Run station setup wizard now? (Y/n): " RUN_WIZARD
    if [ "${RUN_WIZARD,,}" != "n" ]; then
        source venv/bin/activate
        python3 -c "from TFT_Control import setup_wizard; setup_wizard()"
    else
        info "Skipped — run 'python3 TFT_Control.py' and select 's' to configure later"
    fi

    step "5c" "Dashboard password"
    info "The web dashboard can originate live EAS alerts, so it requires a login."
    info "The web service will not start until a password is set."
    source venv/bin/activate
    until python3 web.py --set-password; do
        warn "Password not saved — try again (Ctrl-C to abort setup)"
    done

    step "5d" "Whisper transcription"
    if $IS_PI; then install_local_transcriber N; else install_local_transcriber Y; fi

    step "6" "Creating systemd services"
    sudo tee /etc/systemd/system/tft911-eas.service > /dev/null <<SVCEOF
[Unit]
Description=TFT EAS 911 Logger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$INSTALL_PATH
Environment="PATH=$INSTALL_PATH/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$INSTALL_PATH/venv/bin/python3 TFT_logger.py --serial
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
After=network-online.target tft911-eas.service
Wants=network-online.target

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
    SERVICES="tft911-eas.service tft911-eas-web.service"
    $TRANSCRIBER_INSTALLED && SERVICES="tft911-transcriber.service $SERVICES"
    sudo systemctl daemon-reload
    sudo systemctl enable $SERVICES
    sudo systemctl restart $SERVICES
    ok "Services enabled and started: $SERVICES"

    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<host-ip>")

    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${GREEN}${BOLD}  Setup complete — all services running${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  ${BOLD}Web dashboard${RESET}"
    echo "    http://$HOST_IP:5000"
    echo ""
    echo -e "  ${BOLD}Alert files${RESET}"
    echo "    $INSTALL_PATH/alerts/"
    echo ""
    echo -e "  ${BOLD}Service management${RESET}"
    echo "    sudo systemctl status  $SERVICES"
    echo "    sudo journalctl -u tft911-eas -f"
    echo ""
    echo -e "  ${BOLD}Configuration${RESET}"
    echo "    nano $INSTALL_PATH/config.ini      # settings"
    echo "    nano $INSTALL_PATH/secrets.ini     # MQTT password etc."
    echo "    sudo systemctl restart tft911-eas tft911-eas-web"
    echo ""
    warn "Log out and back in (or reboot) for the new group memberships to apply to your shell."
    echo ""
    echo -e "  ${BOLD}Current status${RESET}"
    sudo systemctl status $SERVICES --no-pager | head -30

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
    echo -e "  ${BOLD}Set dashboard password (required before web.py will start)${RESET}"
    echo "    python3 web.py --set-password"
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
    echo -e "  ${BOLD}Deploying on a server / Proxmox VM instead?${RESET}"
    echo "    bash setup.sh --server      # see PROXMOX_DEPLOYMENT.md"
    echo ""
    echo "  See README.md for full documentation."
    echo ""
fi
