"""The camera picture with what the tracker found drawn on it: the tracked person boxed and each arm
coloured by how well it is seen. Used where the frame is (the GX10 when it owns the camera, the
dashboard when it holds a webcam), so every screen shows the same picture."""
from __future__ import annotations

COLORS = {"TRACKING": (80, 220, 80), "HOLD": (0, 210, 255), "LOST": (60, 60, 255)}  # BGR
PERSON_COLOR = (255, 190, 40)   # BGR: the box round whoever is being tracked


def scale_overlay(overlay: dict, k: float) -> dict:
    """The overlay for a picture `k` times the size of the one that was tracked (it was sent smaller)."""
    if k == 1.0 or not overlay.get("shoulders"):
        return overlay
    out = {**overlay,
           "shoulders": [[x * k, y * k] for x, y in overlay["shoulders"]],
           "arms": {a: {**v, "pts": [[x * k, y * k] for x, y in v["pts"]]} for a, v in overlay["arms"].items()}}
    if overlay.get("person"):
        out["person"] = [v * k for v in overlay["person"]]
    return out


def annotate(frame, overlay: dict, people: int = 0, quality: int = 80) -> bytes | None:
    """`frame` (BGR) with the tracked person and arms drawn on it, as a JPEG."""
    import cv2
    view = frame.copy()
    if overlay.get("person"):
        x0, y0, x1, y1 = (int(v) for v in overlay["person"])
        cv2.rectangle(view, (x0, y0), (x1, y1), PERSON_COLOR, 2, cv2.LINE_AA)
        label = "Tracking" if people <= 1 else f"Tracking 1 of {people}"
        cv2.putText(view, label, (x0 + 4, max(16, y0 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, PERSON_COLOR, 2, cv2.LINE_AA)
    if overlay.get("shoulders"):
        a, b = (tuple(int(v) for v in p) for p in overlay["shoulders"])
        cv2.line(view, a, b, (220, 220, 220), 1, cv2.LINE_AA)
        for arm in overlay["arms"].values():
            pts = [tuple(int(v) for v in p) for p in arm["pts"]]
            for p, q in zip(pts, pts[1:]):
                cv2.line(view, p, q, COLORS.get(arm["state"], COLORS["LOST"]), 3, cv2.LINE_AA)
            for p in pts:
                cv2.circle(view, p, 5, (255, 255, 255), -1)
    ok, buf = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None
