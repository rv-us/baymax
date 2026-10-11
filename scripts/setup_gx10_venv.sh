#!/usr/bin/env bash
# Build (or update) the GX10's Python environment for this repo with uv.
#
#   scripts/setup_gx10_venv.sh            # ~/venvs/baymax from requirements.txt
#   scripts/setup_gx10_venv.sh --models   # also download the Whisper model for speech-to-text
#   BAYMAX_VENV=~/venvs/other scripts/setup_gx10_venv.sh
#
# The venv lives OUTSIDE the repo on purpose: ~/baymax gets deleted and re-cloned, and a
# venv inside it goes with it (that lost the GX10's environment once). uv keeps every
# package it has downloaded in ~/.cache/uv, so rebuilding takes seconds, not a re-download
# of torch and CUDA.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${BAYMAX_VENV:-$HOME/venvs/baymax}"

case "$(realpath -m "$VENV")/" in
  "$(realpath "$REPO")"/*)
    echo "Refusing to put the venv inside the repo ($VENV): re-cloning the repo would delete it." >&2
    echo "Use the default (~/venvs/baymax) or set BAYMAX_VENV outside $REPO." >&2
    exit 1 ;;
esac

if ! command -v uv >/dev/null && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  echo "== Installing uv (in its own small venv, ~/.local/share/uv-tool)"
  python3 -m venv "$HOME/.local/share/uv-tool"
  "$HOME/.local/share/uv-tool/bin/pip" install -q uv
  mkdir -p "$HOME/.local/bin"
  ln -sf "$HOME/.local/share/uv-tool/bin/uv" "$HOME/.local/bin/uv"
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

echo "== Building $VENV"
mkdir -p "$(dirname "$VENV")"
"$UV" venv --allow-existing --python /usr/bin/python3 "$VENV"
# unsafe-best-match: take each package's newest version from either index, so the PyTorch
# index (needed for the CUDA build of torch) doesn't pin numpy and friends to old versions.
"$UV" pip install --python "$VENV/bin/python" --index-strategy unsafe-best-match -r "$REPO/requirements.txt"

echo "== Checking"
"$VENV/bin/python" - <<'EOF'
import cv2, mediapipe, numpy, torch, transformers, ultralytics  # noqa: F401
print(f"torch {torch.__version__}, CUDA available: {torch.cuda.is_available()}, numpy {numpy.__version__}")
EOF

if [[ "${1:-}" == "--models" ]]; then
  echo "== Downloading the Whisper model (about 1 GB, into ~/.cache/huggingface)"
  (cd "$REPO" && "$VENV/bin/python" -c "from voice.listener import WhisperTranscriber; WhisperTranscriber()")
fi

echo
echo "Done. Activate with:  source $VENV/bin/activate"
