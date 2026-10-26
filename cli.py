#!/usr/bin/env python3
"""
facecam-lock command line.

  facecam-lock              open the Face Unlock panel (enroll, photos, people, settings)
  facecam-lock daemon       run the face daemon in the foreground (systemd uses this)
  facecam-lock scan         ask the daemon to start a scan now
  facecam-lock status       print profile, settings and integration status
  facecam-lock lock         lock the session (Omarchy)
  facecam-lock preview      show the lock screen without locking (click to close)
  facecam-lock photo FILE   set the photo shown with "Welcome back" after a match
  facecam-lock person add --name NAME [--photos DIR] [--live] [--photo FILE]
                            let someone else unlock; greeted by NAME with their own photo
  facecam-lock person list | remove ID | photo ID FILE
  facecam-lock boot status | enable | disable
                            unlock the disk with the TPM at power-on so the face lock is
                            the only prompt (needs Secure Boot; see README)
"""

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "facecam-lock"


def open_panel(action: str = "") -> None:
    payload = json.dumps({"action": action} if action else {})
    result = subprocess.run(["omarchy-shell", "shell", "summon", "facecam.lock", payload],
                            capture_output=True, text=True)
    if result.returncode != 0 or "unknown" in result.stdout:
        sys.exit("The Face Unlock panel isn't installed. Run ./install.sh, or use `facecam-lock person ...`.")


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
        ("People", ", ".join(p["display_name"] for p in storage.list_people()) or "none"),
        ("Startup lock", "on" if settings.get("lock_on_startup") else "off"),
        ("Boot unlock", "TPM" if Path("/etc/facecam-lock/boot-unlock.conf").exists() else "disk password"),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}}  {value}")


def person_command(argv) -> None:
    """facecam-lock person add|list|remove: other people who may unlock this session."""
    parser = argparse.ArgumentParser(prog="facecam-lock person")
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add", help="enroll someone from photos and/or the webcam")
    add.add_argument("--name", required=True, help='name on the lock screen, e.g. "Alex"')
    add.add_argument("--id", help="short id (default: derived from the name)")
    add.add_argument("--photos", help="folder of photos of this person")
    add.add_argument("--live", action="store_true", help="capture from the webcam (person in front of it)")
    add.add_argument("--photo", help="welcome photo shown only when this person is recognised")
    sub.add_parser("list", help="show everyone who can unlock")
    rm = sub.add_parser("remove", help="remove a person (and their photo)")
    rm.add_argument("id")
    photo = sub.add_parser("photo", help="set or change the welcome photo for an enrolled person")
    photo.add_argument("id", help="person id, e.g. alex")
    photo.add_argument("path", help="path to image file")
    args = parser.parse_args(argv)

    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    from core.storage import StorageManager
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

    if args.action == "photo":
        set_photo(args.path, person_id=args.id)
        return

    from core.avatar import make_avatar
    from core.detector import FaceDetector
    from core.enroll import Enroller, person_id_for, save_enrollment
    from core.recognizer import FaceRecognizer

    if not (args.photos or args.live):
        sys.exit("Give --photos FOLDER and/or --live.")
    person_id = args.id or person_id_for(args.name)
    known = [p for p in storage.list_people() if p["id"] != person_id]
    enroller = Enroller(FaceDetector(), FaceRecognizer(), known)

    if args.photos:
        used = enroller.add_photos(Path(args.photos))
        print(f"Photos: {used} usable")
    if args.live:
        from core.camera import CameraManager
        camera = CameraManager()
        if not camera.start(int(storage.get_settings().get("camera_index", 0))):
            sys.exit("Could not open the camera (is another app using it?).")
        print(f"Camera on. {args.name}, look at the screen:")
        try:
            used = enroller.add_live(camera, lambda text: print(f"  → {text}"))
        finally:
            camera.stop(force=True)
        print(f"Camera: {used} captures")

    avatar = None
    if args.photo:
        avatar = make_avatar(Path(args.photo).expanduser().read_bytes(), enroller.detector)[0]
    try:
        profile, dropped, closest = save_enrollment(storage, enroller, person_id, args.name, avatar)
    except ValueError as e:
        for line in enroller.log:
            print("  " + line)
        sys.exit(f"{e}. Add more photos or use --live.")
    for line in enroller.log:
        print("  " + line)
    print(f"Saved {args.name} as '{person_id}' with {profile['sample_count']} samples"
          f"{' (dropped ' + str(dropped) + ' outliers)' if dropped else ''}")
    if closest:
        print(f"Separation from {closest[0]}: similarity {closest[1]:.2f} (lower is better; unlock needs 0.38)")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "person":
        person_command(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "boot":
        script = BASE_DIR / "scripts" / "boot-unlock.sh"
        os.execv("/bin/bash", ["/bin/bash", str(script), *sys.argv[2:]])
    parser = argparse.ArgumentParser(prog="facecam-lock", description="Face unlock for the Omarchy lock screen")
    parser.add_argument("command", nargs="?", default="setup",
                        choices=["setup", "studio", "add", "retrain", "daemon", "scan", "stop", "status",
                                 "lock", "preview", "photo"])
    parser.add_argument("path", nargs="?", help="image file for `photo`")
    parser.add_argument("--person", default="owner", help="person id for `photo` (default: owner)")
    args = parser.parse_args()
    command = args.command

    if command in ("setup", "studio"):
        open_panel()
    elif command in ("add", "retrain"):
        open_panel(command)
    elif command == "preview":
        subprocess.run(["omarchy-shell", "lock", "preview"])
    elif command == "daemon":
        import facecamd
        sys.argv = [sys.argv[0]]
        facecamd.main()
    elif command in ("scan", "stop"):
        sys.exit(0 if send(command) else 1)
    elif command == "status":
        print_status()
    elif command == "lock":
        subprocess.run(["omarchy-system-lock"])
    elif command == "photo":
        set_photo(args.path, person_id=args.person)


def set_photo(path: str, person_id: str = "owner") -> None:
    """Set the photo shown with "Welcome back" once that person's face is matched."""
    if not path:
        sys.exit("usage: facecam-lock photo <image> [--person ID]  or  facecam-lock person photo <id> <image>")
    img_path = Path(path).expanduser()
    if not img_path.exists():
        sys.exit(f"Image file not found: {img_path}")

    from core.avatar import make_avatar
    from core.detector import FaceDetector
    from core.recognizer import FaceRecognizer
    from core.storage import StorageManager
    storage = StorageManager()

    if person_id == "owner":
        profile = storage.get_profile()
        target_name = storage.get_display_name(profile) or "owner"
    else:
        target_json = storage.people_dir / f"{person_id}.json"
        if not target_json.exists():
            sys.exit(f"No enrolled person '{person_id}'. See `facecam-lock person list`.")
        try:
            with open(target_json, "r", encoding="utf-8") as f:
                profile = json.load(f)
        except Exception as e:
            sys.exit(f"Could not read profile for '{person_id}': {e}")
        target_name = profile.get("display_name") or person_id

    if not profile:
        sys.exit(f"No enrolled profile found for '{person_id}'.")

    raw_bytes = img_path.read_bytes()
    jpeg, score, face_found = make_avatar(raw_bytes, FaceDetector(), FaceRecognizer(), profile)
    saved_path = storage.save_person_avatar(person_id, jpeg)
    print(f"Saved welcome photo for {target_name} ({person_id}) → {saved_path}")

    if not face_found:
        print("No face found in the photo; used a centre crop.")
    elif score is not None:
        if score >= 0.38:
            print(f"Photo matches {target_name}'s enrolled face (similarity {score:.2f}).")
        else:
            print(f"Note: Photo similarity is {score:.2f} (different pose/lighting); saved anyway.")


if __name__ == "__main__":
    main()
