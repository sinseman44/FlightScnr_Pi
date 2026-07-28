#!/usr/bin/env bash
# Install only the Debian Lite Cage/Wayland service. This script deliberately
# does not modify boot firmware files, install Plymouth, or configure a desktop.
set -euo pipefail

NO_START=0
case "${1:-}" in
    "") ;;
    --no-start) NO_START=1 ;;
    -h|--help)
        echo "Usage: sudo bash flightscnr/setup/install-cage-service.sh [--no-start]"
        exit 0
        ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

SETUP_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SETUP_DIR/../.." && pwd)"
VENV_DIR="$REPO_ROOT/flightscnr-venv"
ENV_FILE="/etc/flightscnr.env"
DATA_DIR="/var/lib/flightscnr"
SERVICE_DEST="/etc/systemd/system/flightscnr.service"

if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    REPO_OWNER="$SUDO_USER"
else
    REPO_OWNER="$(stat -c '%U' "$REPO_ROOT")"
fi
REPO_GROUP="$(id -gn "$REPO_OWNER")"
SYSTEMCTL="$(command -v systemctl)"

for command in cage dbus-run-session chvt visudo; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Missing command: $command" >&2
        echo "Install prerequisites: sudo apt install --no-install-recommends cage dbus-user-session sudo" >&2
        exit 1
    fi
done

if [ ! -x "$VENV_DIR/bin/python3" ]; then
    echo "Missing Python environment: $VENV_DIR/bin/python3" >&2
    echo "Create the FlightScnr virtual environment and install requirements first." >&2
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    install -m 0600 -o root -g root "$REPO_ROOT/.env.example" "$ENV_FILE"
fi

set_env() {
    local key="$1" value="$2"
    if grep -qE "^[[:space:]]*${key}=" "$ENV_FILE"; then
        sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

set_env DISPLAY_WIDTH 480
set_env DISPLAY_HEIGHT 320
set_env DISPLAY_ROTATION 0
set_env DISPLAY_FULLSCREEN False
set_env SDL_VIDEODRIVER x11
set_env TOUCH_USE_FINGER_EVENTS False
set_env WEB_PORT 8080
chmod 0600 "$ENV_FILE"
chown root:root "$ENV_FILE"

install -d -m 0755 -o "$REPO_OWNER" -g "$REPO_GROUP" "$DATA_DIR" "$DATA_DIR/maps"
install -m 0644 "$SETUP_DIR/cage.pam" /etc/pam.d/cage

sed \
    -e "s|__REPO_DIR__|$REPO_ROOT|g" \
    -e "s|__REPO_OWNER__|$REPO_OWNER|g" \
    -e "s|__REPO_GROUP__|$REPO_GROUP|g" \
    "$SETUP_DIR/flightscnr-cage.service" > "$SERVICE_DEST"
chmod 0644 "$SERVICE_DEST"

SUDOERS_DEST="/etc/sudoers.d/flightscnr-cage"
sed \
    -e "s|__REPO_OWNER__|$REPO_OWNER|g" \
    -e "s|__SYSTEMCTL__|$SYSTEMCTL|g" \
    "$SETUP_DIR/sudoers-flightscnr-cage" > "$SUDOERS_DEST"
chmod 0440 "$SUDOERS_DEST"
if ! visudo -cf "$SUDOERS_DEST" >/dev/null; then
    rm -f "$SUDOERS_DEST"
    echo "Invalid sudoers file; installation aborted." >&2
    exit 1
fi

systemctl disable --now cage@tty7.service >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable flightscnr.service

if [ "$NO_START" -eq 0 ]; then
    systemctl restart flightscnr.service
fi

cat <<MSG
Cage service installed.
  Service: sudo systemctl status flightscnr.service
  Logs:    sudo journalctl -t flightscnr -f
  Config:  sudoedit /etc/flightscnr.env
MSG
