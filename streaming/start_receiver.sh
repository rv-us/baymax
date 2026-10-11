#!/usr/bin/env bash
# Run on the GX10. Receives the stream and prints the frame rate (headless, works over SSH).
# For using frames in code, use receiver.py instead.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

exec gst-launch-1.0 -v udpsrc port="$VIDEO_PORT" buffer-size=4194304 \
  caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" \
  ! rtpjitterbuffer latency="$JITTER_LATENCY_MS" drop-on-latency=true \
  ! rtph264depay ! h264parse ! avdec_h264 max-threads=1 \
  ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false
