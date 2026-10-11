"""
The single joint table. Every servo pulse in arm_mvp is computed here:

    pulse = stand + sign * ppd * (angle - neutral), clamped to [min, max]

Human angle conventions (degrees), one per joint kind:
  shoulder_pitch  0 arm down, -90 forward, -180 overhead (retarget.py / URDF convention)
  shoulder_roll   left arm positive outward, right arm negative outward (retarget.py / URDF)
  elbow           interior angle, 180 straight (so neutral is 180)
  hip_pitch       + thigh forward
  hip_roll        + abduction (thigh outward), same meaning on both sides
  knee            + flexion (shin swings back), 0 straight
  ankle_pitch     + dorsiflexion (toes up)
  ankle_roll      + sole turns the way hip abduction turns the thigh (flat foot: -hip_roll)
  hand            0 closed (the stand pulse), 1 fully open (ppd is pulses for the full travel)

Bus servos are 240 degrees per 1000 pulses. stand / min / max repeat the repo constants
(STAND_PULSES, SERVO_LIMITS tightened to robot_api/servo/limits.py) and a test keeps them equal.
safe_min / safe_max are extra caps on the human angle, deliberately conservative.

verified = the sign was confirmed against recorded robot poses (digital_twin/mapping.py) and
every source in the repo agrees. False means: do not trust the direction on hardware yet.
"""
import numpy as np

PPD = 1000.0 / 240.0
SAFE_CAPS_VERIFIED = False  # none of the safe_min / safe_max caps has been tested on hardware
ARM_STEP, LEG_STEP, HAND_STEP = 33, 12, 60  # max pulse change per tick (33 = robot_api's 1 pulse/ms at 30 Hz)

_COLUMNS = ("id", "name", "side", "group", "stand", "min", "max", "sign", "ppd", "neutral",
            "safe_min", "safe_max", "step", "verified", "positive")
_ROWS = [
    # id  name                    side group  stand min  max   sign ppd  neutral safe_min safe_max step    verified  + angle looks like
    (1, "left_ankle_roll", "L", "leg", 500, 395, 761, +1, PPD, 0.0, -15.0, 15.0, LEG_STEP, False, "left sole tilts to face outward (left)"),
    (2, "left_ankle_pitch", "L", "leg", 390, 50, 819, -1, PPD, 0.0, -40.0, 50.0, LEG_STEP, False, "left toes lift (dorsiflexion)"),
    (3, "left_knee", "L", "leg", 500, 0, 960, +1, PPD, 0.0, 0.0, 60.0, LEG_STEP, True, "left knee bends, shin swings back"),
    (4, "left_hip_pitch", "L", "leg", 600, 125, 857, -1, PPD, 0.0, -10.0, 40.0, LEG_STEP, True, "left thigh swings forward"),
    (5, "left_hip_roll", "L", "leg", 500, 375, 750, +1, PPD, 0.0, -5.0, 15.0, LEG_STEP, False, "left leg swings out to the left"),
    (6, "left_elbow", "L", "arm", 575, 125, 875, -1, PPD, 180.0, None, None, ARM_STEP, True, "left elbow straightens (interior angle grows)"),
    (7, "left_shoulder_roll", "L", "arm", 800, 125, 875, -1, PPD, 0.0, None, None, ARM_STEP, True, "left arm lifts out to the left"),
    (8, "left_shoulder_pitch", "L", "arm", 725, 0, 1000, +1, PPD, 0.0, None, None, ARM_STEP, True, "left arm swings backward"),
    (9, "right_ankle_roll", "R", "leg", 500, 366, 570, -1, PPD, 0.0, -15.0, 15.0, LEG_STEP, False, "right sole tilts to face outward (right)"),
    (10, "right_ankle_pitch", "R", "leg", 610, 140, 950, +1, PPD, 0.0, -40.0, 50.0, LEG_STEP, False, "right toes lift (dorsiflexion)"),
    (11, "right_knee", "R", "leg", 500, 40, 1000, -1, PPD, 0.0, 0.0, 60.0, LEG_STEP, True, "right knee bends, shin swings back"),
    (12, "right_hip_pitch", "R", "leg", 400, 141, 875, +1, PPD, 0.0, -10.0, 40.0, LEG_STEP, True, "right thigh swings forward"),
    (13, "right_hip_roll", "R", "leg", 500, 249, 624, -1, PPD, 0.0, -5.0, 15.0, LEG_STEP, False, "right leg swings out to the right"),
    (14, "right_elbow", "R", "arm", 425, 125, 875, +1, PPD, 180.0, None, None, ARM_STEP, True, "right elbow straightens (interior angle grows)"),
    (15, "right_shoulder_roll", "R", "arm", 200, 125, 875, -1, PPD, 0.0, None, None, ARM_STEP, True, "right arm drops inward (negative lifts it out to the right)"),
    (16, "right_shoulder_pitch", "R", "arm", 275, 31, 1000, -1, PPD, 0.0, None, None, ARM_STEP, True, "right arm swings backward"),
    (17, "right_hand", "R", "hand", 500, 300, 760, +1, 260.0, 0.0, 0.0, 1.0, HAND_STEP, False, "right hand opens"),
    (18, "left_hand", "L", "hand", 500, 240, 500, -1, 260.0, 0.0, 0.0, 1.0, HAND_STEP, False, "left hand opens"),
]
JOINTS = {row[0]: dict(zip(_COLUMNS, row)) for row in _ROWS}
BY_NAME = {j["name"]: j["id"] for j in JOINTS.values()}
GROUPS = {g: tuple(i for i, j in JOINTS.items() if j["group"] == g) for g in ("arm", "leg", "hand")}
BODY_IDS = GROUPS["leg"] + GROUPS["arm"]
ARM_SERVOS = {"L": (8, 7, 6), "R": (16, 15, 14)}       # shoulder pitch, shoulder roll, elbow
LEG_SERVOS = {"L": (4, 5, 3, 2, 1), "R": (12, 13, 11, 10, 9)}  # hip pitch, hip roll, knee, ankle pitch, ankle roll
ARM_REST = (0.0, 0.0, 180.0)  # the stand pose in arm angles


def sid(side, kind):
    """Servo id from a side and a joint kind, for example sid("L", "knee") == 3."""
    return BY_NAME[("left_" if side == "L" else "right_") + kind]


def pulse(servo, angle, safe=True):
    """Human angle -> pulse for one servo. Never leaves [min, max], whatever the input
    (NaN and inf fall back to the neutral angle). safe=False skips the safe_min / safe_max caps."""
    j = JOINTS[servo]
    angle = float(np.clip(angle, -360.0, 360.0)) if np.isfinite(angle) else j["neutral"]
    if safe and j["safe_min"] is not None:
        angle = min(max(angle, j["safe_min"]), j["safe_max"])
    return int(min(max(round(j["stand"] + j["sign"] * j["ppd"] * (angle - j["neutral"])), j["min"]), j["max"]))


def stand(groups=("leg", "arm")):
    return {i: JOINTS[i]["stand"] for g in groups for i in GROUPS[g]}


def rate_limit(prev, target):
    """Per tick pulse limit: each servo moves at most its table `step` toward its target."""
    return {i: int(prev[i] + min(max(p - prev[i], -JOINTS[i]["step"]), JOINTS[i]["step"])) for i, p in target.items()}


def safe_angle(servo, angle):
    """The angle `pulse` will actually use: finite and inside the joint's safe caps."""
    j = JOINTS[servo]
    angle = float(np.clip(angle, -360.0, 360.0)) if np.isfinite(angle) else j["neutral"]
    return angle if j["safe_min"] is None else min(max(angle, j["safe_min"]), j["safe_max"])


def arm_pulses(side, pitch, roll, elbow):
    """Arm angles (shoulder pitch, shoulder roll, interior elbow) -> {servo id: pulse}."""
    return {s: pulse(s, a) for s, a in zip(ARM_SERVOS[side], (pitch, roll, elbow))}


def leg_angles_flat_foot(side, hip_pitch, hip_roll, knee):
    """
    The five leg angles for one side with the ankle computed, not measured, so the sole stays
    parallel to the torso's horizontal plane (flat on the ground while the torso is upright).
    Robot chain: hip roll (x), hip pitch (y), knee (y), ankle pitch (y), ankle roll (x).
    In the human conventions of this table, thigh forward tips the foot toes up and knee
    flexion tips it toes down, so:
        ankle_pitch (+ dorsiflexion) = knee - hip_pitch
        ankle_roll                   = -hip_roll
    Both are derived from the capped hip and knee angles, the ones the servos really get.
    """
    ids = LEG_SERVOS[side]
    hip_pitch, hip_roll, knee = (safe_angle(s, a) for s, a in zip(ids, (hip_pitch, hip_roll, knee)))
    return hip_pitch, hip_roll, knee, knee - hip_pitch, -hip_roll


def leg_pulses(side, hip_pitch, hip_roll, knee):
    """Leg angles (hip pitch, hip abduction, knee flexion) -> {servo id: pulse} for all five leg servos."""
    return {s: pulse(s, a) for s, a in zip(LEG_SERVOS[side], leg_angles_flat_foot(side, hip_pitch, hip_roll, knee))}
