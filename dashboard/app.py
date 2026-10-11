"""The dashboard's FastAPI app: robot proxy, twin, file stores, coach jobs, live events.

    create_app(Settings(dry_run=True))     # everything fake; what the tests use

Every robot refusal reaches the browser as {"detail": "<the robot's own message>"}
with the robot API's status (409 for a safety refusal), never a generic error.
"""
from __future__ import annotations

import os
import threading
import time

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from robot_api.errors import RobotError
from workout import keyframes
from workout.plan import EASINGS, KEYFRAME_JOINTS, PlanError, check_keyframes, parse_plan
from workout.parts import answer_complete
from workout.session import calories

from . import poses, preview, store, synthetic
from .calibration import FACTORY, SPEED_CAP, MAX_OFFSET, MIN_SPEED, CalibrationError, Profiles
from .chat import Chat, clean_messages
from .coach import Coach
from .config import Settings, ollama_url
from .events import Hub
from .gx10 import Gx10Link
from .generator import Generator
from .live import Live
from .mirror import Mirror
from .monitor import Monitor
from .echo import is_echo
from .player import Player
from .recorder import Recorder
from .robot import ARM_SERVOS, Gateway, Refused
from .voice_link import VoiceLink
from .safety import LATCHING, Safety
from .tunnel import Tunnel, TunnelError

# The robot's own action groups worth offering as emotes: (group, label, what it does).
LISTEN_GRACE_S = 5.0   # the microphone stays on this long after the robot stops talking or a question is answered

BUILTIN_EMOTES = (
    ("wave", "Wave", "A friendly wave with one hand."),
    ("bow", "Bow", "A small bow from the waist."),
    ("chest", "Chest stretch", "Opens and closes the arms across the chest."),
    ("twist", "Twist", "Turns the waist from side to side."),
    ("wings_up", "Wings up", "Lifts both arms out like wings."),
    ("stepping", "March on the spot", "Steps in place."),
    ("squat", "Squat", "Bends the knees and stands back up."),
    ("go_hand_up", "Hands up", "Raises both hands."),
)
MAX_SOLVE_POSES = 1500


def create_app(settings: Settings | None = None, backend=None) -> FastAPI:
    from digital_twin.fastapi_router import mount_twin

    settings = settings or Settings()
    app = FastAPI(title="Baymax dashboard")
    hub = Hub()
    gateway = Gateway(settings, backend)
    safety = Safety(gateway, hub)
    sessions = store.Sessions(settings.sessions_dir)
    animations = store.Animations(settings.animations_dir)
    plans = store.Plans(settings.plans_dir)
    profiles = Profiles(settings.calibration_dir)
    gateway.limiter.use(profiles.active())
    coach = Coach(settings, sessions, hub)
    chat = Chat(settings, sessions)
    generator = Generator(settings, gateway.limiter)
    monitor = Monitor(settings, gateway, safety, coach, hub)
    tunnel = Tunnel(hub)
    gx10 = Gx10Link(settings, tunnel, monitor, hub, get_webcam=lambda: gateway.webcam,
                    on_pose_service=lambda url: attach_gx10_camera(url))
    twin = mount_twin(app, "/twin")
    player = Player(gateway, twin.state, hub)
    live = Live(settings, gateway, safety, sessions, hub)
    recorder = Recorder(settings, gateway, hub, animations, sessions)
    webcam = None
    if settings.webcam:  # a dry run's camera: the laptop's own, with arm tracking, if it can be opened
        from .webcam import Webcam

        webcam = Webcam(settings.webcam, on_pose=live.apply_pose, pose_url=settings.pose_url)
        if webcam.start():
            gateway.webcam = webcam
        else:
            print(f"webcam: {webcam.status['detail']} Using the fake frame instead.")

    def attach_gx10_camera(url: str) -> bool:
        """Take the robot's camera from the GX10 pose service when it owns it: the picture and the pose then
        come from there for everything (camera panel, sessions, Mirror) and this program never opens the
        robot's camera. A laptop webcam in use is left alone."""
        old = gateway.webcam
        if old is not None and not getattr(old, "gx10_camera", False):
            return False
        if old is not None and old.pose_url == url.rstrip("/") and old.tracking:
            return True
        from .gx10_camera import Gx10Camera
        cam = Gx10Camera(url, on_pose=live.apply_pose)
        if not cam.start():
            return False
        gateway.webcam, settings.pose_url = cam, cam.pose_url
        app.state.parts["webcam"] = cam
        if old is not None:
            old.stop()
        return True

    spoken = {"done": None, "utt": None, "partial": ""}   # the utterance already acted on; the latest partial words

    def hear(text: str, age: float = 0.0, final: bool = True, utt: int | None = None) -> str:
        """What became of words from the microphone. They arrive as they are spoken: partial words every quarter
        second (`final` False), then the whole utterance when the patient pauses. The first words that already
        make a complete answer to the question being asked are taken at once, so the session doesn't wait for
        the pause (workout.parts.answer_complete); pain words stop the robot at once, wherever they are.

        "echo": the robot's own voice coming back, ignored. "pain": the robot was stopped. "answer": a session was
        waiting and took it. "hearing": a partial that isn't an answer yet. "already": the rest of an utterance
        that was already acted on. "unprompted": nothing was asked, so it is shown and dropped (a stray remark
        must never answer a later question)."""
        if not text:
            return "unprompted"
        if is_echo(text, list(gateway.said)):
            return "echo"
        if utt is not None and spoken["done"] == utt:
            return "already"
        before = spoken["partial"] if spoken["utt"] == utt else ""
        stable = bool(before) and before == text.strip().lower()
        spoken.update(utt=utt, partial="" if final else text.strip().lower())
        if final:
            live.update(heard=text)
        if safety.heard(text):
            spoken["done"] = utt
            return "pain"
        if live.running and live.state.get("waiting") and not live.simulate:
            kind = "ready" if "ready" in (live.said or "").lower() else "trouble"
            if final or answer_complete(kind, text, stable):
                live.answer(text[:300])
                spoken["done"] = utt
                return "answer"
        return "unprompted" if final else "hearing"

    needed = {"until": 0.0}

    def listening_needed() -> bool:
        """The microphone is wanted while the robot talks (it is about to ask something) and while a
        question waits for its answer, plus a few seconds either side so Whisper isn't loaded and dropped
        between two sentences. During a set it is off, so the GX10 spends nothing on it (BAYMAX_VOICE_IN_SETS=1
        keeps it on for the whole session, so "stop" is heard mid-exercise as well)."""
        now = time.monotonic()
        if live.running and (gateway.speaking() or live.state.get("waiting")
                             or os.environ.get("BAYMAX_VOICE_IN_SETS") == "1"):
            needed["until"] = now + LISTEN_GRACE_S
        return live.running and now < needed["until"]

    voice = VoiceLink(settings, hear, hub, active=listening_needed)
    gateway.on_speaking = voice.set_speaking

    mirror = Mirror(settings, gateway, hub, busy=lambda: (
        "A session is running. End it first." if live.running else
        "An animation is playing. Stop it first." if player.playing else
        "A recording is running. Stop it first." if recorder.recording else None))
    safety.on_trip.append(lambda kind, entry: mirror.stop(rest=False))   # a stop freezes the robot where it is
    safety.on_trip.append(lambda kind, entry: player.stop())
    safety.on_trip.append(lambda kind, entry: recorder.stop())
    app.state.parts = dict(settings=settings, hub=hub, gateway=gateway, safety=safety, sessions=sessions,
                           animations=animations, plans=plans, coach=coach, monitor=monitor,
                           twin=twin, player=player, live=live, tunnel=tunnel, profiles=profiles,
                           recorder=recorder, chat=chat, generator=generator, webcam=gateway.webcam, mirror=mirror, gx10=gx10, voice=voice, hear=hear,
                           attach_gx10_camera=attach_gx10_camera)

    def calibration_state() -> dict:
        active = gateway.limiter.profile
        return {"active": active["id"], "name": active["name"], "narrowed": gateway.limiter.narrowed(),
                "allow_action_groups": active.get("allow_action_groups", True)}

    hub.publish("calibration", calibration_state())

    @app.exception_handler(CalibrationError)
    def _calibration_error(request, e: CalibrationError):
        return JSONResponse({"detail": str(e)}, status_code=404 if "no calibration profile" in str(e) else 422)

    @app.exception_handler(Refused)
    def _refused(request, e: Refused):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    @app.exception_handler(RobotError)
    def _robot_error(request, e: RobotError):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(PlanError)
    def _plan_error(request, e: PlanError):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(store.NotFound)
    def _not_found(request, e: store.NotFound):
        return JSONResponse({"detail": f"{e.args[0]} was not found"}, status_code=404)

    if settings.monitor:
        attach_stop = threading.Event()

        def _attach_when_up():
            """The GX10's pose service may start after the dashboard does: keep asking until it owns the camera."""
            while not attach_stop.is_set() and not attach_gx10_camera(settings.pose_url):
                attach_stop.wait(5.0)

        @app.on_event("startup")
        def _start():
            if settings.dry_run:
                synthetic.refresh(sessions)
            twin.attach_robot(gateway, hz=4.0, label="dry run" if settings.dry_run else settings.robot_url)
            monitor.start()
            voice.start()
            if settings.pose_url and not settings.webcam and not settings.dry_run:
                threading.Thread(target=_attach_when_up, name="dashboard-gx10-camera-attach", daemon=True).start()
            if settings.gx10_auto:
                gx10.start()   # in the background: the page is usable at once, and it keeps trying until the GX10 answers

        @app.on_event("shutdown")
        def _stop():
            attach_stop.set()
            voice.stop()
            gx10.stop()
            mirror.stop(rest=False)
            monitor.stop()
            tunnel.close(publish=False)

    # ---- status and events --------------------------------------------------------

    @app.get("/api/status")
    def status():
        return {**monitor.snapshot(), "safety": safety.snapshot(), "live": live.state,
                "playback": player.state, "tunnel": tunnel.status(), "recorder": recorder.state,
                "mirror": mirror.snapshot(), "gx10": gx10.snapshot(), "voice": voice.snapshot(),
                "calibration": calibration_state(),
                "settings": {"ollama_url": settings.ollama_url, "model": settings.model,
                             "stub_llm": settings.stub_llm, "robot": "fake hardware" if settings.dry_run
                             else settings.robot_url}}

    @app.get("/api/events")
    async def events():
        return StreamingResponse(hub.stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---- robot --------------------------------------------------------------------

    @app.post("/api/robot/{path:path}")
    def robot(path: str, body: dict | None = Body(default=None)):
        """Any robot API endpoint, with the dashboard's token and safety latch applied."""
        return gateway.call(path, **(body or {}))

    @app.get("/api/camera.jpg", response_class=Response)
    def camera(width: int = 640):
        try:
            jpeg = gateway.camera_jpeg(width=max(160, min(1280, width)))
        except Refused as e:
            monitor.camera_failed(str(e))
            raise
        monitor.camera_ok(len(jpeg))
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/stop")
    def stop():
        """The Stop button: freeze the robot, end playback and any running session.
        Works even while a safety stop is latched, and never latches one itself."""
        player.stop()
        live.stop()
        recorder._cancel.set()  # ends a recording without waiting for its thread
        errors = gateway.stop_now()
        if errors:
            raise Refused("Stop was sent, but the robot didn't confirm: " + "; ".join(errors), 502)
        return {"stopped": True}

    # ---- safety -------------------------------------------------------------------

    @app.get("/api/safety")
    def safety_state():
        return safety.snapshot()

    @app.post("/api/safety/heard")
    def safety_heard(body: dict = Body(...)):
        """Text the microphone picked up (voice.Listener's on_transcript goes here)."""
        text = str(body.get("text") or "")[:500]
        return {"pain": safety.heard(text), "text": text}

    # ---- the microphone (voice/service.py runs on the GX10; dashboard/voice_link.py reads it) ----------------------

    @app.get("/api/voice")
    def voice_state():
        """The microphone link (to the GX10's voice service), what it heard last, and whether an answer is awaited."""
        return {**voice.snapshot(), "speaking": gateway.speaking(),
                "waiting": bool(live.running and live.state.get("waiting") and not live.simulate)}

    @app.post("/api/voice/heard")
    def voice_heard(body: dict = Body(...)):
        """Something the patient said (the voice link posts here; so can a script). See hear()."""
        outcome = hear(str(body.get("text") or "")[:500].strip())
        return {"outcome": outcome, "pain": outcome == "pain", "answered": outcome == "answer"}

    @app.post("/api/safety/report")
    def safety_report(body: dict = Body(...)):
        """A detector reporting pain or a fall. Freezes the robot, then latches."""
        kind = body.get("kind")
        if kind not in LATCHING:
            raise HTTPException(422, f"kind must be one of {LATCHING}")
        return safety.trip(kind, str(body.get("detail") or "")[:300], body.get("transcript"))

    @app.post("/api/safety/clear")
    def safety_clear(body: dict = Body(...)):
        if body.get("confirm") is not True:
            raise HTTPException(422, "Clearing a safety stop needs {\"confirm\": true}.")
        safety.clear(str(body.get("by") or "therapist")[:60])
        monitor.publish()
        return safety.snapshot()

    # ---- animations and preview ---------------------------------------------------

    @app.get("/api/animation-model")
    def animation_model():
        """What the editor can keyframe: arm joints with the robot API's own limits."""
        from robot_api.config import MIN_MOVE_MS
        from robot_api.servo.joints import HEAD, JOINTS, STAND_POSE
        from robot_api.servo.limits import HEAD_CENTER, HEAD_LIMITS, LIMITS
        from workout.plan import MAX_KEYFRAME_MS, MAX_KEYFRAMES

        return {"joints": [{"name": j, "id": JOINTS[j], "limits": list(LIMITS[JOINTS[j]]),
                            "stand": STAND_POSE[JOINTS[j]]} for j in KEYFRAME_JOINTS],
                "head": {axis: {"limits": list(HEAD_LIMITS[sid]), "center": HEAD_CENTER}
                         for axis, sid in HEAD.items()},
                "start": keyframes.start_pose(), "easings": list(EASINGS),
                "min_ms": MIN_MOVE_MS, "max_ms": MAX_KEYFRAME_MS, "max_keyframes": MAX_KEYFRAMES,
                "track_joints": list(store.TRACK_JOINTS)}

    @app.get("/api/animations")
    def animation_list():
        return animations.list()

    @app.post("/api/animations")
    def animation_create(body: dict = Body(...)):
        return animations.save(body)

    @app.post("/api/animations/import")
    def animation_import(body: dict = Body(...)):
        return store.import_frames(body)

    @app.get("/api/animations/{aid}")
    def animation_get(aid: str):
        return animations.get(aid)

    @app.put("/api/animations/{aid}")
    def animation_save(aid: str, body: dict = Body(...)):
        return animations.save(body, aid)

    @app.delete("/api/animations/{aid}")
    def animation_delete(aid: str):
        animations.delete(aid)
        return {"deleted": aid}

    @app.post("/api/animations/{aid}/exercise")
    def animation_to_plan(aid: str, body: dict = Body(default={})):
        """Add the animation to a plan as an exercise (plan.py checks it)."""
        exercise = store.as_exercise(animations.get(aid))
        if body.get("plan"):
            return {"exercise": exercise, "plan": plans.add_exercise(store.check_id(body["plan"]), exercise)}
        return {"exercise": exercise}

    @app.post("/api/preview/pose")
    def preview_pose(body: dict = Body(...)):
        """Show a pose on the twin only: {keyframes, t_ms} or {servos, head}. Never
        touches the robot."""
        if "keyframes" in body:
            check_keyframes(body["keyframes"])
            pose = keyframes.pose_at(body["keyframes"], float(body.get("t_ms") or 0))
        else:
            check_keyframes([{"ms": 300, "servos": body.get("servos") or {}, "head": body.get("head") or {}}])
            pose = {"servos": body.get("servos") or {}, "head": body.get("head")}
        twin.state.set_command(pose["servos"], source="dashboard", label="preview", head=pose["head"])
        return pose

    @app.post("/api/preview/session")
    def preview_session(body: dict = Body(...)):
        """Loop the session being built on the twin: the pose `t_ms` into all its exercises
        played end to end. Twin only. Does nothing while a session or animation is running."""
        if live.running or player.playing or mirror.running or not body.get("exercises"):
            return {"skipped": True}
        plan = parse_plan({"name": "preview", "exercises": body.get("exercises") or []}, source="preview")
        out = preview.session_pose([vars(e) for e in plan.exercises], float(body.get("t_ms") or 0))
        if out["pose"]:
            twin.state.set_command(out["pose"]["servos"], source="dashboard", label="preview", head=out["pose"]["head"])
        return {"index": out["index"], "total_ms": out["total_ms"]}

    def ready_to_move() -> None:
        """Refuse (409, in words) unless the robot may be moved right now."""
        if mirror.running:
            raise Refused("Mirror is running. Stop it before playing an animation.")
        if live.running:
            raise Refused("A session is running. End it before playing an animation.")
        if recorder.recording:
            raise Refused("A recording is running. Stop it before playing an animation.")
        if monitor.robot["state"] == "unknown":
            monitor.poll_robot()
        motion = monitor.motion()
        if not motion["ok"]:
            raise Refused(" ".join(motion["reasons"]))

    def speed_of(body: dict) -> float:
        try:
            return max(0.25, min(1.5, float(body.get("speed") or 1.0)))
        except (TypeError, ValueError):
            raise HTTPException(422, "speed must be a number from 0.25 to 1.5") from None

    @app.post("/api/playback/start")
    def playback_start(body: dict = Body(...)):
        check_keyframes(body.get("keyframes"))
        ready_to_move()
        return player.start(body["keyframes"], speed_of(body), bool(body.get("loop")),
                            str(body.get("name") or "")[:80])

    # ---- library: preview anywhere, play on the robot over the LAN ------------------

    def builtin_emotes() -> list:
        try:
            safe = set(gateway.call("actions/list").get("safe", []))
        except Refused:
            safe = set()
        return [{"id": f"builtin:{g}", "group": g, "name": label, "description": text, "kind": "builtin",
                 "available": g in safe} for g, label, text in BUILTIN_EMOTES]

    @app.get("/api/library")
    def library():
        """Everything that can be played: saved animations, and the robot's built-in moves."""
        return {"animations": animations.list(), "builtins": builtin_emotes(),
                "calibration": calibration_state()}

    @app.post("/api/library/play")
    def library_play(body: dict = Body(...)):
        """Play a saved animation ({"id"}) or a built-in move ({"builtin"}) on the robot.
        The request is checked here, the keyframes again by plan.py, and every servo
        command by the calibration profile and by robot_api."""
        if body.get("builtin") is not None:
            group = str(body["builtin"])
            match = next((b for b in BUILTIN_EMOTES if b[0] == group), None)
            if match is None:
                raise HTTPException(422, f"{group!r} is not one of the built-in moves offered here.")
            ready_to_move()
            return player.start_group(group, match[1])
        anim = animations.get(store.check_id(str(body.get("id") or "")))
        check_keyframes(anim.get("keyframes"), f"animation {anim['name']!r}")
        ready_to_move()
        return player.start(anim["keyframes"], speed_of(body), False, anim["name"])

    def solve(pose_list: list) -> list:
        """Joint angles and floor contact for each pose, for a twin that plays in the
        browser. Uses the twin's own kinematics; touches neither the shared twin nor the robot."""
        from digital_twin import mapping

        robot, out, cache = twin.state.robot, [], {}
        for p in pose_list:
            head = {**keyframes.start_pose()["head"], **(p.get("head") or {})}
            key = (tuple(sorted(p["servos"].items())), head["pan"], head["tilt"])
            if key not in cache:
                angles = mapping.pulses_to_angles(mapping.full_servos(p["servos"]))
                angles.update(mapping.head_to_angles(head["pan"], head["tilt"]))
                cache[key] = {"angles": {k: round(v, 2) for k, v in angles.items()},
                              "base": robot.ground_contact(robot.fk(angles))}
            out.append(cache[key])
        return out

    @app.post("/api/preview/solve")
    def preview_solve(body: dict = Body(...)):
        """{keyframes, hz?} -> {hz, total_ms, frames: [{angles, base}]}: a whole animation
        solved once, so hovering a library item can preview it without a request per frame."""
        check_keyframes(body.get("keyframes"))
        frames = body["keyframes"]
        hz = max(5, min(30, int(body.get("hz") or 20)))
        total = keyframes.duration_ms(frames)
        count = min(MAX_SOLVE_POSES, int(total / 1000 * hz) + 1)
        step = total / max(1, count - 1)
        limiter = gateway.limiter
        pose_list = []
        for i in range(count):
            pose = keyframes.pose_at(frames, min(total, i * step))
            pose_list.append({"servos": limiter.clamp_pose(pose["servos"]), "head": pose["head"]})
        return {"hz": 1000 / step if step else hz, "total_ms": total, "frames": solve(pose_list)}

    # ---- recorder -----------------------------------------------------------------

    @app.post("/api/recorder/start")
    def recorder_start(body: dict = Body(default={})):
        if mirror.running:
            raise Refused("Mirror is running. Stop it first.")
        if player.playing:
            raise Refused("An animation is playing on the robot. Stop it first.")
        if live.running:
            raise Refused("A session is running. End it before recording.")
        return recorder.start(coast=bool(body.get("coast", True)))

    @app.post("/api/recorder/stop")
    def recorder_stop():
        return recorder.stop()

    @app.get("/api/recorder/takes/{tid}")
    def recorder_take(tid: str):
        take = recorder.take(tid)
        return {**take, "joints": list(KEYFRAME_JOINTS)}

    def trim_of(body: dict):
        try:
            start = float(body.get("start_ms") or 0)
            end = None if body.get("end_ms") is None else float(body["end_ms"])
        except (TypeError, ValueError):
            raise HTTPException(422, "start_ms and end_ms must be numbers") from None
        return start, end

    @app.post("/api/recorder/takes/{tid}/draft")
    def recorder_draft(tid: str, body: dict = Body(default={})):
        """The trimmed take as unsaved keyframes, for the preview."""
        return recorder.keyframes(tid, *trim_of(body), name=str(body.get("name") or "Recording")[:80])

    @app.post("/api/recorder/takes/{tid}/save")
    def recorder_save(tid: str, body: dict = Body(...)):
        start, end = trim_of(body)
        kind = body.get("kind") if body.get("kind") in store.KINDS else "emote"
        return recorder.save(tid, str(body.get("name") or ""), start, end, kind,
                             str(body.get("description") or "")[:300])

    # ---- animation generator (the model on the GX10) --------------------------------

    @app.post("/api/generate")
    def generate(body: dict = Body(...)):
        """A text prompt -> an unsaved animation. Falls back to a labelled mock when the
        GX10 can't be reached, so this never fails for a network reason."""
        return generator.generate(str(body.get("prompt") or ""), force_mock=bool(body.get("mock")))

    # ---- calibration and debug ------------------------------------------------------

    def activate(pid: str) -> dict:
        profile = profiles.set_active(pid)
        gateway.limiter.use(profile)
        hub.publish("calibration", calibration_state())
        return profile

    @app.get("/api/calibration")
    def calibration_get():
        return {"profiles": profiles.list(), **calibration_state(),
                "bounds": {"max_offset": MAX_OFFSET, "min_speed": MIN_SPEED, "max_speed": SPEED_CAP}}

    @app.post("/api/calibration")
    def calibration_create(body: dict = Body(...)):
        saved = profiles.save(body)
        if body.get("activate"):
            activate(saved["id"])
        return saved

    @app.put("/api/calibration/{pid}")
    def calibration_save(pid: str, body: dict = Body(...)):
        profiles.get(pid)  # 404 if it doesn't exist
        saved = profiles.save(body, pid)
        if profiles.active_id() == pid:
            activate(pid)  # the edited profile takes effect at once
        return saved

    @app.delete("/api/calibration/{pid}")
    def calibration_delete(pid: str):
        was_active = profiles.active_id() == pid
        profiles.delete(pid)
        if was_active:
            activate(FACTORY)
        return {"deleted": pid, **calibration_state()}

    @app.post("/api/calibration/active")
    def calibration_activate(body: dict = Body(...)):
        return activate(str(body.get("id") or ""))

    @app.get("/api/debug/servos")
    def debug_servos():
        """Per-servo readout for tuning: commanded against actual, temperature, voltage,
        torque, and the active profile's limits. Reads the robot once."""
        error, status = None, None
        try:
            status = gateway.call("servo/status")
        except Refused as e:
            error = str(e)
        command = (twin.state.snapshot()["channels"]["command"] or {}).get("servos")
        return {"servos": gateway.limiter.table(status, command), "error": error, "t": time.time(),
                "max_allowed_c": (status or {}).get("max_allowed_c"), **calibration_state()}

    @app.post("/api/debug/jog")
    def debug_jog(body: dict = Body(...)):
        """Move one arm or head-free servo to a pulse, through every limit. Legs are
        refused: moving one leg joint at a time makes the robot fall."""
        joint = str(body.get("servo") or "")
        if joint not in KEYFRAME_JOINTS:
            raise HTTPException(422, f"Only the arm joints can be jogged from here: {', '.join(KEYFRAME_JOINTS)}.")
        pulse = body.get("pulse")
        if isinstance(pulse, bool) or not isinstance(pulse, (int, float)):
            raise HTTPException(422, "pulse must be a number")
        ready_to_move()
        if player.playing:
            raise Refused("An animation is playing on the robot. Stop it first.")
        result = gateway.call("servo/set_many", targets={joint: pulse}, ms=int(body.get("ms") or 600), wait=False)
        safe = gateway.limiter.clamp_pose({joint: pulse})
        twin.state.set_command(safe, source="dashboard", label="playing on robot")
        return result

    @app.post("/api/playback/stop")
    def playback_stop():
        player.stop()
        return {"stopping": True}

    @app.post("/api/record/coast")
    def record_coast():
        """Let the arms go limp so they can be posed by hand (legs keep holding)."""
        return gateway.call("servo/coast", servos=list(ARM_SERVOS))

    @app.post("/api/record/hold")
    def record_hold():
        return gateway.call("servo/hold", servos=list(ARM_SERVOS))

    # ---- plans --------------------------------------------------------------------

    @app.get("/api/plans")
    def plan_list():
        return plans.list()

    @app.get("/api/exercises")
    def exercise_library():
        """Everything that can be dropped into a session: each saved animation as an
        exercise, then any exercise only a plan has. Names are unique, animations win."""
        out, seen = [], set()
        for row in animations.list():
            if row.get("unreadable"):
                continue
            try:
                ex = store.as_exercise(animations.get(row["id"]))
            except (PlanError, ValueError, KeyError, TypeError):
                continue
            seen.add(ex["name"])
            out.append({**ex, "kind": row["kind"], "source": "animation"})
        for p in plans.list():
            for ex in p["exercises"]:
                if ex["name"] not in seen:
                    seen.add(ex["name"])
                    out.append({**ex, "kind": "exercise", "source": "plan"})
        return out

    @app.put("/api/plans/{name}")
    def plan_save(name: str, body: dict = Body(...)):
        return plans.save(name, body)

    # ---- sessions and the coach ---------------------------------------------------

    def with_summary(session: dict) -> dict:
        sid = session["id"]
        cached = session.get("summary")
        session["summary"] = cached or coach.template(session, sessions.history(session.get("started") or 0))
        session["summary_cached"] = bool(cached)
        session["summary_pending"] = sid in coach.pending
        return session

    @app.get("/api/sessions")
    def session_list():
        return sessions.list()

    @app.post("/api/sessions/synthetic")
    def session_seed(body: dict = Body(default={})):
        count = max(1, min(20, int(body.get("count") or 8)))
        return {"created": synthetic.seed_sessions(sessions, count, int(body.get("seed") or 7))}

    @app.delete("/api/sessions/synthetic")
    def session_unseed():
        return {"deleted": sessions.delete_synthetic()}

    @app.get("/api/analytics/trends")
    def trends():
        """Scored exercise sessions, oldest first, for the session-over-session charts."""
        rows = [r for r in sessions.list() if not r.get("unreadable") and r["kind"] == "exercise"
                and r["scores"]["overall"]["score"] is not None]
        return [{"id": r["id"], "started": r["started"], "status": r["status"], "synthetic": r["synthetic"],
                 "overall": r["scores"]["overall"]["score"], "accuracy_pct": r["scores"]["accuracy_pct"],
                 "pacing_pct": r["scores"]["pacing_pct"], "smoothness_pct": r["scores"]["smoothness_pct"],
                 "symmetry_pct": r["scores"]["symmetry_pct"], "reps": r["scores"]["reps"],
                 "target_reps": r["scores"]["target_reps"]} for r in reversed(rows)]

    @app.delete("/api/sessions/{sid}")
    def session_delete(sid: str):
        if live.running and (live.state.get("last") or {}).get("id") == sid:
            raise Refused("That session is still being written.")
        sessions.delete(sid)
        hub.publish("sessions", {"changed": sid, "deleted": True})
        return {"deleted": sid}

    @app.get("/api/sessions/{sid}/replay")
    def session_replay(sid: str, set: int = 0):
        """What the patient did (or what was recorded), solved for the twin: one frame
        per trace sample. Arms only: the tracked angle drives the shoulders, and the
        elbows follow when the trace has them."""
        session = sessions.get(sid)
        if session["kind"] == "recording":
            anim = animations.get(store.check_id(str((session.get("recording") or {}).get("animation_id") or "")))
            return {**preview_solve({"keyframes": anim["keyframes"]}), "name": anim["name"], "kind": "recording"}
        sets = session["sets"] + session["interrupted"]
        if not 0 <= set < len(sets) or not sets[set].get("trace"):
            raise store.NotFound("a joint-angle trace for that set")
        s = sets[set]
        trace, joints = s["trace"], s["trace"].get("joints") or {}
        elbow_tracked = s.get("track_joint") == "elbow"
        n = min(len(trace["patient"]), MAX_SOLVE_POSES)
        pose_list, last = [], {"servos": keyframes.start_pose()["servos"]}

        def at(name: str, i: int, fallback):
            row = joints.get(name)
            return row[i] if row and i < len(row) and row[i] is not None else fallback

        for i in range(n):
            a = trace["patient"][i]
            if a is not None:
                servos = {}
                for side in ("left", "right"):
                    if elbow_tracked:
                        servos.update(poses.arm(side, forward=25, elbow=a))
                    else:
                        bend = max(0.0, 175 - at(f"{side}_elbow", i, 175))
                        servos.update(poses.arm(side, forward=at(f"{side}_shoulder", i, a), elbow=bend))
                last = {"servos": servos}
            pose_list.append(last)
        return {"hz": trace["hz"], "total_ms": round(n / trace["hz"] * 1000), "frames": solve(pose_list),
                "name": f"{s['exercise']}, set {s['set']}", "kind": "exercise", "set": set}

    @app.get("/api/sessions/{sid}")
    def session_get(sid: str, weight_kg: float | None = None):
        session = with_summary(sessions.get(sid))
        if weight_kg and 20 <= weight_kg <= 250:
            session["calories"] = calories(session, weight_kg)
        return session

    @app.post("/api/sessions/{sid}/summary")
    def session_summarize(sid: str):
        """(Re)generate the coach summary in the background; the result arrives as a
        "summary" event and is cached in the session file."""
        sessions.get(sid)
        return {"started": coach.request(sid), "pending": True}

    @app.post("/api/chat")
    def coach_chat(body: dict = Body(...)):
        """Chat with the coach about one session. Streams Server-Sent Events (see chat.py)."""
        messages = clean_messages(body.get("messages"))
        if not messages or messages[-1]["role"] != "user":
            raise HTTPException(422, "Send at least one message, ending with the user's question.")
        sid = body.get("session_id")
        if sid:
            sessions.get(store.check_id(str(sid)))  # 404 before the stream starts
        return StreamingResponse(chat.stream(str(sid) if sid else None, messages),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---- mirror: the robot copies the patient's arms ------------------------------

    @app.post("/api/mirror/start")
    def mirror_start():
        return mirror.start()

    @app.post("/api/mirror/stop")
    def mirror_stop():
        mirror.stop()
        return {"stopping": mirror.running}

    @app.get("/api/mirror")
    def mirror_state():
        return mirror.snapshot()

    @app.get("/api/mirror.jpg", response_class=Response)
    def mirror_frame(width: int = 640):
        jpeg = mirror.jpeg(max(160, min(1280, width)))
        if jpeg is None:
            raise Refused("Mirror isn't running.", 404)
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---- live session and coaching ------------------------------------------------

    @app.post("/api/live/start")
    def live_start(body: dict = Body(...)):
        if mirror.running:
            raise Refused("Mirror is running. Stop it first.")
        if player.playing:
            raise Refused("An animation is playing on the robot. Stop it first.")
        simulate, pace = bool(body.get("simulate", True)), float(body.get("pace") or 1.0)
        if body.get("exercises") is not None:  # a plan built on the spot in Start a session
            plan = parse_plan({"name": str(body.get("name") or "Custom session")[:80],
                               "exercises": body["exercises"]}, source="session")
            if body.get("save_as"):
                plans.save(store.check_id(str(body["save_as"])), {"name": plan.name, "exercises": body["exercises"]})
            return live.start(str(body.get("save_as") or "custom"), simulate, pace, body.get("seed"), plan=plan)
        try:
            return live.start(store.check_id(str(body.get("plan") or "")), simulate, pace, body.get("seed"))
        except FileNotFoundError:
            raise store.NotFound(body.get("plan")) from None

    @app.post("/api/live/answer")
    def live_answer(body: dict = Body(...)):
        live.answer(str(body.get("text") or "")[:300])
        return {"ok": True}

    @app.post("/api/live/stop")
    def live_stop():
        live.stop()
        return {"stopping": live.running}

    @app.post("/api/live/reposition")
    def live_reposition(body: dict = Body(...)):
        return live.reposition(str(body.get("kind")), int(body.get("steps") or 1))

    @app.post("/api/live/frame")
    def live_frame(body: dict = Body(...)):
        """Where the patient is in the camera frame. The pose tracker will post this;
        until then the dry-run panel does."""
        return live.set_frame(**body)

    # ---- settings -----------------------------------------------------------------

    @app.put("/api/settings")
    def settings_put(body: dict = Body(...)):
        if body.get("ollama_url"):
            settings.ollama_url = ollama_url(str(body["ollama_url"])[:200])
            settings.stub_llm = False
        if body.get("model"):
            settings.model = str(body["model"]).strip()[:80]
        monitor.poll_llm()
        monitor.publish()
        return {"ollama_url": settings.ollama_url, "model": settings.model, "llm": monitor.llm}

    @app.post("/api/tunnel")
    def tunnel_open(body: dict = Body(...)):
        try:
            info = tunnel.open(str(body.get("host") or ""), str(body.get("user") or "asus"),
                               body.get("password") or None)
        except TunnelError as e:
            raise HTTPException(502, str(e)) from None
        settings.ollama_url, settings.stub_llm = f"http://127.0.0.1:{info['port']}", False
        monitor.poll_llm()
        monitor.publish()
        return info

    @app.delete("/api/tunnel")
    def tunnel_close():
        tunnel.close()
        return tunnel.status()

    # ---- dry-run controls ---------------------------------------------------------

    if settings.dry_run:
        @app.post("/api/dev/fake")
        def dev_fake(body: dict = Body(...)):
            """Dry run only: set the fake robot's temperatures, battery or a stuck servo."""
            from robot_api.fake import STATE
            from robot_api.servo.joints import resolve

            for servo, temp in (body.get("temps") or {}).items():
                STATE.temps[resolve(servo)] = int(temp)
            if body.get("battery_mv"):
                STATE.battery_mv = int(body["battery_mv"])
            if "stall" in body:
                gateway.fake_stall = {resolve(k): int(v) for k, v in (body["stall"] or {}).items()}
            if body.get("reset"):
                STATE.reset()
                gateway.fake_stall = {}
            monitor.poll_robot()
            monitor.publish()
            return monitor.snapshot()

    # ---- the web app --------------------------------------------------------------

    index = os.path.join(settings.web_dir, "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        if path.startswith(("api/", "twin/")):
            raise HTTPException(404, "not found")
        target = os.path.normpath(os.path.join(settings.web_dir, path))
        if path and target.startswith(os.path.normpath(settings.web_dir)) and os.path.isfile(target):
            return FileResponse(target)
        if "." in path.rsplit("/", 1)[-1]:
            raise HTTPException(404, "not found")  # a missing file, not a page of the app
        if os.path.isfile(index):
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "The web app isn't built. Run: cd dashboard/web && npm install "
                             "&& npm run build"}, status_code=503)

    return app
