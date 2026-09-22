#!/bin/bash
# Download the YuNet detector and SFace recognizer from OpenCV Zoo and verify them.
set -euo pipefail

MODELS_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/models}"
BASE_URL="https://github.com/opencv/opencv_zoo/raw/main/models"
mkdir -p "$MODELS_DIR"

fetch() {
  local file="$1" url="$2" sum="$3"
  local target="$MODELS_DIR/$file"
  if [[ -f $target ]] && echo "$sum  $target" | sha256sum -c --status; then
    echo "  ✓ $file"
    return
  fi
  echo "  ↓ $file"
  curl -fsSL --retry 3 -o "$target.part" "$url"
  echo "$sum  $target.part" | sha256sum -c --status || { rm -f "$target.part"; echo "checksum mismatch for $file" >&2; exit 1; }
  mv "$target.part" "$target"
}

fetch face_detection_yunet_2023mar.onnx \
  "$BASE_URL/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4

fetch face_recognition_sface_2021dec.onnx \
  "$BASE_URL/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
  0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
