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
for e in udpsrc rtpjitterbuffer rtph264depay h264parse avdec_h264 videoconvert videoscale \
         rtpopusdepay opusdec audioconvert audioresample fdsink; do
  gst-inspect-1.0 "$e" >/dev/null 2>&1 && echo "  OK       $e" || echo "  MISSING  $e"
done

echo "== Checking numpy (receiver.py needs it; any OpenCV works, with or without GStreamer)"
if python3 -c "import numpy" 2>/dev/null; then
  echo "  OK       numpy $(python3 -c 'import numpy; print(numpy.__version__)') in $(python3 -c 'import sys; print(sys.prefix)')"
else
  echo "  PROBLEM  $(command -v python3) can't import numpy. In a venv: pip install numpy"
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

echo "== Checking the Pi has the audio elements"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$PI_HOST" \
  "gst-inspect-1.0 alsasrc >/dev/null 2>&1 && gst-inspect-1.0 opusenc >/dev/null 2>&1 && gst-inspect-1.0 rtpopuspay >/dev/null 2>&1" \
  && echo "  OK       Pi has alsasrc, opusenc, rtpopuspay" \
  || echo "  PROBLEM  On the Pi run: sudo apt install -y gstreamer1.0-alsa gstreamer1.0-plugins-base gstreamer1.0-plugins-good"

echo "Next: ./setup_pi_audio.sh (once) so the mic can be shared."
echo "Done."
