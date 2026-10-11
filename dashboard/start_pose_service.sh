#!/usr/bin/env bash
# Start (or restart) the GX10 pose service in the background: ./dashboard/start_pose_service.sh [port]
# Makes .venv with the service's dependencies the first time. Log: ~/pose_service.log
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-9100}"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q numpy opencv-python-headless mediapipe fastapi uvicorn pyyaml
fi
# Stop a previous copy by its pid file (never by pattern: that can match this very shell).
if [ -f .pose_service.pid ] && kill -0 "$(cat .pose_service.pid)" 2>/dev/null; then
  kill "$(cat .pose_service.pid)"; sleep 1
fi
setsid nohup .venv/bin/python -m dashboard.pose_service --port "$PORT" > "$HOME/pose_service.log" 2>&1 < /dev/null &
echo $! > .pose_service.pid
for _ in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then echo "pose service up on :$PORT"; exit 0; fi
  sleep 1
done
echo "pose service did not start; see ~/pose_service.log" >&2; tail -20 "$HOME/pose_service.log" >&2; exit 1
