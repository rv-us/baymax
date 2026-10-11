#!/usr/bin/env bash
# Start everything the GX10 runs for a live session: the arm_mvp pose service and the dashboard.
#
#     ./scripts/start_gx10.sh            # then open http://<gx10-ip>:8080 on any computer on the network
#     ./scripts/stop_gx10.sh             # stops both
#
# The dashboard runs here (not on a laptop) so the robot's camera video reaches it directly: this
# is the one program that opens the camera. It tracks on the pose service, shows the annotated picture
# to browsers, and drives the robot through its API. The robot URL and token come from the environment
# (BAYMAX_API_URL, BAYMAX_API_TOKEN), else from streaming/config.env. Logs: ~/pose_service.log, ~/dashboard.log
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${DASHBOARD_PORT:-8080}"

# Settings already in the environment win over streaming/config.env.
url="${BAYMAX_API_URL:-}"; token="${BAYMAX_API_TOKEN:-}"
set -a; . streaming/config.env; set +a
export BAYMAX_API_URL="${url:-$BAYMAX_API_URL}" BAYMAX_API_TOKEN="${token:-${BAYMAX_API_TOKEN:-}}"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi
.venv/bin/pip install -q fastapi uvicorn httpx pyyaml numpy opencv-python-headless mediapipe

if ss -ulpn 2>/dev/null | grep -q ':5000 '; then
  echo "warning: something already receives the robot's UDP video on port 5000; the dashboard will fall back to MJPEG." >&2
  echo "         (ss -ulpn | grep 5000, then stop that program)" >&2
fi

./dashboard/start_pose_service.sh

if [ -f .dashboard.pid ] && kill -0 "$(cat .dashboard.pid)" 2>/dev/null; then
  kill "$(cat .dashboard.pid)"; sleep 2
fi
setsid nohup .venv/bin/python -m dashboard --robot-url "$BAYMAX_API_URL" --pose-url "http://127.0.0.1:9100" \
  --ollama-url "http://127.0.0.1:11434" --gx10 off --host 0.0.0.0 --port "$PORT" \
  > "$HOME/dashboard.log" 2>&1 < /dev/null &
echo $! > .dashboard.pid
for _ in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    echo "dashboard up: http://$(hostname -I | awk '{print $1}'):$PORT   (robot: $BAYMAX_API_URL)"
    exit 0
  fi
  sleep 1
done
echo "the dashboard did not start; see ~/dashboard.log" >&2; tail -20 "$HOME/dashboard.log" >&2; exit 1
