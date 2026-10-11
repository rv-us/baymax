#!/usr/bin/env bash
# Run once on the GX10. Installs dependencies and checks everything is ready.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

echo "== Installing packages"
sudo apt update
sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav python3-opencv iw tmux

chmod +x ./*.sh

echo "== Checking GStreamer elements"
for e in udpsrc rtpjitterbuffer rtph264depay h264parse avdec_h264 appsink; do
  gst-inspect-1.0 "$e" >/dev/null 2>&1 && echo "  OK       $e" || echo "  MISSING  $e"
done

echo "== Checking OpenCV GStreamer support"
if python3 -c "import cv2, sys; sys.exit(0 if any('GStreamer' in l and 'YES' in l for l in cv2.getBuildInformation().splitlines()) else 1)"; then
  echo "  OK       OpenCV has GStreamer"
else
  echo "  PROBLEM  OpenCV lacks GStreamer. A pip-installed opencv-python is probably shadowing the apt one:"
  echo "           pip uninstall -y opencv-python opencv-python-headless"
fi

echo "== Checking SSH to the Pi ($PI_HOST)"
if ssh -o BatchMode=yes -o ConnectTimeout=5 "$PI_HOST" "cat /proc/device-tree/model" 2>/dev/null; then
  echo
  echo "  OK       Can reach the Pi"
else
  echo "  PROBLEM  Can't SSH to '$PI_HOST' without a password."
  echo "           Set up a key: ssh-keygen -t ed25519 && ssh-copy-id <pi-user>@<pi-ip>"
  echo "           and add a 'Host $PI_HOST' entry to ~/.ssh/config"
fi

echo "== Checking the Pi has GStreamer + x264"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$PI_HOST" "gst-inspect-1.0 x264enc >/dev/null 2>&1" \
  && echo "  OK       Pi has x264enc" \
  || echo "  PROBLEM  On the Pi run: sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly"

echo "Done."
