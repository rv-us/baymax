import os
import sys

os.environ["BAYMAX_DRY_RUN"] = "1"  # must precede any robot_api import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fake_robot():
    from robot_api import hardware
    from robot_api.fake import STATE

    STATE.reset()
    hardware._last_cool_check = 0.0
    yield STATE
