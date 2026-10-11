# demo

A scripted run of the whole Baymax story for presentations: the robot waits for a wave, greets, checks temperature, demonstrates and coaches a set, gets stopped by a pain word, and says goodbye. It does exactly the same thing every time.

```bash
python -m demo --dry-run --auto      # fake robot, no waiting: check the script on a laptop
python -m demo                       # real robot (BAYMAX_API_URL), Enter at each cue
python -m demo --auto                # real robot, cues wait a fixed 2.5 s instead of Enter
python -m demo --fall --walk         # also the fall safety demo, and really walk to the patient
```

## What is real and what is scripted

- **Real:** every move and line goes through the robot API (speech, servos, LED matrix, arms, head), the front arm raise is the actual animation from `animations/`, and the pain stop is the same `actions/stop` + `servo/stop` the live sessions use.
- **Scripted:** nothing is perceived. No camera, no microphone, no model. "Waves", "answers" and "ow" are done out loud by whoever presents, who then presses Enter. The rep count, the form note and the temperature are fixed. The temperature motion is real but no reading is taken; the log says `"temperature_simulated": true`.

Say that when you present it. The session log is written to `sessions/demo-<time>.json` with `"source": "scripted-demo"`, so it can never be mistaken for a live session.

## Cues

With Enter, the demo waits at five points: the wave, "I'm doing fine", "No, it was fine", the pain word, and "I'm okay". Everything between cues runs on fixed timings, so a run takes about the same time each time. The lines are in [script.py](script.py); edit them there.

## Safety

The same rules as the live sessions apply (see the root `CLAUDE.md`). Someone stands next to the robot. The fall demo and walking are off unless you ask for them, and the fall needs about 40 cm of clear, flat floor. Ctrl-C freezes the robot where it is. After the pain stop nothing else moves: the demo only speaks and shows an icon.
