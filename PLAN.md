# Plan: Baymax dashboard, pass 2

What exists (read end to end): `robot_api` (the only door to the robot, with clamping, speed cap,
temperature cutoff and fakes), `digital_twin` (URDF kinematics, three.js viewer, SSE), `workout`
(session engine, plan format, session schema v2 and scoring), and `dashboard` (FastAPI app plus a
Preact web app with Studio, Live, Sessions, Plans). 163 tests pass, 2 older ones fail before this work.

## Gaps against the ten required features

| # | Feature | State before | Work |
|---|---|---|---|
| 1 | Library with twin preview | Only an "Open" menu in Studio | New Library screen, private in-browser twin preview (`/api/preview/solve`) |
| 2 | Play on robot over LAN | Only from Studio | Library list: hover previews, click plays, arm switch, send status, e-stop |
| 3 | Emote recorder | A fold in Studio that captures single poses | Record tab: server-side recorder, trim, name, save, preview, one click play |
| 4 | More animations | 3 | 7 more, generated from one pose helper so they stay inside limits |
| 5 | Sessions | List and detail, no delete or replay | Delete, replay on the twin, recordings auto-logged as sessions |
| 6 | Analytics | One overlay chart and rep strip | Headline score, then range, reps, tempo, smoothness, symmetry, heatmap, trends, plain summary |
| 7 | Debug and calibration | Nothing | Calibrate tab, per-servo readout, limits, offsets, speed caps, profiles, enforced in the gateway |
| 8 | Coach chat | One-shot summary only | Chat tab, streamed over SSE, session metrics as context, offline fallback |
| 9 | Animation generator | Nothing | Generate tab: prompt to keyframes through Ollama on the GX10, mock mode, save or discard |
| 10 | Landing page | Nothing | Pitch page at `#/`, dashboard behind it |

## Order of work

1. Backend: analytics in `workout/session.py`, calibration limiter in the gateway, recorder,
   generator, chat, preview solve, session delete, library play. Tests for each.
2. New animations.
3. Frontend shell: grouped navigation (Move, Train, Review, Tune), landing page.
4. Screens: Library, Record, Generate, Sessions analytics, Coach, Calibrate.
5. Build, run in dry run, click through every tab at 390, 768 and 1280 px, fix.
6. AGY review loop: `reviews/review-N.md`, fix, repeat until clean.
7. `FINAL_REPORT.md`, commit, push.

## Rules kept from the first pass

- The browser talks only to the dashboard server. The robot token never reaches it.
- `robot_api` limits are never weakened. Calibration can only narrow them.
- Numbers on screen are computed in `workout/session.py`, never by the model.
- Everything works in `python -m dashboard --dry-run` with no robot, camera or GX10.
- No em dashes or en dashes in code or docs.
