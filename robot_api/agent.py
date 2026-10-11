"""Chat with Claude driving the robot through the LangChain tools.

On the robot:      python -m robot_api.agent --backend local
From the laptop:   python -m robot_api.agent --backend http --url http://169.233.72.93:8000
One-shot:          python -m robot_api.agent -p "how far is the wall?"
Needs ANTHROPIC_API_KEY. --dry-run (local backend) fakes all hardware.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SYSTEM_PROMPT = """You control Baymax, a Hiwonder TonyPi Pro humanoid robot (about 37 cm tall), \
through tools. You are the robot; speak in first person.

Body: 16 bus servos (pulse 0-1000, 500 = middle) named left_/right_ + ankle_roll, ankle_pitch, \
knee, hip_pitch, hip_roll, elbow, shoulder_roll, shoulder_pitch; hands left_hand/right_hand; \
head pan/tilt (PWM 1200-1800, 1500 = center). Sensors: ultrasonic distance + RGB eyes, \
temperature/humidity, light sensor, 16x8 LED matrix, speaker, microphone, voice-command box, buzzer.

Safety rules:
- Prefer high-level tools (stand, walk, turn, side_step, move_arm, gripper, run_action_group) \
over raw set_servo. Use raw servos only for small, deliberate adjustments, and check joint_map.
- Joint limits are enforced but collisions are not: never move shoulder_roll inward while that \
arm is down at the side (it hits the leg); move the arm forward first. Never bend leg joints \
individually while standing; the robot will fall.
- Stand before walking. Check read_distance before walking forward; don't walk if something \
is closer than 200 mm.
- If a tool reports a servo is too hot, stop moving and say so. Holding still for long heats \
the leg servos.
- coast_servos and risky action groups make the robot fall or move violently: ask the human \
first and only proceed after they confirm.
- If anything goes wrong, call stop_motion.
Keep replies short."""


def build_agent(backend_name: str, url: str | None, model: str):
    from langchain.agents import create_agent
    from langchain_anthropic import ChatAnthropic

    from .tools import make_tools

    if backend_name == "local":
        from .api import LocalBackend
        from .hardware import check_exclusive

        check_exclusive()
        backend = LocalBackend()
    else:
        from .client import HttpBackend

        backend = HttpBackend(url)
    llm = ChatAnthropic(model=model, max_tokens=4096)
    return create_agent(llm, make_tools(backend), system_prompt=SYSTEM_PROMPT)


def _show(msg) -> None:
    kind = msg.type
    if kind == "ai":
        for tc in getattr(msg, "tool_calls", None) or []:
            print(f"  -> {tc['name']}({json.dumps(tc['args'])})")
        text = msg.text if isinstance(getattr(msg, "text", None), str) else msg.content
        if isinstance(text, str) and text.strip():
            print(f"baymax: {text.strip()}")
    elif kind == "tool":
        out = str(msg.content)
        print(f"  <- {out[:300]}{'...' if len(out) > 300 else ''}")


def run_turn(agent, history: list, user_text: str) -> list:
    history = history + [{"role": "user", "content": user_text}]
    for update in agent.stream({"messages": history}, stream_mode="updates"):
        for node in update.values():
            for msg in (node or {}).get("messages", []):
                _show(msg)
                history.append(msg)
    return history


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude agent for the Baymax robot")
    ap.add_argument("--backend", choices=["local", "http"], default="local")
    ap.add_argument("--url", help="robot API URL for --backend http (default $BAYMAX_API_URL)")
    ap.add_argument("--model", default=os.environ.get("BAYMAX_MODEL", "claude-opus-5-5"))
    ap.add_argument("--dry-run", action="store_true", help="fake hardware (local backend)")
    ap.add_argument("-p", "--prompt", help="run one request and exit")
    args = ap.parse_args()
    if args.dry_run:
        os.environ["BAYMAX_DRY_RUN"] = "1"  # before robot_api.config is imported
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("set ANTHROPIC_API_KEY first", file=sys.stderr)
        return 2

    agent = build_agent(args.backend, args.url, args.model)
    if args.prompt:
        run_turn(agent, [], args.prompt)
        return 0
    print("Talk to Baymax (empty line or Ctrl+C to quit).")
    history: list = []
    try:
        while True:
            text = input("you: ").strip()
            if not text:
                break
            history = run_turn(agent, history, text)
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
