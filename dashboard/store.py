"""Plain files, no database: sessions/*.json, animations/*.json, workout/plans/*.yaml.

Sessions are the workout loop's own log (workout/session.py); this only reads them,
adds the cached coach summary, and writes synthetic ones. Animations are keyframe
lists checked by workout.plan.check_keyframes, and become plan exercises through
workout.plan.parse_plan, so plan.py stays the one place that says what is valid.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import yaml

from workout.keyframes import duration_ms
from workout.plan import JOINTS, PlanError, check_keyframes, parse_plan
from workout.session import upgrade

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_lock = threading.Lock()


class NotFound(KeyError):
    pass


def check_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise PlanError("names use letters, digits, - and _ only (64 at most)")
    return value


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")[:48]
    return s or "untitled"


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise NotFound(os.path.basename(path)) from None


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---- sessions ---------------------------------------------------------------------


class Sessions:
    def __init__(self, directory: str):
        self.dir = directory

    def _path(self, sid: str) -> str:
        return os.path.join(self.dir, check_id(sid) + ".json")

    def ids(self) -> list:
        try:
            names = [f[:-5] for f in os.listdir(self.dir) if f.endswith(".json") and _ID.match(f[:-5])]
        except FileNotFoundError:
            return []
        return names

    def raw(self, sid: str) -> dict:
        return _read(self._path(sid))

    def get(self, sid: str) -> dict:
        return {"id": sid, **upgrade(self.raw(sid))}

    def list(self) -> list:
        """Newest first, without traces (the list view doesn't need them)."""
        rows = []
        for sid in self.ids():
            try:
                s = self.get(sid)
            except (ValueError, NotFound, TypeError, KeyError, AttributeError):
                rows.append({"id": sid, "unreadable": True, "started": 0})
                continue
            rows.append({
                "id": sid, "plan": s.get("plan"), "started": s.get("started"), "ended": s.get("ended"),
                "status": s.get("status"), "flagged": bool(s.get("flagged")), "synthetic": s["synthetic"],
                "scores": s["scores"], "exercises": list(dict.fromkeys(x["exercise"] for x in s["sets"])),
                "sets": len(s["sets"]), "has_detail": any(x["reps_detail"] for x in s["sets"] + s["interrupted"]),
                "headline": (s.get("summary") or {}).get("headline"),
            })
        return sorted(rows, key=lambda r: r.get("started") or 0, reverse=True)

    def history(self, before: float, limit: int = 5) -> list:
        """Earlier sessions (oldest first) for the coach's rolling history."""
        earlier = [r for r in self.list() if not r.get("unreadable") and (r["started"] or 0) < before]
        return list(reversed(earlier[:limit]))

    def save(self, sid: str, data: dict) -> None:
        with _lock:
            _write(self._path(sid), data)

    def update(self, sid: str, **fields) -> None:
        with _lock:
            data = _read(self._path(sid))
            data.update(fields)
            _write(self._path(sid), data)

    def delete_synthetic(self) -> int:
        n = 0
        for sid in self.ids():
            try:
                if self.raw(sid).get("synthetic"):
                    os.remove(self._path(sid))
                    n += 1
            except (ValueError, NotFound):
                continue
        return n


# ---- animations -------------------------------------------------------------------

EXERCISE_DEFAULTS = {"cue": "", "sets": 2, "reps": 8, "mirror": "arms",
                     "track_joint": "shoulder_elevation", "targets": {}}


def check_animation(data: dict) -> dict:
    """A clean animation dict, or PlanError saying what is wrong."""
    if not isinstance(data, dict):
        raise PlanError("an animation must be a JSON object")
    name = str(data.get("name") or "").strip()[:80]
    if not name:
        raise PlanError("the animation needs a name")
    check_keyframes(data.get("keyframes"), f"animation {name!r}")
    frames = [{k: kf[k] for k in ("ms", "hold_ms", "ease", "servos", "head") if kf.get(k) not in (None, {})}
              for kf in data["keyframes"]]
    exercise = {**EXERCISE_DEFAULTS, **{k: v for k, v in (data.get("exercise") or {}).items()
                                        if k in EXERCISE_DEFAULTS}}
    out = {"version": 1, "name": name, "keyframes": frames, "exercise": exercise,
           "source": str(data.get("source") or "manual")[:40],
           "loop": bool(data.get("loop")), "speed": float(data.get("speed") or 1.0)}
    as_exercise(out)  # the exercise fields must pass plan.py too
    return out


def as_exercise(anim: dict) -> dict:
    """The animation as a plan exercise (the shape workout/plan.py loads)."""
    ex = anim["exercise"]
    tempo = max(1.0, min(30.0, round(duration_ms(anim["keyframes"]) / 1000, 1)))
    exercise = {"name": anim["name"], "cue": ex["cue"] or f"Follow along with {anim['name'].lower()}.",
                "demo": {"keyframes": anim["keyframes"], "times": 1},
                "sets": ex["sets"], "reps": ex["reps"], "mirror": ex["mirror"],
                "track_joint": ex["track_joint"], "tempo_s": tempo, "targets": ex["targets"] or {}}
    parse_plan({"name": "check", "exercises": [exercise]}, source="exercise")
    return exercise


class Animations:
    def __init__(self, directory: str):
        self.dir = directory

    def _path(self, aid: str) -> str:
        return os.path.join(self.dir, check_id(aid) + ".json")

    def list(self) -> list:
        rows = []
        try:
            names = sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".json") and _ID.match(f[:-5]))
        except FileNotFoundError:
            names = []
        for aid in names:
            try:
                a = _read(self._path(aid))
                rows.append({"id": aid, "name": a.get("name", aid), "keyframes": len(a.get("keyframes", [])),
                             "duration_ms": duration_ms(a.get("keyframes", [])),
                             "source": a.get("source"), "updated": a.get("updated")})
            except (ValueError, KeyError, TypeError):
                rows.append({"id": aid, "name": aid, "unreadable": True})
        return rows

    def get(self, aid: str) -> dict:
        return {"id": aid, **_read(self._path(aid))}

    def save(self, data: dict, aid: str | None = None) -> dict:
        clean = check_animation(data)
        aid = check_id(aid) if aid else slug(clean["name"])
        clean["updated"] = time.time()
        with _lock:
            _write(self._path(aid), clean)
        return {"id": aid, **clean}

    def delete(self, aid: str) -> None:
        try:
            os.remove(self._path(aid))
        except FileNotFoundError:
            raise NotFound(aid) from None


def import_frames(data: dict) -> dict:
    """The keyframe import contract, for retargeting output (arm_mvp) and anything else
    that produces servo targets over time:

        { "name": "...", "frames": [ { "t_ms": 0, "servos": { "left_shoulder_pitch": 700, ... } }, ... ] }

    `servos` is the same payload arm_mvp sends to servo/set_many (joint names or ids).
    Frames are thinned to keyframes: a frame is kept when a joint has moved 25 pulses
    (about 6 degrees) since the last kept one, and no closer together than 300 ms.
    Returns an unsaved animation for the previewer."""
    from robot_api.errors import RobotError
    from robot_api.servo.joints import joint_name, resolve
    from workout.plan import KEYFRAME_JOINTS

    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list) or not frames:
        raise PlanError("import needs a non-empty 'frames' list of {t_ms, servos}")
    rows = []
    for i, f in enumerate(frames, start=1):
        if not isinstance(f, dict) or not isinstance(f.get("servos"), dict) \
                or not isinstance(f.get("t_ms"), (int, float)):
            raise PlanError(f"frame {i}: needs t_ms and servos")
        try:
            servos = {joint_name(resolve(k)): int(round(v)) for k, v in f["servos"].items()}
        except (RobotError, TypeError, ValueError) as e:
            raise PlanError(f"frame {i}: {e}") from e
        rows.append((float(f["t_ms"]), {j: p for j, p in servos.items() if j in KEYFRAME_JOINTS}))
    rows.sort(key=lambda r: r[0])
    kept, last_t, last = [], rows[0][0], {}
    for n, (t, servos) in enumerate(rows):
        moved = any(abs(p - last.get(j, p + 1000)) >= 25 for j, p in servos.items())
        final = n == len(rows) - 1 and servos != last
        if servos and (moved or final) and (not kept or t - last_t >= 300 or final):
            kept.append({"ms": int(max(300, min(10000, round(t - last_t)))) if kept else 1000,
                         "ease": "linear" if kept else "inout", "servos": servos})
            last_t, last = t, {**last, **servos}
    if not kept:
        raise PlanError("no arm joints found in the frames (legs and hands are not imported)")
    return {"name": str(data.get("name") or "Imported motion")[:80], "keyframes": kept[:60],
            "source": "imported", "exercise": dict(EXERCISE_DEFAULTS),
            "dropped_frames": len(rows) - len(kept[:60])}


# ---- plans ------------------------------------------------------------------------


class Plans:
    def __init__(self, directory: str):
        self.dir = directory

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, check_id(name) + ".yaml")

    def names(self) -> list:
        try:
            return sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".yaml") and _ID.match(f[:-5]))
        except FileNotFoundError:
            return []

    def raw(self, name: str) -> dict:
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            raise NotFound(name) from None

    def get(self, name: str) -> dict:
        data = self.raw(name)
        try:
            plan = parse_plan(data, source=name)
        except PlanError as e:
            return {"id": name, "name": data.get("name", name), "exercises": [], "error": str(e)}
        return {"id": name, "name": plan.name, "exercises": [vars(e) for e in plan.exercises]}

    def list(self) -> list:
        return [self.get(n) for n in self.names()]

    def save(self, name: str, data: dict) -> dict:
        parse_plan(data, source=name)  # PlanError if it isn't a valid plan
        os.makedirs(self.dir, exist_ok=True)
        with _lock, open(self._path(name), "w", encoding="utf-8") as f:
            f.write("# Saved by the dashboard. See workout/plan.py for the format.\n")
            yaml.safe_dump({"name": data.get("name") or name, "exercises": data["exercises"]}, f,
                           sort_keys=False, allow_unicode=True, width=100)
        return self.get(name)

    def add_exercise(self, name: str, exercise: dict) -> dict:
        try:
            data = self.raw(name)
        except NotFound:
            data = {"name": name.replace("_", " ").capitalize(), "exercises": []}
        exercises = [e for e in data.get("exercises") or [] if e.get("name") != exercise["name"]]
        return self.save(name, {"name": data.get("name"), "exercises": exercises + [exercise]})


TRACK_JOINTS = JOINTS
