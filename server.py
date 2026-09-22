"""
FastAPI server for the FaceCam Lock Face Studio.

Heavy OpenCV work runs in worker threads (sync route handlers and
asyncio.to_thread) so the event loop only shuffles bytes. The live stream sends
one binary WebSocket message per frame: a 4-byte big-endian header length, a
JSON header, then raw JPEG bytes.
"""

import asyncio
import base64
import binascii
import json
import os
import re
import struct
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core import (
    FaceDetector,
    FaceRecognizer,
    StorageManager,
    CameraManager,
    FaceVerifier,
    verify_user_password,
    get_current_username,
)
from core.avatar import make_avatar
from core.camera import scale_to_width
from core.detector import scale_raw_face
from core.storage import MIN_ENROLL_SAMPLES

cv2.setNumThreads(max(1, min(4, os.cpu_count() or 1)))

app = FastAPI(title="FaceCam Lock", version="2.0.0")

BASE_DIR = Path(__file__).parent
ui_dir = BASE_DIR / "ui"
app.mount("/static", StaticFiles(directory=str(ui_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(ui_dir / "templates"))

detector = FaceDetector()
recognizer = FaceRecognizer()
storage = StorageManager()
camera = CameraManager()
verifier = FaceVerifier(detector, recognizer, storage, camera)

STREAM_WIDTH = 480
DETECT_WIDTH = 320
STREAM_FPS = 30.0
THEMES = ("omarchy", "midnight", "matrix", "sunrise")
OMARCHY_COLORS = Path.home() / ".local/state/omarchy/current/theme/colors.toml"


class SettingsUpdate(BaseModel):
    threshold: Optional[float] = None
    camera_index: Optional[int] = None
    max_attempts: Optional[int] = None
    attempt_timeout_sec: Optional[float] = None
    enable_face_unlock: Optional[bool] = None
    enable_lid_open_trigger: Optional[bool] = None
    show_camera_preview_on_lock: Optional[bool] = None
    theme: Optional[str] = None


class PasswordCheck(BaseModel):
    password: str


class EnrollSaveRequest(BaseModel):
    samples: List[Dict[str, Any]]
    avatar_base64: Optional[str] = None
    display_name: Optional[str] = None
    password: str


class StudioPasswordRequest(BaseModel):
    password: str


class AvatarRequest(BaseModel):
    password: str
    image: str  # data URL or bare base64


def _public_settings() -> Dict[str, Any]:
    settings = dict(storage.get_settings())
    settings.pop("studio_password", None)
    settings["has_studio_password"] = storage.has_studio_password()
    settings["min_enroll_samples"] = MIN_ENROLL_SAMPLES
    return settings


def _require_studio_password(password: str) -> None:
    if not storage.verify_studio_password(password or ""):
        raise HTTPException(status_code=401, detail="Incorrect training password")


def _read_omarchy_colors() -> Dict[str, str]:
    colors: Dict[str, str] = {}
    try:
        for line in OMARCHY_COLORS.read_text(encoding="utf-8").splitlines():
            match = re.match(r'^\s*([A-Za-z0-9_]+)\s*=\s*"?(#[0-9A-Fa-f]{6}|dark|light)"?', line)
            if match:
                colors[match.group(1)] = match.group(2)
    except OSError:
        pass
    return colors


def _rgb(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"{int(h[0:2], 16)} {int(h[2:4], 16)} {int(h[4:6], 16)}"


def _omarchy_css_vars() -> str:
    """Map the desktop's colors.toml onto the studio's theme tokens."""
    c = _read_omarchy_colors()
    if "background" not in c or "foreground" not in c:
        return ""
    pick = lambda *keys: next((c[k] for k in keys if k in c and c[k].startswith("#")), None)
    tokens = {
        "bg": pick("background"),
        "bg-deep": pick("darker_background", "dark_background", "background"),
        "surface": pick("lighter_background", "dark_background", "background"),
        "line": pick("selection", "muted", "lighter_background"),
        "fg": pick("foreground"),
        "muted": pick("dark_foreground", "muted", "foreground"),
        "accent": pick("accent", "blue", "cyan"),
        "accent2": pick("magenta", "blue", "accent"),
        "ok": pick("green", "bright_green"),
        "warn": pick("yellow", "orange"),
        "bad": pick("red", "bright_red"),
        "on-accent": pick("darker_background", "background"),
    }
    parts = [f"--{k}: {_rgb(v)}" for k, v in tokens.items() if v]
    parts.append(f"color-scheme: {'light' if c.get('mode') == 'light' else 'dark'}")
    return "; ".join(parts)


def _page_context() -> Dict[str, Any]:
    theme = storage.get_settings().get("theme", "omarchy")
    omarchy_vars = _omarchy_css_vars()
    if theme == "omarchy" and not omarchy_vars:
        theme = "midnight"
    return {
        "username": get_current_username(),
        "display_name": storage.get_display_name(),
        "has_profile": storage.has_profile(),
        "theme": theme,
        "omarchy_vars": omarchy_vars,
    }


@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context=_page_context())


@app.get("/lock", response_class=HTMLResponse)
def lock_page(request: Request):
    return templates.TemplateResponse(request=request, name="lock.html", context=_page_context())


@app.get("/api/status")
def get_status():
    profile = storage.get_profile()
    profile_summary = None
    if profile:
        profile_summary = {
            "display_name": storage.get_display_name(profile),
            "created_at": profile.get("created_at"),
            "sample_count": len(profile.get("exemplars", [])),
            "avatar_base64": profile.get("avatar_base64"),
            "min_samples": MIN_ENROLL_SAMPLES,
            "photo_version": int(storage.avatar_path.stat().st_mtime) if storage.has_avatar() else None,
        }
    return {
        "has_profile": profile is not None,
        "profile": profile_summary,
        "settings": _public_settings(),
        "username": get_current_username(),
        "display_name": storage.get_display_name(profile),
        "min_enroll_samples": MIN_ENROLL_SAMPLES,
        "camera_active": camera.is_running,
    }


@app.get("/api/theme")
def get_theme():
    """Current Omarchy palette as CSS variables, so the studio can follow the desktop theme."""
    css = _omarchy_css_vars()
    return {"available": bool(css), "omarchy_vars": css, "themes": THEMES}


@app.get("/api/system/health")
def system_health():
    """What the Settings tab shows under Omarchy integration."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "facecam-lock"
    plugin_root = Path.home() / ".config/omarchy/plugins"
    pam = Path("/etc/pam.d/omarchy-lock-fingerprint")
    try:
        pam_ok = "facecam" in pam.read_text(encoding="utf-8")
    except OSError:
        pam_ok = False
    plugin_ok = any((plugin_root / d / "manifest.json").exists()
                    and "facecam" in (plugin_root / d / "manifest.json").read_text(encoding="utf-8", errors="ignore")
                    for d in (os.listdir(plugin_root) if plugin_root.is_dir() else []))
    return {
        "daemon": (runtime / "ctl.sock").exists(),
        "pam": pam_ok,
        "plugin": plugin_ok,
        "profile": storage.has_profile(),
        "samples": len((storage.get_profile() or {}).get("exemplars", [])),
    }


@app.get("/api/settings")
def get_settings():
    return _public_settings()


@app.post("/api/settings")
def update_settings(update: SettingsUpdate):
    data = {k: v for k, v in update.model_dump().items() if v is not None}
    if "theme" in data and data["theme"] not in THEMES:
        raise HTTPException(status_code=400, detail="Unknown theme")
    if not storage.save_settings(data):
        raise HTTPException(status_code=500, detail="Failed to save settings")
    return {"status": "ok", "settings": _public_settings()}


@app.post("/api/enroll/capture")
def capture_enrollment_sample(pose_name: str = "frontal"):
    """Capture one frame and compute its face embedding."""
    frame = camera.capture_single_frame(timeout=2.5)
    if frame is None:
        raise HTTPException(status_code=503, detail="Failed to capture frame from webcam")

    faces = detector.detect(frame)
    if not faces:
        raise HTTPException(status_code=400, detail="No face detected in camera view")

    face_info = faces[0]
    if face_info["score"] < 0.65:
        raise HTTPException(status_code=400, detail="Face detection confidence too low. Please center your face.")

    feature = recognizer.extract_feature(frame, face_info["raw"])

    x, y, w, h = face_info["box"]
    fh, fw = frame.shape[:2]
    pad_x, pad_y = int(w * 0.25), int(h * 0.25)
    crop = frame[max(0, y - pad_y):min(fh, y + h + pad_y), max(0, x - pad_x):min(fw, x + w + pad_x)]
    crop = scale_to_width(crop, 160)

    return {
        "status": "success",
        "pose": pose_name,
        "score": float(face_info["score"]),
        "sharpness": round(face_info["sharpness"], 1),
        "is_good_quality": face_info["is_good_quality"],
        "embedding": feature.tolist(),
        "thumbnail": storage.frame_to_base64_jpeg(crop, quality=80),
    }


@app.post("/api/enroll/save")
def save_enrollment(payload: EnrollSaveRequest):
    _require_studio_password(payload.password)
    if len(payload.samples) < MIN_ENROLL_SAMPLES:
        raise HTTPException(
            status_code=400,
            detail=f"Please capture at least {MIN_ENROLL_SAMPLES} face photos before training",
        )

    embeddings = [np.asarray(s["embedding"], dtype=np.float32) for s in payload.samples]
    centroid = FaceRecognizer.calculate_centroid(embeddings)
    display_name = (payload.display_name or "").strip() or storage.get_display_name(None)
    storage.save_settings({"display_name": display_name})

    profile_data = {
        "username": get_current_username(),
        "display_name": display_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "centroid": centroid.tolist(),
        "exemplars": [
            {
                "pose": s.get("pose", f"pose_{i}"),
                "pose_title": s.get("pose_title", s.get("pose", f"pose_{i}")),
                "embedding": s["embedding"],
                "score": s.get("score", 1.0),
            }
            for i, s in enumerate(payload.samples)
        ],
        "sample_count": len(payload.samples),
        "avatar_base64": payload.avatar_base64 or payload.samples[0].get("thumbnail"),
    }

    if not storage.save_profile(profile_data):
        raise HTTPException(status_code=500, detail="Failed to save face profile")
    return {"status": "ok", "display_name": display_name, "sample_count": len(payload.samples)}


@app.get("/api/profile/photo")
def get_profile_photo():
    if not storage.has_avatar():
        raise HTTPException(status_code=404, detail="No profile photo")
    return FileResponse(storage.avatar_path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/profile/photo")
def set_profile_photo(payload: AvatarRequest):
    """Crop the uploaded photo around the face and keep it for the lock screen's welcome state."""
    _require_studio_password(payload.password)
    profile = storage.get_profile()
    if not profile:
        raise HTTPException(status_code=400, detail="Enroll your face before adding a photo")
    raw = payload.image.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
        if len(data) > 15 * 1024 * 1024:
            raise ValueError("Photo is larger than 15 MB")
        jpeg, score, face_found = make_avatar(data, detector, recognizer, profile)
    except (ValueError, binascii.Error) as e:
        raise HTTPException(status_code=400, detail=str(e) or "Not a readable image")
    storage.save_avatar(jpeg)
    return {"status": "ok", "face_found": face_found, "similarity": None if score is None else round(score, 3)}


@app.delete("/api/profile/photo")
def delete_profile_photo(payload: StudioPasswordRequest):
    _require_studio_password(payload.password)
    storage.avatar_path.unlink(missing_ok=True)
    return {"status": "ok"}


@app.post("/api/enroll/delete")
def delete_profile(payload: StudioPasswordRequest):
    _require_studio_password(payload.password)
    if storage.delete_profile():
        return {"status": "ok"}
    raise HTTPException(status_code=500, detail="Failed to delete profile")


@app.post("/api/auth/studio")
def check_studio_password(payload: StudioPasswordRequest):
    """Validate the training password; on first run, the first password entered becomes it."""
    if not storage.has_studio_password():
        if len(payload.password or "") < 4:
            return {"valid": False, "created": False, "message": "Choose a training password (4+ characters)"}
        storage.save_settings({"studio_password": payload.password})
        return {"valid": True, "created": True, "message": "Training password set"}
    valid = storage.verify_studio_password(payload.password)
    return {"valid": valid, "created": False, "message": "ok" if valid else "Incorrect training password"}


@app.post("/api/camera/release")
def release_camera():
    camera.stop(force=True)
    return {"status": "ok", "camera_active": camera.is_running}


@app.post("/api/verify")
def run_verify():
    settings = storage.get_settings()
    return verifier.run_verification(
        max_attempts=settings.get("max_attempts", 3),
        attempt_timeout_sec=settings.get("attempt_timeout_sec", 2.2),
        threshold=settings.get("threshold", 0.38),
    )


@app.post("/api/auth/password")
def check_password(payload: PasswordCheck):
    valid, msg = verify_user_password(payload.password)
    return {"valid": valid, "message": msg}


@app.post("/api/system/lock")
def trigger_lock():
    try:
        subprocess.Popen(["omarchy-system-lock"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {"status": "ok"}
    except OSError as e:
        return {"status": "error", "message": str(e)}


def _pack(header: Dict[str, Any], jpeg: bytes) -> bytes:
    head = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(head)) + head + jpeg


class FrameAnalyzer:
    """Per-connection state for the studio stream (runs in a worker thread)."""

    def __init__(self, profile, threshold: float):
        self.profile = profile
        self.threshold = threshold
        self.tick = 0
        self.last_similarity = None

    def process(self, frame: np.ndarray) -> bytes:
        self.tick += 1
        view = scale_to_width(frame, STREAM_WIDTH)
        small = scale_to_width(frame, DETECT_WIDTH)
        to_view = view.shape[1] / float(small.shape[1])
        to_full = frame.shape[1] / float(small.shape[1])

        faces_out = []
        best = None
        for face in detector.detect(small, compute_quality=False)[:3]:
            box = [round(v * to_view) for v in face["box"]]
            landmarks = [[round(x * to_view), round(y * to_view)] for x, y in face["landmarks"]]
            item = {"box": box, "score": round(face["score"], 3), "landmarks": landmarks}
            # Recognition every other frame keeps the stream at full rate.
            if self.profile and self.tick % 2 == 0:
                try:
                    feature = recognizer.extract_feature(frame, scale_raw_face(face["raw"], to_full))
                    _, sim = recognizer.match_against_profile(feature, self.profile, self.threshold)
                    best = sim if best is None else max(best, sim)
                except cv2.error:
                    pass
            faces_out.append(item)

        if self.profile and self.tick % 2 == 0:
            self.last_similarity = best
        elif not faces_out:
            self.last_similarity = None

        header = {
            "faces": faces_out,
            "best_similarity": None if self.last_similarity is None else round(self.last_similarity, 4),
            "threshold": self.threshold,
            "w": view.shape[1],
            "h": view.shape[0],
        }
        return _pack(header, storage.frame_to_jpeg(view, quality=72))


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    settings = storage.get_settings()
    if not await asyncio.to_thread(camera.start, int(settings.get("camera_index", 0))):
        await websocket.send_text(json.dumps({"error": "Cannot open camera"}))
        await websocket.close()
        return

    analyzer = FrameAnalyzer(storage.get_profile(), float(settings.get("threshold", 0.38)))
    min_interval = 1.0 / STREAM_FPS - 0.005
    seq = 0
    last = 0.0
    try:
        while True:
            seq, frame = await asyncio.to_thread(camera.wait_frame, seq, 0.5)
            if frame is None:
                if not camera.is_running:
                    break
                continue
            now = time.monotonic()
            if now - last < min_interval:
                continue
            last = now
            payload = await asyncio.to_thread(analyzer.process, frame)
            await websocket.send_bytes(payload)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        camera.stop()


@app.websocket("/ws/verify")
async def websocket_verify(websocket: WebSocket):
    """Lock-screen simulator: streams frames plus attempt status as JSON."""
    await websocket.accept()
    settings = storage.get_settings()
    profile = storage.get_profile()
    max_attempts = int(settings.get("max_attempts", 3))
    attempt_timeout = float(settings.get("attempt_timeout_sec", 2.2))
    threshold = float(settings.get("threshold", 0.38))

    if not profile or not await asyncio.to_thread(camera.start, int(settings.get("camera_index", 0))):
        await websocket.send_json({"type": "result", "success": False, "require_password": True,
                                   "message": "Can't detect face"})
        await websocket.close()
        return

    def analyze(frame):
        small = scale_to_width(frame, DETECT_WIDTH)
        faces = detector.detect(small, compute_quality=False)
        matched, best = False, -1.0
        if faces:
            factor = frame.shape[1] / float(small.shape[1])
            try:
                feature = recognizer.extract_feature(frame, scale_raw_face(faces[0]["raw"], factor))
                matched, best = recognizer.match_against_profile(feature, profile, threshold)
            except cv2.error:
                pass
        return matched, best, storage.frame_to_base64_jpeg(small, quality=60)

    try:
        seq = 0
        for attempt in range(1, max_attempts + 1):
            await websocket.send_json({"type": "attempt_start", "attempt": attempt, "max_attempts": max_attempts})
            deadline = time.monotonic() + attempt_timeout
            while time.monotonic() < deadline:
                seq, frame = await asyncio.to_thread(camera.wait_frame, seq, 0.3)
                if frame is None:
                    continue
                matched, best, b64 = await asyncio.to_thread(analyze, frame)
                await websocket.send_json({"type": "frame", "frame": b64, "attempt": attempt,
                                           "best_score": round(best, 4)})
                if matched:
                    name = storage.get_display_name(profile)
                    await websocket.send_json({"type": "result", "success": True, "display_name": name,
                                               "message": f"Welcome back {name}", "require_password": False})
                    return
            await websocket.send_json({"type": "attempt_failed", "attempt": attempt, "max_attempts": max_attempts})
        await websocket.send_json({"type": "result", "success": False, "require_password": True,
                                   "message": "Can't detect face"})
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        camera.stop()
