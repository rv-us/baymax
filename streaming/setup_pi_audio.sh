#!/usr/bin/env bash
# Run once on the GX10. Creates a shared microphone device ("mic_shared") on the Pi.
#
# ALSA lets only one program open the mic directly, so the audio stream and the
# robot API's mic_level would fight over it. This adds a dsnoop device to the Pi
# user's ~/.asoundrc that any number of programs can record from at once.
# Re-running replaces the block it added; the rest of ~/.asoundrc is left alone.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

HW="hw:CARD=$PI_AUDIO_CARD,DEV=0"

echo "== Checking for a sound server on the Pi"
if ssh "$PI_HOST" "pgrep -x pipewire >/dev/null || pgrep -x pulseaudio >/dev/null"; then
  echo "  WARNING  PipeWire or PulseAudio is running on the Pi. If it holds the mic, the"
  echo "           check at the end fails with 'Device or resource busy'."
fi

echo "== Reading what the mic supports ($HW)"
# --dump-hw-params prints the card's supported formats before recording; the
# 1 s recording itself may fail and that's fine.
PARAMS=$(ssh "$PI_HOST" "arecord -D $HW -f S16_LE --dump-hw-params -d 1 /dev/null 2>&1 || true")
if ! grep -q '^RATE' <<<"$PARAMS"; then
  echo "  PROBLEM  Couldn't read the mic's parameters. arecord said:" >&2
  echo "$PARAMS" | sed 's/^/           /' >&2
  echo "           Check the card name with: ssh $PI_HOST arecord -l" >&2
  exit 1
fi

# dsnoop needs one fixed rate and channel count. Prefer 48 kHz (the usual USB
# rate), then 16 kHz; take the fewest channels. Override with MIC_HW_RATE /
# MIC_HW_CHANNELS if the guess is wrong.
read -r GUESS_RATE GUESS_CHANNELS < <(python3 - "$PARAMS" <<'PY'
import re, sys

def values(name):
    line = next(l for l in sys.argv[1].splitlines() if l.startswith(name))
    nums = [int(n) for n in re.findall(r"\d+", line.split(":", 1)[1])]
    if "[" in line or "(" in line:  # a range like [8000 48000]; "(" / ")" exclude the end
        return ("range", nums[0] + ("(" in line), nums[-1] - (")" in line))
    return ("list", nums)

def supports(v, x):
    return v[1] <= x <= v[2] if v[0] == "range" else x in v[1]

rate = values("RATE")
rate_pick = next((r for r in (48000, 16000) if supports(rate, r)),
                 rate[2] if rate[0] == "range" else max(rate[1]))
ch = values("CHANNELS")
ch_pick = ch[1] if ch[0] == "range" else min(ch[1])
print(rate_pick, ch_pick)
PY
)
RATE=${MIC_HW_RATE:-$GUESS_RATE}
CHANNELS=${MIC_HW_CHANNELS:-$GUESS_CHANNELS}
echo "  Using $RATE Hz, $CHANNELS channel(s)"

echo "== Writing the mic_shared block to ~/.asoundrc on the Pi"
ssh "$PI_HOST" 'f=~/.asoundrc; touch "$f"; [ -e "$f.bak" ] || cp "$f" "$f.bak";
  sed -i "/^# >>> baymax mic_shared >>>/,/^# <<< baymax mic_shared <<</d" "$f"; cat >> "$f"' <<EOF
# >>> baymax mic_shared >>>
# Written by baymax/streaming/setup_pi_audio.sh. Shared mic: record from
# "$PI_MIC_PCM" instead of $HW so several programs can use it at once.
pcm.mic_snoop {
    type dsnoop
    ipc_key 52559
    ipc_perm 0666
    slave {
        pcm "$HW"
        format S16_LE
        rate $RATE
        channels $CHANNELS
    }
}
pcm.$PI_MIC_PCM {
    type plug
    slave.pcm "mic_snoop"
}
# <<< baymax mic_shared <<<
EOF

echo "== Checking two programs can record at once"
if ssh "$PI_HOST" "arecord -q -D $PI_MIC_PCM -f S16_LE -r 16000 -c 1 -d 2 /dev/null & \
    sleep 0.5; arecord -q -D $PI_MIC_PCM -f S16_LE -r 16000 -c 1 -d 1 /dev/null; a=\$?; wait \$!; b=\$?; \
    [ \$a -eq 0 ] && [ \$b -eq 0 ]"; then
  echo "  OK       $PI_MIC_PCM works and can be shared"
else
  echo "  PROBLEM  Recording from $PI_MIC_PCM failed. Is something else holding the mic?" >&2
  echo "           See what is: ssh $PI_HOST fuser -v /dev/snd/*" >&2
  exit 1
fi

echo
echo "Done. Start the robot API with BAYMAX_MIC_PCM=$PI_MIC_PCM so mic_level shares the mic too."
echo "This applies to the user that SSH host '$PI_HOST' logs in as; programs run as root won't see ~/.asoundrc."
