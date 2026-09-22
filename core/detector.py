"""
YuNet Face Detector module using OpenCV's FaceDetectorYN.
Handles real-time multi-angle face detection and landmark localization.
"""

import os
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import cv2
import numpy as np

DEFAULT_MODEL_PATH = str(Path(__file__).parent.parent / "models" / "face_detection_yunet_2023mar.onnx")


class FaceDetector:
    def __init__(
        self,
        model_path: str = DEFAULT_MODEL_PATH,
        score_threshold: float = 0.65,
        nms_threshold: float = 0.3,
        top_k: int = 50,
    ):
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"YuNet model not found at {model_path}")

        self.model_path = model_path
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.top_k = top_k
        self._input_size = (320, 240)
        self._lock = threading.Lock()

        self.detector = cv2.FaceDetectorYN.create(
            model_path,
            "",
            self._input_size,
            self.score_threshold,
            self.nms_threshold,
            self.top_k,
        )

    def detect(self, frame: np.ndarray, compute_quality: bool = True) -> List[Dict[str, Any]]:
        """
        Detect faces in a BGR frame.
        Returns a list of face dictionaries containing bounding boxes,
        confidence scores, 5 facial landmarks, and raw face arrays.
        Set compute_quality=False on the lock path to skip Laplacian sharpness.
        """
        if frame is None or frame.size == 0:
            return []

        h, w = frame.shape[:2]
        # cv2.dnn nets are not thread-safe, and the input size is shared state.
        with self._lock:
            if self._input_size != (w, h):
                self._input_size = (w, h)
                self.detector.setInputSize((w, h))
            _, faces = self.detector.detect(frame)
        if faces is None or len(faces) == 0:
            return []

        results = []
        for raw_face in faces:
            # Face format in YuNet:
            # [x1, y1, w, h, x_re, y_re, x_le, y_le, x_nt, y_nt, x_rcm, y_rcm, x_lcm, y_lcm, score]
            x, y, bw, bh = (
                int(raw_face[0]),
                int(raw_face[1]),
                int(raw_face[2]),
                int(raw_face[3]),
            )
            score = float(raw_face[-1])

            landmarks = [
                [int(raw_face[4]), int(raw_face[5])],    # Right eye
                [int(raw_face[6]), int(raw_face[7])],    # Left eye
                [int(raw_face[8]), int(raw_face[9])],    # Nose tip
                [int(raw_face[10]), int(raw_face[11])],  # Right corner of mouth
                [int(raw_face[12]), int(raw_face[13])],  # Left corner of mouth
            ]

            sharpness = 0.0
            centeredness = 0.0
            rel_size = float(bw / max(1, w))
            is_good_quality = score > 0.7 and rel_size > 0.12

            if compute_quality:
                face_roi = frame[max(0, y):min(h, y + bh), max(0, x):min(w, x + bw)]
                if face_roi.size > 0:
                    gray_roi = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
                    sharpness = float(cv2.Laplacian(gray_roi, cv2.CV_64F).var())
                center_x = x + bw / 2.0
                center_y = y + bh / 2.0
                dist_from_center = np.sqrt(
                    ((center_x - w / 2.0) / (w / 2.0)) ** 2
                    + ((center_y - h / 2.0) / (h / 2.0)) ** 2
                )
                centeredness = max(0.0, float(1.0 - dist_from_center / 1.414))
                is_good_quality = bool(sharpness > 25.0 and rel_size > 0.15 and score > 0.7)

            results.append({
                "box": [x, y, bw, bh],
                "score": score,
                "landmarks": landmarks,
                "sharpness": sharpness,
                "centeredness": centeredness,
                "rel_size": rel_size,
                "is_good_quality": is_good_quality,
                "raw": raw_face,
            })

        # Sort by area descending (largest/closest face first)
        results.sort(key=lambda f: f["box"][2] * f["box"][3], reverse=True)
        return results


def scale_raw_face(raw_face: np.ndarray, factor: float) -> np.ndarray:
    """Map a YuNet row detected on a downscaled frame back to full-resolution coords."""
    scaled = raw_face.copy()
    scaled[:14] *= factor
    return scaled
