"""The dashboard's FastAPI app: robot proxy, twin, file stores, coach jobs, live events.

    create_app(Settings(dry_run=True))     # everything fake; what the tests use

Every robot refusal reaches the browser as {"detail": "<the robot's own message>"}
with the robot API's status (409 for a safety refusal), never a generic error.
"""
from __future__ import annotations

import os

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from robot_api.errors import RobotError
from workout import keyframes
from workout.plan import EASINGS, KEYFRAME_JOINTS, PlanError, check_keyframes
from workout.session import calories

from . import store, synthetic
from .coach import Coach
from .config import Settings, ollama_url
from .events import Hub
from .live import Live
from .monitor import Monitor
from .player import Player
from .robot import ARM_SERVOS, Gateway, Refused
from .safety import LATCHING, Safety
from .tunnel import Tunnel, TunnelError


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
    coach = Coach(settings, sessions, hub)
    monitor = Monitor(settings, gateway, safety, coach, hub)
    tunnel = Tunnel(hub)
    twin = mount_twin(app, "/twin")
    player = Player(gateway, twin.state, hub)
    live = Live(settings, gateway, safety, sessions, hub)
    safety.on_trip.append(lambda kind, entry: player.stop())
    app.state.parts = dict(settings=settings, hub=hub, gateway=gateway, safety=safety, sessions=sessions,
                           animations=animations, plans=plans, coach=coach, monitor=monitor,
                           twin=twin, player=player, live=live, tunnel=tunnel)

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
        @app.on_event("startup")
        def _start():
            if settings.dry_run and not sessions.ids():
                synthetic.seed_sessions(sessions)
            twin.attach_robot(gateway, hz=4.0, label="dry run" if settings.dry_run else settings.robot_url)
            monitor.start()

        @app.on_event("shutdown")
        def _stop():
            monitor.stop()
            tunnel.close(publish=False)

    # ---- status and events --------------------------------------------------------

    @app.get("/api/status")
    def status():
        return {**monitor.snapshot(), "safety": safety.snapshot(), "live": live.state,
                "playback": player.state, "tunnel": tunnel.status(),
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

    @app.post("/api/playback/start")
    def playback_start(body: dict = Body(...)):
        check_keyframes(body.get("keyframes"))
        if live.running:
            raise Refused("A session is running. End it before playing an animation.")
        if monitor.robot["state"] == "unknown":
            monitor.poll_robot()
        motion = monitor.motion()
        if not motion["ok"]:
            raise Refused(" ".join(motion["reasons"]))
        return player.start(body["keyframes"], float(body.get("speed") or 1.0), bool(body.get("loop")),
                            str(body.get("name") or ""))

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

    # ---- live session and coaching ------------------------------------------------

    @app.post("/api/live/start")
    def live_start(body: dict = Body(...)):
        if player.playing:
            raise Refused("An animation is playing on the robot. Stop it first.")
        try:
            return live.start(store.check_id(str(body.get("plan") or "")), bool(body.get("simulate", True)),
                              float(body.get("pace") or 1.0), body.get("seed"))
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
        if os.path.isfile(index):
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "The web app isn't built. Run: cd dashboard/web && npm install "
                             "&& npm run build"}, status_code=503)

    return app
