"""Signal filters for the arm mimic pipeline: One Euro (on 3D points) and a slew limit (on angles)."""
import math

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
