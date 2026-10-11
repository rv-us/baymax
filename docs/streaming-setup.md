# GX10 ↔ TonyPi Low-Latency Video Streaming Setup

How to stream the TonyPi's camera (Raspberry Pi 5) to the GX10 over Wi-Fi with low latency, including the network fixes needed on a crowded shared network.

> **Day to day, you don't run the pipelines below by hand.** The robot API starts the same H.264 sender when the GX10 asks for it (`POST /sensors/camera/udp`, used by `streaming/robot_camera.py`), and `streaming/start_pi_sender.sh` starts the microphone. See [running-the-stream.md](running-the-stream.md). This page is for first-time setup, the network fixes (sections 3 and 4), and debugging the pipeline directly (section 5).

**Result on the AIHackathon@UCSC network:** a steady ~22 fps (the camera's actual limit) after pinning the GX10 to the same access point as the Pi.

---

## Architecture

```
TonyPi (Raspberry Pi 5)                                 GX10
USB camera (YUYV 640x480)                               udpsrc :5000
  -> x264enc (software, zerolatency)                      -> rtpjitterbuffer
  -> RTP/H.264  ------- UDP over Wi-Fi ------->           -> avdec_h264 -> display / app
```

- The Pi 5 has **no hardware video encoder**, so encoding is done in software with x264. This is cheap at 480p.
- The TonyPi camera only outputs raw **YUYV 640x480**, and sending it raw would need ~150 Mbps, so it must be encoded.

---

## 1. Install GStreamer

**On the Pi:**
```bash
sudo apt update
sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly iw
```

**On the GX10:**
```bash
sudo apt update
sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav iw
```

**Verify the GX10 has the receiver elements:**
```bash
for e in udpsrc rtpjitterbuffer rtph264depay h264parse avdec_h264; do
  gst-inspect-1.0 $e >/dev/null 2>&1 && echo "OK  $e" || echo "MISSING  $e"
done
```

**Check the camera formats on the Pi (optional):**
```bash
v4l2-ctl -d /dev/video0 --list-formats-ext
```

---

## 2. Identify each machine and its IP

Make sure you know which terminal is on which machine. It's easy to mix them up, and this caused a false start the first time.

**Confirm you're on the Pi:**
```bash
cat /proc/device-tree/model     # should print: Raspberry Pi 5 Model B ...
```

**Get each machine's IP** (run on each):
```bash
hostname -I
```
- On the GX10, ignore `172.17.0.1`. That's Docker's internal bridge.
- DHCP addresses can change between sessions, so **re-check the IPs every time.**

| Machine | Wi-Fi interface | Example IP (this session) |
|---|---|---|
| Pi | `wlan0` | 169.233.72.93 |
| GX10 | `wlP9s9` | 169.233.72.183 |

> On the Pi, `iw` lives in `/usr/sbin` and isn't on the normal user's PATH in zsh. Always run it as `sudo iw ...`.

---

## 3. Diagnose the Wi-Fi link

If video arrives at a low frame rate (e.g. 2–4 fps) even though the camera delivers ~22 fps, the network is dropping or delaying packets. Find out **which device's hop** is bad.

**a. Find the gateway** (on each machine):
```bash
ip route | grep default
```

**b. Ping the gateway from each machine:**
```bash
ping -c 50 -i 0.2 <gateway-ip> | tail -2
```
- Good: avg < ~10 ms, mdev < ~5 ms.
- The device with large spikes (100+ ms) is the problem.
- In the first session, **the Pi was fine (≤5 ms) and the GX10 spiked to ~200 ms.**

**c. Check each machine's link details:**
```bash
sudo iw dev wlan0 link       # on the Pi
sudo iw dev wlP9s9 link      # on the GX10
```
Check these fields:
- `Connected to <BSSID>`: **which access point** the device is on.
- `freq`: 5xxx = 5 GHz (good), 24xx = 2.4 GHz (crowded).
- `signal`: better than about −65 dBm is good.

**The key finding:** the Pi and GX10 were connected to **different access points** with the same SSID. The GX10's access point was congested.

---

## 4. Fix: same access point and power saving off

### 4a. Turn off Wi-Fi power saving (both machines)

**Immediately (resets on reboot):**
```bash
sudo iw dev wlan0 set power_save off      # Pi
sudo iw dev wlP9s9 set power_save off     # GX10
```

**Permanently (NetworkManager).** First find the connection name:
```bash
nmcli -t -f NAME,DEVICE con show --active
```
Then set powersave to `2` (disabled) and reconnect:
```bash
sudo nmcli con modify "<connection-name>" 802-11-wireless.powersave 2
sudo nmcli con up "<connection-name>"
```

Power saving alone did **not** fix the jitter, but it's still worth disabling.

### 4b. Pin the GX10 to the Pi's access point (the actual fix)

1. **Get the Pi's access point address (BSSID).** On the Pi:
   ```bash
   sudo iw dev wlan0 link | head -1
   # Connected to 20:cc:27:8b:c9:59 (on wlan0)
   ```

2. **Pin the GX10 to that BSSID and reconnect.** On the GX10:
   ```bash
   sudo nmcli con modify "<connection-name>" 802-11-wireless.bssid <PI_BSSID>
   sudo nmcli con up "<connection-name>"
   ```
   This session's actual command:
   ```bash
   sudo nmcli con modify "AIHackathon@UCSC" 802-11-wireless.bssid 20:cc:27:8b:c9:59
   sudo nmcli con up "AIHackathon@UCSC"
   ```
   If you're SSH'd into the GX10 over Wi-Fi, the reconnect will briefly drop your session.

3. **Verify both are now on the same access point:**
   ```bash
   sudo iw dev wlP9s9 link | head -1     # should match the Pi's BSSID
   ping -c 100 -i 0.2 <gateway-ip> | tail -2
   ```

**If the connection fails after pinning,** that access point is out of the GX10's range. Undo the pin:
```bash
sudo nmcli con modify "<connection-name>" 802-11-wireless.bssid ""
sudo nmcli con up "<connection-name>"
```

> ⚠️ **Remove the BSSID pin when you leave the venue** (same undo command). While pinned, the GX10 will only connect to that one physical access point.

> If the robot moves around and the Pi roams to a different access point, the two will split up again. You can also pin the Pi to the same BSSID, using its own connection name, to keep them together.

---

## 5. Run the stream

By hand, for debugging the pipeline itself. Normally the robot API runs the sender (see the note at the top). Stop the API's stream first (`POST /sensors/camera/udp/stop`), since only one program can open the camera.

Start the **receiver first**, then the sender.

### GX10: receiver

With a display:
```bash
gst-launch-1.0 udpsrc port=5000 buffer-size=4194304 \
  caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" \
  ! rtpjitterbuffer latency=50 drop-on-latency=true \
  ! rtph264depay ! h264parse ! avdec_h264 max-threads=1 \
  ! videoconvert ! autovideosink sync=false
```

Headless (prints the fps as text):
```bash
gst-launch-1.0 -v udpsrc port=5000 buffer-size=4194304 \
  caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" \
  ! rtpjitterbuffer latency=50 drop-on-latency=true \
  ! rtph264depay ! h264parse ! avdec_h264 max-threads=1 \
  ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false
```

### Pi: sender

Replace `<GX10_IP>` with the GX10's current address (from step 2):
```bash
gst-launch-1.0 v4l2src device=/dev/video0 \
  ! video/x-raw,format=YUY2,width=640,height=480,framerate=30/1 \
  ! videoconvert ! video/x-raw,format=I420 \
  ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=1500 key-int-max=15 threads=2 \
  ! video/x-h264,profile=baseline ! h264parse ! rtph264pay config-interval=1 pt=96 \
  ! udpsink host=<GX10_IP> port=5000 sync=false
```

**Key settings:**
| Setting | Purpose |
|---|---|
| `tune=zerolatency`, `speed-preset=ultrafast` | No frame buffering in the encoder; minimal CPU use |
| `key-int-max=15` | A keyframe every 0.5 s, so a lost packet only corrupts the image briefly |
| `config-interval=1` | Re-sends stream headers so the receiver can join at any time |
| `rtpjitterbuffer latency=50` | Smooths Wi-Fi jitter. Lower it (toward 0) for less delay, raise it if video stutters |
| `buffer-size=4194304` | Larger UDP receive buffer so bursts of packets aren't dropped |
| `max-threads=1` | Avoids the extra frame of delay from multithreaded decoding |

---

## 6. Verify performance

**Camera-only frame rate (on the Pi).** This is the ceiling for everything downstream:
```bash
gst-launch-1.0 -v v4l2src device=/dev/video0 \
  ! video/x-raw,format=YUY2,width=640,height=480,framerate=30/1 \
  ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false
```
This was ~22 fps. To get closer to 30, add more light, or disable dynamic framerate if the camera supports it (`v4l2-ctl -d /dev/video0 --list-ctrls`, then e.g. `-c exposure_dynamic_framerate=0`).

**Simulated stream with loss and jitter (iperf3):**
```bash
sudo apt install -y iperf3                         # both machines
iperf3 -s                                          # GX10
iperf3 -c <GX10_IP> -u -b 2M -t 30                 # Pi
```
Target: under 1% lost datagrams, jitter under ~5 ms.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `zsh: no such file or directory: gx10-ip` | Placeholder pasted literally | Replace `<GX10_IP>` with the real address |
| `command not found: gst-launch-1.0` | GStreamer not installed | Step 1 |
| `no element "x264enc"` on the GX10 | `plugins-ugly` not installed there | Expected; only the Pi needs x264enc |
| `command not found: iw` on the Pi | `/usr/sbin` not on PATH | Use `sudo iw ...` |
| Pi pings its "GX10 IP" in ~0.03 ms | That IP is the Pi's own address | Re-check IPs with `hostname -I` on each machine |
| Receiver stuck after "New clock", no fps lines | No packets arriving | `sudo tcpdump -i any udp port 5000` on the GX10; check IP, firewall (`sudo ufw allow 5000/udp`), and that both are on the same network |
| Sender says device busy | Hiwonder software holding the camera | Stop the stock camera service first |
| Low or bursty fps (2–6) | Wi-Fi jitter/loss | Step 3 diagnosis, then step 4 fixes |
| Still jittery after the fixes | Congested network / GX10 Wi-Fi | Use the Ethernet fallback below |

---

## Fallback: direct Ethernet (most reliable)

If the shared Wi-Fi can't be made stable, connect the Pi and GX10 with an Ethernet cable. Both machines keep Wi-Fi for internet, and only robot traffic goes over the cable.

**On the Pi:**
```bash
sudo nmcli con add type ethernet ifname eth0 con-name robotlink \
  ipv4.method manual ipv4.addresses 10.0.0.2/24
sudo nmcli con up robotlink
```

**On the GX10** (find the Ethernet interface with `ip link`; it starts with `en`):
```bash
sudo nmcli con add type ethernet ifname <en-interface> con-name robotlink \
  ipv4.method manual ipv4.addresses 10.0.0.1/24
sudo nmcli con up robotlink
```

Test with `ping -c 10 10.0.0.1` from the Pi (should be under 1 ms), then use `host=10.0.0.1` in the sender.

For an untethered robot, the best long-term setup is your own 5 GHz travel router, with the GX10 plugged into it by Ethernet and only the Pi on Wi-Fi.
