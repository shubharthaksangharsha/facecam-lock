"""
Build a face profile for an additional person, from photos and/or the webcam.

Photos with several faces are handled by skipping faces that already belong to
an enrolled person (e.g. the owner in a couple photo); a photo is only used if
exactly one unknown face remains. Outliers that don't agree with the rest of the
set are dropped, so a stray photo of someone else doesn't poison the profile.
"""

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

from .camera import scale_to_width
from .detector import FaceDetector, scale_raw_face
from .recognizer import FaceRecognizer

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
KNOWN_FACE_SIMILARITY = 0.42   # a face this close to an enrolled person is theirs
OUTLIER_SIMILARITY = 0.30      # samples below this vs the rest are dropped
MIN_FACE_PX = 80
MIN_DETECTION_SCORE = 0.8


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else v


class Enroller:
    def __init__(self, detector: FaceDetector, recognizer: FaceRecognizer, known_people: List[Dict]):
        self.detector = detector
        self.recognizer = recognizer
        self.known = known_people
        self.samples: List[Dict] = []
        self.log: List[str] = []

    def _unknown_faces(self, image: np.ndarray):
        """(embedding, face) for every sufficiently large face that isn't an enrolled person."""
        small = scale_to_width(image, 1280)
        factor = image.shape[1] / float(small.shape[1])
        found = []
        for face in self.detector.detect(small, compute_quality=False):
            if face["score"] < MIN_DETECTION_SCORE or face["box"][2] * factor < MIN_FACE_PX:
                continue
            try:
                emb = self.recognizer.extract_feature(image, scale_raw_face(face["raw"], factor))
            except cv2.error:
                continue
            if self.known:
                _, score, person = self.recognizer.match_people(emb, self.known, KNOWN_FACE_SIMILARITY)
                if score >= KNOWN_FACE_SIMILARITY:
                    continue
            box = [int(v * factor) for v in face["box"]]
            found.append((emb, box, face["score"]))
        return found

    def add_image(self, image: np.ndarray, source: str) -> bool:
        faces = self._unknown_faces(image)
        if len(faces) != 1:
            self.log.append(f"skip {source}: {'no usable face' if not faces else f'{len(faces)} unknown faces'}")
            return False
        emb, (x, y, w, h), score = faces[0]
        pad = int(max(w, h) * 0.3)
        crop = image[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
        ok, jpg = cv2.imencode(".jpg", scale_to_width(crop, 160), [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        self.samples.append({"embedding": emb, "source": source, "score": score,
                             "thumbnail": jpg.tobytes() if ok else b""})
        self.log.append(f"use  {source}")
        return True

    def add_photos(self, folder: Path) -> int:
        paths = sorted(p for p in Path(folder).expanduser().iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        used = 0
        for path in paths:
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                self.log.append(f"skip {path.name}: unreadable")
                continue
            used += self.add_image(image, path.name)
        return used

    def add_live(self, camera, prompt: Callable[[str], None], target: int = 10, seconds: float = 12.0) -> int:
        """Capture from the webcam while the person turns their head a little."""
        poses = ["look straight at the screen", "turn slightly left", "turn slightly right",
                 "tilt your chin up a little", "smile", "look straight again"]
        used, seq = 0, 0
        start = time.monotonic()
        next_pose = start
        pose_index = 0
        last_take = 0.0
        while used < target and time.monotonic() - start < seconds:
            now = time.monotonic()
            if now >= next_pose and pose_index < len(poses):
                prompt(poses[pose_index])
                pose_index += 1
                next_pose = now + seconds / len(poses)
            seq, frame = camera.wait_frame(seq, timeout=0.5)
            if frame is None or now - last_take < 0.45:
                continue
            if self.add_image(frame.copy(), f"camera #{used + 1}"):
                used += 1
                last_take = now
        return used

    def prune_outliers(self) -> int:
        """Drop samples that disagree with the rest (other people, bad crops)."""
        dropped = 0
        while len(self.samples) > 3:
            embs = np.vstack([_unit(s["embedding"]) for s in self.samples])
            total = embs.sum(axis=0)
            scores = [float(np.dot(embs[i], _unit(total - embs[i]))) for i in range(len(embs))]
            worst = int(np.argmin(scores))
            if scores[worst] >= OUTLIER_SIMILARITY:
                break
            self.log.append(f"drop {self.samples[worst]['source']}: doesn't match the others ({scores[worst]:.2f})")
            self.samples.pop(worst)
            dropped += 1
        return dropped

    def build_profile(self, display_name: str) -> Dict:
        embeddings = [s["embedding"] for s in self.samples]
        centroid = FaceRecognizer.calculate_centroid(embeddings)
        return {
            "display_name": display_name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "centroid": centroid.tolist(),
            "exemplars": [{"pose": s["source"], "embedding": s["embedding"].tolist(), "score": s["score"]}
                          for s in self.samples],
            "sample_count": len(self.samples),
        }

    def closest_known(self, profile: Dict) -> Optional[tuple]:
        """How close the new centroid is to someone already enrolled (sanity check)."""
        if not self.known:
            return None
        _, score, person = self.recognizer.match_people(np.asarray(profile["centroid"], np.float32), self.known)
        return person["display_name"], score
