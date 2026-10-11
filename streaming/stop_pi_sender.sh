#!/usr/bin/env bash
# Run on the GX10. Stops the camera and microphone senders on the Pi.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

ssh "$PI_HOST" "pkill -f '[g]st-launch-1.0.* v4l2src' && echo 'Video sender stopped.' || echo 'No video sender was running.'"
ssh "$PI_HOST" "pkill -f '[g]st-launch-1.0.* alsasrc' && echo 'Audio sender stopped.' || echo 'No audio sender was running.'"
