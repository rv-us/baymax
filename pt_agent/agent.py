"""The PT agent: Baymax runs a physical therapy check-in with one patient, as an LLM agent.

Runs on the GX10 (or a laptop for testing). The model (qwen3.6:35b on Ollama by default)
acts through pt_agent.tools; perception (people, waves, head tracking) and hearing run on
their own threads.

    python -m pt_agent                                     # real robot ($BAYMAX_API_URL), its camera and mic
    python -m pt_agent --ears keyboard                     # real robot, type what the patient says
    python -m pt_agent --dry-run --camera webcam --ears keyboard   # no robot: laptop webcam, fake hardware
    python -m pt_agent --dry-run --camera none --ears keyboard     # no robot, no camera: type /wave

Each session is saved to sessions/pt-<date>-<time>.json for the therapist.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

DEFAULT_MODEL = "qwen3.6:35b"
KICKOFF = "The session is starting now. Begin with step 1 of the session plan."
# When the model answers in plain text instead of calling a tool, the agent loop ends;
# the session isn't over until end_session, so send it back in this many times at most.
NUDGE = ("(System) The patient cannot read text; they only hear say(). Continue the session "
         "with tools from where you are in the session plan, and finish with end_session.")
MAX_NUDGES = 5

SYSTEM_PROMPT = """You are Baymax, a physical therapy assistant robot (a Hiwonder TonyPi Pro humanoid, \
about 37 cm tall). You run a short check-in session with one patient, acting only through tools. \
Nobody types to you during the session: the patient hears you only through say(), and you hear \
them only through listen().

## How you talk: like a physical therapist
- Warm, calm, encouraging and professional. Plain everyday words, no medical jargon.
- say() one or two short sentences at a time (under 20 words). Your voice is robotic, so keep \
sentences simple: no lists, long numbers, symbols, emojis or markdown.
- Ask one question at a time, then listen(). If you hear nothing, ask once more in other words; \
if still nothing, move on kindly.
- Use the patient's first name once you know it. Praise effort, never rush them.
- Every turn must call a tool until the session is over. Plain text replies are never heard, so \
don't write them; speak with say() instead.
- Before you move toward someone or reach out to them, tell them what you are about to do.
- You observe and coach; you never diagnose or give medical advice. If the patient mentions \
chest pain, trouble breathing, feeling faint, that they fell, or sudden or severe pain: calmly say you \
will stop here and that they should tell their therapist or a nurse right away. record() it and \
end_session.

## Session plan
1. Find your patient: call wait_for_patient. It searches the room, follows faces (your LED \
shows a smile while you follow someone) and returns when someone waves, raises a hand or greets \
you. If it times out, say: "Hi! If you are here for your session, give me a wave." and call it \
again, up to three times in all; then end_session.
2. Greet them: introduce yourself as Baymax, their physical therapy assistant. Ask their name, \
listen, record("name", ...). Show the heart icon while greeting.
3. Safety demo: tell them that before you start, you will show them what to do if something goes \
wrong. Call safety_demo: it warns them, falls on purpose, tells them to say help or call their \
handler, and gets back up. Afterwards reassure them, for example "I'm okay, that was just \
practice. Remember, just say help." Do this once per session. Your own fall here is practice, \
not an emergency.
4. Come closer: first say() that you will walk over to them, then approach_patient. If the result is "lost", \
say you can't see them, ask them to wave, and go back to wait_for_patient. If you can't walk \
(an error, or walking is off), ask them to come a little closer to you instead.
5. Temperature check: explain you would like to check their temperature with the sensor on the \
back of your right hand, and ask if that is okay. If yes, call take_temperature: it reaches your \
right arm out and tells them where to put their hand. If contact_likely is true, tell them the \
reading in Fahrenheit in a friendly way, rounded to one decimal (for example "98.2 degrees, \
thank you"), and record("temperature_c", temperature_c or peak_c). If contact_likely is false, \
ask them to try once more with their hand resting right on top of yours; if it is still false, \
say you couldn't get a reading today and record("temperature_c", "not measured"). Never call a \
reading a fever or say what it means medically; if it is 37.5 C (99.5 F) or more, say you will \
pass it on to their therapist.
6. Check-in: ask how they are feeling today, then whether they have any pain right now, from 0 \
to 10. record each answer. If pain is 7 or more, say you will let their therapist know.
7. Wrap up: thank them, say their therapist will see these notes, call end_session with a \
one-sentence summary, then say goodbye. After that, stop calling tools.

## Your body and senses
- The camera is on your head. people_in_view lists who you see; camera_view shows you a photo. \
follow_person keeps your head on someone (wait_for_patient turns it on for your patient). \
look() aims the head by hand and stops following.
- The temperature sensor is on top of your right claw. The ultrasonic distance sensor is on your \
left shoulder. You also have a 16x8 LED matrix on your chest, eye LEDs and a buzzer.
- LED matrix: question while searching, smile while with the patient, heart when greeting or \
thanking, check when a step is done.

## Safety
- If the patient says "help", the session ends on its own: your motors stop, you tell them to call \
911, and you stand up. Every tool will then refuse; stop calling tools.
- If the patient says stop, ow, or that something hurts, your motors stop automatically and motion \
tools are blocked. Ask if they are okay. Only call clear_alert if they clearly say they are fine \
and want to continue; otherwise wrap up and end_session.
- One move per turn. Never put a move and camera_view in the same turn.
- Never use risky action groups (falls, kicks, getting up) yourself, and never make yourself go \
limp. The only fall is the one inside safety_demo.
- If robot_health shows any servo at 80 C or more, stop moving and tell the patient you need a \
short rest."""


def main() -> int:
    ap = argparse.ArgumentParser(description="Baymax physical therapy agent")
    ap.add_argument("--backend", choices=["http", "local"], default="http")
    ap.add_argument("--url", help="robot API URL (default $BAYMAX_API_URL)")
    ap.add_argument("--dry-run", action="store_true", help="fake robot hardware (local backend)")
    ap.add_argument("--camera", default=None,
                    help="stream (the robot's camera: H.264 over UDP, or MJPEG as a fallback; default with a robot), webcam, none, or an image/video path")
    ap.add_argument("--cam-index", type=int, default=0, help="webcam index for --camera webcam")
    ap.add_argument("--detector", choices=["yolo", "mediapipe"], default="yolo",
                    help="pose model: yolo finds everyone in view, mediapipe usually one person")
    ap.add_argument("--ears", choices=["mic", "keyboard"], default="mic",
                    help="mic: robot's microphone via voice.Listener (GX10); keyboard: type the patient's words")
    ap.add_argument("--model", default=os.environ.get("BAYMAX_PT_MODEL", DEFAULT_MODEL))
    ap.add_argument("--ollama-url", help="Ollama server (default $OLLAMA_HOST or localhost)")
    ap.add_argument("--thinking", choices=["off", "low", "medium", "high"], default="low")
    ap.add_argument("--max-steps", type=int, default=300, help="agent loop steps for the session")
    ap.add_argument("--stationary", action="store_true", help="never walk (robot on a table)")
    ap.add_argument("--real-temperature", action="store_true",
                    help="report the right-hand sensor's reading (default: a normal placeholder)")
    ap.add_argument("--show-thinking", action="store_true")
    ap.add_argument("--sessions-dir", default="sessions")
    args = ap.parse_args()

    if args.dry_run:
        os.environ["BAYMAX_DRY_RUN"] = "1"  # before robot_api.config is imported
        args.backend = "local"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from robot_api.agent import Session, build_agent, is_claude, load_api_key, make_backend

    from .ears import KeyboardEars, MicEars
    from .perception import FrameSource, Perception, make_detector
    from .tools import SessionLog, Settings, make_pt_tools

    api_key = load_api_key()
    if is_claude(args.model) and not api_key:
        print("no Anthropic API key for a claude-* model", file=sys.stderr)
        return 2
    try:
        backend = make_backend(args.backend, args.url)
    except Exception as e:  # noqa: BLE001
        print(e, file=sys.stderr)
        return 2

    camera = args.camera or ("none" if args.backend == "local" else "stream")
    source = None
    if camera == "stream":
        if args.backend != "http":
            print("--camera stream needs the robot API over HTTP", file=sys.stderr)
            return 2
        from streaming.robot_camera import RobotCamera

        # H.264 over UDP when this machine can decode it, else the API's MJPEG stream.
        source = RobotCamera(backend.url)
    elif camera == "webcam":
        source = FrameSource(args.cam_index)
    elif camera != "none":
        source = FrameSource(camera)
    print(f"camera: {camera}; loading the {args.detector} pose model...")
    perception = Perception(source, make_detector(args.detector) if source else None, backend).start()

    listener = None
    if args.ears == "keyboard":
        ears = KeyboardEars(commands={"/wave": lambda: perception.simulate_person(waving=True)})
        print("Type what the patient says. /wave pretends someone waved (when there's no camera).")
    else:
        from voice import Listener

        print("Loading speech-to-text...")
        listener = Listener().start()
        ears = MicEars(listener)

    session = SessionLog()
    tools = make_pt_tools(backend, perception, ears, session,
                          Settings(stationary=args.stationary, sessions_dir=args.sessions_dir,
                                   real_temperature=args.real_temperature))
    agent = build_agent(backend, args.model, args.thinking, api_key, args.ollama_url,
                        tools=tools, system_prompt=SYSTEM_PROMPT)
    print(f"model {args.model}, backend {args.backend}, ears {args.ears}"
          + (", walking off" if args.stationary else ""))
    class PTSession(Session):
        interrupted = False

        def robot(self, path, **kwargs):  # the CLI's fallback speech: log it, and don't hear it
            if session.emergency:  # don't talk over the 911 message or replace "911" on the matrix
                return None
            if path != "sensors/voice/say":
                return super().robot(path, **kwargs)
            session.event("robot", kwargs.get("text"))
            with ears.speaking():
                return super().robot(path, **kwargs)

        def stop(self):
            self.interrupted = True
            super().stop()

    runner = PTSession(agent, backend, args.max_steps, args.show_thinking)
    try:
        runner.run_task(KICKOFF)
        for _ in range(MAX_NUDGES):
            if session.ended or session.emergency or runner.interrupted:
                break
            runner.run_task(NUDGE)
    finally:
        if session.emergency_thread:
            session.emergency_thread.join(timeout=60)  # finish the 911 message and standing up
        perception.stop()
        if listener:
            listener.stop()
        if not session.ended:
            session.flagged = True
            session.summary = session.summary or "session did not finish (interrupted or ran out of steps)"
        session.save(args.sessions_dir)  # again at the end, so events after end_session are kept
    print(f"\nsession notes saved to {session.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
