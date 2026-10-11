"""Send webcam frames to the vision model on the GX10 and show what it says it sees.

Every --every seconds the newest frame is resized, JPEG-encoded and sent to Ollama
(/api/chat with an image). The reply is printed and drawn over the video. The
request runs in a background thread so the video doesn't freeze while the model
thinks; frames captured meanwhile are skipped, never queued.

    python scripts/vision_test.py                          # laptop webcam -> qwen3.6:35b on the GX10
    python scripts/vision_test.py --prompt "How many people are there?"
    python scripts/vision_test.py --image photo.jpg        # one image, no camera
    python scripts/vision_test.py --no-window --once       # one webcam frame, print and exit

Keys in the window: q quits, space asks about the current frame right away.
"""

import argparse
import base64
import os
import textwrap
import threading
import time

import cv2
import requests

DEFAULT_URL = os.environ.get("OLLAMA_HOST", "http://169.233.72.183:11434")
DEFAULT_PROMPT = "Describe what you see in one or two short sentences."


def encode(frame, width: int) -> str:
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return base64.b64encode(jpg.tobytes()).decode()


def describe(url: str, model: str, prompt: str, image_b64: str, timeout: float = 120) -> str:
    r = requests.post(f"{url.rstrip('/')}/api/chat", timeout=timeout, json={
        "model": model,
        "stream": False,
        "think": False,          # a caption doesn't need reasoning; much faster
        "keep_alive": "30m",     # keep the model loaded between frames
        "options": {"num_predict": 120},
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
    })
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


class Describer:
    """Runs one request at a time in the background; holds the latest reply."""

    def __init__(self, args):
        self.args = args
        self.text = "waiting for the first reply..."
        self.latency = None
        self.busy = False
        self._lock = threading.Lock()

    def submit(self, frame) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True
        threading.Thread(target=self._run, args=(frame.copy(),), daemon=True).start()
        return True

    def _run(self, frame):
        a = self.args
        started = time.time()
        try:
            text = describe(a.url, a.model, a.prompt, encode(frame, a.width))
        except Exception as e:  # noqa: BLE001  (keep the video running; show the error)
            text = f"[error] {type(e).__name__}: {e}"
        self.latency = time.time() - started
        self.text = text
        print(f"[{self.latency:4.1f} s] {text}", flush=True)
        self.busy = False


def draw_caption(frame, text: str, status: str):
    lines = textwrap.wrap(text, width=max(20, frame.shape[1] // 11))[:5]
    pad, lh = 8, 22
    h = pad * 2 + lh * (len(lines) + 1)
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (frame.shape[1], h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    cv2.putText(frame, status, (pad, pad + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 220, 255), 1)
    for i, line in enumerate(lines):
        cv2.putText(frame, line, (pad, pad + 14 + lh * (i + 1)), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser(description="Webcam -> vision model on the GX10.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"Ollama server (default {DEFAULT_URL})")
    ap.add_argument("--model", default="qwen3.6:35b")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--cam", type=int, default=0, help="webcam index")
    ap.add_argument("--every", type=float, default=3.0, help="seconds between requests")
    ap.add_argument("--width", type=int, default=512, help="resize frames to this width before sending")
    ap.add_argument("--image", help="describe this image file instead of the webcam, then exit")
    ap.add_argument("--once", action="store_true", help="describe one webcam frame, then exit")
    ap.add_argument("--no-window", action="store_true", help="print replies only")
    args = ap.parse_args()
    if "://" not in args.url:
        args.url = f"http://{args.url}"
    print(f"model {args.model} at {args.url}")

    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            raise SystemExit(f"can't read {args.image}")
        started = time.time()
        text = describe(args.url, args.model, args.prompt, encode(frame, args.width))
        print(f"[{time.time() - started:4.1f} s] {text}")
        return

    cap = cv2.VideoCapture(args.cam)
    if not cap.isOpened():
        raise SystemExit(f"can't open camera {args.cam} (on macOS, allow camera access for your "
                         "terminal or VS Code in System Settings > Privacy & Security > Camera)")
    for _ in range(30):  # the first frames are often black while auto-exposure settles
        cap.read()
    describer = Describer(args)
    last_sent = 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            if args.once:
                describer._run(frame)
                return
            now = time.time()
            key = -1
            if not args.no_window:
                status = ("thinking..." if describer.busy else "idle") + (
                    f"  last reply {describer.latency:.1f} s" if describer.latency else "")
                shown = frame.copy()
                draw_caption(shown, describer.text, status)
                cv2.imshow("vision test (q quits, space asks now)", shown)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
            if (now - last_sent >= args.every or key == ord(" ")) and describer.submit(frame):
                last_sent = now
            if args.no_window:
                time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
