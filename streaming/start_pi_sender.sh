#!/usr/bin/env bash
# Run on the GX10. Starts the camera sender on the Pi over SSH, pointed at this GX10.
# The GX10's IP is detected automatically, so nothing needs editing when it changes.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

GX10_IP=$(ip -4 -o addr show dev "$GX10_IFACE" | awk '{print $4}' | cut -d/ -f1 | head -1)
if [[ -z "$GX10_IP" ]]; then
  echo "Could not find an IPv4 address on $GX10_IFACE. Is the GX10 on Wi-Fi?" >&2
  exit 1
fi
echo "GX10 address on $GX10_IFACE: $GX10_IP"

# Stop any sender that's already running so we don't fight over the camera.
ssh "$PI_HOST" "pkill -f '[g]st-launch-1.0.* v4l2src' || true"
sleep 1

# The camera feed is split (tee): one branch is the H.264 stream to the GX10,
# the other writes JPEG snapshots on the Pi for the robot API's camera endpoint
# (only one program can open the camera, so this sender feeds both).
ssh "$PI_HOST" "nohup gst-launch-1.0 v4l2src device=$VIDEO_DEVICE \
  ! video/x-raw,format=YUY2,width=$VIDEO_WIDTH,height=$VIDEO_HEIGHT,framerate=$VIDEO_FPS/1 \
  ! tee name=t \
  t. ! queue leaky=downstream max-size-buffers=2 ! videorate ! video/x-raw,framerate=$SNAPSHOT_FPS/1 \
  ! videoconvert ! jpegenc quality=85 ! multifilesink location=$SNAPSHOT_PATH \
  t. ! queue ! videoconvert ! video/x-raw,format=I420 \
  ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=$VIDEO_BITRATE_KBPS key-int-max=$KEYFRAME_INTERVAL threads=2 \
  ! video/x-h264,profile=baseline ! h264parse ! rtph264pay config-interval=1 pt=96 \
  ! udpsink host=$GX10_IP port=$VIDEO_PORT sync=false \
  < /dev/null > /tmp/tonypi-sender.log 2>&1 &"

sleep 2
if ssh "$PI_HOST" "pgrep -f '[g]st-launch-1.0.* v4l2src' >/dev/null"; then
  echo "Sender running on the Pi, streaming to $GX10_IP:$VIDEO_PORT"
  echo "Pi log: ssh $PI_HOST 'tail -f /tmp/tonypi-sender.log'"
else
  echo "Sender failed to start. Last lines of the Pi log:" >&2
  ssh "$PI_HOST" "tail -20 /tmp/tonypi-sender.log" >&2
  exit 1
fi
