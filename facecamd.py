#!/usr/bin/env python3
"""
FaceCam Lock daemon.

Keeps OpenCV and both ONNX models loaded so a lock never pays Python start-up or
model load time. The Omarchy lock screen connects to a Unix socket and sends
`scan`; the daemon then waits for the lid to be open, opens the webcam, and
streams status lines back while it matches faces.

Protocol (newline-delimited):
  client -> daemon : scan | rescan | stop | status | ping
  daemon -> client : JSON objects, {"t":"status",...} or {"t":"frame","n":<seq>}

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

from core.camera import CameraManager, scale_to_width
from core.detector import FaceDetector, scale_raw_face
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
            if self.scanning and not force:
                self.hub.broadcast(self.hub.last_status)
                return
            self._cancel_release_timer()
            if self.scanning:
                self._cancel_locked()
            # A camera still warm from a failed scan is reused as-is: no 0.7 s start.
            self.cancel = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(self.cancel,), daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
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
        # The Face Studio server may be holding the webcam.
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:8765/api/camera/release", method="POST")
            urllib.request.urlopen(req, timeout=0.4)
        except Exception:
            pass
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
