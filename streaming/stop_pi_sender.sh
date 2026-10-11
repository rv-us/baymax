#!/usr/bin/env bash
# Run on the GX10. Stops the camera sender on the Pi.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

ssh "$PI_HOST" "pkill -f '[g]st-launch-1.0.* v4l2src' && echo 'Sender stopped.' || echo 'No sender was running.'"
