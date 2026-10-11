"""The GX10 link made at startup: Ollama directly or through a tunnel, then the pose service."""
import pytest

from dashboard import gx10 as gx10_module
from dashboard.config import Settings
from dashboard.gx10 import Gx10Link, find_password, parse_target
from dashboard.tunnel import TunnelError


class FakeTunnel:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def open(self, host, user, password=None, port=11434, wait_s=10.0):
        self.calls.append((host, user, password, port))
        if self.fail:
            raise TunnelError(self.fail)


class FakeMonitor:
    def __init__(self):
        self.polled = self.published = 0

    def poll_llm(self):
        self.polled += 1

    def publish(self):
        self.published += 1


class FakeWebcam:
    def __init__(self):
        self.pose_urls = []

    def use_pose_service(self, url):
        self.pose_urls.append(url)


def link(tunnel=None, webcam=None, **settings):
    s = Settings(dry_run=True, stub_llm=True, gx10_auto=True, gx10_password="pw", **settings)
    mon, tun = FakeMonitor(), tunnel or FakeTunnel()
    return Gx10Link(s, tun, mon, None, get_webcam=lambda: webcam), s, tun, mon


def test_the_default_target_is_the_gx10_the_user_named():
    assert parse_target(None) == ("asus", "169.233.72.183")
    assert parse_target("pi@10.0.0.7") == ("pi", "10.0.0.7")
    assert parse_target("10.0.0.7") == ("asus", "10.0.0.7")
    s = Settings()
    assert (s.gx10_user, s.gx10_host) == ("asus", "169.233.72.183") and s.gx10_auto is False   # off unless the CLI turns it on


def test_the_password_comes_from_the_environment_or_a_local_file_never_the_code(tmp_path, monkeypatch):
    monkeypatch.delenv("BAYMAX_GX10_PASSWORD", raising=False)
    f = tmp_path / ".gx10_password"
    assert find_password(str(f)) is None
    f.write_text("from-file\nignored second line\n")
    assert find_password(str(f)) == "from-file"
    monkeypatch.setenv("BAYMAX_GX10_PASSWORD", "from-env")
    assert find_password(str(f)) == "from-env"
    import glob
    import os
    for path in glob.glob(os.path.join(os.path.dirname(gx10_module.__file__), "*.py")):
        text = open(path, encoding="utf-8").read()
        assert '"baymax"' not in text and "'baymax'" not in text, f"the GX10 password is written in {path}"


def test_ollama_answering_on_the_gx10_is_used_directly_with_no_tunnel(monkeypatch):
    cam = FakeWebcam()
    lk, s, tun, mon = link(webcam=cam)
    monkeypatch.setattr(lk, "_ollama_answers", lambda url: url == "http://169.233.72.183:11434")
    monkeypatch.setattr(lk, "_pose_service", lambda host: f"http://{host}:9100")
    assert lk.connect() is True
    assert s.ollama_url == "http://169.233.72.183:11434" and s.stub_llm is False
    assert tun.calls == [] and mon.polled == 1 and mon.published == 1
    assert cam.pose_urls == ["http://169.233.72.183:9100"]                  # frames go to its pose service too
    assert lk.state["state"] == "ok" and lk.state["route"] == "direct" and lk.state["pose"] is True


def test_otherwise_an_ssh_tunnel_is_opened_with_the_password(monkeypatch):
    lk, s, tun, mon = link()
    monkeypatch.setattr(lk, "_ollama_answers", lambda url: False)
    monkeypatch.setattr(lk, "_pose_service", lambda host: None)
    monkeypatch.setattr(gx10_module, "_local_port_free", lambda p: True)
    assert lk.connect() is True
    assert tun.calls == [("169.233.72.183", "asus", "pw", 11434)]
    assert s.ollama_url == "http://127.0.0.1:11434" and s.stub_llm is False
    assert lk.state["route"] == "tunnel" and lk.state["pose"] is False


def test_the_tunnel_uses_another_local_port_when_a_local_ollama_holds_11434(monkeypatch):
    lk, s, tun, mon = link()
    monkeypatch.setattr(lk, "_ollama_answers", lambda url: False)
    monkeypatch.setattr(lk, "_pose_service", lambda host: None)
    monkeypatch.setattr(gx10_module, "_local_port_free", lambda p: p != 11434)
    assert lk.connect()
    assert tun.calls[0][3] == 11435 and s.ollama_url == "http://127.0.0.1:11435"


def test_failure_changes_nothing_and_says_why(monkeypatch):
    lk, s, tun, mon = link(tunnel=FakeTunnel(fail="No connection to 169.233.72.183 after 10 s."))
    monkeypatch.setattr(lk, "_ollama_answers", lambda url: False)
    monkeypatch.setattr(lk, "_pose_service", lambda host: None)
    monkeypatch.setattr(gx10_module, "_local_port_free", lambda p: True)
    assert lk.connect() is False
    assert s.stub_llm is True and s.ollama_url.startswith("http://127.0.0.1:11434")
    assert lk.state["state"] == "down" and "No connection" in lk.state["error"] and mon.polled == 0


def test_it_keeps_trying_until_the_gx10_answers_and_then_stops(monkeypatch):
    import time
    lk, s, tun, mon = link()
    monkeypatch.setattr(gx10_module, "RETRY_S", (0.01,))
    attempts = []
    def flaky():
        attempts.append(1)
        return len(attempts) >= 3
    monkeypatch.setattr(lk, "connect", flaky)
    lk.start()
    for _ in range(100):
        time.sleep(0.02)
        if len(attempts) >= 3:
            break
    time.sleep(0.1)
    assert len(attempts) == 3                          # two misses, one success, then it stopped trying
    lk.stop()


def test_with_no_webcam_the_app_is_told_when_the_gx10s_pose_service_turns_up(monkeypatch):
    lk, s, tun, mon = link()
    told = []
    lk.on_pose_service = told.append
    monkeypatch.setattr(lk, "_ollama_answers", lambda url: True)
    monkeypatch.setattr(lk, "_pose_service", lambda host: f"http://{host}:9100")
    assert lk.connect() is True
    assert told == ["http://169.233.72.183:9100"]          # it can then take the robot's camera from the GX10
