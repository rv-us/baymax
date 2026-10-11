"""In-memory stand-ins for the Hiwonder drivers, used when BAYMAX_DRY_RUN=1.

They record every command in STATE.log and answer reads from STATE, so tests
can set a servo temperature and check that motion is refused.
"""
from __future__ import annotations

import threading


class FakeState:
    def __init__(self):
        self.reset()

    def reset(self):
        from .servo.joints import STAND_POSE

        self.positions = dict(STAND_POSE)
        self.temps = {i: 40 for i in range(1, 17)}
        self.torque = {i: 1 for i in range(1, 17)}
        self.battery_mv = 12400
        self.distance_mm = 800
        self.climate = (22.5, 45)
        self.bright = True
        self.log: list[tuple] = []


STATE = FakeState()


class FakeBoard:
    def bus_servo_set_position(self, duration_s, positions):
        STATE.log.append(("bus_servo_set_position", duration_s, [list(p) for p in positions]))
        for servo_id, pulse in positions:
            STATE.positions[servo_id] = pulse
            if servo_id in STATE.torque:
                STATE.torque[servo_id] = 1

    def bus_servo_stop(self, servo_ids):
        STATE.log.append(("bus_servo_stop", list(servo_ids)))

    def pwm_servo_set_position(self, duration_s, positions):
        STATE.log.append(("pwm_servo_set_position", duration_s, [list(p) for p in positions]))

    def set_buzzer(self, freq, on_time, off_time, repeat=1):
        STATE.log.append(("set_buzzer", freq, on_time, off_time, repeat))


class FakeCtl:
    def __init__(self):
        self.board = FakeBoard()

    def unload_bus_servo(self, servo_id):
        STATE.log.append(("unload_bus_servo", servo_id))
        STATE.torque[servo_id] = 0

    def set_pwm_servo_pulse(self, servo_id, pulse, use_time):
        self.board.pwm_servo_set_position(use_time / 1000, [[servo_id, pulse]])


class FakeAGC:
    """Mimics the hiwonder.ActionGroupControl module."""

    def __init__(self):
        self.ctl = FakeCtl()
        self.board = self.ctl.board
        self.runningAction = False

    def runActionGroup(self, name, times=1, with_stand=False):
        from .servo.joints import STAND_POSE

        STATE.log.append(("runActionGroup", name, times, with_stand))
        STATE.positions.update(STAND_POSE)

    def stopAction(self):
        STATE.log.append(("stopAction",))

    def stopActionGroup(self):
        STATE.log.append(("stopActionGroup",))


class FakeReader:
    lock = threading.Lock()

    def read(self, servo_id, what):
        if servo_id > 16:
            return None  # hand servos never answer reads on the real robot
        return {
            "position": STATE.positions.get(servo_id),
            "temp": STATE.temps.get(servo_id),
            "vin": 12400,
            "torque": STATE.torque.get(servo_id),
        }[what]

    def battery_mv(self, timeout=2.0):
        return STATE.battery_mv

    def buzzer(self, freq, on_s, off_s, repeat):
        STATE.log.append(("set_buzzer", freq, on_s, off_s, repeat))
