"""Synthetic tests for depth_fusion (no model download required)."""
import numpy as np

from depth_fusion import L_ELBOW, L_HIP, L_SHOULDER, L_WRIST, R_HIP, R_SHOULDER, fuse_world_landmarks
from mediapose_est import LandmarkListWrapper, LandmarkPoint


def build(elbow_z, wrist_z):
    img = [LandmarkPoint(0.5, 0.5, 0, 1.0) for _ in range(33)]
    world = [LandmarkPoint(0, 0, 0, 1.0) for _ in range(33)]

    def put(i, ix, iy, wx, wy, wz):
        img[i] = LandmarkPoint(ix, iy, 0, 1.0)
        world[i] = LandmarkPoint(wx, wy, wz, 1.0)

    put(L_SHOULDER, 0.55, 0.3, 0.2, -0.3, 0.0)
    put(R_SHOULDER, 0.45, 0.3, -0.2, -0.3, 0.0)
    put(L_HIP, 0.53, 0.6, 0.1, 0.0, 0.0)
    put(R_HIP, 0.47, 0.6, -0.1, 0.0, 0.0)
    put(L_ELBOW, 0.60, 0.40, 0.25, -0.15, elbow_z)
    put(L_WRIST, 0.60, 0.50, 0.25, -0.02, wrist_z)
    return LandmarkListWrapper(img), LandmarkListWrapper(world)


def seg_len(lms, a, b):
    return float(np.linalg.norm([lms[a].x - lms[b].x, lms[a].y - lms[b].y, lms[a].z - lms[b].z]))


def test_depth_moves_wrist_and_keeps_bones():
    # MediaPipe puts the wrist level with the shoulder; the depth map says it is much closer.
    img, world = build(elbow_z=0.0, wrist_z=0.0)
    depth = np.ones((100, 100), dtype=np.float32)
    depth[45:56, 55:66] = 1.4  # wrist patch: closer to camera
    fused, info = fuse_world_landmarks(world, img, depth, (640, 480))
    assert info["active"] == 1.0
    assert fused[L_WRIST].z < -0.02, fused[L_WRIST].z
    for a, b in ((L_SHOULDER, L_ELBOW), (L_ELBOW, L_WRIST)):
        assert abs(seg_len(world, a, b) - seg_len(fused, a, b)) < 1e-6
    assert fused[L_SHOULDER].z == world[L_SHOULDER].z
    print("[PASS] wrist pulled toward camera (z=%.3f), bone lengths and torso preserved" % fused[L_WRIST].z)


def test_flat_depth_is_nearly_noop():
    img, world = build(elbow_z=-0.05, wrist_z=-0.1)
    fused, _ = fuse_world_landmarks(world, img, np.ones((50, 50), np.float32), (640, 480))
    assert abs(fused[L_WRIST].z - world[L_WRIST].z) < 0.25
    print("[PASS] flat depth map keeps pose near MediaPipe's")


def test_no_depth_passthrough():
    img, world = build(0.0, 0.0)
    fused, info = fuse_world_landmarks(world, img, None)
    assert fused is world and info["active"] == 0.0
    print("[PASS] missing depth map is a passthrough")


if __name__ == "__main__":
    test_depth_moves_wrist_and_keeps_bones()
    test_flat_depth_is_nearly_noop()
    test_no_depth_passthrough()
    print("ALL DEPTH FUSION TESTS PASSED")
