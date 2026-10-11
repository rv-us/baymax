"""python -m digital_twin [--robot-url http://robot:8000] [--demo] [--port 8765]"""
import argparse
import os
import webbrowser

from .demo import PoseDemo
from .robot_link import make_backend
from .server import TwinServer
from .state import TwinState


def main() -> None:
    ap = argparse.ArgumentParser(prog="digital_twin", description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to serve a dashboard on another machine")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--robot-url", default=os.environ.get("BAYMAX_API_URL"),
                    help="robot_api server to mirror (default: $BAYMAX_API_URL; read-only)")
    ap.add_argument("--hz", type=float, default=4.0, help="robot readback rate")
    ap.add_argument("--pose", help="start in this recorded pose")
    ap.add_argument("--demo", action="store_true", help="cycle through the recorded poses")
    ap.add_argument("--open", action="store_true", help="open the viewer in a browser")
    args = ap.parse_args()

    state = TwinState()
    server = TwinServer(state, host=args.host, port=args.port)
    if args.pose:
        state.apply_pose(args.pose)
    server.start()
    if args.robot_url:
        server.api.attach_robot(make_backend(args.robot_url), hz=args.hz, label=args.robot_url)
        print(f"mirroring robot at {args.robot_url} ({args.hz:g} Hz, read-only)", flush=True)
    if args.demo:
        PoseDemo(state).start()
    print(f"digital twin viewer: {server.url}   (poses: {', '.join(state.poses)})", flush=True)
    if args.open:
        webbrowser.open(server.url)
    server.serve_forever()


if __name__ == "__main__":
    main()
