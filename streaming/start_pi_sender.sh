#!/usr/bin/env bash
# Run on the GX10. Starts the camera and microphone senders on the Pi over SSH, pointed at this GX10.
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

# VIDEO_ENABLED=0 ./start_pi_sender.sh starts only the microphone, and leaves the camera alone
# (for when the robot API streams it: POST /sensors/camera/udp, see streaming/robot_camera.py).
VIDEO_ENABLED="${VIDEO_ENABLED:-1}"

# Stop any senders that are already running so we don't fight over the camera or mic.
if [[ "$VIDEO_ENABLED" == 1 ]]; then
  ssh "$PI_HOST" "pkill -f '[g]st-launch-1.0.* v4l2src'; true"
fi
ssh "$PI_HOST" "pkill -f '[g]st-launch-1.0.* alsasrc'; true"
sleep 1

if [[ "$VIDEO_ENABLED" == 1 ]]; then
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
fi

# The microphone (WonderEcho Pro) as Opus over RTP. It records from the shared
# dsnoop device made by setup_pi_audio.sh, so the robot API can still use the mic.
# In-band FEC lets the GX10 rebuild a lost packet from the next one.
if [[ "${AUDIO_ENABLED:-1}" == 1 ]]; then
  ssh "$PI_HOST" "nohup gst-launch-1.0 alsasrc device=$PI_MIC_PCM buffer-time=100000 latency-time=10000 \
    ! audioconvert ! audioresample ! audio/x-raw,rate=$AUDIO_RATE,channels=1 \
    ! opusenc audio-type=voice frame-size=$AUDIO_FRAME_MS bitrate=$AUDIO_BITRATE \
      inband-fec=true packet-loss-percentage=10 \
    ! rtpopuspay pt=97 ! udpsink host=$GX10_IP port=$AUDIO_PORT sync=false \
    < /dev/null > /tmp/tonypi-audio.log 2>&1 &"
fi

sleep 2
failed=0
if [[ "$VIDEO_ENABLED" != 1 ]]; then
  echo "Video not started (VIDEO_ENABLED=0)"
elif ssh "$PI_HOST" "pgrep -f '[g]st-launch-1.0.* v4l2src' >/dev/null"; then
  echo "Video sender running on the Pi, streaming to $GX10_IP:$VIDEO_PORT"
  echo "  Pi log: ssh $PI_HOST 'tail -f /tmp/tonypi-sender.log'"
else
  echo "Video sender failed to start. Last lines of the Pi log:" >&2
  ssh "$PI_HOST" "tail -20 /tmp/tonypi-sender.log" >&2
  failed=1
fi
if [[ "${AUDIO_ENABLED:-1}" == 1 ]]; then
  if ssh "$PI_HOST" "pgrep -f '[g]st-launch-1.0.* alsasrc' >/dev/null"; then
    echo "Audio sender running on the Pi, streaming to $GX10_IP:$AUDIO_PORT"
    echo "  Pi log: ssh $PI_HOST 'tail -f /tmp/tonypi-audio.log'"
  else
    echo "Audio sender failed to start. Last lines of the Pi log:" >&2
    ssh "$PI_HOST" "tail -20 /tmp/tonypi-audio.log" >&2
    echo "If it says '$PI_MIC_PCM' is unknown, run ./setup_pi_audio.sh once." >&2
    failed=1
  fi
fi
exit $failed
