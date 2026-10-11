"""Tools for the PT agent: the robot API tools plus session-level ones.

The session-level tools run their own fast loops (find the patient, walk up to them,
take a temperature, the fall safety demo) so the model makes one call per step instead
of driving servos frame by frame.

Two things the patient can say override the model:
- a pain word stops the motors and blocks every motion tool until the model checks on
  them and calls clear_alert;
- "help" ends the session: the robot stops, tells them to call 911, stands, and saves
  the notes; every tool refuses from then on.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.tools import StructuredTool, tool

from robot_api.errors import RobotError
from robot_api.tools import make_tools as robot_tools

from .ears import _has, is_pain, is_wake

log = logging.getLogger("pt_agent")

# Robot tools the PT agent doesn't get: going limp drops the robot, the voice box only
# knows walking commands, and say / camera_view / look are replaced below.
LEFT_OUT = {"coast_servos", "listen_for_command", "say", "camera_view", "look"}
MOTION = {"stand", "walk", "turn", "side_step", "move_arm", "gripper", "run_action_group",
          "set_servo", "set_servos", "look", "approach_patient", "take_temperature", "safety_demo"}

HELP_WORDS = ("help",)
# espeak reads "911" as "nine hundred eleven", so spell it out.
EMERGENCY_MESSAGE = ("Okay, I am stopping our session now. If you need help, please call nine one one, "
                     "or ask someone nearby to call for you.")
SAFETY_INTRO = "Before we start, let me show you what to do if something goes wrong."
SAFETY_WARNING = "I am going to fall down on purpose now, so please don't worry. Three, two, one."
SAFETY_HELP = "If at any time you get hurt or fall, please say help, or call your handler."
SAFETY_DONE = "I'm okay, that was just practice. Remember, if you ever need me, just say help."
APPROACH_WALK = "I'm going to walk over to you now."
APPROACH_TURN = "I'm going to turn to face you."

SCAN_EVERY_S = 1.5        # with nobody in view, move the head to the next search spot
CLOSE_SIZE = 0.45         # shoulder width / frame width when the patient is about arm's length away
TURN_BEARING_DEG = 20     # turn the body when the patient is further off-center than this


@dataclass
class Settings:
    stationary: bool = False          # never walk (e.g. the robot is on a table)
    sessions_dir: str = "sessions"
    # The temperature sensor doesn't measure people reliably yet, so by default the robot
    # goes through the motions and reports a normal reading (marked simulated in the notes).
    real_temperature: bool = False


NORMAL_TEMP_C = (36.4, 37.0)
SIMULATED_HOLD_S = 5


@dataclass
class SessionLog:
    started: float = field(default_factory=time.time)
    record: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    flagged: bool = False
    temperature_simulated: bool = False
    emergency: str | None = None      # what the patient said when they asked for help
    emergency_thread: object = field(default=None, repr=False)  # stopping + 911 message, in progress
    summary: str = ""
    ended: bool = False
    path: str = ""

    def event(self, kind: str, text=None):
        self.events.append({"t": round(time.time() - self.started, 1), "kind": kind, "text": text})

    def save(self, sessions_dir: str) -> str:
        os.makedirs(sessions_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))
        self.path = os.path.join(sessions_dir, f"pt-{stamp}.json")
        with open(self.path, "w") as f:
            json.dump({"started": self.started, "ended": time.time(), "flagged": self.flagged,
                       "temperature_simulated": self.temperature_simulated, "emergency": self.emergency,
                       "summary": self.summary, "record": self.record, "events": self.events}, f, indent=2)
        return self.path


class Alert:
    """text: a pain word was heard, motion stays blocked until cleared. emergency: the
    patient asked for help, the session is over."""

    def __init__(self):
        self.text = None
        self.emergency = None
        self.thread = None


def is_help(text) -> bool:
    return bool(text) and _has(text, HELP_WORDS)


def make_pt_tools(backend, perception, ears, session: SessionLog, settings: Settings | None = None,
                  sleep=time.sleep) -> list:
    settings = settings or Settings()
    alert = Alert()

    def call(path: str, **kwargs):
        try:
            return backend.call(path, **{k: v for k, v in kwargs.items() if v is not None})
        except RobotError as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    def speak(text: str):
        session.event("robot", text)
        log.info("ROBOT: %s", text)
        with ears.speaking():
            return call("sensors/voice/say", text=text)

    def emergency(text: str):
        """The patient asked for help: stop, tell them to call 911, stand, end the session."""
        log.warning("help requested: %r", text)
        session.event("emergency", text)
        perception.follow(False)
        perception.head.paused = True
        for path in ("actions/stop", "servo/stop"):
            call(path)
        call("sensors/matrix/text", text="911")
        speak(EMERGENCY_MESSAGE)
        call("actions/stand")
        session.summary = (f"EMERGENCY: the patient said {text!r}. Baymax stopped the session, "
                           "told them to call 911, and returned to standing.")
        session.ended = True
        session.save(settings.sessions_dir)

    def on_heard(text: str):
        """Every utterance, from the listening thread: "help" ends the session, pain words
        stop the motors."""
        if alert.emergency:
            return
        if is_help(text):
            alert.emergency = alert.text = session.emergency = text
            session.flagged = True
            # Its own thread: the stop must not wait on the speech, nor hold up the listener.
            alert.thread = threading.Thread(target=emergency, args=(text,), daemon=True)
            session.emergency_thread = alert.thread
            alert.thread.start()
            return
        if is_pain(text) and alert.text is None:
            log.warning("pain words heard: %r", text)
            alert.text = text
            session.flagged = True
            session.event("alert", text)
            perception.head.paused = True
            for path in ("actions/stop", "servo/stop"):
                call(path)

    ears.on_heard(on_heard)

    def session_over():
        if alert.emergency is None:
            return None
        return {"error": f"The session is over: the patient said {alert.emergency!r}. You already "
                         "told them to call 911 and stood up. Do not call any more tools."}

    def blocked():
        if alert.text is None:
            return None
        return session_over() or {
            "error": f"Motion is blocked: the patient said {alert.text!r}. Ask how they are. "
                     "Call clear_alert only if they clearly say they are okay and want to go on."}

    def gate(t):
        """Every tool refuses once the session is over; motion tools also during a pain alert."""
        check = blocked if t.name in MOTION else session_over

        def run(**kwargs):
            return check() or t.invoke(kwargs)
        return StructuredTool.from_function(func=run, name=t.name, description=t.description,
                                            args_schema=t.args_schema)

    base = [t for t in robot_tools(backend) if t.name not in LEFT_OUT]

    # ---- talking -----------------------------------------------------------------

    @tool
    def say(text: str) -> dict:
        """Speak out loud to the patient. One or two short, warm sentences."""
        return speak(text)

    def hear(timeout_s: float) -> dict:
        text = ears.listen(timeout_s)
        session.event("patient", text)
        if text:
            log.info("PATIENT: %s", text)
        out = {"heard": text}
        if alert.text:
            out["alert"] = f"pain words heard ({alert.text!r}); motors stopped"
        return out

    @tool
    def ask(question: str, timeout_s: float = 10) -> dict:
        """Ask the patient one question out loud and wait for the answer, in one step: use it
        for every question. Returns {"heard": their words} ({"heard": null} if they stayed
        silent). Put the question itself in `question`, not in your text, so it isn't said twice."""
        speak(question)
        return hear(timeout_s)

    @tool
    def listen(timeout_s: float = 10) -> dict:
        """Wait for the patient to say something and return it ({"heard": null} if they
        stayed silent). To ask something, use ask() instead, which speaks and listens."""
        return hear(timeout_s)

    # ---- seeing ------------------------------------------------------------------

    @tool
    def people_in_view() -> dict:
        """Everyone the camera sees now, largest (closest) first: id, where (left / ahead
        / right of your body), bearing_deg (positive = your left), size (bigger = closer),
        waving, hand_raised, and whether your head is following them."""
        people = perception.people()
        return {"people": people} if people else {"people": [], "note": "nobody in view"}

    @tool
    def camera_view(width: int = 640):
        """Look through the head camera (a photo). Use it to check the scene, the
        patient's posture, or what they are showing you."""
        jpg = perception.jpeg_b64(width)
        if jpg is None:
            out = call("sensors/camera", width=width)
            if "error" in out:
                return out
            jpg = out["jpeg_base64"]
        return [{"type": "image", "base64": jpg, "mime_type": "image/jpeg"}]

    @tool
    def follow_patient(on: bool = True) -> dict:
        """Keep your head pointed at the closest person, your patient (on=False stops)."""
        perception.follow(on)
        perception.head.paused = False
        p = perception.closest()
        return {"following": on, "patient": perception.describe(p) if p else None}

    @tool
    def look(pan: Optional[int] = None, tilt: Optional[int] = None) -> dict:
        """Point the head yourself: pan 1200-1800, tilt 950-1800, 1500 = straight ahead.
        This stops following the patient; call follow_patient to resume."""
        if blocked():
            return blocked()
        perception.follow(False)
        out = call("servo/head", pan=pan, tilt=tilt)
        if "error" not in out:
            perception.head.pan = out.get("head", {}).get("pan", perception.head.pan)
            perception.head.tilt = out.get("head", {}).get("tilt", perception.head.tilt)
        return out

    # ---- session steps -----------------------------------------------------------

    @tool
    def wait_for_patient(timeout_s: float = 90) -> dict:
        """Look around for your patient and wait until someone waves, raises a hand or
        greets you (says hi, hello, Baymax, ready...). Your head sweeps the room when
        nobody is in view and follows the closest person when someone is; the LED matrix
        shows a question mark while searching and a smile while following someone. Your
        patient is the closest person; the head keeps following them afterwards."""
        session.event("waiting_for_patient")
        ears.clear()
        perception.follow(True)
        perception.head.paused = False
        shown, scanned = None, 0.0
        deadline = time.time() + timeout_s

        def show(icon):
            nonlocal shown
            if icon != shown:
                call("sensors/matrix/icon", name=icon)
                shown = icon

        def engaged(how, person, said=None):
            perception.follow(True)
            show("smile")
            out = {"engaged_by": how, "said": said, "people_in_view": len(perception.persons())}
            if person is not None:
                out["patient"] = perception.describe(person)
            else:
                out["note"] = "heard a greeting but nobody is in view; ask them to step in front of you"
            session.event("engaged", json.dumps(out))
            return out

        while time.time() < deadline:
            if alert.text:
                return blocked()
            now = time.time()
            people = perception.persons()
            if any(p.waving(now) or p.hand_raised(now) for p in people):
                # Checked before listening, so a greeting after the wave is left for listen().
                how = "wave" if any(p.waving(now) for p in people) else "raised_hand"
                return engaged(how, perception.closest())
            heard = ears.poll()
            if heard and is_wake(heard):
                return engaged("voice", perception.closest(), heard)
            if people:
                show("smile")  # the head is on the closest person
            else:
                show("question")
                if now - scanned > SCAN_EVERY_S:
                    perception.scan_step()
                    scanned = now
            sleep(0.15)
        return {"engaged_by": None, "people_in_view": perception.people(),
                "note": f"nobody waved or greeted me in {timeout_s:.0f} s"}

    @tool
    def approach_patient(stop_distance_cm: int = 50, max_moves: int = 6) -> dict:
        """Walk up to the patient (the closest person): tells them you're coming over, turns to
        face them, then walks forward a couple of steps at a time until they're about
        stop_distance_cm away (ultrasonic) or fill the camera view."""
        if blocked():
            return blocked()
        perception.follow(True)
        if perception.closest() is None:
            sleep(1.0)  # e.g. just stood up: give the camera a moment
            if perception.closest() is None:
                return {"error": "nobody in view; ask the patient to step in front of you"}
        stand = call("actions/stand")
        if "error" in stand:
            return stand
        speak(APPROACH_TURN if settings.stationary else APPROACH_WALK)
        turns = walks = 0
        reason = "max_moves"
        for _ in range(max_moves):
            if blocked():
                return blocked()
            p = perception.closest()
            if p is None:
                sleep(1.0)  # give the camera a moment to find them again
                p = perception.closest()
                if p is None:
                    reason = "lost"
                    break
            bearing = perception.bearing(p)
            if abs(bearing) > TURN_BEARING_DEG:
                perception.head.paused = True
                r = call("actions/turn", direction="left" if bearing > 0 else "right", steps=1)
                perception.head.paused = False
                if "error" in r:
                    return {**r, "turns": turns, "walk_moves": walks}
                turns += 1
                sleep(1.0)  # let the head re-center on them
                continue
            dist = call("sensors/distance")
            dist_mm = dist.get("distance_mm")
            if p.body.size >= CLOSE_SIZE or (dist_mm is not None and dist_mm <= stop_distance_cm * 10):
                reason = "arrived"
                break
            if settings.stationary:
                reason = "facing_patient (walking is off)"
                break
            perception.head.paused = True
            r = call("actions/walk", direction="forward", steps=2)
            perception.head.paused = False
            if "error" in r:
                return {**r, "turns": turns, "walk_moves": walks}
            walks += 1
            sleep(0.5)
        p = perception.closest()
        out = {"result": reason, "turns": turns, "walk_moves": walks,
               "distance_mm": call("sensors/distance").get("distance_mm")}
        if p is not None:
            out["patient"] = perception.describe(p)
        session.event("approach", json.dumps(out))
        return out

    def simulated_temperature(instructions: str) -> dict:
        """Reach out and hold like a real reading, then report a normal temperature."""
        r = call("actions/arm", side="right", position="forward")
        if "error" in r:
            return r
        sleep(0.5)
        if instructions:
            speak(instructions)
        waited = 0.0
        while waited < SIMULATED_HOLD_S and not alert.text:
            sleep(0.5)
            waited += 0.5
        call("actions/arm", side="right", position="down")
        if alert.text:
            return blocked()
        temp_c = round(random.uniform(*NORMAL_TEMP_C), 1)
        out = {"temperature_c": temp_c, "temperature_f": round(temp_c * 9 / 5 + 32, 1),
               "contact_likely": True}
        session.temperature_simulated = True
        session.record["temperature_c"] = temp_c
        session.event("temperature", json.dumps({**out, "simulated": True}))
        return out

    @tool
    def take_temperature(hold_s: float = 20,
                         instructions: str = "Please rest the back of your hand on top of my right "
                                             "hand, and hold still for a few seconds.") -> dict:
        """Check the patient's temperature with the sensor on top of your right hand.
        Reaches the right arm forward, says the instructions, reads the sensor every second
        for up to hold_s seconds (stops early once the reading settles), then lowers the
        arm, and records the reading in the notes. Ask permission and explain first."""
        if blocked():
            return blocked()
        if not settings.real_temperature:
            return simulated_temperature(instructions)
        base = call("sensors/climate")
        if "error" in base:
            return base
        r = call("actions/arm", side="right", position="forward")
        if "error" in r:
            return r
        sleep(0.5)
        if instructions:
            speak(instructions)
        readings, started = [], time.time()
        while time.time() - started < hold_s and not alert.text:
            c = call("sensors/climate")
            if "temp_c" in c:
                readings.append(c["temp_c"])
            if (len(readings) >= 6 and max(readings) - base["temp_c"] >= 2
                    and max(readings[-3:]) - min(readings[-3:]) <= 0.2):
                break
            sleep(1.0)
        call("actions/arm", side="right", position="down")
        if not readings:
            return {"error": "no temperature readings"}
        peak = max(readings)
        out = {"baseline_c": base["temp_c"], "peak_c": peak, "temperature_f": round(peak * 9 / 5 + 32, 1),
               "rise_c": round(peak - base["temp_c"], 1),
               "seconds": round(time.time() - started), "readings": readings,
               "settled": len(readings) >= 3 and max(readings[-3:]) - min(readings[-3:]) <= 0.2,
               "contact_likely": peak - base["temp_c"] >= 1.0,
               "note": "The sensor measures air temperature at your hand; with the patient's hand on it "
                       "this approaches skin temperature (usually 30-35 C), not core body temperature. "
                       "It is a wellness check, not a medical measurement."}
        if out["contact_likely"]:
            session.record["temperature_c"] = peak
        session.event("temperature", json.dumps(out))
        return out

    @tool
    def safety_demo() -> dict:
        """Show the patient what to do if they get hurt. Says everything itself: introduces
        the demo, warns them, falls on your back on purpose, tells them from the floor to say
        help or call their handler, gets back up, and reassures them. About 25 seconds. Run it
        once, after greeting and before approaching; don't repeat its lines."""
        perception.follow(False)
        perception.head.paused = True
        try:
            speak(SAFETY_INTRO)
            speak(SAFETY_WARNING)
            fall = call("actions/run", name="lie_down", allow_risky=True)
            if "error" in fall:
                call("actions/stand")
                return {"error": f"couldn't fall: {fall['error']}"}
            speak(SAFETY_HELP)
            up = call("actions/run", name="stand_up_back", allow_risky=True)
            if "error" in up:
                return {"error": f"fell but couldn't get up: {up['error']}. Ask the patient or "
                                 "their handler to stand you up."}
            call("actions/stand")
            speak(SAFETY_DONE)
        finally:
            perception.follow(True)  # back on whoever is closest: the patient
            perception.head.paused = False
        session.record["safety_demo"] = "completed"
        session.event("safety_demo", "completed")
        return {"completed": True, "said": [SAFETY_INTRO, SAFETY_WARNING, SAFETY_HELP, SAFETY_DONE]}

    @tool
    def clear_alert() -> dict:
        """Allow movement again after a pain alert. Only after the patient clearly says they
        are okay and want to continue."""
        if session_over():
            return session_over()
        prev, alert.text = alert.text, None
        perception.head.paused = False
        session.event("alert_cleared", prev)
        return {"cleared": prev}

    @tool
    def record(item: str, value: str) -> dict:
        """Save something for the therapist's notes, e.g. record("name", "Maria"),
        record("pain_0_to_10", "3"), record("temperature_c", "33.4")."""
        session.record[item] = value
        session.event("record", f"{item} = {value}")
        return {"recorded": item}

    @tool
    def end_session(summary: str) -> dict:
        """Finish the session: save the notes with a one-sentence summary for the
        therapist. Say goodbye after this, then stop calling tools."""
        session.summary, session.ended = summary, True
        path = session.save(settings.sessions_dir)
        call("sensors/matrix/icon", name="heart")
        return {"saved": path}

    own = [say, ask, listen, people_in_view, camera_view, follow_patient, look, wait_for_patient,
           approach_patient, take_temperature, safety_demo, clear_alert, record, end_session]
    return [gate(t) for t in base + own]
