"""
Monocular depth estimation + fusion with MediaPipe world landmarks.

MediaPipe's per-joint z is its weakest axis (arms pointing at the camera are
badly foreshortened). This module adds a depth map from Depth Anything V2
(Small) and fuses it into the world landmarks so every joint gets a measured
depth:

1. Sample the (relative, disparity-like) depth map at each landmark's pixel.
2. Anchor it to metres: torso depth Z0 = f * shoulder_width_3D / shoulder_width_px,
   then joint depth Z_i = Z0 * d_torso / d_i  (disparity ~ 1/Z).
3. Blend the depth-derived z with MediaPipe's z using per-joint confidence.
4. Rebuild the arm/leg chains so MediaPipe's bone lengths are preserved
   (depth only changes the *direction* of each segment, never its length).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np

# MediaPipe pose indices
L_SHOULDER, R_SHOULDER = 11, 12
L_ELBOW, R_ELBOW = 13, 14
L_WRIST, R_WRIST = 15, 16
L_HIP, R_HIP = 23, 24
L_KNEE, R_KNEE = 25, 26
L_ANKLE, R_ANKLE = 27, 28

TORSO = (L_SHOULDER, R_SHOULDER, L_HIP, R_HIP)
# (parent, child) chains, ordered root -> tip
CHAINS = (
    (L_SHOULDER, L_ELBOW), (L_ELBOW, L_WRIST),
    (R_SHOULDER, R_ELBOW), (R_ELBOW, R_WRIST),
    (L_HIP, L_KNEE), (L_KNEE, L_ANKLE),
    (R_HIP, R_KNEE), (R_KNEE, R_ANKLE),
)
# Extremity landmarks that follow their chain tip (hand points, foot points)
FOLLOWERS = {
    L_WRIST: (17, 19, 21), R_WRIST: (18, 20, 22),
    L_ANKLE: (29, 31), R_ANKLE: (30, 32),
}

DEFAULT_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"


class DepthEstimator:
    """Depth Anything V2 wrapper. Sets `available=False` (never raises) if it can't load."""

    def __init__(self, model_name: str = DEFAULT_MODEL, input_w: int = 322, input_h: int = 238):
        # Sizes must be multiples of 14 (ViT patch size)
        self.input_w, self.input_h = input_w, input_h
        self.available = False
        self.error = ""
        try:
            import torch
            from transformers import AutoModelForDepthEstimation

            self._torch = torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model = AutoModelForDepthEstimation.from_pretrained(model_name).to(self.device).eval()
            self._mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
            self._std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
            self.available = True
        except Exception as exc:  # missing deps, no network for weights, etc.
            self.error = f"{type(exc).__name__}: {exc}"

    def estimate(self, bgr_frame: np.ndarray) -> Optional[np.ndarray]:
        """Returns a float32 relative-disparity map (larger = closer) at network resolution."""
        if not self.available:
            return None
        import cv2

        small = cv2.resize(bgr_frame, (self.input_w, self.input_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        x = np.ascontiguousarray(((rgb - self._mean) / self._std).transpose(2, 0, 1)[None])
        with self._torch.no_grad():
            out = self.model(pixel_values=self._torch.from_numpy(x).to(self.device)).predicted_depth
        return out[0].float().cpu().numpy()


def _sample(depth: np.ndarray, x: float, y: float, r: int = 2) -> Tuple[float, float]:
    """Median and MAD of a (2r+1)^2 patch at normalized image coords (x, y)."""
    h, w = depth.shape
    cx = int(np.clip(round(x * (w - 1)), 0, w - 1))
    cy = int(np.clip(round(y * (h - 1)), 0, h - 1))
    patch = depth[max(0, cy - r):cy + r + 1, max(0, cx - r):cx + r + 1]
    med = float(np.median(patch))
    return med, float(np.median(np.abs(patch - med)))


def fuse_world_landmarks(
    world_lms,
    img_lms,
    depth_map: Optional[np.ndarray],
    frame_size: Tuple[int, int] = (640, 480),
    focal_px: Optional[float] = None,
    weight: float = 0.6,
    min_vis: float = 0.3,
):
    """
    Returns (fused_world_landmarks, info). Falls back to the input on any failure.
    `frame_size` is (width, height) of the image the landmarks were detected on.
    """
    from mediapose_est import LandmarkListWrapper, LandmarkPoint  # local import: avoids cycle

    info: Dict[str, float] = {"active": 0.0, "mean_weight": 0.0, "z0_m": 0.0}
    if depth_map is None or world_lms is None or img_lms is None:
        return world_lms, info

    fw, fh = frame_size
    f = float(focal_px) if focal_px else 0.9 * fw  # ~58 deg HFOV default

    pts = np.array([[l.x, l.y, l.z] for l in world_lms], dtype=np.float64)
    vis = np.array([min(l.visibility, getattr(l, "presence", 1.0)) for l in world_lms])

    d, mad = np.zeros(33), np.zeros(33)
    for i in range(33):
        d[i], mad[i] = _sample(depth_map, img_lms[i].x, img_lms[i].y)

    if min(vis[list(TORSO)]) < min_vis:
        return world_lms, info
    d0 = float(np.median(d[list(TORSO)]))
    if d0 < 1e-6:
        return world_lms, info

    # Metric anchor from shoulder width (3D metres vs. projected pixels)
    sw_m = float(np.linalg.norm(pts[L_SHOULDER] - pts[R_SHOULDER]))
    sw_px = float(np.hypot((img_lms[L_SHOULDER].x - img_lms[R_SHOULDER].x) * fw,
                           (img_lms[L_SHOULDER].y - img_lms[R_SHOULDER].y) * fh))
    if sw_m < 1e-3 or sw_px < 5.0:
        return world_lms, info
    z0 = float(np.clip(f * sw_m / sw_px, 0.5, 6.0))

    # Per-joint camera depth from disparity ratio, expressed relative to mid-hip
    ratio = np.clip(d0 / np.maximum(d, 0.2 * d0), 0.6, 1.6)
    z_cam = z0 * ratio
    hip_ref = float(np.mean(z_cam[[L_HIP, R_HIP]]))
    z_depth = z_cam - hip_ref  # same sign convention as MediaPipe: smaller = closer

    # Confidence: visibility, and patch consistency (depth edges are unreliable)
    edge = np.clip(mad / (0.15 * d0), 0.0, 1.0)
    w = np.clip(weight * vis * (1.0 - 0.8 * edge), 0.0, 1.0)
    w[list(TORSO)] = 0.0  # keep MediaPipe's torso frame stable

    fused = pts.copy()
    fused[:, 2] = w * z_depth + (1.0 - w) * pts[:, 2]

    # Rebuild chains root -> tip, preserving MediaPipe bone lengths
    for parent, child in CHAINS:
        length = float(np.linalg.norm(pts[child] - pts[parent]))
        vec = fused[child] - fused[parent]
        n = float(np.linalg.norm(vec))
        if n > 1e-6 and length > 1e-6:
            fused[child] = fused[parent] + vec * (length / n)
    for tip, followers in FOLLOWERS.items():
        delta = fused[tip] - pts[tip]
        for idx in followers:
            fused[idx] = pts[idx] + delta

    out = [LandmarkPoint(fused[i, 0], fused[i, 1], fused[i, 2], world_lms[i].visibility,
                         getattr(world_lms[i], "presence", 1.0)) for i in range(33)]
    info.update(active=1.0, mean_weight=float(np.mean(w[11:33])), z0_m=z0)
    return LandmarkListWrapper(out), info


def colorize_depth(depth_map: np.ndarray, size: Tuple[int, int]) -> np.ndarray:
    """BGR visualization of a depth map resized to (width, height)."""
    import cv2

    lo, hi = float(np.percentile(depth_map, 2)), float(np.percentile(depth_map, 98))
    norm = np.clip((depth_map - lo) / max(hi - lo, 1e-6), 0, 1)
    return cv2.resize(cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_INFERNO), size)


class AsyncDepth:
    """Runs a DepthEstimator on the latest submitted frame in a worker thread (~4 Hz on CPU)."""

    def __init__(self, estimator: DepthEstimator):
        import threading

        self.estimator = estimator
        self._lock = threading.Lock()
        self._pending: Optional[np.ndarray] = None
        self._latest: Optional[np.ndarray] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, bgr_frame: np.ndarray) -> None:
        with self._lock:
            self._pending = bgr_frame.copy()
        self._wake.set()

    def latest(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._latest

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                frame, self._pending = self._pending, None
            if frame is None:
                continue
            try:
                depth = self.estimator.estimate(frame)
            except Exception:
                continue
            with self._lock:
                self._latest = depth

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
