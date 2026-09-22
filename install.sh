#!/bin/bash
# FaceCam Lock installer for Omarchy.
#
#   omarchy plugin add https://github.com/shubharthaksangharsha/facecam-lock --enable
#   ~/.config/omarchy/plugins/facecam.lock/install.sh
#
# or from a git checkout anywhere: ./install.sh
# Safe to re-run; each step skips work that is already done.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="facecam.lock"
PLUGIN_DIR="$HOME/.config/omarchy/plugins/$PLUGIN_ID"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/facecam-lock"
VENV="$DATA_DIR/venv"
PAM_HELPER="/usr/local/lib/facecam-lock/pam-auth"
PAM_FILE="/etc/pam.d/omarchy-lock-fingerprint"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

step() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok() { printf '    \033[32m✓\033[0m %s\n' "$*"; }

step "System packages"
missing=()
python3 -c 'import cv2' 2>/dev/null || missing+=(python-opencv)
python3 -c 'import numpy' 2>/dev/null || missing+=(python-numpy)
if ((${#missing[@]})); then
  if command -v omarchy >/dev/null; then omarchy pkg add "${missing[@]}"; else sudo pacman -S --needed "${missing[@]}"; fi
fi
ok "OpenCV $(python3 -c 'import cv2; print(cv2.__version__)')"

step "Python environment ($VENV)"
mkdir -p "$DATA_DIR"
[[ -x $VENV/bin/python ]] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -c 'import fastapi, uvicorn, jinja2' 2>/dev/null ||
  "$VENV/bin/pip" install --quiet --disable-pip-version-check fastapi uvicorn jinja2
ok "fastapi, uvicorn, jinja2"

step "Face models"
bash "$APP_DIR/scripts/fetch-models.sh" "$APP_DIR/models"

step "Command"
mkdir -p "$HOME/.local/bin"
ln -sf "$APP_DIR/bin/facecam-lock" "$HOME/.local/bin/facecam-lock"
chmod +x "$APP_DIR/bin/facecam-lock"
ok "~/.local/bin/facecam-lock"

step "Face daemon (systemd user service)"
mkdir -p "$UNIT_DIR"
cat >"$UNIT_DIR/facecam-lock.service" <<EOF
[Unit]
Description=FaceCam Lock face daemon (keeps face models warm for the lock screen)

[Service]
ExecStart=$APP_DIR/bin/facecam-lock daemon
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable facecam-lock.service >/dev/null 2>&1
systemctl --user restart facecam-lock.service
ok "facecam-lock.service enabled"

step "PAM hook (needs sudo)"
tmp_helper=$(mktemp)
sed "s|@APP_DIR@|$APP_DIR|" "$APP_DIR/facecam-lock-pam-auth" >"$tmp_helper"
sudo install -Dm755 "$tmp_helper" "$PAM_HELPER"
rm -f "$tmp_helper"
if [[ -f $PAM_FILE ]] && ! grep -q facecam "$PAM_FILE"; then
  sudo cp "$PAM_FILE" "$PAM_FILE.facecam-bak"
fi
sudo tee "$PAM_FILE" >/dev/null <<EOF
#%PAM-1.0
auth       sufficient                  pam_exec.so quiet $PAM_HELPER
account    include                     system-local-login
EOF
ok "$PAM_FILE"

step "Omarchy lock plugin"
if [[ $APP_DIR != "$PLUGIN_DIR" ]]; then
  # Running from a checkout: copy just the plugin files (the shell refuses symlinks).
  mkdir -p "$PLUGIN_DIR"
  cp "$APP_DIR/manifest.json" "$APP_DIR/Service.qml" "$APP_DIR/LockView.qml" "$PLUGIN_DIR/"
fi
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
sleep 0.3
omarchy plugin enable "$PLUGIN_ID" >/dev/null
ok "$PLUGIN_ID enabled (replaces the built-in lock; 'omarchy plugin disable $PLUGIN_ID' restores it)"

step "Desktop entry"
mkdir -p "$HOME/.local/share/applications"
cat >"$HOME/.local/share/applications/facecam-lock.desktop" <<EOF
[Desktop Entry]
Name=FaceCam Lock
Comment=Train face unlock for the Omarchy lock screen
Exec=$APP_DIR/bin/facecam-lock
Icon=camera-web
Terminal=false
Type=Application
Categories=Utility;Security;Settings;
StartupWMClass=FaceCamLock
EOF
ok "FaceCam Lock in the app launcher"

printf '\n\033[1;32mDone.\033[0m Run \033[1mfacecam-lock\033[0m to enroll your face, then lock with Super+Ctrl+L.\n'
