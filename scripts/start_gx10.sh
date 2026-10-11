#!/usr/bin/env bash
# Start everything the GX10 runs for a live session:
#   - the pose service, which OWNS the robot's camera and tracks it with arm_mvp (port 9100)
#   - the voice service, which OWNS the robot's microphone: Whisper, on the GPU (port 9200)
#   - the dashboard, which gets the picture, the pose and what the patient says from those two (port 8080)
#
#     ./scripts/start_gx10.sh            # then open http://<gx10-ip>:8080 on any computer on the network
#     VOICE=0 ./scripts/start_gx10.sh    # without the microphone
#     ./scripts/stop_gx10.sh             # stops them all
#
# The robot URL and token come from the environment (BAYMAX_API_URL, BAYMAX_API_TOKEN), else from
# streaming/config.env. The Python environment is ~/venvs/baymax (scripts/setup_gx10_venv.sh builds it).
# Logs: ~/pose_service.log, ~/voice_service.log, ~/dashboard.log
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${DASHBOARD_PORT:-8080}"
VENV="${BAYMAX_VENV:-$HOME/venvs/baymax}"

# Settings already in the environment win over streaming/config.env.
url="${BAYMAX_API_URL:-}"; token="${BAYMAX_API_TOKEN:-}"
set -a; . streaming/config.env; set +a
export BAYMAX_API_URL="${url:-$BAYMAX_API_URL}" BAYMAX_API_TOKEN="${token:-${BAYMAX_API_TOKEN:-}}"

[ -x "$VENV/bin/python" ] || ./scripts/setup_gx10_venv.sh

if ss -ulpn 2>/dev/null | grep -q ':5000 '; then
  echo "warning: something already receives the robot's UDP video on port 5000; the GX10 will fall back to MJPEG." >&2
  echo "         (ss -ulpn | grep 5000, then stop that program)" >&2
fi

./dashboard/start_pose_service.sh 9100 --camera

if [ "${VOICE:-1}" = 1 ]; then
  # The robot's microphone stream goes to this machine (needs ssh to the Pi: `tonypi`), then Whisper listens to it.
  (cd streaming && ./start_pi_sender.sh) > "$HOME/pi_sender.log" 2>&1 \
    || echo "warning: couldn't start the Pi's microphone sender (see ~/pi_sender.log); there will be no voice." >&2
  for old in .voice_bridge.pid .voice_service.pid; do   # (the first is the earlier name of the same thing)
    if [ -f "$old" ] && kill -0 "$(cat "$old")" 2>/dev/null; then kill "$(cat "$old")"; sleep 1; fi
  done
  setsid nohup "$VENV/bin/python" -m voice.service --port 9200 > "$HOME/voice_service.log" 2>&1 < /dev/null &
  echo $! > .voice_service.pid
  echo "voice service up, idle: Whisper loads when a question is asked and unloads after 15 s: ~/voice_service.log"
fi

if [ -f .dashboard.pid ] && kill -0 "$(cat .dashboard.pid)" 2>/dev/null; then
  kill "$(cat .dashboard.pid)"; sleep 2
fi
setsid nohup "$VENV/bin/python" -m dashboard --robot-url "$BAYMAX_API_URL" --pose-url "http://127.0.0.1:9100" \
  --voice-url "$([ "${VOICE:-1}" = 1 ] && echo http://127.0.0.1:9200 || echo off)" \
  --ollama-url "http://127.0.0.1:11434" --gx10 off --host 0.0.0.0 --port "$PORT" \
  > "$HOME/dashboard.log" 2>&1 < /dev/null &
echo $! > .dashboard.pid
up=0
for _ in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done
if [ "$up" = 0 ]; then
  echo "the dashboard did not start; see ~/dashboard.log" >&2; tail -20 "$HOME/dashboard.log" >&2; exit 1
fi
echo "dashboard up: http://$(hostname -I | awk '{print $1}'):$PORT   (robot: $BAYMAX_API_URL)"
