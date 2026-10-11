"""The GX10 pose service and the dashboard's use of it, with a stand-in tracker (no MediaPipe)."""
import socket
import threading
import time

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
uvicorn = pytest.importorskip("uvicorn")

from dashboard import pose_service  # noqa: E402
from dashboard.webcam import Webcam  # noqa: E402


class StandIn:
    """Remembers how many frames it has seen, like a tracker holds one patient's state."""
    made = []

    def __init__(self):
        self.n = 0
        StandIn.made.append(self)

    def process(self, frame, t):
        self.n += 1
        pose = {"visible": True, "x": 0.25, "size": 0.5, "calibrated": True, "arms": {"L": "TRACKING", "R": "LOST"},
                "angles": {"shoulder_elevation": 10.0 * self.n, "elbow": None}}
        return pose, {"shoulders": [[1, 2], [3, 4]], "arms": {}}

    def close(self):
        pass


FRAME = np.zeros((48, 64, 3), np.uint8)


def jpeg():
    return cv2.imencode(".jpg", FRAME)[1].tobytes()


def free_port() -> int:
    """A port that is free for both UDP (the frames) and TCP (the HTTP side)."""
    last = None
    for _ in range(200):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t = socket.socket()
        try:
            u.bind(("127.0.0.1", 0))
            port = u.getsockname()[1]
            t.bind(("127.0.0.1", port))
            return port
        except OSError as e:
            last = e
        finally:
            u.close()
            t.close()
    raise RuntimeError(f"no port free for both UDP and TCP: {last!r}")


@pytest.fixture
def service():
    StandIn.made.clear()
    port = free_port()
    app = pose_service.make_app(StandIn, udp_port=port)
    udp = pose_service.UdpServer(app.state.sessions, port, "127.0.0.1").start()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    server.udp = udp
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", server
    server.should_exit = True
    udp.close()


def test_the_dashboard_gets_the_pose_the_gx10_computed(service):
    url, _ = service
    cam = Webcam(pose_url=url, transport="http")
    assert cam._remote_health()
    pose, overlay, where = cam._compute(FRAME, 1.0)
    assert where == "gx10" and pose["angles"]["shoulder_elevation"] == 10.0 and overlay["shoulders"] == [[1, 2], [3, 4]]
    assert cam._compute(FRAME, 1.1)[0]["angles"]["shoulder_elevation"] == 20.0  # same patient, same tracker


def test_each_patient_session_has_its_own_tracker(service):
    url, _ = service
    a, b = Webcam(pose_url=url, transport="http"), Webcam(pose_url=url, transport="http")
    a._compute(FRAME, 1.0); a._compute(FRAME, 1.1)
    assert b._compute(FRAME, 1.0)[0]["angles"]["shoulder_elevation"] == 10.0
    assert len(StandIn.made) == 2


def test_a_slow_link_sends_half_size_frames_and_the_overlay_is_scaled_back(service):
    url, _ = service
    cam = Webcam(pose_url=url, transport="http")
    cam._cv2 = cv2
    cam._rtt = 1.0
    seen = {}
    orig = StandIn.process
    StandIn.process = lambda self, frame, t: (seen.setdefault("shape", frame.shape), orig(self, frame, t))[1]
    try:
        _, overlay, where = cam._compute(FRAME, 1.0)
    finally:
        StandIn.process = orig
    assert where == "gx10" and seen["shape"][:2] == (24, 32)
    assert overlay["shoulders"] == [[2, 4], [6, 8]]


def test_a_body_that_is_not_a_jpeg_is_refused(service):
    import urllib.error
    import urllib.request
    url, _ = service
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(f"{url}/track", data=b"nope", method="POST"))
    assert e.value.code == 400


def test_when_the_gx10_stops_answering_tracking_continues_here(service):
    url, server = service
    cam = Webcam(pose_url=url, transport="http")
    cam._local = StandIn()
    assert cam._compute(FRAME, 1.0)[2] == "gx10"
    server.should_exit = True
    time.sleep(0.6)
    pose, _, where = cam._compute(FRAME, 2.0)
    assert where == "local" and cam._remote_down_until > time.monotonic()
    assert cam._compute(FRAME, 2.1)[2] == "local"       # and it doesn't hammer the dead service


def test_the_pose_service_is_found_next_to_the_ollama_host(service):
    from dashboard.__main__ import pose_service_url
    url, _ = service
    port = int(url.rsplit(":", 1)[1])
    assert pose_service_url("off", "http://gx10:11434") is None
    assert pose_service_url("auto", None) is None and pose_service_url("auto", "http://localhost:11434") is None
    assert pose_service_url("http://box:9000", None) == "http://box:9000"
    assert pose_service_url("auto", "http://203.0.113.9:11434") is None      # nothing answering there


def test_the_older_pose_server_on_the_same_box_is_not_mistaken_for_this_one(service):
    import http.server
    class Old(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok": true, "model": "x", "frames": 3}')
        def log_message(self, *a): pass
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Old)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    assert not Webcam(pose_url=f"http://127.0.0.1:{httpd.server_port}")._remote_health()
    assert Webcam(pose_url=service[0])._remote_health()
    httpd.shutdown()


# ---------------------------------------------------------------- UDP: the venue Wi-Fi transport


def udp_client(port):
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(2.0)
    return c, ("127.0.0.1", port)


def replies(sock, n, wait=2.0):
    out, end = [], time.time() + wait
    while len(out) < n and time.time() < end:
        try:
            data = sock.recvfrom(65535)[0]
        except socket.timeout:
            break
        out.append(pose_service.REPLY_HEADER.unpack_from(data)[1])
    return out


def test_udp_round_trip_returns_the_pose_for_the_frame(service):
    url, server = service
    c, addr = udp_client(server.udp.port)
    for d in pose_service.encode_frame(b"\x01" * 8, 1, 1.0, jpeg()):
        c.sendto(d, addr)
    data = c.recvfrom(65535)[0]
    import json
    assert data[:4] == pose_service.REPLY_MAGIC and pose_service.REPLY_HEADER.unpack_from(data)[1] == 1
    assert json.loads(data[8:])["pose"]["angles"]["shoulder_elevation"] == 10.0


def test_a_frame_missing_a_chunk_is_dropped_when_a_newer_one_arrives(service):
    url, server = service
    c, addr = udp_client(server.udp.port)
    big = jpeg() + b"\x00" * 3000                       # several datagrams
    old = pose_service.encode_frame(b"\x02" * 8, 1, 1.0, big)
    assert len(old) > 3
    for d in old[:-2]:                                 # the last two datagrams of frame 1 are "lost" (one would be rebuilt)
        c.sendto(d, addr)
    for d in reversed(pose_service.encode_frame(b"\x02" * 8, 2, 1.1, big)):   # frame 2 arrives out of order
        c.sendto(d, addr)
    assert replies(c, 2, wait=1.0) == [2]              # only the newest is answered; nothing waited for frame 1
    assert server.udp.dropped >= 1
    for d in pose_service.encode_frame(b"\x02" * 8, 1, 1.0, big):             # frame 1 turning up late
        c.sendto(d, addr)
    assert replies(c, 1, wait=0.7) == []


def test_the_dashboard_uses_the_gx10_over_udp_and_never_waits_for_it(service):
    url, server = service
    cam = Webcam(pose_url=url)
    cam._cv2, cam._alive = cv2, True
    assert cam._remote_health() and cam.transport == "udp"
    t0 = time.monotonic()
    pose, overlay, where = cam._compute(FRAME, 1.0)       # nothing has come back yet: no waiting
    assert time.monotonic() - t0 < 0.3 and where == "gx10"
    for _ in range(100):
        time.sleep(0.05)
        cam._last_send = 0                                 # let the next frame go
        pose, overlay, where = cam._compute(FRAME, 1.1)
        if pose["visible"]:
            break
    assert pose["angles"]["shoulder_elevation"] >= 10.0 and where == "gx10"
    cam._alive = False


def test_frames_get_smaller_when_few_are_answered_and_bigger_when_the_link_recovers():
    cam = Webcam(pose_url="http://x")
    now = time.monotonic()
    cam._window = [now - 4, 20, 5]                         # a quarter answered
    cam._adapt(now)
    assert cam._tier == 2 and cam.delivery == 0.25
    cam._window = [now - 4, 20, 20]
    cam._adapt(now)
    cam._window = [now - 4, 20, 20]
    cam._adapt(now)
    assert cam._tier == 0
    cam._window = [now - 1, 20, 5]                         # too soon to judge
    cam._adapt(now)
    assert cam._tier == 0


def test_udp_tracking_falls_back_here_when_the_gx10_goes_quiet(service):
    url, server = service
    cam = Webcam(pose_url=url)
    cam._cv2, cam._alive, cam._local = cv2, True, StandIn()
    assert cam._remote_health()
    server.udp.close()                                     # the service stops answering
    server.should_exit = True
    pose_service_stale = pytest.importorskip("dashboard.webcam")
    pose_service_stale.STALE_REPLY_S = 0.3
    try:
        where = ""
        for _ in range(30):
            time.sleep(0.1)
            cam._last_send = 0
            where = cam._compute(FRAME, 2.0)[2]
            if where == "local":
                break
        assert where == "local" and cam._remote_down_until > time.monotonic()
    finally:
        pose_service_stale.STALE_REPLY_S = 3.0
        cam._alive = False


def test_poses_keep_flowing_through_a_network_that_drops_25_percent_of_packets(service):
    import random
    url, server = service
    rng = random.Random(4)
    front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    front.bind(("127.0.0.1", 0))
    front.settimeout(0.2)
    back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    back.settimeout(0.2)
    client = {}
    run = threading.Event()
    run.set()

    def relay(src, dst, addr):
        while run.is_set():
            try:
                data, frm = src.recvfrom(65535)
            except OSError:
                continue
            if src is front:
                client["a"] = frm
            if rng.random() >= 0.25 and addr():
                dst.sendto(data, addr())

    threading.Thread(target=relay, args=(front, back, lambda: ("127.0.0.1", server.udp.port)), daemon=True).start()
    threading.Thread(target=relay, args=(back, front, lambda: client.get("a")), daemon=True).start()
    cam = Webcam(pose_url=url)
    cam._cv2, cam._alive = cv2, True
    assert cam._remote_health()
    cam._addr = ("127.0.0.1", front.getsockname()[1])
    cam._open_socket = lambda: None
    cam._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cam._sock.settimeout(0.3)
    cam._started_sending = time.monotonic()
    threading.Thread(target=cam._listen, args=(cam._sock,), daemon=True).start()
    img = np.tile(np.arange(640, dtype=np.uint8), (480, 1))[:, :, None].repeat(3, 2)   # a few datagrams per frame
    seen, started = set(), time.monotonic()
    while time.monotonic() - started < 4.0:
        cam._last_send = 0
        try:
            cam._remote_udp(img, 0.0)
        except Exception as e:
            raise AssertionError(repr(e) + f" tier={cam._tier} delivery={cam.delivery} sent={cam._window}")
        pose, _, where = cam._compute(img, time.monotonic() - started)
        assert where == "gx10"                                # never falls over, never waits
        if pose["visible"]:
            seen.add(pose["angles"]["shoulder_elevation"])
        time.sleep(1 / 15)
    run.clear()
    cam._alive = False
    assert len(seen) >= 8, f"only {len(seen)} different poses came back in 4 s"


def test_any_single_lost_datagram_of_a_frame_is_rebuilt_from_the_parity(service):
    url, server = service
    c, addr = udp_client(server.udp.port)
    big = jpeg() + b"\x00" * 4000
    n = len(pose_service.encode_frame(b"\x05" * 8, 1, 1.0, big))
    assert n >= 5                                    # several chunks, then the parity
    for lost in range(n):                            # lose each datagram in turn: a middle chunk, the short last one, the parity
        seq = lost + 1
        for i, d in enumerate(pose_service.encode_frame(b"\x05" * 8, seq, 1.0, big)):
            if i != lost:
                c.sendto(d, addr)
        assert replies(c, 1, wait=1.0) == [seq], f"datagram {lost} of {n} lost"
    # two lost datagrams cannot be rebuilt: that frame is simply never answered
    seq = n + 1
    for i, d in enumerate(pose_service.encode_frame(b"\x05" * 8, seq, 1.0, big)):
        if i not in (0, 1):
            c.sendto(d, addr)
    assert replies(c, 1, wait=0.7) == []
