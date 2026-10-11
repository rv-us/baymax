"""
Several people in the frame: track the one closest to the centre.

The camera is aimed at the patient, so whoever stands nearest the middle of the picture is the
person to mirror and to score. Everyone else (a therapist, a relative, someone walking past) is
ignored. MediaPipe is asked for up to MAX_PEOPLE poses and `Selector` picks one per frame.

Two people at about the same distance from the centre would otherwise swap the tracking every
few frames, so the person already being tracked is kept unless someone else is clearly closer
(by SWITCH_MARGIN of the frame). A real change of person is reported, so the caller can reset
the arm filters instead of reading the new person's arms through the old person's smoothing.
"""
import math

import numpy as np

MAX_PEOPLE = 4
TORSO = (11, 12, 23, 24)   # shoulders and hips
SWITCH_MARGIN = 0.08       # another person must be this much closer (normalised) to take over
SAME_PERSON = 0.25         # a torso that moved less than this between frames is the same person
FORGET_AFTER = 15          # frames with nobody in view before the tracked person is forgotten


def torso_center(img, vis, min_vis: float = 0.5):
    """Where a person is in the frame: the middle of their visible torso landmarks (normalised x, y)."""
    img, vis = np.asarray(img), np.asarray(vis)
    for idx, floor in ((TORSO, min_vis), (range(len(img)), 0.2)):
        idx = [i for i in idx if vis[i] >= floor and np.all(np.isfinite(img[i, :2]))]
        if idx:
            return float(img[idx, 0].mean()), float(img[idx, 1].mean())
    return float(np.nanmean(img[:, 0])), float(np.nanmean(img[:, 1]))


class Selector:
    def __init__(self, margin: float = SWITCH_MARGIN):
        self.margin = margin
        self.center = None    # where the tracked person was last seen
        self.missed = 0
        self.people = 0       # how many were in the last frame
        self.switched = False # the last choice is a different person from the one before it

    def choose(self, centers):
        """Index into `centers` (one (x, y) per person) of the person to track, or None."""
        self.people, self.switched = len(centers), False
        if not centers:
            self.missed += 1
            if self.missed >= FORGET_AFTER:
                self.center = None
            return None
        self.missed = 0
        dist = [math.hypot(x - 0.5, y - 0.5) for x, y in centers]
        best = min(range(len(centers)), key=dist.__getitem__)
        if self.center is not None:
            gap = [math.hypot(x - self.center[0], y - self.center[1]) for x, y in centers]
            same = min(range(len(centers)), key=gap.__getitem__)
            if gap[same] <= SAME_PERSON:
                if best != same and dist[best] + self.margin >= dist[same]:
                    best = same                       # not clearly closer: stay with who we had
                self.switched = best != same
            else:
                self.switched = True                  # the person we had is gone: someone new
        self.center = centers[best]
        return best

    def pick(self, poses_img, poses_vis, min_vis: float = 0.5):
        """Index of the person to track among MediaPipe's poses (lists of (33, 3) arrays and (33,) visibilities)."""
        return self.choose([torso_center(i, v, min_vis) for i, v in zip(poses_img, poses_vis)])
