#!/usr/bin/env python3
"""
FaceCam Lock daemon.

Keeps OpenCV and both ONNX models loaded so a lock never pays Python start-up or
model load time. The Omarchy lock screen connects to a Unix socket and sends
`scan`; the daemon then waits for the lid to be open, opens the webcam, and
streams status lines back while it matches faces.

Protocol (newline-delimited):
  client -> daemon : scan | rescan | stop | status | ping
                     or a JSON object for the Face Unlock settings panel:
                     {"cmd":"people"} {"cmd":"enroll","id":..,"name":..} {"cmd":"enroll_stop"}
                     {"cmd":"photo","id":..,"path":..} {"cmd":"rename","id":..,"name":..}
                     {"cmd":"remove","id":..} {"cmd":"set","key":..,"value":..}
  daemon -> client : JSON objects, {"t":"status",...}, {"t":"frame","n":<seq>},
                     {"t":"enroll",...}, {"t":"people",...} or {"t":"result",...}

Files in $XDG_RUNTIME_DIR/facecam-lock (tmpfs):
  preview.jpg   latest downscaled camera frame
  status.json   latest status (read by the PAM helper)
  unlock.token  written on a successful match, consumed by the PAM helper
"""

import glob
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import cv2
import numpy as np

from core.avatar import make_avatar
from core.camera import CameraManager, scale_to_width
from core.detector import FaceDetector, scale_raw_face
from core.enroll import Enroller, person_id_for, save_enrollment
from core.recognizer import FaceRecognizer
from core.storage import StorageManager, ensure_runtime_dir, RUNTIME_DIR

SOCKET_PATH = RUNTIME_DIR / "ctl.sock"
PREVIEW_PATH = RUNTIME_DIR / "preview.jpg"
STATUS_PATH = RUNTIME_DIR / "status.json"
TOKEN_PATH = RUNTIME_DIR / "unlock.token"

DETECT_WIDTH = 320
PREVIEW_QUALITY = 60
PREVIEW_INTERVAL = 0.028  # just under the sensor's 33 ms so no frame is skipped by jitter
SUSPEND_GAP_SEC = 1.0

# (prompt, captures) for a guided enrollment; small head movements give the
# profile enough variety to match at the angles a laptop camera sees.
ENROLL_POSES = [
    ("Look straight at the camera", 3),
    ("Turn your head slightly left", 2),
    ("Turn your head slightly right", 2),
    ("Tilt your chin up a little", 2),
    ("Tilt your chin down a little", 2),
    ("Now smile", 2),
]
ENROLL_TIME_LIMIT_SEC = 45.0
ENROLL_POSE_MAX_SEC = 6.0
ENROLL_CAPTURE_GAP_SEC = 0.35
SETTABLE = {"enable_face_unlock": bool, "lock_on_startup": bool}

cv2.setNumThreads(max(1, min(4, os.cpu_count() or 1)))


def log(msg: str) -> None:
    print(f"[facecamd] {msg}", flush=True)


def lid_closed() -> bool:
    for path in glob.glob("/proc/acpi/button/lid/*/state"):
        try:
            with open(path, "r", encoding="ascii") as handle:
                if "closed" in handle.read():
                    return True
        except OSError:
            continue
    return False


def suspend_offset() -> float:
    """CLOCK_BOOTTIME keeps counting through suspend, CLOCK_MONOTONIC does not."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


def write_atomic(path, data: bytes, mode: int = 0o600) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class Hub:
    """Fan-out of status lines to every connected client."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.last_status = {"t": "status", "state": "idle", "message": ""}

    def add(self, conn):
        # No snapshot on connect: a stale "failed" from the previous lock would
        # open the password panel before the new scan starts.
        with self.lock:
            self.clients.append(conn)

    def remove(self, conn):
        with self.lock:
            if conn in self.clients:
                self.clients.remove(conn)

    def _send(self, conn, msg) -> bool:
        try:
            conn.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())
            return True
        except OSError:
            return False

    def broadcast(self, msg):
        with self.lock:
            clients = list(self.clients)
        for conn in clients:
            if not self._send(conn, msg):
                self.remove(conn)

    def status(self, **fields):
        msg = {"t": "status", "ts": time.time(), **fields}
        self.last_status = msg
        try:
            write_atomic(STATUS_PATH, json.dumps(msg, separators=(",", ":")).encode(), 0o644)
        except OSError as e:
            log(f"status write failed: {e}")
        self.broadcast(msg)


class Scanner:
    def __init__(self, hub: Hub):
        self.hub = hub
        self.storage = StorageManager()
        self.detector = FaceDetector()
        self.recognizer = FaceRecognizer()
        self.camera = CameraManager()
        self.lock = threading.Lock()
        self.thread = None
        self.cancel = threading.Event()
        self.release_timer = None
        self.mode = "scan"
        self._warm_up()

    def _warm_up(self):
        """First ONNX inference allocates buffers; do it at boot, not at lock."""
        t0 = time.perf_counter()
        self.detector.detect(np.zeros((240, 320, 3), dtype=np.uint8), compute_quality=False)
        self.recognizer.recognizer.feature(np.zeros((112, 112, 3), dtype=np.uint8))
        self.storage.get_profile()
        log(f"models warm in {(time.perf_counter() - t0) * 1000:.0f} ms")

    @property
    def scanning(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def request(self, force: bool = False):
        with self.lock:
            if self.scanning and self.mode == "scan" and not force:
                self.hub.broadcast(self.hub.last_status)
                return
            self._cancel_release_timer()
            if self.scanning:
                # The lock screen always wins over an enrollment in progress.
                self._cancel_locked()
            # A camera still warm from a failed scan is reused as-is: no 0.7 s start.
            self._start("scan", self._run)

    def request_enroll(self, person_id: str, name: str):
        with self.lock:
            self._cancel_release_timer()
            if self.scanning:
                self._cancel_locked()
            self._start("enroll", lambda cancel: self._run_enroll(cancel, person_id, name))

    def stop_enroll(self):
        with self.lock:
            if self.scanning and self.mode == "enroll":
                self._cancel_locked()

    def _start(self, mode: str, target):
        self.mode = mode
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=target, args=(self.cancel,), daemon=True)
        self.thread.start()

    def stop(self):
        with self.lock:
            if self.mode == "enroll" and self.scanning:
                return
            self._cancel_release_timer()
            self._cancel_locked()
        self.hub.status(state="idle", message="")

    def _cancel_locked(self):
        self.cancel.set()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.5)
        self.camera.stop(force=True)

    def _cancel_release_timer(self):
        if self.release_timer is not None:
            self.release_timer.cancel()
            self.release_timer = None

    def _schedule_release(self, seconds: float):
        """Keep the camera on briefly after a failed scan so "try again" starts instantly."""
        def release():
            with self.lock:
                if not self.scanning:
                    self.camera.stop(force=True)
                self.release_timer = None
        with self.lock:
            self._cancel_release_timer()
            self.release_timer = threading.Timer(seconds, release)
            self.release_timer.daemon = True
            self.release_timer.start()

    def _open_camera(self, index: int) -> bool:
        if self.camera.start(device_index=index):
            return True
        # A camera released a moment ago can still be busy for a few frames.
        time.sleep(0.15)
        return self.camera.start(device_index=index)

    def _run(self, cancel: threading.Event):
        result = None
        try:
            result = self._scan(cancel)
        except Exception as e:
            log(f"scan crashed: {e!r}")
            self.hub.status(state="failed", message="Can't detect face", require_password=True)
        finally:
            linger = float(self.storage.get_settings().get("camera_linger_sec", 8))
            if result == "failed" and linger > 0 and not cancel.is_set() and not lid_closed():
                self._schedule_release(linger)
            else:
                self.camera.stop(force=True)

    def _scan(self, cancel: threading.Event):
        storage = self.storage
        settings = storage.get_settings()
        people = storage.list_people()
        owner = people[0] if people and people[0]["id"] == "owner" else None
        name = owner["display_name"] if owner else ""
        max_attempts = max(1, int(settings.get("max_attempts", 3)))
        timeout = max(0.5, float(settings.get("attempt_timeout_sec", 2.2)))
        threshold = float(settings.get("threshold", 0.38))
        cam_idx = int(settings.get("camera_index", 0))
        base = {"display_name": name, "max_attempts": max_attempts}
        if owner and owner["avatar"]:
            # Sent up front so the lock screen can decode it before a match.
            base["avatar"] = owner["avatar"]

        try:
            TOKEN_PATH.unlink()
        except FileNotFoundError:
            pass

        if not people or not settings.get("enable_face_unlock", True):
            self.hub.status(state="no_profile", message="Can't detect face", require_password=True, **base)
            return

        while not cancel.is_set():
            # Hold the camera closed while the lid is shut; start the instant it opens.
            if lid_closed():
                self.hub.status(state="waiting", message="Open the lid to scan", **base)
                while lid_closed() and not cancel.wait(0.1):
                    pass
                if cancel.is_set():
                    return

            self.hub.status(state="scanning", attempt=1, message="Looking for your face…", **base)
            t_open = time.perf_counter()
            if not self._open_camera(cam_idx):
                self.hub.status(state="camera_error", message="Can't detect face", require_password=True, **base)
                return

            offset = suspend_offset()
            attempt = 1
            deadline = None
            seq = 0
            last_preview = 0.0
            restart = False

            while not cancel.is_set():
                if abs(suspend_offset() - offset) > SUSPEND_GAP_SEC or lid_closed():
                    log("suspend or lid close detected; restarting scan")
                    self.camera.stop(force=True)
                    restart = True
                    break

                seq, frame = self.camera.wait_frame(seq, timeout=0.3)
                now = time.monotonic()
                if frame is None:
                    if self.camera.read_failures > 20:
                        log("camera stalled; reopening")
                        self.camera.stop(force=True)
                        restart = True
                        break
                    if deadline is not None and now > deadline:
                        pass
                    else:
                        continue
                else:
                    if deadline is None:
                        # Attempts are timed from the first real frame, not camera open.
                        log(f"first frame after {(time.perf_counter() - t_open) * 1000:.0f} ms")
                        deadline = now + timeout

                    small = scale_to_width(frame, DETECT_WIDTH)
                    if now - last_preview >= PREVIEW_INTERVAL:
                        last_preview = now
                        ok, jpg = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_QUALITY])
                        if ok:
                            write_atomic(PREVIEW_PATH, jpg.tobytes(), 0o600)
                            self.hub.broadcast({"t": "frame", "n": seq})

                    faces = self.detector.detect(small, compute_quality=False)
                    if faces:
                        factor = frame.shape[1] / float(small.shape[1])
                        raw = scale_raw_face(faces[0]["raw"], factor)
                        try:
                            feature = self.recognizer.extract_feature(frame, raw)
                            matched, score, person = self.recognizer.match_people(feature, people, threshold)
                        except cv2.error:
                            matched, score, person = False, -1.0, None
                        if matched:
                            who = person["display_name"]
                            log(f"match {person['id']} score={score:.3f} after {(time.perf_counter() - t_open) * 1000:.0f} ms")
                            self.camera.stop(force=True)
                            # Each person's photo is shown only for that person.
                            self.hub.status(**{**base, "state": "matched", "message": f"Welcome back {who}",
                                               "display_name": who, "person": person["id"],
                                               "avatar": person["avatar"] or "", "score": round(score, 4)})
                            write_atomic(TOKEN_PATH, f"{secrets.token_hex(16)} {time.time():.3f}\n".encode())
                            return "matched"

                if deadline is not None and now > deadline:
                    if attempt < max_attempts:
                        attempt += 1
                        deadline = now + timeout
                        self.hub.status(state="scanning", attempt=attempt,
                                        message=f"Scanning face… ({attempt}/{max_attempts})", **base)
                    else:
                        self.hub.status(state="failed", message="Can't detect face",
                                        require_password=True, **base)
                        return "failed"

            if not restart:
                return

    # ---------------------------------------------------------------- enrollment
    def _enroll_msg(self, **fields):
        self.hub.broadcast({"t": "enroll", **fields})

    def _run_enroll(self, cancel: threading.Event, person_id: str, name: str):
        base = {"person": person_id, "name": name, "target": sum(n for _, n in ENROLL_POSES)}
        try:
            self._enroll(cancel, person_id, name, base)
        except Exception as e:
            log(f"enroll crashed: {e!r}")
            self._enroll_msg(state="error", message="Something went wrong; try again", **base)
        finally:
            self.camera.stop(force=True)

    def _enroll(self, cancel: threading.Event, person_id: str, name: str, base: dict):
        settings = self.storage.get_settings()
        self._enroll_msg(state="starting", message="Starting camera…", count=0, **base)
        if not self._open_camera(int(settings.get("camera_index", 0))):
            self._enroll_msg(state="error", message="Camera unavailable", **base)
            return

        # Faces of everyone else are ignored, so the owner can sit beside a new person.
        others = [p for p in self.storage.list_people() if p["id"] != person_id]
        enroller = Enroller(self.detector, self.recognizer, others)
        seq = 0
        last_preview = last_take = 0.0
        best_frame, best_score = None, 0.0
        pose_index, pose_taken = 0, 0
        started = pose_started = time.monotonic()
        prompt = ENROLL_POSES[0][0]
        hint = ""

        def next_pose(now):
            nonlocal pose_index, pose_taken, pose_started, prompt
            pose_index, pose_taken, pose_started = pose_index + 1, 0, now
            if pose_index < len(ENROLL_POSES):
                prompt = ENROLL_POSES[pose_index][0]

        while not cancel.is_set() and pose_index < len(ENROLL_POSES):
            now = time.monotonic()
            if now - started > ENROLL_TIME_LIMIT_SEC:
                break
            if pose_taken > 0 and now - pose_started > ENROLL_POSE_MAX_SEC:
                next_pose(now)
                continue
            seq, frame = self.camera.wait_frame(seq, timeout=0.5)
            if frame is None:
                continue

            small = scale_to_width(frame, DETECT_WIDTH)
            faces = self.detector.detect(small, compute_quality=False)
            if now - last_preview >= PREVIEW_INTERVAL:
                last_preview = now
                ok, jpg = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_QUALITY])
                if ok:
                    write_atomic(PREVIEW_PATH, jpg.tobytes(), 0o600)
                    msg = {"t": "frame", "n": seq}
                    if faces:
                        h, w = small.shape[:2]
                        x, y, fw, fh = faces[0]["box"]
                        msg["face"] = [round(x / w, 3), round(y / h, 3), round(fw / w, 3), round(fh / h, 3)]
                    self.hub.broadcast(msg)

            if not faces or now - last_take < ENROLL_CAPTURE_GAP_SEC:
                continue
            last_take = now
            if enroller.add_image(frame.copy(), f"{prompt} #{pose_taken + 1}"):
                hint = ""
                sample_score = enroller.samples[-1]["score"]
                if pose_index == 0 and sample_score > best_score:
                    best_frame, best_score = frame.copy(), sample_score
                pose_taken += 1
                if pose_taken >= ENROLL_POSES[pose_index][1]:
                    next_pose(now)
            else:
                skip = enroller.last_skip
                hint = ("That's " + skip.split(":", 1)[1] + ". Only " + name + " should be in view"
                        if skip.startswith("known:") else
                        "Only one face should be in view" if skip == "multiple" else
                        "Move a little closer")
            self._enroll_msg(state="capturing", message=prompt, hint=hint,
                             count=len(enroller.samples), **base)

        if cancel.is_set():
            self._enroll_msg(state="cancelled", message="Cancelled", **base)
            return
        self.camera.stop(force=True)
        self._enroll_msg(state="saving", message="Saving…", count=len(enroller.samples), **base)

        avatar = None
        existing = next((p for p in self.storage.list_people() if p["id"] == person_id), None)
        if best_frame is not None and not (existing and existing["avatar"]):
            ok, jpg = cv2.imencode(".jpg", best_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if ok:
                avatar = make_avatar(jpg.tobytes(), self.detector)[0]
        try:
            profile, dropped, _ = save_enrollment(self.storage, enroller, person_id, name, avatar)
        except ValueError as e:
            self._enroll_msg(state="error", message=str(e), count=len(enroller.samples), **base)
            return
        log(f"enrolled {person_id} with {profile['sample_count']} samples (dropped {dropped})")
        self._enroll_msg(state="done", message=f"{name} can now unlock with their face",
                         count=profile["sample_count"], **base)
        self.hub.broadcast(self.people())

    # ---------------------------------------------------------------- management
    def people(self) -> dict:
        settings = self.storage.get_settings()
        return {
            "t": "people",
            "people": [{"id": p["id"], "name": p["display_name"], "owner": p["id"] == "owner",
                        "samples": len(p["profile"].get("exemplars", [])), "avatar": p["avatar"] or ""}
                       for p in self.storage.list_people()],
            "settings": {key: bool(settings.get(key, False)) for key in SETTABLE},
        }

    def manage(self, req: dict) -> dict:
        cmd = req.get("cmd")
        pid = str(req.get("id") or "")
        storage = self.storage
        known = {p["id"]: p for p in storage.list_people()}
        if cmd == "photo":
            if pid not in known:
                return {"ok": False, "message": "Enroll this person first"}
            path = os.path.expanduser(str(req.get("path") or "").strip())
            try:
                with open(path, "rb") as handle:
                    data = handle.read(40 * 1024 * 1024)
                jpeg, score, found = make_avatar(data, self.detector, self.recognizer, known[pid]["profile"])
            except (OSError, ValueError):
                return {"ok": False, "message": "Couldn't read that image"}
            storage.save_person_avatar(pid, jpeg)
            if not found:
                return {"ok": True, "message": "Photo saved (no face found, used the centre)"}
            if score is not None and score < 0.38:
                return {"ok": True, "message": "Photo saved, but it doesn't look much like " + known[pid]["display_name"]}
            return {"ok": True, "message": "Photo saved"}
        if cmd == "rename":
            name = str(req.get("name") or "").strip()[:40]
            if pid not in known or not name:
                return {"ok": False, "message": "Enter a name"}
            profile = dict(known[pid]["profile"], display_name=name)
            if pid == "owner":
                storage.save_profile(profile)
                storage.save_settings({"display_name": name})
            else:
                storage.save_person(pid, profile)
            return {"ok": True, "message": "Renamed to " + name}
        if cmd == "remove":
            if pid == "owner":
                ok = storage.delete_profile()
            else:
                ok = storage.delete_person(pid)
            return {"ok": ok, "message": "Removed" if ok else "Nothing to remove"}
        if cmd == "set":
            key = req.get("key")
            if key not in SETTABLE:
                return {"ok": False, "message": "Unknown setting"}
            storage.save_settings({key: SETTABLE[key](req.get("value"))})
            return {"ok": True, "message": ""}
        return {"ok": False, "message": "Unknown command"}


class Server:
    def __init__(self):
        self.hub = Hub()
        self.scanner = Scanner(self.hub)
        self.sock = None

    def handle(self, conn):
        self.hub.add(conn)
        try:
            with conn.makefile("r", encoding="utf-8", errors="ignore") as reader:
                for line in reader:
                    if line.startswith("{"):
                        self.handle_json(conn, line)
                        continue
                    cmd = line.strip().lower()
                    if cmd == "scan":
                        self.scanner.request()
                    elif cmd == "rescan":
                        self.scanner.request(force=True)
                    elif cmd == "stop":
                        self.scanner.stop()
                    elif cmd == "status":
                        self.hub.broadcast(self.hub.last_status)
                    elif cmd == "ping":
                        conn.sendall(b'{"t":"pong"}\n')
        except OSError:
            pass
        finally:
            self.hub.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def handle_json(self, conn, line: str):
        try:
            req = json.loads(line)
        except ValueError:
            return
        if not isinstance(req, dict):
            return
        cmd = req.get("cmd")
        scanner = self.scanner
        if cmd == "people":
            self.hub._send(conn, scanner.people())
        elif cmd == "enroll":
            name = str(req.get("name") or "").strip()[:40]
            pid = str(req.get("id") or "") or person_id_for(name)
            if pid == "owner" and not name:
                name = scanner.storage.get_display_name()
            if not name:
                self.hub._send(conn, {"t": "result", "cmd": cmd, "ok": False, "message": "Enter a name"})
                return
            scanner.request_enroll(pid, name)
        elif cmd == "enroll_stop":
            scanner.stop_enroll()
        else:
            try:
                result = scanner.manage(req)
            except Exception as e:
                log(f"{cmd} failed: {e!r}")
                result = {"ok": False, "message": "That didn't work"}
            self.hub._send(conn, {"t": "result", "cmd": cmd, **result})
            if result.get("ok"):
                self.hub.broadcast(scanner.people())

    def serve(self):
        ensure_runtime_dir()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        sock.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        sock.listen(8)
        self.sock = sock
        log(f"listening on {SOCKET_PATH}")
        while True:
            conn, _ = sock.accept()
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()


def already_running() -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(SOCKET_PATH))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def send_command(cmd: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(SOCKET_PATH))
            client.sendall((cmd + "\n").encode())
        return True
    except OSError:
        return False


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "serve":
        sys.exit(0 if send_command(sys.argv[1]) else 1)

    ensure_runtime_dir()
    if already_running():
        log("already running")
        return
    server = Server()

    def shutdown(*_):
        server.scanner.camera.stop(force=True)
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve()


if __name__ == "__main__":
    main()
