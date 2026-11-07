"""
Anti-spoof guard.

The local matcher says *whose* face it is. It cannot say whether that face is
sitting in front of the camera or is a photo on someone's phone. After a match
the daemon unlocks at once, then asks a Gemini vision model about the frames
that unlocked it. This module is that second opinion: a small stdlib-only
client with key rotation, a model fallback chain, hedged requests and a kept
alive TLS connection (so the check starts without a handshake).
"""

import base64
import concurrent.futures
import http.client
import json
import os
import random
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

import cv2

from core.storage import CONFIG_DIR

HOST = "generativelanguage.googleapis.com"
KEYS_PATH = CONFIG_DIR / "guard.env"

# Benchmarked on spoof / live frames: gemini-3.5-flash was right every time with the highest
# confidence; gemini-3-flash-preview is ~1 s quicker and also right. The flash-lite models
# false-alarmed or timed out, so they are only a last resort. A model that errors or is slow
# is skipped for a few minutes.
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite"]

FRAME_WIDTH = 768
FRAME_QUALITY = 82
HEDGE_SEC = 2.5          # start the next model if the first hasn't answered by then
REQUEST_TIMEOUT = 8.0
COOLDOWN_SEC = 300.0
KEY_REST_SEC = 600.0

PROMPT = (
    "You are the anti-spoofing check of a laptop face unlock. The images are consecutive webcam "
    "frames, about 0.3 s apart, taken the moment a face unlocked the laptop. Decide whether the "
    "face belongs to a LIVE person physically sitting in front of the camera, or whether it is a "
    "presentation attack: a face shown on a phone, tablet or monitor (photo or video), a printed "
    "photo, a face cut-out, a mask, or a doll.\n"
    "Cues for an attack: the face sits inside a screen or paper rectangle, visible bezel, edges or "
    "a hand holding a device or sheet, glare or moire, flat lighting that differs from the room, "
    "the face stays perfectly rigid or identical across frames while a hand moves, or the face is "
    "far sharper or a different colour than its surroundings.\n"
    "Cues for a live person: the head and shoulders are part of the scene with consistent "
    "lighting and perspective, natural small movement between frames. A person who is holding a "
    "phone while their own face is visible is LIVE. If the frames are too dark, blurry or empty "
    "to judge, answer live=true, attack=\"unusable\", confidence 0.1; never call an unusable "
    "frame an attack.\n"
    "Answer with JSON only."
)

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "live": {"type": "BOOLEAN"},
        "attack": {"type": "STRING", "enum": ["none", "screen", "print", "mask", "other", "unusable"]},
        "confidence": {"type": "NUMBER"},
        "reason": {"type": "STRING"},
    },
    "required": ["live", "attack", "confidence", "reason"],
}


class GuardError(Exception):
    pass


@dataclass
class Verdict:
    live: bool
    attack: str
    confidence: float
    reason: str
    model: str = ""
    ms: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def load_keys() -> List[str]:
    """Keys come from the environment first, then ~/.config/facecam-lock/guard.env."""
    keys: List[str] = []

    def add(raw: str) -> None:
        for part in raw.replace(";", ",").split(","):
            part = part.strip().strip("'\"")
            if part and part not in keys:
                keys.append(part)

    for name in ("GEMINI_API_KEYS", "GEMINI_API_KEY"):
        add(os.environ.get(name, ""))
    try:
        for line in KEYS_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() in ("GEMINI_API_KEYS", "GEMINI_API_KEY"):
                add(value)
    except OSError:
        pass
    return keys


def save_keys(keys: List[str], models: Optional[List[str]] = None) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["GEMINI_API_KEYS=" + ",".join(keys)]
    if models:
        lines.append("GUARD_MODELS=" + ",".join(models))
    tmp = f"{KEYS_PATH}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode())
    finally:
        os.close(fd)
    os.replace(tmp, KEYS_PATH)


def configured_models() -> List[str]:
    try:
        for line in KEYS_PATH.read_text().splitlines():
            if line.startswith("GUARD_MODELS="):
                models = [m.strip() for m in line.split("=", 1)[1].split(",") if m.strip()]
                if models:
                    return models
    except OSError:
        pass
    return list(DEFAULT_MODELS)


def encode_frames(frames, width: int = FRAME_WIDTH, quality: int = FRAME_QUALITY) -> List[bytes]:
    out = []
    for frame in frames:
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if ok:
            out.append(jpg.tobytes())
    return out


class _Retry(Exception):
    """This key can't serve the request right now; try the next one."""


class Guard:
    def __init__(self, models: Optional[List[str]] = None):
        self.keys = load_keys()
        self.models = models or configured_models()
        self._pool: List[http.client.HTTPSConnection] = []
        self._pool_lock = threading.Lock()
        self._cooldown: Dict[str, float] = {}
        self._rested: Dict[str, float] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="guard")

    @property
    def ready(self) -> bool:
        return bool(self.keys)

    def reload(self) -> None:
        self.keys = load_keys()
        self.models = configured_models()

    # ------------------------------------------------------------- transport
    def _take(self) -> http.client.HTTPSConnection:
        with self._pool_lock:
            if self._pool:
                return self._pool.pop()
        return http.client.HTTPSConnection(HOST, timeout=REQUEST_TIMEOUT)

    def _give(self, conn: http.client.HTTPSConnection) -> None:
        with self._pool_lock:
            if len(self._pool) < 3:
                self._pool.append(conn)
                return
        conn.close()

    def prewarm(self) -> None:
        """Open the TLS connection ahead of time; a cheap GET that needs no quota."""
        if not self.keys:
            return
        try:
            conn = self._take()
            conn.request("GET", "/v1beta/models?pageSize=1", headers={"x-goog-api-key": self.keys[0]})
            conn.getresponse().read()
            self._give(conn)
        except (OSError, http.client.HTTPException):
            pass

    def _post(self, model: str, key: str, body: bytes) -> dict:
        path = f"/v1beta/models/{model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        for attempt in (0, 1):
            conn = self._take()
            try:
                conn.request("POST", path, body=body, headers=headers)
                res = conn.getresponse()
                raw = res.read()
            except (OSError, http.client.HTTPException):
                conn.close()
                if attempt == 0:  # a kept-alive connection the server already closed
                    continue
                raise
            self._give(conn)
            break
        if res.status in (429, 403, 401):
            raise _Retry(f"HTTP {res.status}")
        if res.status >= 400:
            try:
                msg = json.loads(raw)["error"]["message"].split("\n")[0][:120]
            except (ValueError, KeyError, TypeError):
                msg = ""
            raise GuardError(f"{model}: HTTP {res.status} {msg}".strip())
        return json.loads(raw)

    # --------------------------------------------------------------- request
    @staticmethod
    def _body(model: str, jpegs: List[bytes]) -> bytes:
        parts = [{"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(j).decode()}} for j in jpegs]
        parts.append({"text": PROMPT})
        config = {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0.0}
        if "lite" not in model and "gemini-3" in model:
            config["thinkingConfig"] = {"thinkingLevel": "low"}  # "minimal" isn't accepted by every 3.x model
        elif model.startswith("gemini-2.5"):
            config["thinkingConfig"] = {"thinkingBudget": 0}
        return json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": config}).encode()

    def _ask(self, model: str, jpegs: List[bytes]) -> Verdict:
        body = self._body(model, jpegs)
        now = time.time()
        order = [k for k in self.keys if self._rested.get(f"{k}:{model}", 0) <= now] or list(self.keys)
        # Spread load over keys so one key's per-minute quota isn't the bottleneck.
        start = random.randrange(len(order))
        order = order[start:] + order[:start]
        last = ""
        t0 = time.perf_counter()
        for key in order:
            try:
                data = self._post(model, key, body)
            except _Retry as e:
                self._rested[f"{key}:{model}"] = time.time() + KEY_REST_SEC
                last = str(e)
                continue
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            try:
                obj = json.loads(text)
                return Verdict(live=bool(obj["live"]), attack=str(obj.get("attack", "none")),
                               confidence=max(0.0, min(1.0, float(obj["confidence"]))),
                               reason=str(obj.get("reason", ""))[:140], model=model,
                               ms=int((time.perf_counter() - t0) * 1000))
            except (ValueError, KeyError, TypeError):
                raise GuardError(f"{model}: unreadable answer")
        raise GuardError(f"{model}: every key refused ({last})")

    def verify(self, jpegs: List[bytes], deadline_sec: float = 12.0) -> Verdict:
        """First good answer wins; slower models are only started as a hedge."""
        if not self.keys:
            raise GuardError("no Gemini key configured")
        if not jpegs:
            raise GuardError("no frames")
        now = time.time()
        ready = [m for m in self.models if self._cooldown.get(m, 0) <= now] or list(self.models)
        pending = {}
        errors: List[str] = []
        t_end = time.monotonic() + deadline_sec
        queue = list(ready)

        def launch():
            model = queue.pop(0)
            pending[self._executor.submit(self._ask, model, jpegs)] = model

        launch()
        while pending:
            remaining = t_end - time.monotonic()
            if remaining <= 0:
                break
            wait = min(remaining, HEDGE_SEC) if queue else remaining
            done, _ = concurrent.futures.wait(pending, timeout=wait,
                                              return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                if queue:
                    launch()  # hedge: the first model is slow
                continue
            for fut in done:
                model = pending.pop(fut)
                try:
                    verdict = fut.result()
                except (GuardError, OSError, http.client.HTTPException) as e:
                    errors.append(str(e))
                    self._cooldown[model] = time.time() + COOLDOWN_SEC
                    if queue and not pending:
                        launch()
                    continue
                self._cooldown.pop(model, None)
                for other in pending:
                    other.cancel()
                return verdict
        raise GuardError("; ".join(errors) or "timed out")


def is_fishy(verdict: Verdict, min_confidence: float) -> bool:
    return (not verdict.live) and verdict.attack != "unusable" and verdict.confidence >= min_confidence


def usable(frames, min_brightness: float = 28.0) -> bool:
    """A webcam that just started can return near-black frames; there is nothing to judge in those."""
    return any(float(f.mean()) >= min_brightness for f in frames)
