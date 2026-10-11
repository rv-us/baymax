# Decisions

Made without asking, as instructed. Newest at the bottom.

1. **Keep the stack.** Preact, signals, Vite, hand-drawn SVG charts, one FastAPI app. No chart or
   UI library is added: the demo must run on a LAN with no internet and the bundle is committed.
2. **Keep the visual identity** (the goniometer idea, Atkinson Hyperlegible, clinic-mist ground,
   one blue accent, red only for stopping) and extend it to the new screens and the landing page.
3. **Navigation is grouped by job**: Move (Library, Studio, Record, Generate), Train (Live, Plans),
   Review (Sessions, Coach), Tune (Calibrate). A side rail on wide screens, a bottom bar with a
   "More" sheet on phones. Stop stays in the top bar on every screen.
4. **The landing page is `#/`.** The dashboard opens on Library, since "pick a move and play it"
   is the most common job. Studio moved to `#/studio`.
5. **Hover preview runs in the browser.** `/api/preview/solve` returns solved joint angles for a
   whole animation and a private twin instance plays them. The shared twin is not touched, so two
   people on the LAN can preview different moves at once.
6. **Click to play uses an arm switch.** Disarmed (default), a click asks for confirmation. Armed
   (auto disarms after 5 minutes), one click plays. The e-stop is always on screen. This satisfies
   both "clicking plays it" and "add a confirm or e-stop".
7. **Calibration can only narrow limits.** Profile min and max must sit inside `robot_api`'s
   limits, speed caps cannot exceed its 1000 pulses per second, offsets are capped at 100 pulses.
   The gateway applies the active profile to `servo/set`, `servo/set_many` and `actions/arm`
   (rewritten to `servo/set_many`), so every keyframe animation is covered.
8. **Factory action groups cannot be clamped** because they run inside the robot's own controller.
   A profile has an `allow_action_groups` switch; when off, the gateway refuses them.
9. **Servo load is not reported by this robot's bus API.** The debug table shows position,
   temperature, input voltage and torque state, and labels load as "not reported" instead of
   inventing a number. Position error (commanded minus actual) is shown as the stall signal.
10. **Recordings are sessions.** Saving a recording writes the animation and a session log with
    `kind: "recording"`, so it shows in Sessions with timestamp, name, duration and stats.
11. **Squats and balance work on the legs are not offered.** `workout/plan.py` forbids keyframing
    leg joints because single leg moves make the robot fall. The new exercises are upper body.
12. **Analytics extend schema v2, backwards compatible.** `trace.joints` (left and right shoulder
    and elbow angles) is optional. Symmetry and per-joint range show "not measured" without it.
13. **The generator asks the model for degrees, not pulses.** The model fills a small schema
    (arm raise, side lift, elbow bend, per keyframe); code converts to pulses and clamps. A bad
    or unreachable model falls back to a labelled rule-based mock.
14. **Coach chat falls back to an offline answer** built from the same facts when Ollama cannot be
    reached, with the reason shown, so the tab is always demoable.
15. **Commits carry no AI co-author line.** `dashboard/prompt.md` (the owner's earlier brief) says
    never to co-author an AI agent, and the owner's own commits follow that.
16. **Usage guard.** This session has no tool that reports the remaining 5 hour usage window, so
    the guard is honoured by committing after every feature; a fresh session can resume from
    `PLAN.md`, this file and the latest `reviews/review-N.md`.
17. **`credentials.txt` is never read or touched** (the earlier brief forbids it), and
    `dashboard/prompt.md`, the owner's untracked brief, is read but left uncommitted.
18. **Touch and mouse behave differently in the Library, on purpose.** A mouse hovers to preview and
    clicks to play. A finger cannot hover, so a tap previews and reveals a separate "Play on robot"
    button. Both end at the same confirm unless one-click play is on. (From review 1.)
19. **The AGY reviewer runs read only**: `agy -p "<prompt>" --mode plan --print-timeout 27m`, with
    the prompt in `reviews/PROMPT.md`, the app running in dry run on port 8090, and fresh screenshots
    in `reviews/shots/` (not committed). It was not given `--dangerously-skip-permissions`.
20. **The generator tolerates schema drift.** Run against the real `qwen3.6:35b` on the GX10, the
    model sometimes named arms `left_arm`, or gave one set of angles for both arms. Those shapes
    are accepted, the motion is still clamped in code, and a reply that never moves the arms is
    rejected in favour of the labelled mock.
21. **Stale failure screenshots were left in `reviews/shots/`.** A safety check refused the bulk
    delete, so the reviewer was told to ignore files named `flow-1280-fail-*`. The folder is
    git-ignored.
