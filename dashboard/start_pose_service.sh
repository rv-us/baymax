#!/usr/bin/env bash
# Start (or restart) the GX10 pose service in the background: ./dashboard/start_pose_service.sh [port] [--camera]
# --camera makes the service own the robot's camera (BAYMAX_API_URL / BAYMAX_API_TOKEN must be set), so the
# dashboard gets the picture and the pose from here. Uses the GX10's venv (~/venvs/baymax, built by
# scripts/setup_gx10_venv.sh if it isn't there). Log: ~/pose_service.log
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=9100; CAMERA=()
for arg in "$@"; do
  case "$arg" in --camera) CAMERA=(--camera) ;; *) PORT="$arg" ;; esac
done
VENV="${BAYMAX_VENV:-$HOME/venvs/baymax}"
[ -x "$VENV/bin/python" ] || ./scripts/setup_gx10_venv.sh
# Stop a previous copy by its pid file (never by pattern: that can match this very shell).
if [ -f .pose_service.pid ] && kill -0 "$(cat .pose_service.pid)" 2>/dev/null; then
  kill "$(cat .pose_service.pid)"; sleep 1
fi
setsid nohup "$VENV/bin/python" -m dashboard.pose_service --port "$PORT" "${CAMERA[@]}" > "$HOME/pose_service.log" 2>&1 < /dev/null &
echo $! > .pose_service.pid
for _ in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then echo "pose service up on :$PORT${CAMERA:+ (owns the robot camera)}"; exit 0; fi
  sleep 1
done
echo "pose service did not start; see ~/pose_service.log" >&2; tail -20 "$HOME/pose_service.log" >&2; exit 1
