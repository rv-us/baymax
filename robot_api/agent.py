"""Baymax agent CLI: type a task, Claude drives the robot until it's done.

On the robot:      .venv/bin/python -m robot_api
From the laptop:   python -m robot_api --backend http --url http://169.233.72.93:8000
One-shot:          python -m robot_api -p "how far is the wall?"
Fake hardware:     python -m robot_api --dry-run

The API key comes from robot_api/.env (claude_key = ...), a repo-root .env, or
ANTHROPIC_API_KEY. Default model: Claude Haiku 5.5 with low thinking.
Commands: /stop /rest /status /clear /help /quit. Ctrl+C stops the current task.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

DEFAULT_MODEL = "claude-haiku-5-5"
THINKING_BUDGET = {"off": 0, "low": 1024, "medium": 4096, "high": 12000}
IMAGE_OMITTED = "[earlier camera image omitted]"
FEEDBACK_TOOLS = {"show_on_matrix", "set_eye_color", "beep"}

SYSTEM_PROMPT = """You are Baymax, a Hiwonder TonyPi Pro humanoid robot (about 37 cm tall). You act \
through tools; speak in first person. A human types tasks; finish each one with tool calls, then \
reply briefly.

## Your body
- 16 bus servos (pulse 0-1000, 500 = middle) named left_/right_ + ankle_roll, ankle_pitch, knee, \
hip_pitch, hip_roll, elbow, shoulder_roll, shoulder_pitch. Hands: left_hand, right_hand (small \
grippers; they only fit objects about 3 cm wide, a soda can is too big).
- Head pan/tilt (PWM, 1500 = center; lower tilt looks down, ~1000 sees your feet).

## Where your sensors are
- Camera: on top of your head. It sees where the head points; aim with look(), then camera_view.
- Ultrasonic distance sensor (and its RGB eye LEDs): on your LEFT SHOULDER, pointing forward. It \
measures ahead of your left side; keep the left arm down for a forward reading. Something only in \
front of your right side can be missed, so check with the camera too.
- Temperature sensor: on top of your RIGHT CLAW. It measures the air at your right hand; to sense \
how warm something is, bring the right hand close to it. Humidity is broken.
- Also: 16x8 LED dot matrix, speaker, microphone, voice-command box, buzzer, battery/servo health.

## Always narrate (required)
- Before you start a task, say() one short sentence about what you'll do.
- After each meaningful step (moved, found, measured, blocked), say() a short update. At most one \
say per step; under 12 words; never read out JSON or long number lists.
- End every task with a spoken result (a say() call after your last action).
- Your text replies are shown in a terminal: plain text, one or two short sentences, no markdown.
- Keep the LED matrix showing something useful (show_on_matrix):
  - a measurement you just took: the number (distance in cm, temperature in C);
  - walking or turning: icon up (forward) / down (back) / left / right;
  - looking around or searching: icon question;
  - done: icon check; failed: icon cross; too hot: text HOT; idle: icon smile.

## Safety
- Tools you call in the same turn run at the same time. Never combine a move (look, walk, turn, \
arm, gripper, servo) with camera_view or read_distance in one turn: move first, then sense in the \
next turn, so you see the result of the move. Never put two moves in one turn.
- Prefer high-level tools (stand, walk, turn, side_step, move_arm, gripper, run_action_group) \
over raw set_servo; use raw servos only for small deliberate adjustments (check joint_map).
- Joint limits are enforced but collisions are not: never move shoulder_roll inward while that arm \
is down at the side (it hits the leg). Never bend leg joints one at a time while standing.
- Stand before walking. Before walking forward, check read_distance and the camera; don't walk if \
something is closer than 200 mm. Watch for table edges in the camera.
- Servos heat up while holding a pose. Motion is refused at 83 C (hardware cutoff 85 C). If \
robot_health shows any servo at 80 C or more, stop moving, say so, and ask the human to hold you \
so you can rest.
- coast_servos (going limp) and risky action groups make you fall or move violently: ask the human \
first and only proceed after they confirm.
- If anything goes wrong, call stop_motion."""


# ---- config -------------------------------------------------------------------

def load_api_key() -> str | None:
    """API key from robot_api/.env or a repo-root .env (key claude_key), or
    ANTHROPIC_API_KEY. Existing environment variables win."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    here = os.path.dirname(os.path.abspath(__file__))
    if load_dotenv:
        for path in (os.path.join(here, ".env"), os.path.join(here, "..", ".env")):
            if os.path.isfile(path):
                load_dotenv(path, override=False)
    return (os.environ.get("claude_key") or os.environ.get("CLAUDE_KEY")
            or os.environ.get("ANTHROPIC_API_KEY"))


def make_backend(name: str, url: str | None):
    if name == "local":
        from .api import LocalBackend
        from .hardware import check_exclusive

        check_exclusive()
        return LocalBackend()
    from .client import HttpBackend

    return HttpBackend(url)


def uses_adaptive_thinking(model: str) -> bool:
    """Claude 5.x models take adaptive thinking + an effort level instead of a
    fixed token budget (they reject thinking.type=enabled)."""
    return re.match(r"claude-[a-z]+-5(\b|-|$)", model) is not None


def thinking_kwargs(model: str, thinking: str) -> dict:
    if uses_adaptive_thinking(model):
        if thinking == "off":
            return {"max_tokens": 4096, "thinking": {"type": "disabled"}}
        return {"max_tokens": 8192, "effort": thinking}  # low | medium | high
    budget = THINKING_BUDGET[thinking]
    kwargs = {"max_tokens": budget + 4096}
    if budget:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return kwargs


def build_agent(backend, model: str, thinking: str, api_key: str):
    from langchain.agents import create_agent
    from langchain_anthropic import ChatAnthropic

    from .tools import make_tools

    llm = ChatAnthropic(model=model, api_key=api_key, **thinking_kwargs(model, thinking))
    return create_agent(llm, make_tools(backend), system_prompt=SYSTEM_PROMPT)


# ---- message helpers ------------------------------------------------------------

def _blocks(msg) -> list:
    content = msg.content
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


def _text(msg) -> str:
    return "".join(b.get("text", "") for b in _blocks(msg)
                   if isinstance(b, dict) and b.get("type") == "text").strip()


def _has_image(msg) -> bool:
    return msg.type == "tool" and any(isinstance(b, dict) and b.get("type") == "image"
                                      for b in _blocks(msg))


def prune_images(history: list) -> list:
    """Keep only the newest camera image; older ones become a short note so
    the conversation doesn't grow by a photo every step."""
    image_idx = [i for i, m in enumerate(history) if not isinstance(m, dict) and _has_image(m)]
    for i in image_idx[:-1]:
        history[i] = history[i].model_copy(update={"content": IMAGE_OMITTED})
    return history


def trim_dangling(history: list) -> list:
    """Drop a trailing assistant turn whose tool calls never got results (the
    task was interrupted), so the next request is valid."""
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if not isinstance(m, dict) and m.type == "ai" and getattr(m, "tool_calls", None):
            ids = {tc["id"] for tc in m.tool_calls}
            answered = {getattr(x, "tool_call_id", None) for x in history[i + 1:]}
            return history if ids <= answered else history[:i]
    return history


# ---- the CLI -----------------------------------------------------------------------

class Session:
    def __init__(self, agent, backend, max_steps: int, show_thinking: bool):
        self.agent, self.backend = agent, backend
        self.max_steps, self.show_thinking = max_steps, show_thinking
        self.history: list = []

    def robot(self, path: str, **kwargs):
        """Direct robot call for CLI feedback; never let it break the loop."""
        try:
            return self.backend.call(path, **kwargs)
        except Exception as e:  # noqa: BLE001
            print(f"  (robot: {e})")
            return None

    def _show(self, msg, state: dict) -> None:
        if msg.type == "ai":
            for b in _blocks(msg):
                if self.show_thinking and isinstance(b, dict) and b.get("type") == "thinking":
                    print(f"  ~ {b.get('thinking', '').strip()}")
            for tc in getattr(msg, "tool_calls", None) or []:
                state["tools"].add(tc["name"])
                # Has it spoken since its last real action? Feedback-only tools
                # (matrix, eyes, beep) don't count as acting.
                if tc["name"] == "say":
                    state["spoke_last"] = True
                elif tc["name"] not in FEEDBACK_TOOLS:
                    state["spoke_last"] = False
                print(f"  -> {tc['name']}({json.dumps(tc['args'])})")
            text = _text(msg)
            if text:
                state["reply"] = text
                print(f"baymax: {text}")
        elif msg.type == "tool":
            if _has_image(msg):
                print("  <- [camera image]")
                return
            out = msg.content if isinstance(msg.content, str) else str(msg.content)
            state["error"] = "'error'" in out or '"error"' in out
            print(f"  <- {out[:200]}{'...' if len(out) > 200 else ''}")

    def run_task(self, text: str) -> None:
        from langgraph.errors import GraphRecursionError

        state = {"tools": set(), "reply": "", "error": False, "spoke_last": False}
        self.robot("sensors/matrix/icon", name="dots")
        self.history.append({"role": "user", "content": text})
        try:
            for update in self.agent.stream({"messages": self.history}, stream_mode="updates",
                                            config={"recursion_limit": self.max_steps}):
                for node in update.values():
                    for msg in (node or {}).get("messages", []):
                        self._show(msg, state)
                        self.history.append(msg)
        except KeyboardInterrupt:
            print("\n  interrupted: stopping the robot")
            self.stop()
            self.history = trim_dangling(self.history)
            return
        except GraphRecursionError:
            print(f"  gave up after {self.max_steps} steps")
            state["error"] = True
            self.history = trim_dangling(self.history)
        except Exception as e:  # noqa: BLE001  (API errors: report, keep the CLI alive)
            print(f"  task failed: {type(e).__name__}: {str(e)[:300]}")
            state["error"] = True
            self.history = trim_dangling(self.history)
            self.robot("sensors/matrix/icon", name="cross")
            self.robot("sensors/voice/say", text="Sorry, something went wrong.")
            return
        self.history = prune_images(self.history)

        # Guarantee a spoken result even if the model skipped it.
        if state["reply"] and not state["spoke_last"]:
            first = re.sub(r"[*_`#>]", "", state["reply"].split("\n")[0]).strip()
            self.robot("sensors/voice/say", text=first[:200])
        if "show_on_matrix" not in state["tools"]:
            self.robot("sensors/matrix/icon", name="cross" if state["error"] else "check")

    def stop(self) -> None:
        self.robot("servo/stop")
        self.robot("sensors/matrix/icon", name="cross")
        self.robot("sensors/voice/say", text="Stopping.")

    def status(self) -> None:
        bat = self.robot("sensors/battery") or {}
        temps = self.robot("sensors/servo_temps") or {}
        dist = self.robot("sensors/distance") or {}
        hot = temps.get("hottest") or {}
        print(f"  battery {bat.get('battery_v')} V ({bat.get('approx_pct')}%), hottest servo "
              f"{hot.get('joint')} {hot.get('temp_c')} C, distance {dist.get('distance_mm')} mm")

    def repl(self) -> None:
        print("Baymax ready. Type a task, or /help. Ctrl+C stops a running task.")
        self.robot("sensors/matrix/icon", name="smile")
        commands = {
            "/stop": self.stop,
            "/rest": lambda: print("  limp:", (self.robot("actions/rest") or {}).get("limp")),
            "/status": self.status,
            "/clear": lambda: (self.history.clear(), print("  conversation cleared")),
            "/help": lambda: print("  /stop  freeze servos   /rest  go limp (hold the robot!)\n"
                                   "  /status battery, heat, distance   /clear  forget the "
                                   "conversation\n  /quit"),
        }
        while True:
            try:
                line = input("baymax> ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line.startswith("/"):
                commands.get(line, lambda: print("  unknown command; /help"))()
                continue
            self.run_task(line)


def main() -> int:
    ap = argparse.ArgumentParser(description="Baymax agent CLI")
    ap.add_argument("--backend", choices=["local", "http"], default="local")
    ap.add_argument("--url", help="robot API URL for --backend http (default $BAYMAX_API_URL)")
    ap.add_argument("--model", default=os.environ.get("BAYMAX_MODEL", DEFAULT_MODEL))
    ap.add_argument("--thinking", choices=list(THINKING_BUDGET), default="low")
    ap.add_argument("--max-steps", type=int, default=40, help="agent loop steps per task")
    ap.add_argument("--show-thinking", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="fake hardware (local backend)")
    ap.add_argument("-p", "--prompt", help="run one task and exit")
    args = ap.parse_args()
    if args.dry_run:
        os.environ["BAYMAX_DRY_RUN"] = "1"  # before robot_api.config is imported

    api_key = load_api_key()
    if not api_key:
        print("no API key: put claude_key=... in robot_api/.env or set ANTHROPIC_API_KEY",
              file=sys.stderr)
        return 2
    try:
        backend = make_backend(args.backend, args.url)
    except Exception as e:  # noqa: BLE001
        print(e, file=sys.stderr)
        return 2
    agent = build_agent(backend, args.model, args.thinking, api_key)
    print(f"model {args.model}, thinking {args.thinking}, backend {args.backend}")
    session = Session(agent, backend, args.max_steps, args.show_thinking)
    if args.prompt:
        session.run_task(args.prompt)
    else:
        session.repl()
    return 0


if __name__ == "__main__":
    sys.exit(main())
