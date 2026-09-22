#!/usr/bin/env python3
"""
facecam-lock command line.

  facecam-lock              open the Face Studio (enroll, settings, themes)
  facecam-lock daemon       run the face daemon in the foreground (systemd uses this)
  facecam-lock scan         ask the daemon to start a scan now
  facecam-lock status       print profile, settings and integration status
  facecam-lock server       run the Face Studio server in the foreground
  facecam-lock lock         lock the session (Omarchy)
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))

SERVER_PORT = 8765
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "facecam-lock"


def server_running() -> bool:
    try:
        with urllib.request.urlopen(f"{SERVER_URL}/api/settings", timeout=0.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_server() -> None:
    if server_running():
        return
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(SERVER_PORT), "--log-level", "warning"],
        cwd=str(BASE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(50):
        if server_running():
            return
        time.sleep(0.1)
    print("facecam-lock: Face Studio server did not start", file=sys.stderr)


def open_studio(route: str = "") -> None:
    start_server()
    url = f"{SERVER_URL}{route}"
    for browser in ("chromium", "brave", "google-chrome-stable"):
        try:
            subprocess.Popen([browser, f"--app={url}", "--class=FaceCamLock"], start_new_session=True)
            return
        except FileNotFoundError:
            continue
    import webbrowser
    webbrowser.open(url)


def send(cmd: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(RUNTIME_DIR / "ctl.sock"))
            client.sendall((cmd + "\n").encode())
        return True
    except OSError:
        return False


def print_status() -> None:
    from core.storage import StorageManager
    storage = StorageManager()
    profile = storage.get_profile()
    settings = storage.get_settings()
    pam = Path("/etc/pam.d/omarchy-lock-fingerprint")
    rows = [
        ("Profile", f"{storage.get_display_name(profile)} ({len(profile.get('exemplars', []))} photos)" if profile else "not enrolled"),
        ("Threshold", settings.get("threshold")),
        ("Attempts", f"{settings.get('max_attempts')} x {settings.get('attempt_timeout_sec')}s"),
        ("Camera", f"/dev/video{settings.get('camera_index')}"),
        ("Daemon", "running" if send("ping") else "not running"),
        ("PAM hook", "configured" if pam.exists() and "facecam" in pam.read_text(errors="ignore") else "missing"),
        ("Studio", "running" if server_running() else "stopped"),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}}  {value}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="facecam-lock", description="Face unlock for the Omarchy lock screen")
    parser.add_argument("command", nargs="?", default="studio",
                        choices=["studio", "daemon", "scan", "stop", "status", "server", "lock", "preview"])
    # Flags kept for older desktop entries and scripts.
    parser.add_argument("--lock", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--status", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    command = "preview" if args.lock else "status" if args.status else "server" if args.server else args.command

    if command == "studio":
        open_studio()
    elif command == "preview":
        open_studio("/lock")
    elif command == "daemon":
        import facecamd
        sys.argv = [sys.argv[0]]
        facecamd.main()
    elif command in ("scan", "stop"):
        sys.exit(0 if send(command) else 1)
    elif command == "status":
        print_status()
    elif command == "server":
        os.chdir(BASE_DIR)
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "server:app",
                                  "--host", "127.0.0.1", "--port", str(SERVER_PORT)])
    elif command == "lock":
        subprocess.run(["omarchy-system-lock"])


if __name__ == "__main__":
    main()
