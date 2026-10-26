# FaceCam Lock

Face unlock for the [Omarchy](https://omarchy.org) lock screen.

Lock or open the lid and the webcam comes up. A match greets you with
**Welcome back &lt;name&gt;** and unlocks. A miss opens the password field.
A **Type password** button is always there.

Enrollment is a native Omarchy overlay — **Setup → Security → Face Unlock** —
driven by the same daemon that unlocks the session. There is no separate web app.

## Install

This is a normal Omarchy shell plugin (a git repo with `manifest.json` at the
root). Adding it replaces the built-in lock. Then run `install.sh` once for the
face daemon, models, and PAM hook (`omarchy plugin add` never runs install
hooks or sudo):

```bash
omarchy plugin add https://github.com/shubharthaksangharsha/facecam-lock --enable
~/.config/omarchy/plugins/facecam.lock/install.sh
```

Open **Setup → Security → Face Unlock** (or run `facecam-lock`) and enroll
your face. Lock with Super+Ctrl+L.

Update later with:

```bash
omarchy plugin update facecam.lock
systemctl --user restart facecam-lock
```

`omarchy plugin disable facecam.lock` restores Omarchy's built-in lock.
`./uninstall.sh` also removes the daemon and PAM hook (add `--purge` to delete
your face profile). `omarchy plugin remove facecam.lock` deletes the plugin
checkout.

`install.sh` is idempotent. It installs `python-opencv` / `python-numpy` if
needed, downloads the YuNet + SFace models from OpenCV Zoo (sha256-verified),
starts the `facecam-lock` user service, installs the PAM hook
(`/etc/pam.d/omarchy-lock-fingerprint`, asks for sudo), enables the plugin,
and adds the menu entries.

## Use

**Welcome photo** — shown only after *that* face is recognised:

```bash
facecam-lock photo ~/Pictures/me.jpg
```

**Let someone else unlock**, each with their own name and photo:

```bash
facecam-lock person add --name "Alex" --live --photo ~/Pictures/alex.jpg
facecam-lock person add --name "Alex" --photos ~/Pictures/alex-photos
facecam-lock person list
facecam-lock person remove alex
```

Photos with several people are fine: enrolled faces are skipped, and samples
that disagree with the rest are dropped.

The lock screen follows your Omarchy theme (lock colours, corner radius,
accent, Hyprland active-window border). After a failed scan the camera stays
warm for 8 s (`camera_linger_sec` in `~/.config/facecam-lock/config.json`), so
**Tap to try again** starts instantly.

Preview without locking:
`omarchy-shell lock previewFace matched|failed|scanning`, then
`omarchy-shell lock hidePreview`.

## Face lock at first power-on

The password at power-on is the **disk (LUKS) passphrase**. The camera is not
available there, and a face model cannot safely produce a disk key. Omarchy
then auto-logs you in, so there is no login screen either.

What works: the TPM unlocks the disk, then the session starts behind the face
lock.

```bash
facecam-lock boot status
facecam-lock boot enable    # sudo + your existing disk passphrase
```

`enable` switches the initramfs to systemd's `sd-encrypt` (Omarchy's default
`encrypt` hook ignores TPM tokens), enrolls a TPM2 slot bound to the boot
loader and Secure Boot (PCR 4+7), keeps your passphrase as a fallback, and
turns on **Face lock at startup**.

**Turn Secure Boot on first.** Without it, a replaced boot loader can trick
the TPM. Dual-boot with Ubuntu (shim) + Omarchy (Limine) is supported: enable
Secure Boot in firmware, leave the Microsoft keys so Ubuntu still boots, boot
the Limine entry, then run `enable`. `facecam-lock boot disable` restores the
disk-password prompt.

Limine snapshot entries still work. If a rebuild goes wrong, boot an older
snapshot and run `facecam-lock boot disable`.

## How it works

```
 Omarchy shell (Quickshell)                facecamd (systemd --user)
 +--------------------------+   socket    +-------------------------------+
 | facecam.lock  (lock)     | -- scan --> | models loaded + warmed at boot |
 | facecam.lock  (enroll)   | -- enroll-> | waits for lid open, opens cam  |
 |  camera preview, status  | <- status - | YuNet @320px -> SFace @640px   |
 +------------+-------------+             +---------------+---------------+
              | PAM "fingerprint" stack                    | on match
              v                                            v
   pam_exec -> pam_helper.py  <---- one-shot token in $XDG_RUNTIME_DIR (tmpfs)
```

- **No start-up cost at lock time.** The daemon keeps OpenCV and both models
  loaded and runs one warm-up inference at boot (~330 ms saved per lock,
  ~135 MB idle).
- **Camera starts when the lid opens.** ACPI lid watch plus suspend detection
  (`CLOCK_BOOTTIME` vs `CLOCK_MONOTONIC`).
- **Push, don't poll.** Status and frames over a Unix socket; previews on
  tmpfs, double-buffered.
- **Fast PAM path.** `pam_helper.py` is stdlib-only (~10 ms start, ~25 ms
  after a match).
- **Cheap per frame.** Detect at 320 px, recognise the full-resolution face,
  one matrix product to match (~7 ms).

The remaining floor is the webcam: most sensors need ~0.7 s for the first
frame after power-on.

## Security notes

Face unlock is a convenience, not a strong factor. A good photo of you may
pass, and any process running as your user can write the unlock token. Keep a
strong password and treat this like fingerprint unlock. The token must be
owned by you, not writable by anyone else, and less than 15 s old; it is
deleted when used.

TPM disk unlock without Secure Boot is the same kind of convenience. Your
original LUKS passphrase stays as a fallback.

## License

MIT. See [LICENSE](LICENSE). The lock screen QML is derived from Omarchy's
lock plugin (MIT). Face models come from OpenCV Zoo.
