#!/usr/bin/env bash
# Run on the GX10. Stops the microphone sender on the Pi (and the camera sender, with VIDEO_ENABLED=1).
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

# The camera belongs to the robot API unless VIDEO_ENABLED=1 (its stream stops by itself when
# nobody renews it, or: POST /sensors/camera/udp/stop), so only stop video this script started.
if [[ "$VIDEO_ENABLED" == 1 ]]; then
  ssh "$PI_HOST" "pkill -f '[g]st-launch-1.0.* v4l2src' && echo 'Video sender stopped.' || echo 'No video sender was running.'"
fi
ssh "$PI_HOST" "pkill -f '[g]st-launch-1.0.* alsasrc' && echo 'Audio sender stopped.' || echo 'No audio sender was running.'"
