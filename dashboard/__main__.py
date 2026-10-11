"""
Run the Baymax dashboard.

    python -m dashboard --dry-run                          # fake robot, synthetic sessions, stub coach
    python -m dashboard --robot-url http://<robot>:8000    # real robot (token from BAYMAX_API_TOKEN)
    python -m dashboard --robot-url ... --ollama-url http://localhost:11434

Then open http://127.0.0.1:8080. See dashboard/README.md.
"""
import argparse
import os
import sys


def pose_service_url(choice: str, ollama: str | None) -> str | None:
    """The GX10 pose service to use: given outright, or found next to the Ollama host."""
    if choice == "off":
        return None
    if choice != "auto":
        return choice if "://" in choice else f"http://{choice}"
    from urllib.parse import urlparse
    host = urlparse(ollama if ollama and "://" in ollama else f"http://{ollama or ''}").hostname
    if host in (None, "", "localhost", "127.0.0.1"):
        return None
    from dashboard.pose_service import DEFAULT_PORT, SERVICE
    url = f"http://{host}:{DEFAULT_PORT}"
    try:
        import json
        import urllib.request
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            return url if json.load(r).get("service") == SERVICE else None
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description="Baymax physical-therapy dashboard.")
    ap.add_argument("--dry-run", action="store_true",
                    help="fake hardware, synthetic sessions and a stubbed coach; needs nothing else")
    ap.add_argument("--robot-url", help="robot API base URL (default: BAYMAX_API_URL)")
    ap.add_argument("--ollama-url", help="Ollama base URL (default: OLLAMA_HOST, else localhost:11434)")
    ap.add_argument("--model", help="coach model (default: BAYMAX_COACH_MODEL, else qwen3.6:35b)")
    ap.add_argument("--webcam", default=None, metavar="auto|N|off",
                    help="use the laptop webcam as the robot camera and track the patient's arms with arm_mvp "
                         "(default: auto in a dry run, off with a robot)")
    ap.add_argument("--pose-url", default="auto", metavar="auto|URL|off",
                    help="send camera frames to the GX10 pose service and use its answer (default auto: the "
                         "--ollama-url host on port 9100, if the service answers there)")
    ap.add_argument("--gx10", default=None, metavar="USER@HOST|off",
                    help="where the GX10 is (default: BAYMAX_GX10, else asus@169.233.72.183). Unless --ollama-url or "
                         "OLLAMA_HOST says otherwise, the dashboard connects to it at startup (Ollama directly, or "
                         "through an SSH tunnel with the password from BAYMAX_GX10_PASSWORD or a git-ignored "
                         ".gx10_password file) and uses its pose service if it is running; 'off' skips all that")
    ap.add_argument("--voice-url", default=None, metavar="URL|off",
                    help="the GX10's microphone service (python -m voice.service). Default: beside the pose service, "
                         "on port 9200. Without it answers and pain words have to be typed")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve tablets and phones on the LAN")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    if args.dry_run:
        os.environ["BAYMAX_DRY_RUN"] = "1"  # robot_api reads this on first import

    import uvicorn

    from dashboard.app import create_app
    from dashboard.config import Settings, ollama_url

    settings = Settings(dry_run=args.dry_run, host=args.host, port=args.port)
    if args.robot_url:
        settings.robot_url = args.robot_url.rstrip("/")
    if args.ollama_url:
        settings.ollama_url = ollama_url(args.ollama_url)
    if args.model:
        settings.model = args.model
    choice = args.webcam or ("auto" if args.dry_run else "off")
    settings.webcam = None if choice == "off" else choice
    settings.pose_url = pose_service_url(args.pose_url, args.ollama_url)
    if args.voice_url:
        settings.voice_url = args.voice_url if args.voice_url == "off" or "://" in args.voice_url else f"http://{args.voice_url}"
    settings.stub_llm = args.dry_run and not args.ollama_url
    gx10 = args.gx10 or os.environ.get("BAYMAX_GX10")
    if (gx10 or "").lower() != "off":
        from dashboard.gx10 import find_password, parse_target
        settings.gx10_user, settings.gx10_host = parse_target(gx10)
        settings.gx10_password = find_password()
        # An explicit Ollama address wins; otherwise the GX10 is the coach's default home.
        settings.gx10_auto = not (args.ollama_url or os.environ.get("OLLAMA_HOST"))

    if args.host not in ("127.0.0.1", "localhost"):
        print("warning: the dashboard has no login; anyone on this network can move the robot through it")
    print(f"Baymax dashboard: http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
          f"  ({'dry run' if args.dry_run else settings.robot_url})")
    print("Press Ctrl+C to stop.")
    try:
        # The browser's live-update and camera streams never end by themselves, so a
        # plain shutdown waits on them forever. Give them 2 s, then drop them.
        uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1, log_level="warning",
                    timeout_graceful_shutdown=2)
    except KeyboardInterrupt:
        pass
    finally:
        # Background threads (a running session, the tunnel) must not keep the terminal stuck.
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
