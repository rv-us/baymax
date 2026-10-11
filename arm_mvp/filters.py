"""Signal filters for the arm mimic pipeline: One Euro (on 3D points), a slew limit (on angles) and a
damper on the servo pulses that go to the robot."""
import math
from collections import deque

import numpy as np


def _alpha(cutoff, dt):
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuro:
    """
    One Euro filter (Casiez et al. 2012) over a numpy array of any shape. Low cutoff when the
    signal is still (kills jitter), cutoff rising with speed (kills lag): cutoff = min_cutoff
    + beta * |speed|. Units here are metres and seconds, so beta is in Hz per (m/s).
    """

    def __init__(self, min_cutoff=1.5, beta=4.0, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self):
        self.x = self.dx = self.t = None

    def __call__(self, x, t):
        x = np.asarray(x, dtype=np.float64)
        if self.x is None or t <= self.t:
            self.x, self.dx, self.t = x.copy(), np.zeros_like(x), t
            return self.x
        dt = t - self.t
        a_d = _alpha(self.d_cutoff, dt)
        self.dx = a_d * (x - self.x) / dt + (1.0 - a_d) * self.dx
        a = 1.0 / (1.0 + 1.0 / (2.0 * math.pi * (self.min_cutoff + self.beta * np.abs(self.dx)) * dt))
        self.x, self.t = a * x + (1.0 - a) * self.x, t
        return self.x


def slew(value, target, max_step_deg, dt=1.0 / 30.0):
    """Move value toward target by at most max_step_deg per frame at 30 fps (scaled by real dt).
    Same limit as mediapose_est.JointFilter, without its EMA."""
    limit = max_step_deg * min(max(dt, 1.0 / 120.0), 0.2) * 30.0
    return value + min(max(target - value, -limit), limit)


class PulseDamper:
    """
    The last filter before the robot: servo pulses that follow the camera without passing its noise on.

    Camera noise shows up as the arms trembling and the odd frame where a joint jumps. Three things
    keep the robot calm:
      - a median of the last `window` targets (five), so a burst of wild frames up to two long (the camera's
        depth guesses flip for a few frames at a time) never moves the robot at all, for about two frames
        of delay;
      - a dead band, so a target within `deadband` pulses of where the servo already is does nothing
        (a few pulses is about a degree: invisible, but it is what makes a servo buzz);
      - a gain that is low for small differences (heavy smoothing) and rises toward `fast` for big
        ones, so a real, deliberate move is followed without lag while jitter is damped.
    `start` is {servo id: pulse}, where the arms are now (the stand pose).
    """

    def __init__(self, start, deadband=8, slow=0.2, fast=0.7, knee=120, max_step=33, window=5):
        self.window = window
        self.value = dict(start)
        self.recent = {sid: deque(maxlen=window) for sid in self.value}
        self.deadband, self.slow, self.fast, self.knee, self.max_step = deadband, slow, fast, knee, max_step

    def reset(self, start):
        self.value = dict(start)
        self.recent = {sid: deque(maxlen=self.window) for sid in self.value}

    def __call__(self, target):
        out = {}
        for sid, goal in target.items():
            self.recent[sid].append(goal)
            goal = sorted(self.recent[sid])[len(self.recent[sid]) // 2]   # median of what we have so far
            x = self.value[sid]
            err = goal - x
            if abs(err) > self.deadband:
                gain = self.slow + (self.fast - self.slow) * min(1.0, (abs(err) - self.deadband) / self.knee)
                x += max(-self.max_step, min(self.max_step, gain * err))
                self.value[sid] = int(round(x))
            out[sid] = self.value[sid]
        return out
