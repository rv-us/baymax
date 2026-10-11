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


def jpeg():
    return cv2.imencode(".jpg", np.zeros((48, 64, 3), np.uint8))[1].tobytes()


@pytest.fixture
def service():
    StandIn.made.clear()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(pose_service.make_app(StandIn), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", server
    server.should_exit = True


def test_the_dashboard_gets_the_pose_the_gx10_computed(service):
    url, _ = service
    cam = Webcam(pose_url=url)
    assert cam._remote_health()
    pose, overlay, where = cam._compute(None, jpeg(), 1.0)
    assert where == "gx10" and pose["angles"]["shoulder_elevation"] == 10.0 and overlay["shoulders"] == [[1, 2], [3, 4]]
    assert cam._compute(None, jpeg(), 1.1)[0]["angles"]["shoulder_elevation"] == 20.0  # same patient, same tracker


def test_each_patient_session_has_its_own_tracker(service):
    url, _ = service
    a, b = Webcam(pose_url=url), Webcam(pose_url=url)
    a._compute(None, jpeg(), 1.0); a._compute(None, jpeg(), 1.1)
    assert b._compute(None, jpeg(), 1.0)[0]["angles"]["shoulder_elevation"] == 10.0
    assert len(StandIn.made) == 2


def test_a_body_that_is_not_a_jpeg_is_refused(service):
    import urllib.error
    import urllib.request
    url, _ = service
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(f"{url}/track", data=b"nope", method="POST"))
    assert e.value.code == 400


def test_when_the_gx10_stops_answering_tracking_continues_here(service):
    url, server = service
    cam = Webcam(pose_url=url)
    cam._local = StandIn()
    assert cam._compute(None, jpeg(), 1.0)[2] == "gx10"
    server.should_exit = True
    time.sleep(0.6)
    pose, _, where = cam._compute(None, jpeg(), 2.0)
    assert where == "local" and cam._remote_down_until > time.monotonic()
    assert cam._compute(None, jpeg(), 2.1)[2] == "local"       # and it doesn't hammer the dead service


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
