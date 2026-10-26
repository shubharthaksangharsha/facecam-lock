#!/bin/bash
# Unlock the Omarchy LUKS root with the TPM at power-on, then face-lock the
# session after autologin. Face recognition cannot produce a disk key, so this
# is the only safe way to skip the passphrase prompt.
#
#   facecam-lock boot status
#   facecam-lock boot enable
#   facecam-lock boot disable
#
# enable asks for sudo and your existing disk passphrase. It never stores that
# passphrase. Dual-boot (Ubuntu + Limine) is supported: the TPM slot is only
# sealed when this boot came from the Limine firmware entry.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF=/etc/facecam-lock/boot-unlock.conf
KEY=/etc/facecam-lock/tpm-reseal.key
HOOK=/etc/mkinitcpio.conf.d/zz-facecam-sd-encrypt.conf
UNIT=/etc/systemd/system/facecam-tpm-reseal.service
RESEAL=/usr/local/lib/facecam-lock/tpm-reseal
LIMINE_DEFAULT=/etc/default/limine
KERNEL_CMDLINE=/etc/kernel/cmdline
CRYPTTAB=/etc/crypttab.initramfs
BACKUP_DIR=/etc/facecam-lock/backup
MAPPER=omarchy_root
# PCR 4 = boot loader (Limine vs Ubuntu shim). PCR 7 = Secure Boot policy.
# Together they stop a dual-boot of Ubuntu from unsealing this disk.
PCRS=4+7

usage() {
  cat <<'EOF'
Usage: facecam-lock boot <status|enable|disable>

  status   show disk, TPM, Secure Boot and face-lock state
  enable   TPM-unlock the disk at power-on, then face-lock after login
  disable  restore the disk-password prompt and turn off the startup lock
EOF
}

say() { printf '%s\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

need_root() {
  if ((EUID != 0)); then
    exec sudo --preserve-env=HOME,XDG_RUNTIME_DIR,XDG_CONFIG_HOME,USER "$0" "$@"
  fi
}

efivar() {
  local name=$1 guid=8be4df61-93ca-11d2-aa0d-00e098032b8c
  od -An -t u1 -j4 -N1 "/sys/firmware/efi/efivars/${name}-${guid}" 2>/dev/null | tr -d ' '
}

secure_boot_on() { [[ $(efivar SecureBoot) == 1 && $(efivar SetupMode) != 1 ]]; }

find_luks() {
  local src
  src=$(findmnt -no SOURCE /)
  src=${src%%[*}
  [[ $src == /dev/mapper/* ]] || die "Root is not a mapped LUKS volume ($src)."
  MAPPER=${src#/dev/mapper/}
  DEVICE=$(cryptsetup status "$MAPPER" | awk '/^ *device:/{print $2}')
  [[ -b $DEVICE ]] || die "Could not find the LUKS partition behind /dev/mapper/$MAPPER."
  UUID=$(cryptsetup luksUUID "$DEVICE")
  [[ -n $UUID ]] || die "Could not read the LUKS UUID of $DEVICE."
}

luks_has_tpm() {
  cryptsetup luksDump "$DEVICE" 2>/dev/null | grep -q 'systemd-tpm2'
}

set_user_lock_on_startup() {
  local on=$1 user home
  user=${SUDO_USER:-$USER}
  home=$(getent passwd "$user" | cut -d: -f6)
  python3 - "$home" "$on" <<'PY'
import json, sys
from pathlib import Path
home, on = Path(sys.argv[1]), sys.argv[2] == "1"
path = home / ".config/facecam-lock/config.json"
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        data = {}
data["lock_on_startup"] = on
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2) + "\n")
path.chmod(0o600)
PY
}

status() {
  if ((EUID == 0)); then
    find_luks
  else
    MAPPER=omarchy_root
    if [[ -r $CRYPTTAB ]]; then
      UUID=$(awk '{print $2}' "$CRYPTTAB" | sed 's/^UUID=//')
      DEVICE="(run with sudo to see the partition)"
    else
      UUID=$(sed -n 's/.*cryptdevice=UUID=\([^:]*\).*/\1/p' /proc/cmdline | head -1)
      [[ -n $UUID ]] || UUID=$(sed -n 's/.*rd.luks.name=\([^=[:space:]]*\).*/\1/p' /proc/cmdline | head -1)
      DEVICE="(see /proc/cmdline)"
    fi
  fi
  say "FaceCam Lock — boot unlock"
  say
  printf '  Disk          %s (UUID %s → /dev/mapper/%s)\n' "$DEVICE" "$UUID" "$MAPPER"
  if [[ -e /sys/class/tpm/tpm0 ]]; then
    ok "TPM2 present"
  else
    warn "No TPM found"
  fi
  if secure_boot_on; then
    ok "Secure Boot is on"
  else
    warn "Secure Boot is off (TPM unlock is weaker until you turn it on)"
  fi
  if [[ -f $CONF ]]; then
    ok "Boot unlock is configured"
  else
    warn "Boot unlock is not configured — you type the disk password at power-on"
  fi
  if ((EUID == 0)); then
    if luks_has_tpm; then
      ok "LUKS has a TPM2 slot"
    else
      warn "LUKS has no TPM2 slot yet"
    fi
  elif [[ -f $CONF ]]; then
    ok "LUKS TPM slot was enrolled (see /etc/facecam-lock)"
  else
    warn "LUKS TPM slot not enrolled (sudo facecam-lock boot status for details)"
  fi
  if grep -q sd-encrypt /etc/mkinitcpio.conf.d/*.conf 2>/dev/null; then
    ok "Initramfs uses sd-encrypt (TPM-capable)"
  else
    warn "Initramfs still uses Omarchy's encrypt hook (ignores TPM tokens)"
  fi
  if grep -Eq '"lock_on_startup": *true' "${XDG_CONFIG_HOME:-$HOME/.config}/facecam-lock/config.json" 2>/dev/null; then
    ok "Face lock at startup is on"
  else
    warn "Face lock at startup is off"
  fi
  say
  say "Power-on today: $([[ -f $CONF ]] && echo 'TPM tries to unlock the disk, then the face lock appears' || echo 'disk password, then autologin, no face lock until you lock the session')"
}

backup_file() {
  local src=$1
  [[ -f $src ]] || return 0
  mkdir -p "$BACKUP_DIR"
  cp -a "$src" "$BACKUP_DIR/$(basename "$src")"
}

write_hooks() {
  cat >"$HOOK" <<'EOF'
# FaceCam Lock: systemd + sd-encrypt so the TPM2 LUKS token is honoured.
# Sourced after omarchy_hooks.conf; the last HOOKS assignment wins.
# Snapshot overlay uses the systemd unit (sd-btrfs-overlayfs).
HOOKS=(base systemd plymouth autodetect microcode modconf kms keyboard sd-vconsole block sd-encrypt filesystems fsck sd-btrfs-overlayfs)
EOF
}

write_crypttab() {
  mkdir -p "$(dirname "$CRYPTTAB")"
  printf '%s  UUID=%s  none  luks,discard,tpm2-device=auto,tpm2-measure-pcr=yes\n' "$MAPPER" "$UUID" >"$CRYPTTAB"
}

rewrite_cmdline() {
  local from="cryptdevice=UUID=${UUID}:${MAPPER}"
  local to="rd.luks.name=${UUID}=${MAPPER} rd.luks.options=${UUID}=tpm2-device=auto"
  local file
  for file in "$LIMINE_DEFAULT" "$KERNEL_CMDLINE"; do
    [[ -f $file ]] || continue
    backup_file "$file"
    if grep -q "$from" "$file"; then
      sed -i "s|$from|$to|g" "$file"
    elif grep -q "rd.luks.name=${UUID}=${MAPPER}" "$file"; then
      continue
    else
      warn "Did not find $from in $file — add rd.luks.name=${UUID}=${MAPPER} by hand if boot asks for a password after this."
    fi
  done
}

restore_cmdline() {
  local from="rd.luks.name=${UUID}=${MAPPER} rd.luks.options=${UUID}=tpm2-device=auto"
  local to="cryptdevice=UUID=${UUID}:${MAPPER}"
  local file
  for file in "$LIMINE_DEFAULT" "$KERNEL_CMDLINE"; do
    [[ -f $file ]] || continue
    if grep -q "rd.luks.name=${UUID}=${MAPPER}" "$file"; then
      sed -i "s|$from|$to|g" "$file"
    fi
  done
  if [[ -f $BACKUP_DIR/crypttab.initramfs ]]; then
    cp -a "$BACKUP_DIR/crypttab.initramfs" "$CRYPTTAB"
  else
    printf '%s  UUID=%s  none  luks,discard\n' "$MAPPER" "$UUID" >"$CRYPTTAB"
  fi
}

install_reseal() {
  install -Dm755 "$APP_DIR/scripts/tpm-reseal" "$RESEAL"
  cat >"$UNIT" <<EOF
[Unit]
Description=Reseal FaceCam Lock LUKS TPM slot to this boot
After=cryptsetup.target
ConditionPathExists=$CONF

[Service]
Type=oneshot
ExecStart=$RESEAL

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable facecam-tpm-reseal.service >/dev/null
}

write_conf() {
  mkdir -p /etc/facecam-lock
  chmod 700 /etc/facecam-lock
  cat >"$CONF" <<EOF
DEVICE=$DEVICE
UUID=$UUID
MAPPER=$MAPPER
PCRS=$PCRS
EOF
  chmod 600 "$CONF"
}

add_keyfile() {
  if [[ ! -f $KEY ]]; then
    umask 077
    dd if=/dev/urandom of="$KEY" bs=64 count=1 status=none
    chmod 400 "$KEY"
  fi
  if cryptsetup open --test-passphrase --key-file="$KEY" "$DEVICE" </dev/null 2>/dev/null; then
    return 0
  fi
  say
  say "Add a machine key so the TPM slot can be re-sealed after firmware updates."
  say "Type the disk passphrase you already use at power-on (it is not stored)."
  cryptsetup luksAddKey "$DEVICE" "$KEY"
}

enroll_tpm() {
  if luks_has_tpm; then
    systemd-cryptenroll "$DEVICE" --unlock-key-file="$KEY" --wipe-slot=tpm2 \
      --tpm2-device=auto --tpm2-pcrs="$PCRS"
  else
    systemd-cryptenroll "$DEVICE" --unlock-key-file="$KEY" \
      --tpm2-device=auto --tpm2-pcrs="$PCRS"
  fi
}

rebuild_initramfs() {
  if command -v limine-mkinitcpio >/dev/null; then
    limine-mkinitcpio
  else
    mkinitcpio -P
  fi
}

print_secure_boot_help() {
  cat <<'EOF'

Secure Boot is off. The TPM will still unlock the disk, but a changed boot
loader can trick it — keep the laptop with you, or turn Secure Boot on.

This machine dual-boots Ubuntu (shim) and Omarchy (Limine). On a Dell:

  1. Reboot and tap F2 for Setup.
  2. Find Secure Boot and set it to Enabled / Deployed.
  3. Leave the default / Microsoft keys enrolled so Ubuntu still boots.
  4. Save and boot the Limine (Omarchy) entry.
  5. Run:  facecam-lock boot enable
     It will re-seal the TPM to this Secure Boot state.

If Limine is refused after that, install sbctl, create keys, enroll them
alongside the Microsoft keys, and sign /boot/EFI/limine/limine_x64.efi.
Omarchy's limine-mkinitcpio hook signs automatically once sbctl is set up.

EOF
}

enable() {
  need_root enable
  find_luks
  [[ -e /sys/class/tpm/tpm0 ]] || die "No TPM2 chip. Face lock at first boot needs one."
  command -v systemd-cryptenroll >/dev/null || die "systemd-cryptenroll is missing."
  command -v cryptsetup >/dev/null || die "cryptsetup is missing."

  say "This will:"
  say "  1. Switch the boot image to systemd's LUKS unlock (so the TPM is used)"
  say "  2. Enroll a TPM2 slot on $DEVICE (PCR $PCRS: Limine + Secure Boot)"
  say "  3. Keep your existing disk passphrase as a fallback"
  say "  4. Face-lock the session right after autologin"
  say
  if ! secure_boot_on; then
    print_secure_boot_help
    printf 'Continue without Secure Boot? [y/N] '
    read -r answer
    [[ $answer == [yY] ]] || exit 130
  fi

  backup_file "$CRYPTTAB"
  write_hooks
  write_crypttab
  rewrite_cmdline
  write_conf
  install_reseal
  add_keyfile
  enroll_tpm
  set_user_lock_on_startup 1
  rebuild_initramfs
  ok "Boot unlock is on. Reboot: the disk should open without a passphrase, then the face lock appears."
  say "If anything goes wrong, pick an older Limine snapshot and run: facecam-lock boot disable"
  say "Your disk passphrase still works if the TPM slot fails."
}

disable() {
  need_root disable
  find_luks
  if [[ -f $KEY ]] && luks_has_tpm; then
    systemd-cryptenroll "$DEVICE" --unlock-key-file="$KEY" --wipe-slot=tpm2 || true
  fi
  if [[ -f $KEY ]]; then
    cryptsetup luksRemoveKey "$DEVICE" "$KEY" 2>/dev/null || true
    shred -u "$KEY" 2>/dev/null || rm -f "$KEY"
  fi
  restore_cmdline
  rm -f "$HOOK" "$CONF" "$UNIT"
  systemctl disable --now facecam-tpm-reseal.service >/dev/null 2>&1 || true
  systemctl daemon-reload
  set_user_lock_on_startup 0
  rebuild_initramfs
  ok "Disk password is required at power-on again. Face unlock still works after you lock the session."
}

case ${1:-status} in
  status) status ;;
  enable) enable ;;
  disable) disable ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
