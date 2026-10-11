"""Minimal pure-Python URDF reader, forward kinematics and floor-contact solver.

Frames follow the URDF/ROS convention used by ``tonypi.urdf``: x forward, y left, z up,
origin at the torso centre. No third-party dependencies: the twin must run wherever the
pose estimator does, and the browser does its own rendering from :meth:`Robot.to_model`.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

Vec = Tuple[float, float, float]
Mat = Tuple[Vec, Vec, Vec]
Pose = Tuple[Mat, Vec]  # (rotation, translation)

IDENTITY: Mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


# ------------------------------------------------------------------ small linear algebra
def mat_mul(a: Mat, b: Mat) -> Mat:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))  # type: ignore


def mat_vec(a: Mat, v: Vec) -> Vec:
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))  # type: ignore


def vec_add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vec_norm(v: Vec) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def vec_unit(v: Vec) -> Vec:
    n = vec_norm(v)
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-12 else (0.0, 0.0, 1.0)


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def rot_rpy(r: float, p: float, y: float) -> Mat:
    """URDF fixed-axis roll-pitch-yaw: R = Rz(y) Ry(p) Rx(r)."""
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return ((cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
            (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
            (-sp, cp * sr, cp * cr))


def rot_axis(axis: Vec, angle: float) -> Mat:
    """Rodrigues rotation about a unit axis."""
    x, y, z = vec_unit(axis)
    c, s = math.cos(angle), math.sin(angle)
    t = 1.0 - c
    return ((t * x * x + c, t * x * y - s * z, t * x * z + s * y),
            (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
            (t * x * z - s * y, t * y * z + s * x, t * z * z + c))


def rotation_between(a: Vec, b: Vec) -> Mat:
    """Smallest rotation taking unit vector a onto unit vector b."""
    a, b = vec_unit(a), vec_unit(b)
    c = dot(a, b)
    if c > 1.0 - 1e-12:
        return IDENTITY
    if c < -1.0 + 1e-12:
        helper = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)
        return rot_axis(vec_unit(cross(a, helper)), math.pi)
    axis = cross(a, b)
    return rot_axis(axis, math.atan2(vec_norm(axis), c))


def mat_to_quat(m: Mat) -> Tuple[float, float, float, float]:
    """Rotation matrix -> (x, y, z, w)."""
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        return ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, 0.25 * s)
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        return (0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s)
    if m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        return ((m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s)
    s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
    return ((m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s, (m[1][0] - m[0][1]) / s)


def _floats(text: Optional[str], n: int = 3, default: float = 0.0) -> Tuple[float, ...]:
    if not text:
        return (default,) * n
    return tuple(float(x) for x in text.split())


# ------------------------------------------------------------------ robot description
class Robot:
    """Parsed URDF: links with visuals, joints with origins/axes/limits."""

    def __init__(self, path: str):
        root = ET.parse(path).getroot()
        self.name = root.get("name", "robot")
        self.materials: Dict[str, List[float]] = {}
        for m in root.findall("material"):
            c = m.find("color")
            if c is not None:
                self.materials[m.get("name", "")] = list(_floats(c.get("rgba"), 4, 1.0))

        self.links: Dict[str, List[dict]] = {}
        for link in root.findall("link"):
            visuals = []
            for v in link.findall("visual"):
                geom = v.find("geometry")
                shape = next(iter(geom), None) if geom is not None else None
                if shape is None:
                    continue
                org = v.find("origin")
                mat = v.find("material")
                vis = {
                    "type": shape.tag,
                    "xyz": list(_floats(org.get("xyz") if org is not None else None)),
                    "rpy": list(_floats(org.get("rpy") if org is not None else None)),
                    "material": mat.get("name") if mat is not None else None,
                }
                if shape.tag == "box":
                    vis["size"] = list(_floats(shape.get("size")))
                elif shape.tag == "cylinder":
                    vis["radius"] = float(shape.get("radius"))
                    vis["length"] = float(shape.get("length"))
                elif shape.tag == "sphere":
                    vis["radius"] = float(shape.get("radius"))
                else:
                    continue
                visuals.append(vis)
            self.links[link.get("name")] = visuals

        self.joints: Dict[str, dict] = {}
        self.children: Dict[str, List[str]] = {n: [] for n in self.links}
        child_links = set()
        for j in root.findall("joint"):
            org = j.find("origin")
            lim = j.find("limit")
            axis = j.find("axis")
            d = {
                "name": j.get("name"),
                "type": j.get("type"),
                "parent": j.find("parent").get("link"),
                "child": j.find("child").get("link"),
                "xyz": list(_floats(org.get("xyz") if org is not None else None)),
                "rpy": list(_floats(org.get("rpy") if org is not None else None)),
                "axis": list(_floats(axis.get("xyz") if axis is not None else None, 3, 0.0)) if axis is not None else [0, 0, 1],
                "lower": float(lim.get("lower")) if lim is not None and lim.get("lower") else None,
                "upper": float(lim.get("upper")) if lim is not None and lim.get("upper") else None,
            }
            self.joints[d["name"]] = d
            self.children[d["parent"]].append(d["name"])
            child_links.add(d["child"])
        roots = [n for n in self.links if n not in child_links]
        if len(roots) != 1:
            raise ValueError(f"URDF must have exactly one root link, got {roots}")
        self.root = roots[0]

    # -------------------------------------------------------------- queries
    @property
    def movable_joints(self) -> List[str]:
        return [n for n, j in self.joints.items() if j["type"] in ("revolute", "continuous")]

    def to_model(self) -> dict:
        """JSON-serialisable description for the browser viewer."""
        return {
            "name": self.name,
            "root": self.root,
            "frame": "x forward, y left, z up (URDF)",
            "materials": self.materials,
            "links": {n: v for n, v in self.links.items()},
            "joints": list(self.joints.values()),
        }

    # -------------------------------------------------------------- kinematics
    def fk(self, angles_deg: Optional[Dict[str, float]] = None) -> Dict[str, Pose]:
        """World (torso-frame) pose of every link for the given joint angles in degrees."""
        angles_deg = angles_deg or {}
        out: Dict[str, Pose] = {self.root: (IDENTITY, (0.0, 0.0, 0.0))}
        stack = [self.root]
        while stack:
            parent = stack.pop()
            pr, pt = out[parent]
            for jn in self.children[parent]:
                j = self.joints[jn]
                r = mat_mul(pr, rot_rpy(*j["rpy"]))
                t = vec_add(pt, mat_vec(pr, tuple(j["xyz"])))  # type: ignore[arg-type]
                if j["type"] in ("revolute", "continuous"):
                    r = mat_mul(r, rot_axis(tuple(j["axis"]), math.radians(angles_deg.get(jn, 0.0))))  # type: ignore[arg-type]
                out[j["child"]] = (r, t)
                stack.append(j["child"])
        return out

    def link_points(self, link: str, pose: Pose) -> List[Vec]:
        """World-frame corner points of a link's box visuals (support polygon / bounds)."""
        pr, pt = pose
        pts: List[Vec] = []
        for v in self.links.get(link, []):
            if v["type"] != "box":
                continue
            vr = mat_mul(pr, rot_rpy(*v["rpy"]))
            vt = vec_add(pt, mat_vec(pr, tuple(v["xyz"])))  # type: ignore[arg-type]
            sx, sy, sz = (s / 2.0 for s in v["size"])
            for ix in (-1, 1):
                for iy in (-1, 1):
                    for iz in (-1, 1):
                        pts.append(vec_add(vt, mat_vec(vr, (ix * sx, iy * sy, iz * sz))))
        return pts

    def ground_contact(self, links: Dict[str, Pose], feet: Tuple[str, str] = ("l_foot", "r_foot")) -> dict:
        """
        Where to put the torso so the feet stand flat on the floor (z = 0).

        The average sole normal of both feet is rotated onto world up (no yaw), then the
        robot is lowered until its lowest foot point touches the floor and centred over the
        feet. A pose that leans the body (``scorpion_pose_bending_backwards``) therefore
        shows the torso tilted instead of a robot hovering with slanted feet.
        """
        normals, pts = [], []
        for f in feet:
            if f in links:
                normals.append(mat_vec(links[f][0], (0.0, 0.0, 1.0)))
                pts.extend(self.link_points(f, links[f]))
        if not pts:
            return {"pos": [0.0, 0.0, 0.0], "quat": [0.0, 0.0, 0.0, 1.0], "pitch_deg": 0.0, "roll_deg": 0.0}
        n = vec_unit((sum(v[0] for v in normals), sum(v[1] for v in normals), sum(v[2] for v in normals)))
        rb = rotation_between(n, (0.0, 0.0, 1.0))
        world = [mat_vec(rb, p) for p in pts]
        zmin = min(p[2] for p in world)
        cx = sum(p[0] for p in world) / len(world)
        cy = sum(p[1] for p in world) / len(world)
        up = mat_vec(rb, (0.0, 0.0, 1.0))  # torso "up" in world, for reporting the lean
        return {
            "pos": [-cx, -cy, -zmin],
            "quat": list(mat_to_quat(rb)),
            "pitch_deg": math.degrees(math.atan2(up[0], up[2])),   # + = torso leans forward
            "roll_deg": math.degrees(math.atan2(-up[1], up[2])),
        }
