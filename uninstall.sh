#!/bin/bash
# Remove FaceCam Lock and restore Omarchy's built-in lock screen.
# Your face profile in ~/.config/facecam-lock is kept unless you pass --purge.
# Disk TPM unlock is left alone; run `facecam-lock boot disable` first if you
# want the password prompt back at power-on.
set -uo pipefail

PAM_FILE="/etc/pam.d/omarchy-lock-fingerprint"
MENU_EXT="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/extensions/omarchy-menu.jsonc"

omarchy plugin disable facecam.lock 2>/dev/null
omarchy plugin disable facecam.setup 2>/dev/null
systemctl --user disable --now facecam-lock.service 2>/dev/null
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/facecam-lock.service"
systemctl --user daemon-reload

if [[ -f $PAM_FILE.facecam-bak ]]; then
  sudo mv "$PAM_FILE.facecam-bak" "$PAM_FILE"
elif grep -q facecam "$PAM_FILE" 2>/dev/null; then
  sudo rm -f "$PAM_FILE"
fi
sudo rm -f /usr/local/lib/facecam-lock/pam-auth

rm -f "$HOME/.local/bin/facecam-lock" "$HOME/.local/share/applications/facecam-lock.desktop"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/facecam-lock"
# Leave a git checkout from `omarchy plugin add` in place so `omarchy plugin remove`
# can still see it. A copied checkout from ./install.sh is safe to delete.
if [[ ! -d $HOME/.config/omarchy/plugins/facecam.lock/.git ]]; then
  rm -rf "$HOME/.config/omarchy/plugins/facecam.lock"
fi
rm -rf "$HOME/.config/omarchy/plugins/facecam.setup"

if [[ -f $MENU_EXT ]]; then
  python3 - "$MENU_EXT" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
text = re.sub(r',\s*"setup\.security\.face(?:-boot)?"\s*:\s*\{.*?\}\s*', "\n", text, flags=re.S)
text = re.sub(r',\s*"remove\.security\.face-boot"\s*:\s*\{.*?\}\s*', "\n", text, flags=re.S)
text = re.sub(r'"setup\.security\.face(?:-boot)?"\s*:\s*\{.*?\}\s*,?\s*', "", text, flags=re.S)
text = re.sub(r'"remove\.security\.face-boot"\s*:\s*\{.*?\}\s*,?\s*', "", text, flags=re.S)
path.write_text(text)
PY
fi

[[ ${1:-} == --purge ]] && rm -rf "$HOME/.config/facecam-lock"

echo "FaceCam Lock removed. If the disk still unlocks with the TPM, run: sudo $(dirname "$0")/scripts/boot-unlock.sh disable"
