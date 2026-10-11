from robot_api.api import LocalBackend
from robot_api.tools import make_tools


def tools_by_name():
    return {t.name: t for t in make_tools(LocalBackend())}


def test_tool_schemas_have_descriptions():
    tools = tools_by_name()
    assert len(tools) >= 25
    for t in tools.values():
        assert t.description.strip(), t.name
        t.args  # schema builds


def test_tool_returns_error_instead_of_raising(fake_robot):
    fake_robot.temps[11] = 79
    out = tools_by_name()["walk"].invoke({"direction": "forward", "steps": 1})
    assert "error" in out and "servo 11" in out["error"]


def test_tool_happy_path(fake_robot):
    t = tools_by_name()
    assert t["read_distance"].invoke({})["distance_mm"] == 800
    out = t["set_servos"].invoke({"targets": {"left_elbow": 600, "14": 400}})
    assert out["moved"] == {"left_elbow": 600, "right_elbow": 400}
    assert "cleared" in t["show_on_matrix"].invoke({"text": ""})
