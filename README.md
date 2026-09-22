# FaceCam Lock

Face unlock for the [Omarchy](https://omarchy.org) lock screen. When you lock or
open the lid, the lock screen shows your webcam, greets you with
**"Welcome back &lt;name&gt;"** when it recognises you, and falls back to your
password when it can't. A **Type password** button is always there.

A small **Face Studio** web app handles enrollment (5 labeled photos, protected by
a training password), recognition settings, and themes, including one that
follows your current Omarchy theme.

## Install

```bash
omarchy plugin add https://github.com/shubharthaksangharsha/facecam-lock --enable
~/.config/omarchy/plugins/facecam.lock/install.sh
facecam-lock          # open the Face Studio and enroll your face
```

Add a **welcome photo**: it replaces the camera with your picture and a check badge
once your face is recognised, and never appears for anyone else:

```bash
facecam-lock photo ~/Pictures/me.jpg   # cropped around your face, kept in ~/.config/facecam-lock (0600)
```

(or use **Welcome photo → Change** in the Face Studio). Preview any lock state
without locking: `omarchy-shell lock previewFace matched|failed|scanning`, then
`omarchy-shell lock hidePreview`.

`install.sh` is idempotent. It:

1. installs `python-opencv` / `python-numpy` if missing and creates a venv in
   `~/.local/share/facecam-lock` for the Studio's web dependencies,
2. downloads the YuNet + SFace models from OpenCV Zoo (sha256-verified),
3. starts the `facecam-lock` systemd **user** service (the face daemon),
4. installs the PAM hook in `/etc/pam.d/omarchy-lock-fingerprint` (asks for sudo),
5. enables the `facecam.lock` plugin, which replaces Omarchy's built-in lock.

Undo everything with `./uninstall.sh` (add `--purge` to delete your face profile).
`omarchy plugin disable facecam.lock` alone restores the stock lock screen.

## How it works

```
 Omarchy shell (Quickshell)                facecamd (systemd --user)
 ┌──────────────────────────┐   socket    ┌───────────────────────────────┐
 │ facecam.lock plugin      │ ── scan ──▶ │ models loaded + warmed at boot │
 │  camera preview, status  │ ◀─ status ─ │ waits for lid open, opens cam  │
 │  password field + button │ ◀─ frame ── │ YuNet @320px → SFace @640px    │
 └──────────┬───────────────┘             └───────────────┬───────────────┘
            │ PAM "fingerprint" stack                      │ on match
            ▼                                              ▼
   pam_exec → pam_helper.py  ◀──── one-shot token in $XDG_RUNTIME_DIR (tmpfs)
```

Latency work that went into this:

- **No start-up cost at lock time.** The daemon keeps OpenCV and both models loaded
  and runs one warm-up inference at boot, which saves about 330 ms per lock. It idles
  at about 135 MB of RAM.
- **Camera starts the instant the lid opens.** The daemon watches the ACPI lid switch
  and detects resume from suspend (`CLOCK_BOOTTIME` vs `CLOCK_MONOTONIC`), so scans
  restart cleanly after sleep instead of timing out while suspended.
- **Push, don't poll.** The lock screen gets status and new-frame events over a Unix
  socket, and preview frames live on tmpfs with no `fsync`. It double-buffers the
  preview so frames never blank.
- **Fast PAM path.** `pam_helper.py` uses only the standard library and starts in
  about 10 ms. It unlocks within about 25 ms of a match.
- **Cheap per frame.** Detection runs on a 320 px copy, and alignment and recognition
  use the full-resolution face. Matching is one matrix product against the enrolled
  embeddings. The whole pipeline takes about 7 ms per frame.

The remaining floor is the webcam itself: most sensors need about 0.7 s to deliver
their first frame after power-on.

## Security notes

Face unlock is a convenience, not a strong factor. A good photo of you may pass,
and any process running as your user can write the unlock token. Keep a strong
password and treat this like fingerprint unlock. The token must be owned by you,
not writable by anyone else, and less than 15 s old, and it is deleted when used.

## Development

```bash
./install.sh                 # from a checkout: copies the QML into ~/.config/omarchy/plugins/facecam.lock
npm install && npm run build:css   # rebuild ui/static/css/app.css after editing templates or ui/src/app.css
facecam-lock server          # Face Studio on http://127.0.0.1:8765 in the foreground
journalctl --user -fu facecam-lock   # daemon logs (first-frame and match timings)
```

## License

MIT. See [LICENSE](LICENSE). The lock screen QML is derived from Omarchy's lock
plugin (MIT).
