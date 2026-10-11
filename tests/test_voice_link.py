"""The GX10 owns the robot's microphone: the dashboard reads captions from it, tells it when the robot talks,
and decides what each line means. Plus the Arm flow plan (alternating sides included)."""
import contextlib
import threading
import time

import pytest

uvicorn = pytest.importorskip("uvicorn")

from dashboard.echo import is_echo  # noqa: E402
from dashboard.voice_link import STALE_S, VoiceLink, voice_url  # noqa: E402
from test_dashboard_api import make, tiny_plan, wait_for  # noqa: E402
from test_pose_service import free_port  # noqa: E402
from voice import service as voice_service  # noqa: E402
from voice.listener import _looping  # noqa: E402


class FakeListener:
    """What voice.service uses of a voice.Listener."""

    def __init__(self):
        self.callbacks, self.events, self.partial_callbacks = [], [], []

    def on_transcript(self, cb):
        self.callbacks.append(cb)

    @contextlib.contextmanager
    def robot_speaking(self):
        self.events.append("mute")
        try:
            yield
        finally:
            self.events.append("unmute")

    def say(self, text, age=0.0, utt=None, final=True):
        """The patient says something that ended `age` seconds ago (`final` False: the words so far)."""
        end = time.time() - age
        self.utts = getattr(self, "utts", 0) + (utt is None)
        t = type("T", (), {"text": text, "start": end - 1.0, "end": end,
                           "utt": self.utts if utt is None else utt, "final": final})()
        for cb in (self.callbacks if final else self.partial_callbacks):
            cb(t)

    partial_callbacks = None

    def on_partial(self, cb):
        self.partial_callbacks.append(cb)


@pytest.fixture
def mic():
    """A voice service around a fake listener, on a real port."""
    listener = FakeListener()
    hub = voice_service.VoiceHub(listener)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(voice_service.make_app(hub), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    assert wait_for(lambda: server.started, 5)
    yield f"http://127.0.0.1:{port}", listener, hub
    server.should_exit = True


def dashboard_on(tmp_path, url):
    client, parts = make(tmp_path)
    parts["settings"].voice_url = url
    parts["voice"].start()
    assert wait_for(lambda: parts["voice"].state["state"] == "listening", 5)
    return client, parts


# ---- the service ---------------------------------------------------------------------------

def test_the_service_hands_out_what_was_said_after_a_point_and_waits_for_more(mic):
    url, listener, hub = mic
    link = VoiceLink(type("S", (), {"voice_url": url, "pose_url": None})(), lambda *a: "x")
    first = link._call(url, "GET", "/heard?after=-1&wait=0")
    assert first["items"] == [] and first["seq"] == 0
    threading.Timer(0.2, lambda: listener.say("I'm ready")).start()
    out = link._call(url, "GET", "/heard?after=0&wait=3")                           # held open until something is said
    assert [i["text"] for i in out["items"]] == ["I'm ready"] and out["items"][0]["age"] < 1 and out["seq"] == 1
    assert link._call(url, "GET", "/heard?after=1&wait=0")["items"] == []           # nothing is handed out twice


def test_the_microphone_is_ignored_while_the_robot_talks_and_never_stays_deaf(mic, monkeypatch):
    url, listener, hub = mic
    link = VoiceLink(type("S", (), {"voice_url": url, "pose_url": None})(), lambda *a: "x")
    assert link._call(url, "POST", "/speaking", {"on": True}) == {"muted": True}
    assert link._call(url, "POST", "/speaking", {"on": True}) == {"muted": True}    # saying it twice mutes once
    assert listener.events == ["mute"]
    assert link._call(url, "GET", "/health")["muted"] is True
    assert link._call(url, "POST", "/speaking", {"on": False}) == {"muted": False}
    assert listener.events == ["mute", "unmute"]
    hub.speaking(True)
    monkeypatch.setattr(voice_service, "MAX_MUTE_S", 0.0)                            # the "off" never came
    time.sleep(0.05)
    assert hub.health()["muted"] is False and listener.events[-1] == "unmute"


def test_a_phrase_whisper_repeats_on_noise_is_not_a_transcript():
    assert _looping("No, the... " * 20)
    assert not _looping("Yes, I am ready to start my workout now please")
    assert not _looping("no no no")


# ---- the dashboard reading it --------------------------------------------------------------

def test_the_address_is_beside_the_pose_service_unless_told_otherwise():
    S = lambda **kw: type("S", (), {"voice_url": None, "pose_url": None, **kw})()   # noqa: E731
    assert voice_url(S()) is None
    assert voice_url(S(pose_url="http://10.0.0.5:9100")) == "http://10.0.0.5:9200"
    assert voice_url(S(pose_url="http://10.0.0.5:9100", voice_url="http://other:1234/")) == "http://other:1234"
    assert voice_url(S(pose_url="http://10.0.0.5:9100", voice_url="off")) is None


def test_what_is_heard_is_captioned_and_means_what_the_session_needs(mic, tmp_path, fake_robot):
    url, listener, hub = mic
    client, parts = dashboard_on(tmp_path, url)
    voice = parts["voice"]
    listener.say("Hello there")
    assert wait_for(lambda: (voice.state["last"] or {}).get("text") == "Hello there", 5)
    assert voice.state["last"]["outcome"] == "unprompted"                            # nothing was asked
    assert client.get("/api/status").json()["voice"]["last"]["text"] == "Hello there"

    listener.say("something ancient", age=STALE_S + 5)                               # too old to act on
    assert wait_for(lambda: voice.state["last"]["text"] == "something ancient", 5)
    assert voice.state["last"]["outcome"] == "stale"

    parts["gateway"].said.append((time.time(), "Watch me first."))
    listener.say("Watch me first.")                                                  # the robot's own voice, come back
    assert wait_for(lambda: voice.state["last"]["text"] == "Watch me first.", 5)
    assert voice.state["last"]["outcome"] == "echo"

    tiny_plan(tmp_path)
    client.post("/api/live/start", json={"plan": "tiny", "simulate": False})
    assert wait_for(lambda: parts["live"].state.get("waiting"), 10)                  # "Are you ready?"
    listener.say("Yes I'm ready")
    assert wait_for(lambda: voice.state["last"]["text"] == "Yes I'm ready", 5)
    assert voice.state["last"]["outcome"] == "answer"
    assert wait_for(lambda: parts["live"].state.get("state") not in (None, "READY_CHECK"), 10)
    listener.say("ow, that hurts")
    assert wait_for(lambda: voice.state["last"]["text"] == "ow, that hurts", 5)
    assert voice.state["last"]["outcome"] == "pain"
    assert client.get("/api/safety").json()["stop"]["kind"] == "pain"
    assert wait_for(lambda: not parts["live"].running, 20)
    voice.stop()


def test_the_robot_talking_reaches_the_microphone_before_the_sound(mic, tmp_path, fake_robot):
    url, listener, hub = mic
    client, parts = dashboard_on(tmp_path, url)
    release, inside = threading.Event(), threading.Event()

    class Slow:
        def call(self, path, **kw):
            inside.set()
            release.wait(5)
            return {"said": kw.get("text")}

    parts["gateway"].backend, parts["gateway"].local = Slow(), False
    t = threading.Thread(target=lambda: parts["gateway"].call("sensors/voice/say", text="Hello"))
    t.start()
    assert inside.wait(5)
    assert wait_for(lambda: listener.events == ["mute"], 3)
    assert client.get("/api/voice").json()["speaking"] is True and parts["voice"].state["muted"] is True
    release.set()
    t.join(5)
    assert wait_for(lambda: listener.events == ["mute", "unmute"], 3)
    assert parts["gateway"].said[-1][1] == "Hello"
    parts["voice"].stop()


def test_it_keeps_trying_when_the_gx10_is_not_there_yet(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    parts["settings"].voice_url = f"http://127.0.0.1:{free_port()}"
    parts["voice"].start()
    assert wait_for(lambda: parts["voice"].state["state"] == "down", 5)
    assert "isn't answering" in parts["voice"].state["error"]
    assert client.get("/api/voice").json()["state"] == "down"
    parts["voice"].stop()


def test_typed_lines_still_go_through_the_same_judgement(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    assert client.post("/api/voice/heard", json={"text": "Yes I'm ready"}).json()["outcome"] == "unprompted"
    r = client.post("/api/voice/heard", json={"text": "ow, my shoulder hurts"}).json()
    assert r["pain"] is True and client.get("/api/safety").json()["stop"]["kind"] == "pain"


def test_the_robots_own_sentences_are_told_from_the_patients_words():
    said = [(time.time(), "Hi! Are you ready to start your workout?"), (time.time(), "Let's stop here. I'll let your therapist know.")]
    assert is_echo("Are you ready to start your workout", said)
    assert is_echo("let's stop here", said)                       # a stretch of what it said
    assert not is_echo("ready", said) and not is_echo("Yes, I'm ready", said) and not is_echo("stop", said)
    assert not is_echo("Are you ready to start your workout", [(time.time() - 120, said[0][1])])   # long ago


# ---- the Arm flow plan -----------------------------------------------------------------------

def test_the_arm_flow_plan_is_the_four_moves_asked_for():
    from workout.keyframes import duration_ms, mirrored
    from workout.plan import check_keyframes, load_plan
    plan = load_plan("arm_flow")
    assert [(e.name, e.sets, e.reps) for e in plan.exercises] == [
        ("Arm circles", 1, 3), ("Alternating reach", 1, 2), ("Overhead reach", 1, 2), ("Gentle hug", 1, 1)]
    reach = plan.exercises[1]
    assert reach.demo["alternate"] is True and all(e.mirror == "arms" for e in plan.exercises)
    other = mirrored(reach.demo["keyframes"])
    check_keyframes(other)                                         # the other side is as safe as the first
    assert duration_ms(other) == duration_ms(reach.demo["keyframes"])
    first, second = reach.demo["keyframes"][0]["servos"], other[0]["servos"]
    assert first["left_shoulder_pitch"] < 300 and first["right_shoulder_pitch"] == 275           # left reaches
    assert second["right_shoulder_pitch"] > 600 and second["left_shoulder_pitch"] == 725         # then right


def test_alternate_is_only_for_keyframes():
    from workout.plan import PlanError, parse_plan
    ex = {"name": "x", "cue": "c", "demo": {"arm": "forward", "alternate": True}, "sets": 1, "reps": 1,
          "track_joint": "shoulder_elevation"}
    with pytest.raises(PlanError, match="alternate"):
        parse_plan({"exercises": [ex]})


def test_alternating_reach_does_the_left_side_then_the_right_in_a_session(tmp_path, fake_robot):
    from workout.plan import load_plan
    client, parts = make(tmp_path)
    reach = load_plan("arm_flow").exercises[1]
    body = {"name": reach.name, "cue": reach.cue, "demo": reach.demo, "sets": 1, "reps": 2, "mirror": "arms",
            "track_joint": reach.track_joint, "targets": reach.targets, "tempo_s": reach.tempo_s}
    reached = []
    real = parts["gateway"].call

    def spy(path, **kwargs):
        t = kwargs.get("targets") or {}
        if path == "servo/set_many" and parts["live"].state.get("state") == "MIRROR":
            for side, test in (("left", lambda p: p < 300), ("right", lambda p: p > 600)):
                if test(t.get(f"{side}_shoulder_pitch", 400)) and (not reached or reached[-1] != side):
                    reached.append(side)
        return real(path, **kwargs)

    parts["gateway"].call = spy
    r = client.post("/api/live/start", json={"exercises": [body], "simulate": True, "pace": 8, "seed": 3})
    assert r.status_code == 200
    assert wait_for(lambda: not parts["live"].running, 60)
    assert parts["live"].state["last"]["status"] == "completed"
    assert reached[:2] == ["left", "right"], reached              # the robot leads with one side, then the other


# ---- streaming: the moment it is an answer, move on ----------------------------------------------

def test_the_session_moves_on_the_moment_the_patient_has_answered_not_after_they_pause(mic, tmp_path, fake_robot):
    url, listener, hub = mic
    client, parts = dashboard_on(tmp_path, url)
    tiny_plan(tmp_path)
    client.post("/api/live/start", json={"plan": "tiny", "simulate": False})
    assert wait_for(lambda: parts["live"].state.get("waiting"), 10)                 # "Are you ready?"
    began = time.time()
    listener.say("Yes", utt=7, final=False)                                          # the first word, mid-sentence
    assert wait_for(lambda: parts["live"].state.get("state") not in (None, "READY_CHECK"), 5)
    assert time.time() - began < 1.5                                                  # no waiting for silence
    assert parts["voice"].state["last"]["outcome"] == "answer"
    listener.say("Yes I'm ready", utt=7)                                              # the rest arrives; it is not a second answer
    assert wait_for(lambda: parts["voice"].state["last"]["text"] == "Yes I'm ready", 5)
    assert parts["voice"].state["last"]["outcome"] == "answer"
    client.post("/api/live/stop")
    assert wait_for(lambda: not parts["live"].running, 20)
    parts["voice"].stop()


def test_words_that_may_still_change_are_not_taken_until_they_settle(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    hear = parts["hear"]
    client.post("/api/live/start", json={"plan": "tiny", "simulate": False})
    assert wait_for(lambda: parts["live"].state.get("waiting"), 10)
    assert hear("No", 0.0, False, 1) == "hearing"                                     # "No problem, I'm ready" may follow
    assert hear("No problem", 0.0, False, 1) == "hearing"
    assert hear("No problem, I'm ready", 0.0, False, 1) == "answer"
    assert hear("No problem, I'm ready.", 0.0, True, 1) == "already"
    assert wait_for(lambda: parts["live"].state.get("state") not in (None, "READY_CHECK"), 10)
    client.post("/api/live/stop")
    assert wait_for(lambda: not parts["live"].running, 20)


def test_a_pain_word_stops_the_robot_mid_sentence(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    assert parts["hear"]("Ow", 0.0, False, 3) == "pain"                               # no waiting for the sentence to end
    assert client.get("/api/safety").json()["stop"]["kind"] == "pain"
    assert parts["hear"]("Ow, that really hurts", 0.0, True, 3) == "already"          # and it isn't a second stop
