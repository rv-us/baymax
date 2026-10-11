You are reviewing the Baymax robotics dashboard in this repository as an independent, critical reviewer. Do not edit any files. Read code, call the running app, and look at the screenshots.

## What it is

A dashboard for physical-therapy style exercise sessions with a small humanoid robot (Hiwonder TonyPi Pro). Patients perform exercises, the robot demonstrates and mirrors, clinicians review performance and tune a digital twin against the real robot.

- Backend: `dashboard/` (FastAPI). Robot access only through `robot_api/`. Twin in `digital_twin/`. Session engine and scoring in `workout/`.
- Frontend: `dashboard/web/src/` (Preact + TypeScript, built to `dashboard/web/dist`).
- Docs: `dashboard/README.md`, `PLAN.md`, `DECISIONS.md`.
- The app is running now in dry run (fake robot, synthetic patient, stub coach) at http://127.0.0.1:8090 . Use `curl` against `/api/...` to verify behaviour. Useful: `GET /api/status`, `GET /api/library`, `POST /api/library/play`, `POST /api/preview/solve`, `POST /api/recorder/start|stop`, `POST /api/generate`, `POST /api/chat` (Server-Sent Events), `GET /api/sessions`, `GET /api/sessions/{id}`, `GET /api/analytics/trends`, `GET /api/calibration`, `GET /api/debug/servos`, `POST /api/stop`.
- Screenshots of every screen at 1280, 768 and 390 px wide, plus mid-flow captures, are in `reviews/shots/` (PNG). Look at them.
- Tests: `BAYMAX_DRY_RUN=1 .venv/Scripts/python.exe -m pytest tests -q`. Two older tests (`test_agent.py::test_api_key_from_env_file`, `test_digital_twin.py::test_tables_match_robot_api`) failed before this work and are out of scope. Browser click-through: `dashboard/web/e2e/flows.mjs`.

## Required features (each must be present and working end to end)

1. Animation and exercise library with digital twin preview and playback in the browser.
2. Robot play on LAN: a list of animations; hovering an item shows a live preview; clicking plays it on the real robot; send status, success and failure shown clearly.
3. Emote recording tab: record, trim, name, save, preview, one click playback on the robot.
4. At least 4 more distinct exercises beyond the original 3, each previewable and playable on robot and twin.
5. Sessions: every new recording or exercise session auto-saved and listed with timestamp, exercise, duration, summary stats; reopen, replay, delete.
6. Evaluation and analytics: headline overall score first, then drill-down: joint angle over time vs target, range of motion per joint, rep counting and quality, tempo and smoothness, left vs right symmetry, error/deviation heatmap, session over session trends, plain-language summary. Every chart has labels, units and a one line "what this means".
7. Debug and calibration mode: per-servo live readout (commanded vs actual, load, temperature if available), adjustable min/max limits, offsets and speed limits per servo, side by side twin and real robot view, persistent saved calibration profiles, limits enforced in the command path so no animation can exceed them.
8. Coach chat tab: chat with an AI coach with the selected session's metrics as context, streamed responses, graceful API error handling.
9. Animation generator on the GX10: text prompt to generated animation previewed in the twin, Save or Discard, saved animations join the library, clear status when the GX10 is unreachable, mock mode.
10. Landing homepage for pitching: hero, problem, demo, feature highlights, how it works, clear call to action into the dashboard.

Quality bar: easy navigation is the top priority (consistent layout, clear tab structure, obvious primary actions, responsive, accessible contrast, loading, empty and error states everywhere). Safety: never send commands that violate servo limits; a confirm or e-stop control in the UI; validate everything sent over the LAN. Code and docs must contain no em dashes or en dashes.

## What to report

Be concrete and verify claims yourself (cite file paths and line numbers, or the curl you ran). Do not pad. Use exactly these sections:

### Verdict
- FUNCTIONAL: yes or no
- PRODUCTION_READY: yes or no
- ALL_FEATURES_PRESENT: yes or no

### Feature check
A table: feature number, present (yes/no), working (yes/no), evidence.

### Blocking issues
Numbered. Bugs, missing pieces, safety or validation holes that must be fixed. Write "None." if there are none.

### Design and UX recommendations
Numbered. Only things you would actually change. Write "None." if there are none.

### Notes
Anything else worth knowing (optional, short).

End with these two lines, exactly in this form:
BLOCKING_ISSUES_REMAINING: <number>
DESIGN_RECOMMENDATIONS_REMAINING: <number>

Note: files in reviews/shots/ named flow-1280-fail-*.png are stale captures from earlier test-script runs; ignore them. Files named `<width>_<route>.png` are full pages; `flow-1280-<step>.png` are mid-flow captures.
