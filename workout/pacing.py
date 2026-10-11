"""
Pace leading: the robot demonstrates a little ahead of the patient, never far ahead.

Progress is counted in reps: 2.25 means two reps done and a quarter of the third. The
robot aims to be LEAD (25% of a rep) ahead of the patient and is never allowed further,
so a slow patient is never left behind, and a patient who stops simply gets a robot
that waits one quarter-rep ahead. Moving faster than the patient is also capped, at
CATCH_UP times the exercise's own tempo, so it never lurches.

    est = ProgressEstimator(target)       # target: {"top", "bottom", "tempo_s"}
    leader = PaceLeader(tempo_s, reps)
    progress = est.update(angle)          # patient, from the tracked joint angle
    ahead = leader.step(progress, dt)     # robot's progress; leader.angle(target) its joint angle

Rep shape matches the synthetic patient and the stored reference (a raised cosine), so
the angle -> phase map is its exact inverse.
"""
import math

LEAD = 0.25        # reps the robot aims to be ahead of the patient
CATCH_UP = 1.5     # fastest the robot moves, as a multiple of the exercise's tempo
MAX_JUMP = 0.3     # a bigger single-sample leap in estimated progress looks like a tracking glitch
TURN_MIN_DEG = 5.0  # the angle must retreat this far (or TURN_FRAC of the range) from its extreme to count as a turn
TURN_FRAC = 0.12
REAL_REP = 0.35     # a rise smaller than this share of the range is a wobble, not a rep
MAX_GAP = 0.75     # the robot holds while the two graphs are further apart than this share of the range
RESYNC_AFTER = 4   # ...unless it persists for this many samples in a row


def angle_at(phase: float, target: dict) -> float:
    """Joint angle at `phase` (0..1 through one rep) of the rep shape."""
    bottom, top = target.get("bottom", 0.0), target.get("top", 90.0)
    return bottom + (top - bottom) * (0.5 - 0.5 * math.cos(2 * math.pi * (phase % 1.0)))


class ProgressEstimator:
    """Where the patient is, in reps, from the tracked angle alone. Never goes backwards.

    Patients rarely reach the target angle, so the rep is read against the patient's own
    range: on the way up against the peak of their last rep (the target for the first),
    on the way down against the peak they just made. A turn-around is the half-way mark.
    """

    def __init__(self, target: dict, start: float = 0.0, smooth: int = 2):
        self.bottom = target.get("bottom", 0.0)
        self.top = target.get("top", 90.0)
        self.progress = float(start)
        self.cycle = math.floor(start)
        self.phase = start - self.cycle
        self.rising = self.phase < 0.5
        self.peak = self.expected = self.top   # this rep's high point, and what the last one reached
        self.recent: list = []
        self.smooth = smooth
        self.rejected = 0
        self.extreme = None   # highest angle this way up, or lowest this way down

    def update(self, angle):
        if angle is None:  # no pose this frame: hold
            return self.progress
        self.recent = (self.recent + [angle])[-(self.smooth + 1):]
        raw, angle = angle, sum(self.recent) / len(self.recent)   # turns are judged on the smoothed angle
        span = max(10.0, self.top - self.bottom)
        margin = max(TURN_MIN_DEG, TURN_FRAC * span)
        if self.extreme is None:
            self.extreme = angle
        if self.rising:
            self.extreme = max(self.extreme, angle)
            # A turn-around only counts once the patient has really gone up, then come back by a margin.
            if angle < self.extreme - margin and self.extreme - self.bottom >= REAL_REP * span:
                self.rising, self.extreme = False, angle
                self.expected = max(self.bottom + 10.0, self.peak)  # what this patient reaches
        else:
            self.extreme = min(self.extreme, angle)
            if angle > self.extreme + margin:
                self.rising, self.extreme = True, angle
                self.peak = angle                             # a new rep begins
        if self.rising:
            self.peak = max(self.peak, raw)
        # Phase follows the first sign of a turn; only the committed turn above needs the margin.
        if self.rising:
            down = self.extreme - self.bottom >= REAL_REP * span and angle < self.extreme - 1.5
        else:
            down = angle <= self.extreme + 1.5
        if not down:
            u = (raw - self.bottom) / max(10.0, self.expected - self.bottom)
            phase = min(0.5, math.acos(1 - 2 * max(0.0, min(1.0, u))) / (2 * math.pi))
        else:
            u = (raw - self.bottom) / max(10.0, self.peak - self.bottom)
            phase = max(0.5, 1 - math.acos(1 - 2 * max(0.0, min(1.0, u))) / (2 * math.pi))
        if self.phase > 0.75 and phase < 0.25:
            self.cycle += 1
        elif self.phase < 0.25 and phase > 0.75 and self.cycle > 0:
            return self.progress                              # jitter at the bottom, not a step back
        self.phase = phase
        new = self.cycle + phase
        if new <= self.progress:
            return self.progress
        # One wild leap is a glitch, but a leap that keeps being the answer is the truth:
        # take it after a few samples rather than freezing the robot for the rest of the set.
        self.rejected = self.rejected + 1 if new - self.progress > MAX_JUMP else 0
        if self.rejected and self.rejected < RESYNC_AFTER:
            return self.progress
        self.rejected = 0
        self.progress = new
        return new


class PaceLeader:
    def __init__(self, tempo_s: float, reps: int, start: float = 0.0, lead: float = LEAD):
        self.tempo = max(1.0, float(tempo_s))
        self.reps = reps
        self.lead = lead
        self.progress = float(start)

    def goal(self, patient: float) -> float:
        return min(float(self.reps), patient + self.lead)

    def step(self, patient: float, dt: float, gap: float = 0.0) -> float:
        """Move toward `lead` ahead of the patient, at most CATCH_UP x the natural speed.

        `gap` is the direct error between the robot's and the patient's joint angles as a share
        of the range (what the live graph shows). A quarter-rep lead never makes it above
        MAX_GAP, so more than that means the progress estimate is wrong or the patient is not
        following: the robot waits whatever the estimate says.
        """
        if gap > MAX_GAP:
            return self.progress
        goal = self.goal(patient)
        if self.progress < goal:
            self.progress = min(goal, self.progress + CATCH_UP / self.tempo * dt)
        return self.progress

    def ahead_of(self, patient: float) -> float:
        return self.progress - patient

    def angle(self, target: dict) -> float:
        return angle_at(self.progress, target)

    def phase(self) -> float:
        return self.progress - math.floor(self.progress)
