#!/usr/bin/env python3
"""
facecam-lock command line.

  facecam-lock              open the Face Studio (enroll, settings, themes)
  facecam-lock daemon       run the face daemon in the foreground (systemd uses this)
  facecam-lock scan         ask the daemon to start a scan now
  facecam-lock status       print profile, settings and integration status
  facecam-lock server       run the Face Studio server in the foreground
  facecam-lock lock         lock the session (Omarchy)
  facecam-lock photo FILE   set the photo shown with "Welcome back" after a match
  facecam-lock person add --name NAME [--photos DIR] [--live] [--photo FILE]
                            let someone else unlock; greeted by NAME with their own photo
  facecam-lock person list | remove ID
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


def person_command(argv) -> None:
    """facecam-lock person add|list|remove: other people who may unlock this session."""
    parser = argparse.ArgumentParser(prog="facecam-lock person")
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add", help="enroll someone from photos and/or the webcam")
    add.add_argument("--name", required=True, help='name on the lock screen, e.g. "Pranchu"')
    add.add_argument("--id", help="short id (default: derived from the name)")
    add.add_argument("--photos", help="folder of photos of this person")
    add.add_argument("--live", action="store_true", help="capture from the webcam (person in front of it)")
    add.add_argument("--photo", help="welcome photo shown only when this person is recognised")
    sub.add_parser("list", help="show everyone who can unlock")
    rm = sub.add_parser("remove", help="remove a person (and their photo)")
    rm.add_argument("id")
    args = parser.parse_args(argv)

    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    from core.storage import StorageManager, MIN_ENROLL_SAMPLES
    storage = StorageManager()

    if args.action == "list":
        for p in storage.list_people():
            photo = "photo" if p["avatar"] else "no photo"
            print(f"{p['id']:<14} {p['display_name']:<16} {len(p['profile'].get('exemplars', []))} samples, {photo}")
        return

    if args.action == "remove":
        if storage.delete_person(args.id):
            print(f"Removed {args.id}.")
        else:
            sys.exit(f"No removable person '{args.id}' (see `facecam-lock person list`).")
        return

    import re
    from core.avatar import make_avatar
    from core.detector import FaceDetector
    from core.enroll import Enroller
    from core.recognizer import FaceRecognizer

    if not (args.photos or args.live):
        sys.exit("Give --photos FOLDER and/or --live.")
    person_id = args.id or re.sub(r"[^a-z0-9_-]+", "-", args.name.lower()).strip("-")[:32]
    known = [p for p in storage.list_people() if p["id"] != person_id]
    enroller = Enroller(FaceDetector(), FaceRecognizer(), known)

    if args.photos:
        used = enroller.add_photos(Path(args.photos))
        print(f"Photos: {used} usable")
    if args.live:
        from core.camera import CameraManager
        camera = CameraManager()
        if not camera.start(int(storage.get_settings().get("camera_index", 0))):
            sys.exit("Could not open the camera (close the Face Studio and try again).")
        print(f"Camera on. {args.name}, look at the screen:")
        try:
            used = enroller.add_live(camera, lambda text: print(f"  → {text}"))
        finally:
            camera.stop(force=True)
        print(f"Camera: {used} captures")

    dropped = enroller.prune_outliers()
    for line in enroller.log:
        print("  " + line)
    if len(enroller.samples) < MIN_ENROLL_SAMPLES:
        sys.exit(f"Only {len(enroller.samples)} good samples (need {MIN_ENROLL_SAMPLES}). Add more photos or use --live.")

    profile = enroller.build_profile(args.name)
    closest = enroller.closest_known(profile)
    if closest and closest[1] >= KNOWN_WARNING:
        sys.exit(f"These faces look like {closest[0]} (similarity {closest[1]:.2f}); not saving.")

    avatar = None
    if args.photo:
        avatar, score, face_found = make_avatar(Path(args.photo).expanduser().read_bytes(),
                                                enroller.detector, enroller.recognizer, profile)
        if score is not None and score < 0.38:
            print(f"Note: the welcome photo doesn't look like {args.name} (similarity {score:.2f}); saved anyway.")
    path = storage.save_person(person_id, profile, avatar)
    print(f"Saved {args.name} as '{person_id}' with {len(enroller.samples)} samples"
          f"{' (dropped ' + str(dropped) + ' outliers)' if dropped else ''} → {path}")
    if closest:
        print(f"Separation from {closest[0]}: similarity {closest[1]:.2f} (lower is better; unlock needs 0.38)")


KNOWN_WARNING = 0.45


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "person":
        person_command(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(prog="facecam-lock", description="Face unlock for the Omarchy lock screen")
    parser.add_argument("command", nargs="?", default="studio",
                        choices=["studio", "daemon", "scan", "stop", "status", "server", "lock", "preview", "photo"])
    parser.add_argument("path", nargs="?", help="image file for `photo`")
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
    elif command == "photo":
        set_photo(args.path)


def set_photo(path) -> None:
    """Set the photo shown with "Welcome back" once your face is matched."""
    if not path:
        sys.exit("usage: facecam-lock photo <image>")
    from core.avatar import make_avatar
    from core.detector import FaceDetector
    from core.recognizer import FaceRecognizer
    from core.storage import StorageManager
    storage = StorageManager()
    profile = storage.get_profile()
    if not profile:
        sys.exit("Enroll your face first (run `facecam-lock`).")
    jpeg, score, face_found = make_avatar(Path(path).expanduser().read_bytes(), FaceDetector(), FaceRecognizer(), profile)
    storage.save_avatar(jpeg)
    print(f"Saved {storage.avatar_path}")
    if not face_found:
        print("No face found in the photo; used a centre crop.")
    elif score is not None:
        verdict = "matches your enrolled face" if score >= 0.38 else "does NOT look like your enrolled face"
        print(f"Photo {verdict} (similarity {score:.2f}).")


if __name__ == "__main__":
    main()
