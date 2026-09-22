"""
Thread-safe camera stream manager.

A background thread blocks on `cap.read()` (the driver paces it), publishes the
newest frame with a sequence number, and wakes any consumer waiting on it. Consumers
never process the same frame twice and never busy-poll.
"""

import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np


class CameraManager:
    _instance: Optional["CameraManager"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(CameraManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, device_index: int = 0, width: int = 640, height: int = 480, fps: int = 30):
        if self._initialized:
            return
        self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = fps

        self.cap: Optional[cv2.VideoCapture] = None
        self.is_running = False
        self.latest_frame: Optional[np.ndarray] = None
        self.latest_timestamp: float = 0.0
        self.frame_seq = 0
        self.frame_cond = threading.Condition()
        self.worker_thread: Optional[threading.Thread] = None
        self.active_clients = 0
        self.read_failures = 0
        self._initialized = True

    def _open(self) -> Optional[cv2.VideoCapture]:
        # Passing the format at open time avoids a stream restart after the first
        # read; MJPG keeps 640x480 at the sensor's full 30 fps over USB.
        params = [
            cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"),
            cv2.CAP_PROP_FRAME_WIDTH, self.width,
            cv2.CAP_PROP_FRAME_HEIGHT, self.height,
            cv2.CAP_PROP_FPS, self.fps,
        ]
        cap = cv2.VideoCapture(self.device_index, cv2.CAP_V4L2, params)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.device_index)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def start(self, device_index: Optional[int] = None) -> bool:
        """Start capture (reference counted)."""
        with self._lock:
            if device_index is not None and device_index != self.device_index:
                self._stop_locked()
                self.active_clients = 0
                self.device_index = device_index

            self.active_clients += 1
            if self.is_running:
                return True

            cap = self._open()
            if cap is None:
                print(f"[Camera] Failed to open video device {self.device_index}")
                self.active_clients = max(0, self.active_clients - 1)
                return False

            self.cap = cap
            self.read_failures = 0
            self.is_running = True
            self.worker_thread = threading.Thread(target=self._capture_loop, daemon=True)
            self.worker_thread.start()
            return True

    def stop(self, force: bool = False):
        """Release the camera once no client needs it (or immediately with force)."""
        with self._lock:
            if not force:
                self.active_clients = max(0, self.active_clients - 1)
                if self.active_clients > 0:
                    return
            self.active_clients = 0
            self._stop_locked()

    def _stop_locked(self):
        self.is_running = False
        worker = self.worker_thread
        self.worker_thread = None
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        with self.frame_cond:
            self.latest_frame = None
            self.frame_cond.notify_all()

    def _capture_loop(self):
        cap = self.cap
        while self.is_running and cap is not None:
            ok, frame = cap.read()
            if not ok or frame is None:
                self.read_failures += 1
                time.sleep(0.01)
                continue
            self.read_failures = 0
            with self.frame_cond:
                self.latest_frame = frame
                self.latest_timestamp = time.monotonic()
                self.frame_seq += 1
                self.frame_cond.notify_all()

    def wait_frame(self, after_seq: int, timeout: float = 0.5) -> Tuple[int, Optional[np.ndarray]]:
        """Block until a frame newer than `after_seq` exists. The frame is shared; do not mutate it."""
        deadline = time.monotonic() + timeout
        with self.frame_cond:
            while self.is_running and (self.latest_frame is None or self.frame_seq <= after_seq):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.frame_cond.wait(remaining)
            if self.latest_frame is None or self.frame_seq <= after_seq:
                return after_seq, None
            return self.frame_seq, self.latest_frame

    def get_latest_frame(self, copy: bool = True) -> Tuple[bool, Optional[np.ndarray], float]:
        with self.frame_cond:
            if self.latest_frame is None:
                return False, None, 0.0
            frame = self.latest_frame.copy() if copy else self.latest_frame
            return True, frame, self.latest_timestamp

    def get_latest_frame_scaled(self, max_width: int = 320) -> Tuple[bool, Optional[np.ndarray], float]:
        ok, frame, ts = self.get_latest_frame(copy=False)
        if not ok:
            return False, None, 0.0
        return True, scale_to_width(frame, max_width), ts

    def capture_single_frame(self, timeout: float = 2.0) -> Optional[np.ndarray]:
        """Return one fresh frame, starting the camera temporarily if needed."""
        was_running = self.is_running
        if not was_running and not self.start():
            return None
        seq, frame = self.wait_frame(self.frame_seq, timeout=timeout)
        frame = None if frame is None else frame.copy()
        if not was_running:
            self.stop(force=True)
        return frame


def scale_to_width(frame: np.ndarray, max_width: int) -> np.ndarray:
    """Downscale (always returns a new array, safe to keep)."""
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame.copy()
    scale = max_width / float(w)
    return cv2.resize(frame, (max_width, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
