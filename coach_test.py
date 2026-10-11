"""
Demo: how the robot brain uses a local LLM through Ollama.

- Warms up the model once at startup (keeps it loaded in memory).
- Runs a fast main loop (~30 times per second), like the real robot loop.
- When a (fake) pose deviation happens, the robot stops IMMEDIATELY,
  and the LLM is asked for coaching text in a background thread,
  so the main loop never freezes while waiting for the model.

Run on the GX10:
    source ~/ptrobot/bin/activate
    pip install ollama
    python coach_demo.py
"""

import threading
import time
import json
import ollama

MODEL = "qwen3.6:35b"   # swap for "gemma4:26b" or "nemotron3:33b" to compare

SYSTEM_PROMPT = (
    "You are a friendly physical therapy coach. The robot has paused the exercise "
    "because the patient's pose drifted from the target. Using the measurements given, "
    "tell the patient in ONE short, plain-language sentence what to adjust. "
    "Be encouraging. Never give medical diagnoses."
)

# Forces the reply into exactly this JSON shape.
REPLY_SCHEMA = {
    "type": "object",
    "properties": {"instruction": {"type": "string"}},
    "required": ["instruction"],
}


def warm_up():
    """Load the model into memory once and keep it there (keep_alive=-1)."""
    t0 = time.time()
    ollama.generate(model=MODEL, prompt="", keep_alive=-1)
    print(f"[startup] {MODEL} loaded in {time.time() - t0:.1f}s")


class Coach:
    """Asks the LLM for coaching text in the background."""

    def __init__(self):
        self.busy = False
        self.latest_text = None
        self._lock = threading.Lock()

    def request(self, deviation_description):
        """Start a background LLM call unless one is already running."""
        with self._lock:
            if self.busy:
                return
            self.busy = True
        threading.Thread(target=self._ask, args=(deviation_description,), daemon=True).start()

    def _ask(self, deviation_description):
        t0 = time.time()
        try:
            response = ollama.chat(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": deviation_description},
                ],
                format=REPLY_SCHEMA,
                think=False,            # skip the reasoning step for speed
                keep_alive=-1,
                options={"temperature": 0.3},
            )
            text = json.loads(response.message.content)["instruction"]
        except Exception as e:
            text = f"(coach error: {e})"
        with self._lock:
            self.latest_text = text
            self.busy = False
        print(f"[coach]   reply after {time.time() - t0:.2f}s")

    def take_text(self):
        """Return new coaching text once, or None if nothing new."""
        with self._lock:
            text, self.latest_text = self.latest_text, None
            return text


def fake_pose_check(t):
    """Stand-in for the real pose comparison. Deviates between 2s and 3s."""
    if 2.0 <= t < 3.0:
        return "Left knee is bent 25 degrees less than the target. Hips shifted 4 cm to the right."
    return None


def main():
    warm_up()
    coach = Coach()
    robot_moving = True
    start = time.time()

    while True:
        t = time.time() - start

        deviation = fake_pose_check(t)
        if deviation and robot_moving:
            robot_moving = False                       # 1) stop right away (real code: send STOP to the Pi)
            print(f"[{t:5.2f}s] deviation -> robot STOPPED")
            coach.request(deviation)                   # 2) ask the LLM in the background

        text = coach.take_text()
        if text:
            print(f"[{t:5.2f}s] COACH SAYS: {text}")   # real code: show on screen / speak with Piper

        if t > 8:
            break
        time.sleep(1 / 30)                             # loop keeps running ~30x per second

    print("demo finished")


if __name__ == "__main__":
    main()
