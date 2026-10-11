#!/usr/bin/env bash
# Stop what scripts/start_gx10.sh started (by pid file, never by pattern).
cd "$(dirname "$0")/.."
for pid in .voice_bridge.pid .dashboard.pid .pose_service.pid; do
  if [ -f "$pid" ] && kill -0 "$(cat "$pid")" 2>/dev/null; then kill "$(cat "$pid")" && echo "stopped $pid"; fi
  rm -f "$pid"
done
(cd streaming && ./stop_pi_sender.sh) > /dev/null 2>&1 || true
