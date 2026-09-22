"""
Profile photo for the lock screen's "Welcome back" state.

The photo is cropped to a square around the largest detected face, resized to
AVATAR_SIZE, and stored next to the face profile (mode 0600). It is only shown
after the enrolled face has been matched.
"""

from typing import Optional, Tuple

import cv2
import numpy as np

from .camera import scale_to_width
from .detector import FaceDetector, scale_raw_face
from .recognizer import FaceRecognizer

AVATAR_SIZE = 512


def make_avatar(
    data: bytes,
    detector: FaceDetector,
    recognizer: Optional[FaceRecognizer] = None,
    profile: Optional[dict] = None,
) -> Tuple[bytes, Optional[float], bool]:
    """Return (jpeg bytes, similarity to the enrolled face or None, face_found)."""
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Not a readable image")

    h, w = image.shape[:2]
    small = scale_to_width(image, 640)
    factor = w / float(small.shape[1])
    faces = detector.detect(small, compute_quality=False)

    score = None
    if faces:
        fx, fy, fw, fh = [v * factor for v in faces[0]["box"]]
        side = min(max(fw, fh) * 2.1, w, h)
        cx = fx + fw / 2.0
        cy = fy + fh / 2.0 - fh * 0.08  # a little headroom above the eyes
        if recognizer is not None and profile:
            try:
                feature = recognizer.extract_feature(image, scale_raw_face(faces[0]["raw"], factor))
                _, score = recognizer.match_against_profile(feature, profile)
            except cv2.error:
                score = None
    else:
        side = min(w, h)
        cx, cy = w / 2.0, h / 2.0

    x0 = int(round(min(max(cx - side / 2.0, 0), w - side)))
    y0 = int(round(min(max(cy - side / 2.0, 0), h - side)))
    side = int(side)
    crop = image[y0:y0 + side, x0:x0 + side]
    crop = cv2.resize(crop, (AVATAR_SIZE, AVATAR_SIZE), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise ValueError("Could not encode the photo")
    return jpg.tobytes(), score, bool(faces)
