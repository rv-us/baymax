# Final report: Baymax dashboard, pass 2

Branch `frontend`. Run it with `python -m dashboard --dry-run` and open http://127.0.0.1:8080.

## Exit criteria

| Criterion | State | Evidence |
|---|---|---|
| The AGY review reports functional, production ready, all features present | Met | `reviews/review-2.md` and `reviews/review-3.md`: FUNCTIONAL yes, PRODUCTION_READY yes, ALL_FEATURES_PRESENT yes, 0 blocking issues |
| The same review has zero remaining design recommendations | Met | Review 1 made 4, all fixed. Reviews 2 and 3 report "None." |
| The site is fully and easily navigable end to end | Met | 15 desktop flows and 5 phone flows pass in a real browser (`dashboard/web/e2e`), every route checked at 390, 768, 1024 and 1280 px with no sideways overflow and no console errors |

## What was verified, and how

- **Python tests**: 196 pass (`BAYMAX_DRY_RUN=1 python -m pytest tests`). 33 are new, in `tests/test_dashboard_features.py`. Two older tests fail before and after this work and are out of scope (`test_agent.py::test_api_key_from_env_file`, `test_digital_twin.py::test_tables_match_robot_api`).
- **Browser, dry run**: `e2e/flows.mjs` clicks through the front page, library hover and play, one-click play and stop, a built-in move, a refusal from a hot servo, record to trim to save to play, the recording appearing in Sessions, generate to save, discard, the full session analysis and replay, the coach chat, saving and enforcing a calibration profile, deleting a session, editing in Studio, and Stop on every page. `e2e/touch.mjs` covers the phone.
- **The real GX10**: reachable at its LAN address. The generator produced motions from `qwen3.6:35b` in about 12 seconds and the coach chat streamed an answer in about 4 seconds. This found a real bug (the model does not follow the keyframe schema exactly), which is fixed and tested.
- **Real mode with nothing reachable**: every screen's API answers with a clear message rather than an error page; play is refused with "The robot isn't reachable."

## What was not verified

- **Nothing was run on the real robot.** Its API did not answer from this machine. Robot motion, the recorder's limp and hold, calibration offsets and speed caps are checked against `robot_api`'s fake hardware only.
- **The camera** was not reachable either; the dry run shows the fake frame.
- **Pose tracking is still simulated**, as before this pass. Live sessions are labelled synthetic.

## The ten features

| # | Feature | Where | Notes |
|---|---|---|---|
| 1 | Library with twin preview | `#/library`, `library.tsx`, `motion.tsx` | The preview is a private twin in the browser, solved once per animation |
| 2 | Play on LAN | `#/library`, `play.tsx`, `POST /api/library/play` | Hover or focus previews, click plays behind a confirm or the one-click switch; sending, playing, finished, stopped and refused are all shown |
| 3 | Emote recorder | `#/record`, `recorder.py` | Record, trim (sliders plus 0.1 s buttons), name, save, preview, play |
| 4 | More animations | `animations/`, `starter.py`, `poses.py` | 6 new exercises and 3 emotes, 12 in all |
| 5 | Sessions | `#/sessions`, `store.py` | Live sessions and recordings save themselves; reopen, replay on the twin, delete |
| 6 | Analytics | `#/sessions/<id>`, `analytics.tsx`, `workout/session.py` | Headline score, plain-language reasons, 8 drill-down charts, each with unit and "what this means" |
| 7 | Debug and calibration | `#/calibrate`, `calibration.py`, `robot.py` | Told against doing side by side, per-servo readout, limits, offsets, speed caps, saved profiles, enforced in the gateway |
| 8 | Coach chat | `#/coach`, `chat.py` | Streamed, session metrics as context, offline answer with the reason when the model is down |
| 9 | Generator on the GX10 | `#/generate`, `generator.py` | Prompt to preview to Save or Discard; GX10 status shown; labelled mock |
| 10 | Landing page | `#/`, `landing.tsx` | Hero with the live twin and a protractor reading its shoulder, problem, how it works, a real scored session, features, safety, limits, call to action |

## Safety

- Every servo command passes the active calibration profile and then `robot_api`'s own limits. A profile can only narrow them. Tested: an animation asking for pulse 200 on a joint limited to 500 never sends less than 500.
- Stop is in the top bar of every screen and in the robot bar wherever something can be played. A click on a move asks first unless one-click play is on, which turns itself off after 5 minutes.
- A pain word or reported fall freezes the robot, then latches; every motion call is refused until a person clears it.
- Everything sent over the LAN is validated on the server: ids, keyframes, speeds, trims, calibration values, and which built-in moves may run.

## Review loop

| Round | Verdict | Blocking | Design recommendations | What was done |
|---|---|---|---|---|
| 1 | functional, production ready, all features present | 0 | 4 | Sideways-scroll cue and pinned column in the servo table; exact trim controls; tap-to-preview on touch; the replay names its set |
| 2 | same | 0 | 0 | Confirmed all four resolved |
| 3 | same | 0 | 0 | Confirmation pass on the final commit, after the generator fix |

Invocation: `agy -p "$(cat reviews/PROMPT.md)" --mode plan --print-timeout 27m > reviews/review-N.md`, with the app running in dry run on port 8090.

## Things worth knowing

- **The usage guard could not be measured.** This session had no way to read the remaining usage window, so the guard was honoured by committing after every feature and review pass.
- **Squats and balance holds are not in the library.** `workout/plan.py` refuses leg keyframes because single leg moves make this robot fall. The new exercises are upper body. The robot's own built-in squat is offered as a built-in move, which has no preview.
- **Servo load is not reported** by this robot's bus API. The debug table says "not reported" and uses the gap between told and doing as the stall signal.
- **Scoring changed slightly.** Pace for reps counted from a trace now compares like with like (the rep window is about 70% of a full cycle), which raises pacing scores for existing sessions. Smoothness is new and honest about tracker jitter: a clean rep reads 50% to 70%.
- **`dashboard/prompt.md` is left untracked** and commits carry no AI co-author line, both following the owner's earlier brief. `credentials.txt` was not read.
- Decisions made along the way are in `DECISIONS.md`; the run and API notes are in `dashboard/README.md`.
