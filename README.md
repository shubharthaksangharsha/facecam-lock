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

## Anti-spoof guard (optional, uses Gemini)

A face matcher can't tell you **whose** face it is *and* whether that face is
really in front of the camera. Someone holding up a photo of you on a phone
can fool it. The guard is a second opinion that never slows the unlock:

1. The face matches, the session unlocks, exactly as before.
2. In the background the daemon sends the matched frame plus two more taken
   over ~0.6 s to a Gemini vision model: *live person, or a screen / print /
   mask?*
3. If the answer is "not live" with at least 80% confidence, the session is
   **locked again**, the face unlock is **paused until you type your password**,
   a desktop notification fires, and a photo of what was in front of the camera
   is saved to `~/.local/share/facecam-lock/intruders/`.

```bash
facecam-lock guard setup --from ~/.env          # reads GEMINI_API_KEYS / GEMINI_API_KEY
facecam-lock guard setup --key AIza...          # or pass keys directly (repeatable)
facecam-lock guard test                         # try it on the camera (hold up a phone!)
facecam-lock guard test --image a.jpg b.jpg     # or on pictures, no camera needed
facecam-lock guard status | log | on | off | clear
```

The same switch is in **Setup → Security → Face Unlock → Anti-spoof check**,
with a **Check now** button and the result of the last unlock.

- **Keys** are stored in `~/.config/facecam-lock/guard.env` (mode 600), never in
  this repo. Several keys are rotated automatically when one hits its quota.
- **Models.** Benchmarked on live and spoofed frames: `gemini-3.5-flash` was
  right every time, `gemini-3-flash-preview` is about a second quicker and also
  right, and the flash-lite models false-alarmed or timed out. The chain is
  `gemini-3.5-flash → gemini-3-flash-preview → gemini-3.5-flash-lite`; when the
  first is slow (2.5 s) the next is started alongside it and the first answer
  wins. Override with `GUARD_MODELS=a,b,c` in `guard.env`. Expect a verdict in
  2-4 s, so a fake gets about that long at the desktop.
- **Fails open by default.** No network, no quota, or a dark image means the
  unlock stands and the check is logged as skipped. Set `"guard_fail_closed":
  true` in `config.json` to lock back instead.
- **Tuning** in `config.json`: `guard_min_confidence` (default 0.8),
  `guard_frames` (default 3).
- **Privacy.** With the guard on, those three frames (640 px wide JPEGs, ~100 KB
  total) leave your machine for Google's Gemini API on every face unlock. Leave
  it off if that is not acceptable. Nothing is sent when it is off.
- **Not a guarantee.** It catches phones, screens and prints. A good 3D mask or
  a very good video may still pass. Treat it as one more layer.

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
- **Unlock first, check second.** The camera is released *after* the token is
  written, and the anti-spoof call runs on its own thread over a TLS connection
  that was opened when the scan started.
- **Fast PAM path.** `pam_helper.py` is stdlib-only (~10 ms start, ~25 ms
  after a match).
- **Cheap per frame.** Detect at 320 px, recognise the full-resolution face,
  one matrix product to match (~7 ms).

The remaining floor is the webcam: most sensors need ~0.7 s for the first
frame after power-on.

## Security notes

Face unlock is a convenience, not a strong factor. A good photo of you may
pass (the anti-spoof guard above narrows that gap), and any process running as your user can write the unlock token. Keep a
strong password and treat this like fingerprint unlock. The token must be
owned by you, not writable by anyone else, and less than 15 s old; it is
deleted when used.

TPM disk unlock without Secure Boot is the same kind of convenience. Your
original LUKS passphrase stays as a fallback.

## License

MIT. See [LICENSE](LICENSE). The lock screen QML is derived from Omarchy's
lock plugin (MIT). Face models come from OpenCV Zoo.
