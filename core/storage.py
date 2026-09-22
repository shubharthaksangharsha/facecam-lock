"""
Storage manager for FaceCam Lock.
Profiles and settings live in ~/.config/facecam-lock; per-session lock state
(preview frames, status, unlock token) lives on tmpfs in $XDG_RUNTIME_DIR.
"""

import base64
import hmac
import json
import os
import pwd
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

import cv2
import numpy as np

CONFIG_DIR = Path.home() / ".config" / "facecam-lock"
PROFILE_PATH = CONFIG_DIR / "profile.json"
AVATAR_PATH = CONFIG_DIR / "avatar.jpg"
SETTINGS_PATH = CONFIG_DIR / "config.json"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "facecam-lock"

MIN_ENROLL_SAMPLES = 5


def system_display_name() -> str:
    """Real name from the passwd GECOS field, falling back to the login name."""
    try:
        entry = pwd.getpwuid(os.getuid())
        gecos = entry.pw_gecos.split(",")[0].strip()
        return gecos or entry.pw_name.capitalize()
    except KeyError:
        return os.environ.get("USER", "friend").capitalize()


DEFAULT_SETTINGS = {
    "camera_index": 0,
    "threshold": 0.38,
    "max_attempts": 3,
    "attempt_timeout_sec": 2.2,
    "enable_face_unlock": True,
    "enable_lid_open_trigger": True,
    "show_camera_preview_on_lock": True,
    "studio_password": "",
    "display_name": "",
    "theme": "omarchy",
}


def ensure_runtime_dir() -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
    return RUNTIME_DIR


class StorageManager:
    def __init__(self, config_dir: Path = CONFIG_DIR):
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.profile_path = self.config_dir / "profile.json"
        self.settings_path = self.config_dir / "config.json"
        self._profile_cache: Optional[Dict[str, Any]] = None
        self._profile_mtime: Optional[float] = None
        self._people_cache: Dict[str, Any] = {}

    def get_settings(self) -> Dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                merged.update(data)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[Storage] Error reading settings: {e}")
        return merged

    def save_settings(self, settings: Dict[str, Any]) -> bool:
        try:
            current = self.get_settings()
            current.update(settings)
            tmp = self.settings_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.settings_path)
            return True
        except Exception as e:
            print(f"[Storage] Error saving settings: {e}")
            return False

    def get_display_name(self, profile: Optional[Dict[str, Any]] = None) -> str:
        """The registered face label used in the welcome message."""
        if profile is None:
            profile = self.get_profile()
        if profile:
            name = (profile.get("display_name") or profile.get("label") or "").strip()
            if name:
                return name
        return str(self.get_settings().get("display_name") or system_display_name())

    def has_studio_password(self) -> bool:
        return bool(self.get_settings().get("studio_password"))

    def verify_studio_password(self, password: str) -> bool:
        expected = str(self.get_settings().get("studio_password") or "")
        if not expected:
            return False
        provided = password if isinstance(password, str) else ""
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

    def has_profile(self) -> bool:
        return self.profile_path.exists()

    def get_profile(self) -> Optional[Dict[str, Any]]:
        """Load the enrolled profile, re-reading only when the file changed."""
        try:
            mtime = self.profile_path.stat().st_mtime
        except FileNotFoundError:
            self._profile_cache = None
            self._profile_mtime = None
            return None
        if self._profile_cache is not None and mtime == self._profile_mtime:
            return self._profile_cache
        try:
            with open(self.profile_path, "r", encoding="utf-8") as f:
                self._profile_cache = json.load(f)
            self._profile_mtime = mtime
            return self._profile_cache
        except Exception as e:
            print(f"[Storage] Error loading profile: {e}")
            return None

    def save_profile(self, profile: Dict[str, Any]) -> bool:
        try:
            tmp = self.profile_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(profile, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.profile_path)
            self._profile_cache = None
            return True
        except Exception as e:
            print(f"[Storage] Error saving profile: {e}")
            return False

    @property
    def avatar_path(self) -> Path:
        return self.config_dir / "avatar.jpg"

    def has_avatar(self) -> bool:
        return self.avatar_path.exists()

    def save_avatar(self, jpeg: bytes) -> None:
        tmp = self.avatar_path.with_suffix(".jpg.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, jpeg)
        finally:
            os.close(fd)
        os.replace(tmp, self.avatar_path)

    def delete_profile(self) -> bool:
        try:
            # The photo belongs to the enrolled face; it goes with it.
            self.avatar_path.unlink(missing_ok=True)
            self.profile_path.unlink(missing_ok=True)
            self._profile_cache = None
            return True
        except Exception as e:
            print(f"[Storage] Error deleting profile: {e}")
            return False

    # ------------------------------------------------------------ extra people
    # The owner lives in profile.json (the PAM hook checks for that file). Anyone
    # else who may unlock this session lives in people/<id>.json with an optional
    # people/<id>.jpg welcome photo that is only shown when that face matches.

    @property
    def people_dir(self) -> Path:
        return self.config_dir / "people"

    def list_people(self) -> List[Dict[str, Any]]:
        """Everyone who can unlock: owner first. Each entry: id, display_name, profile, avatar."""
        people = []
        owner = self.get_profile()
        if owner:
            people.append({
                "id": "owner",
                "display_name": self.get_display_name(owner),
                "profile": owner,
                "avatar": str(self.avatar_path) if self.has_avatar() else None,
            })
        if self.people_dir.is_dir():
            for path in sorted(self.people_dir.glob("*.json")):
                try:
                    mtime = path.stat().st_mtime
                    cached = self._people_cache.get(path.name)
                    if cached and cached[0] == mtime:
                        profile = cached[1]
                    else:
                        with open(path, "r", encoding="utf-8") as f:
                            profile = json.load(f)
                        self._people_cache[path.name] = (mtime, profile)
                except (OSError, ValueError) as e:
                    print(f"[Storage] Skipping {path.name}: {e}")
                    continue
                avatar = path.with_suffix(".jpg")
                people.append({
                    "id": path.stem,
                    "display_name": profile.get("display_name") or path.stem.capitalize(),
                    "profile": profile,
                    "avatar": str(avatar) if avatar.exists() else None,
                })
        return people

    def save_person(self, person_id: str, profile: Dict[str, Any], avatar_jpeg: Optional[bytes] = None) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", person_id) or person_id == "owner":
            raise ValueError("Person id must be lowercase letters, digits, - or _")
        self.people_dir.mkdir(mode=0o700, exist_ok=True)
        target = self.people_dir / f"{person_id}.json"
        self._write_private(target, json.dumps(profile).encode())
        if avatar_jpeg:
            self._write_private(target.with_suffix(".jpg"), avatar_jpeg)
        return target

    def delete_person(self, person_id: str) -> bool:
        target = self.people_dir / f"{person_id}.json"
        if person_id == "owner" or not target.exists():
            return False
        target.unlink()
        target.with_suffix(".jpg").unlink(missing_ok=True)
        self._people_cache.pop(target.name, None)
        return True

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    @staticmethod
    def frame_to_jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buffer.tobytes() if ok else b""

    @staticmethod
    def frame_to_base64_jpeg(frame: np.ndarray, quality: int = 85) -> str:
        data = StorageManager.frame_to_jpeg(frame, quality)
        if not data:
            return ""
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
