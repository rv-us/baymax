# Baymax digital twin

A URDF-based simulator of the TonyPi plus a browser 3D viewer. It replaces the old
`laptop_mvp/tonypi_sim.py` (Open3D/PyBullet window). The server side is **pure standard library**
(no new installs), the viewer is three.js (vendored in `web/vendor`, works offline on the robot LAN).

Two channels feed the twin; the viewer shows one and ghosts the other:

| channel | comes from | meaning |
|---|---|---|
| `command` | pose estimator, recorded pose, dashboard sliders | the simulated robot |
| `actual`  | `robot_api` `servo/status` (read-only, ~4 Hz) | the real robot's servo readback |

`Auto` view follows the robot while its readback is fresh (< 3 s), otherwise the command.
The per-servo Δ column is robot − command, so drift and stalled servos show up directly.

## Run it

```bash
python -m digital_twin --demo                                  # cycles scripts/poses.json
python -m digital_twin --robot-url http://<robot>:8000         # mirror the real robot (read-only)
python laptop_mvp/mediapose_est.py                             # estimator starts the twin itself, opens the browser
python laptop_mvp/mediapose_est.py --api-url http://<robot>:8000   # also mirrors the robot readback as the ghost
```

Estimator flags: `--twin-port 8765`, `--no-browser`, `--twin-robot-url URL` (defaults to `--api-url`), `--no-sim`.
The viewer is at `http://127.0.0.1:8765`. If the port is busy the next free one is used (it is printed).

## Embed in a web dashboard

*Any frontend* (the server sends CORS `*`):

```html
<div id="view" style="height:480px"></div>
<script type="module">
  import { BaymaxTwin } from "http://<twin-host>:8765/web/twin.js";
  const twin = new BaymaxTwin(document.getElementById("view"));
  await twin.init();
  twin.connect();                  // live stream; or twin.setState(snapshot) to feed it yourself
</script>
```
or an iframe: `http://<twin-host>:8765/web/index.html?embed=1` (hides the side panel; `&view=actual`, `&ghost=0`, `&ground=0`).
Serve to other machines with `--host 0.0.0.0` (robot connection can then only be configured on the server).

*FastAPI dashboard*: `from digital_twin.fastapi_router import mount_twin; twin = mount_twin(app, "/twin")`,
then `new BaymaxTwin(el, { baseUrl: "/twin" })`. Drive it with `twin.state.set_command(...)`.

## API (`digital_twin/server.py`)

`GET /api/model | /api/poses | /api/state | /api/stream (SSE)`,
`POST /api/command {"servos": {id|joint_name: pulse}, "head": {"pan","tilt"}, "source", "label"}`,
`/api/pose {"name"}`, `/api/view {"view": "auto|command|actual"}`, `/api/robot {"url"} | {"disconnect": true}`, `/api/clone`.
Feeding it from Python: `TonyPiDigitalTwin().update_servos({...})` (same payload as `servo/set_many`).

## Calibration (`mapping.py`) and what is *not* verified

`angle = sign * (pulse - stand_pulse) / (1000/240)`, joint zero = factory stand pose; commands are clamped to
`robot_api.servo.limits` (a test keeps both tables identical). The recorded poses in `scripts/poses.json`
are the regression data: T-pose gives roll ±75°, zombie gives pitch ≈ -97° (forward), scorpion gives a 72° backward torso lean.

* Arms follow `laptop_mvp/retarget.py`; `laptop_mvp/test_retarget.py` (independent math) passes against the twin.
  Elbows 6/14 are mirrored single hinges about local +X/-X: increasing 6 or decreasing 14
  sweeps the forearm toward the robot's centre. They have no independent forward-curl axis.
  Stand pulses 575/425 mean straight forearms; shoulder pitch aims the entire arm forward.
  This mounting correction follows the observed hardware motion, rather than inferring axes
  from the API's joint names. The API reports pulses, not hinge axes or Cartesian arm poses.
  With the current command limits, inward elbow travel from stand is 72 degrees;
  actual readback remains unclamped so commanded and measured positions can differ.
* Leg pitch directions are inferred from `scorpion_pose_bending_backwards`; the **leg/ankle roll** servos (1, 5, 9, 13) and the
  **head** (PWM, 0.09°/µs) barely or never move in the recordings, so their direction is a guess (marked ⚠ in the viewer).
* The twin shows exactly what the pulses say. `mediapose_est.map_leg_servos` uses ~2.7 pulses/deg while the servos are 4.17 pulses/deg,
  so estimator leg angles appear ~20–60 % smaller (knee 0.8×, ankle 0.64×, hip 0.4×) on the twin (and on the robot) than the estimator intends.
* "Floor contact": torso pitch/roll and height are solved so both soles lie flat on the floor, so a pose like the scorpion
  shows a leaning torso, not floating feet. Toggle it off in the panel to see the raw joint chain.
  The renderer adds 1 mm clearance above the floor/grid, and the sole pad sits below the metal plate.
  Overlapping casing faces have different dimensions; ghost surfaces use a depth offset to
  avoid fighting with the main model when the two channels agree.
* Hands (servos 17/18) are shown in the table only; the URDF has no gripper model, and they cannot be read back from the robot.

## Layout

`urdf.py` parser/FK/floor solver · `mapping.py` calibration + pose loading · `state.py` channels · `robot_link.py` robot readback ·
`server.py` / `fastapi_router.py` transport · `twin.py` estimator facade · `web/` viewer · `tonypi.urdf` base model
(extended with hip-roll and ankle-roll joints). Tests: `tests/test_digital_twin.py`.
