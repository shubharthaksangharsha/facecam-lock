#!/usr/bin/env python3
"""
PAM helper for FaceCam Lock (run by pam_exec from the Omarchy lock's
"fingerprint" PAM stack).

Standard library only, so it starts in ~20 ms. The daemon owns the camera and
writes a one-shot token after a match; this helper just waits for it.

Exit 0: fresh token found (session unlocks).  Exit 1: failure/timeout (password).
"""

import json
import os
import sys
import time

UID = os.getuid()
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{UID}", "facecam-lock")
TOKEN_PATH = os.path.join(RUNTIME_DIR, "unlock.token")
STATUS_PATH = os.path.join(RUNTIME_DIR, "status.json")

POLL_SEC = 0.025
WAIT_BUDGET_SEC = 30.0
TOKEN_MAX_AGE_SEC = 15.0
FAILED_STATES = {"failed", "camera_error", "no_profile"}


def consume_token() -> bool:
    try:
        st = os.stat(TOKEN_PATH)
    except FileNotFoundError:
        return False
    # Only trust a token the user owns, that nobody else can write, and that is fresh.
    if st.st_uid != UID or st.st_mode & 0o077 or time.time() - st.st_mtime > TOKEN_MAX_AGE_SEC:
        return False
    try:
        os.unlink(TOKEN_PATH)
    except FileNotFoundError:
        return False
    return True


def scan_failed_since(started: float) -> bool:
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as handle:
            status = json.load(handle)
    except (OSError, ValueError):
        return False
    return status.get("state") in FAILED_STATES and float(status.get("ts", 0)) >= started - 1.0


def main() -> int:
    started = time.time()
    deadline = time.monotonic() + WAIT_BUDGET_SEC
    while time.monotonic() < deadline:
        if consume_token():
            return 0
        if scan_failed_since(started):
            return 1
        time.sleep(POLL_SEC)
    return 1


if __name__ == "__main__":
    sys.exit(main())
