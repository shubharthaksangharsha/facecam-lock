"""
Face Verification Engine for FaceCam Lock.
Executes the 3-attempt biometric verification cycle with real-time feedback.
"""

import time
from typing import Dict, Any, Optional, Callable, Generator
import cv2
import numpy as np

from .detector import FaceDetector
from .recognizer import FaceRecognizer
from .storage import StorageManager
from .camera import CameraManager


class FaceVerifier:
    def __init__(
        self,
        detector: Optional[FaceDetector] = None,
        recognizer: Optional[FaceRecognizer] = None,
        storage: Optional[StorageManager] = None,
        camera: Optional[CameraManager] = None,
    ):
        self.detector = detector or FaceDetector()
        self.recognizer = recognizer or FaceRecognizer()
        self.storage = storage or StorageManager()
        self.camera = camera or CameraManager()

    def run_verification(
        self,
        max_attempts: int = 3,
        attempt_timeout_sec: float = 2.0,
        threshold: Optional[float] = None,
        on_status: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """
        Run the 3-attempt face verification workflow.
        Returns result dict with 'success', 'attempts_used', 'best_score', etc.
        """
        profile = self.storage.get_profile()
        if not profile:
            res = {
                "success": False,
                "reason": "no_profile",
                "message": "Can't detect face",
                "require_password": True
            }
            if on_status:
                on_status(res)
            return res

        settings = self.storage.get_settings()
        sim_threshold = threshold if threshold is not None else settings.get("threshold", 0.38)
        cam_idx = settings.get("camera_index", 0)

        # Ensure camera is started
        if not self.camera.start(device_index=cam_idx):
            res = {
                "success": False,
                "reason": "camera_error",
                "message": "Can't detect face",
                "require_password": True
            }
            if on_status:
                on_status(res)
            return res

        overall_best_score = -1.0
        success = False
        winning_attempt = 0

        try:
            for attempt in range(1, max_attempts + 1):
                attempt_status = {
                    "status": "scanning",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "message": f"Scanning face... (Attempt {attempt} of {max_attempts})"
                }
                if on_status:
                    on_status(attempt_status)

                start_time = time.time()
                attempt_best_score = -1.0
                attempt_matched = False

                while (time.time() - start_time) < attempt_timeout_sec:
                    ok, frame, _ = self.camera.get_latest_frame()
                    if not ok or frame is None:
                        time.sleep(0.04)
                        continue

                    # Detect faces on the live frame; skip Laplacian on the hot path
                    faces = self.detector.detect(frame, compute_quality=False)
                    if not faces:
                        time.sleep(0.04)
                        continue

                    # Evaluate the best candidate face in the frame
                    for face_info in faces:
                        try:
                            feature = self.recognizer.extract_feature(frame, face_info["raw"])
                            is_match, score = self.recognizer.match_against_profile(
                                feature, profile, threshold=sim_threshold
                            )
                            attempt_best_score = max(attempt_best_score, score)
                            overall_best_score = max(overall_best_score, score)

                            if is_match:
                                attempt_matched = True
                                break
                        except Exception as e:
                            print(f"[Verifier] Error processing face: {e}")

                    if attempt_matched:
                        break

                    time.sleep(0.03)

                if attempt_matched:
                    success = True
                    winning_attempt = attempt
                    success_status = {
                        "status": "matched",
                        "success": True,
                        "attempt": attempt,
                        "score": round(overall_best_score, 4),
                        "display_name": self.storage.get_display_name(profile),
                        "message": f"Welcome back {self.storage.get_display_name(profile)}",
                        "require_password": False
                    }
                    if on_status:
                        on_status(success_status)
                    return success_status

                # Attempt failed
                fail_status = {
                    "status": "attempt_failed",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "score": round(attempt_best_score, 4),
                    "message": f"Attempt {attempt} failed." if attempt < max_attempts else "Face not recognized."
                }
                if on_status:
                    on_status(fail_status)

                # Brief pause between attempts for user to adjust
                if attempt < max_attempts:
                    time.sleep(0.3)

            # All attempts exhausted
            all_failed = {
                "status": "exhausted",
                "success": False,
                "attempts_used": max_attempts,
                "best_score": round(overall_best_score, 4),
                "require_password": True,
                "message": "Can't detect face"
            }
            if on_status:
                on_status(all_failed)
            return all_failed

        finally:
            self.camera.stop()
