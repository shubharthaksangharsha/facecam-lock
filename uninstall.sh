#!/bin/bash
# Remove FaceCam Lock and restore Omarchy's built-in lock screen.
# Your face profile in ~/.config/facecam-lock is kept unless you pass --purge.
set -uo pipefail

PAM_FILE="/etc/pam.d/omarchy-lock-fingerprint"

omarchy plugin disable facecam.lock 2>/dev/null
systemctl --user disable --now facecam-lock.service 2>/dev/null
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/facecam-lock.service"
systemctl --user daemon-reload

if [[ -f $PAM_FILE.facecam-bak ]]; then
  sudo mv "$PAM_FILE.facecam-bak" "$PAM_FILE"
elif grep -q facecam "$PAM_FILE" 2>/dev/null; then
  sudo rm -f "$PAM_FILE"
fi
sudo rm -rf /usr/local/lib/facecam-lock

rm -f "$HOME/.local/bin/facecam-lock" "$HOME/.local/share/applications/facecam-lock.desktop"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/facecam-lock"
[[ ${1:-} == --purge ]] && rm -rf "$HOME/.config/facecam-lock"

echo "FaceCam Lock removed. Delete ~/.config/omarchy/plugins/facecam.lock to remove the plugin files."
