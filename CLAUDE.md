# CLAUDE.md

Start with [README.md](README.md): what runs on which machine, and a map of every folder and its docs.

## Working in this repo

- **Tests:** `python -m pytest tests` from the repo root. `tests/conftest.py` sets `BAYMAX_DRY_RUN=1`, so they use the fake robot (`robot_api/fake.py`) and never touch hardware. `arm_mvp/` and `laptop_mvp/` keep their own tests next to the code (run pytest from inside those folders).
- **Dry runs:** almost every entry point takes `--dry-run` (or `BAYMAX_DRY_RUN=1`). Try changes there before the robot.
- **Keep the docs current:** when you add a package, a flag, an endpoint or an environment variable, update its README (and the folder map in README.md). The docs are what the next person, or the next session, starts from.
- **Match the surrounding code:** plain-English docstrings that explain why, small modules, no new dependencies without a reason.
- **Never read or commit `credentials.txt` or `.env` files.**

## Machines

- The robot (TonyPi, a Raspberry Pi 5) runs `python -m robot_api.server`. The GX10 runs everything else and reaches the Pi over SSH as `tonypi`. Addresses change per venue; they're in `streaming/config.env` and `BAYMAX_API_URL`.
- **Python environments live in `~/venvs/baymax`, never inside a checkout.** `~/baymax` gets deleted and re-cloned (that lost the GX10's venv once). On the GX10, `scripts/setup_gx10_venv.sh` rebuilds it with uv in seconds from `~/.cache/uv`; don't clear that cache. `requirements.txt` lists everything the GX10 needs; add new dependencies there.
- Teammates work directly on both machines. Don't reset, pull into, or edit their checkouts (`~/baymax` on either machine may hold unpushed work). Test from a separate `git worktree` and remove it afterwards.
- One program at a time can open the robot's camera, receive a UDP stream on a port, or drive the servo bus. Check for leftovers (`ss -ulpn`, `pgrep -af gst-launch`) before assuming something is broken.

## Robot safety

- **Ask before anything that moves the robot or makes it speak**, and make sure someone is next to it. People nearby get surprised by a robot that suddenly talks or moves.
- **A limp robot falls.** `servo/coast` (and `stand` from limp) needs someone holding the robot or the robot lying down.
- **Servos overheat when the robot stands for a long time** (74 °C after about 25 minutes; they cut out at 85 °C). Run the API with `BAYMAX_TEMP_CHECK=1`, check `sensors/servo_temps` before long runs, and let it cool limp (lying down) between them.
- Start the API with `BAYMAX_SPEAKER_PCM=pulse` (otherwise speech stalls about 30 s on every other sentence) and `BAYMAX_MIC_PCM=mic_shared`.
- A pain word or "stop" must freeze the robot immediately, from the thread that heard it, and the robot then stays where it stopped. Keep that true in any new session code (`workout/loop.py` and `pt_agent/tools.py` do it).
