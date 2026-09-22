"""
SFace face recognizer (OpenCV FaceRecognizerSF).
Extracts 128-d embeddings and scores them against an enrolled profile.
"""

import os
import threading
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import cv2
import numpy as np

DEFAULT_MODEL_PATH = str(Path(__file__).parent.parent / "models" / "face_recognition_sface_2021dec.onnx")


def _unit(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 1e-6 else vec


class FaceRecognizer:
    def __init__(self, model_path: str = DEFAULT_MODEL_PATH, default_threshold: float = 0.38):
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"SFace model not found at {model_path}")

        self.model_path = model_path
        self.default_threshold = default_threshold
        self.recognizer = cv2.FaceRecognizerSF.create(model_path, "")
        self._lock = threading.Lock()
        self._matrix_key = None
        self._matrix: Optional[np.ndarray] = None
        self._people_key = None
        self._people_matrix = np.zeros((0, 128), np.float32)
        self._people_owner = np.zeros(0, np.int32)

    def align_and_crop(self, frame: np.ndarray, raw_face: np.ndarray) -> np.ndarray:
        return self.recognizer.alignCrop(frame, raw_face)

    def extract_feature(self, frame: np.ndarray, raw_face: np.ndarray) -> np.ndarray:
        with self._lock:
            aligned = self.recognizer.alignCrop(frame, raw_face)
            feature = self.recognizer.feature(aligned)
        return feature.flatten().astype(np.float32)

    def compute_similarity(self, feature1: np.ndarray, feature2: np.ndarray) -> float:
        return float(np.dot(_unit(feature1.ravel()), _unit(feature2.ravel())))

    def _profile_matrix(self, profile: Dict[str, Any]) -> np.ndarray:
        """Unit-normalised centroid + exemplars, cached per profile revision."""
        key = (id(profile), profile.get("created_at"), profile.get("sample_count"))
        if key != self._matrix_key or self._matrix is None:
            rows = []
            if profile.get("centroid") is not None:
                rows.append(np.asarray(profile["centroid"], dtype=np.float32))
            for ex in profile.get("exemplars", []):
                rows.append(np.asarray(ex["embedding"], dtype=np.float32))
            if not rows:
                matrix = np.zeros((0, 128), dtype=np.float32)
            else:
                matrix = np.vstack(rows)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                matrix = matrix / np.maximum(norms, 1e-6)
            self._matrix = matrix.astype(np.float32)
            self._matrix_key = key
        return self._matrix

    def match_against_profile(
        self,
        candidate_feature: np.ndarray,
        profile: Dict[str, Any],
        threshold: Optional[float] = None,
    ) -> Tuple[bool, float]:
        """Best cosine similarity against the centroid and every exemplar."""
        if threshold is None:
            threshold = self.default_threshold
        matrix = self._profile_matrix(profile)
        if matrix.shape[0] == 0:
            return False, -1.0
        best = float(np.max(matrix @ _unit(candidate_feature.ravel().astype(np.float32))))
        return best >= threshold, best

    def match_people(
        self,
        candidate_feature: np.ndarray,
        people: List[Dict[str, Any]],
        threshold: Optional[float] = None,
    ) -> Tuple[bool, float, Optional[Dict[str, Any]]]:
        """Best match across every enrolled person (entries from StorageManager.list_people)."""
        if threshold is None:
            threshold = self.default_threshold
        key = tuple((p["id"], p["profile"].get("created_at"), p["profile"].get("sample_count")) for p in people)
        if key != self._people_key:
            rows, owners = [], []
            for index, person in enumerate(people):
                matrix = self._rows(person["profile"])
                rows.append(matrix)
                owners.extend([index] * matrix.shape[0])
            self._people_matrix = np.vstack(rows) if rows else np.zeros((0, 128), np.float32)
            self._people_owner = np.asarray(owners, dtype=np.int32)
            self._people_key = key
        if self._people_matrix.shape[0] == 0:
            return False, -1.0, None
        scores = self._people_matrix @ _unit(candidate_feature.ravel().astype(np.float32))
        best_row = int(np.argmax(scores))
        best = float(scores[best_row])
        person = people[int(self._people_owner[best_row])]
        return best >= threshold, best, person

    @staticmethod
    def _rows(profile: Dict[str, Any]) -> np.ndarray:
        rows = []
        if profile.get("centroid") is not None:
            rows.append(np.asarray(profile["centroid"], dtype=np.float32))
        for ex in profile.get("exemplars", []):
            rows.append(np.asarray(ex["embedding"], dtype=np.float32))
        if not rows:
            return np.zeros((0, 128), dtype=np.float32)
        matrix = np.vstack(rows)
        return (matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-6)).astype(np.float32)

    @staticmethod
    def calculate_centroid(embeddings: List[np.ndarray]) -> np.ndarray:
        if not embeddings:
            raise ValueError("No embeddings provided to calculate centroid")
        stack = np.vstack([_unit(e.ravel()) for e in embeddings])
        return _unit(np.mean(stack, axis=0)).astype(np.float32)
