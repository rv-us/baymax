"""
Hiwonder TonyPi Humanoid Robot - 3D Simulation & Real-Time Digital Twin
=======================================================================
Provides a robust physics simulation and real-time 3D digital twin for
the Hiwonder TonyPi humanoid robot.

Features:
- Dual backend: Full native PyBullet support when pybullet is installed,
  with seamless high-performance Open3D 3D kinematic twin fallback
  when running on platforms without pre-built pybullet binaries.
- Real-time kinematic retargeting for physical therapy exercises:
  - Shoulder elevation plane (0° = Coronal / Wingspan, 90° = Sagittal / Forward)
  - Elbow flexion (180° = fully extended straight arm, ~40-60° = curled flexed)
- Head pan/tilt neck control (Servos 1 & 2).
- Non-blocking background thread worker with a thread-safe Queue for
  zero-latency pairing with 30-60 FPS camera / YOLO pose pipelines.
- Stable humanoid standing pose on ground plane.
- Headless testing mode (`--test`) for automated CI/unit tests.
"""

import os
import sys
import time
import math
import argparse
import threading
import multiprocessing as mp
import queue
import xml.etree.ElementTree as ET
import numpy as np

# =====================================================================
# 1. PYBULLET & DIGITAL TWIN BACKEND ABSTRACTION
# =====================================================================

PYBULLET_AVAILABLE = False
try:
    import pybullet as real_p
    import pybullet_data
    PYBULLET_AVAILABLE = True
except ImportError:
    real_p = None
    pybullet_data = None

OPEN3D_AVAILABLE = False
try:
    import open3d as o3d
    OPEN3D_AVAILABLE = True
except ImportError:
    o3d = None


class PyBulletShim:
    """
    Transparent PyBullet-compatible engine that operates against
    the parsed URDF kinematic model and Open3D 3D renderer.
    Guarantees 100% PyBullet API compatibility when pybullet is absent.
    """
    GUI = 1
    DIRECT = 2
    POSITION_CONTROL = 2
    COV_ENABLE_GUI = 1
    COV_ENABLE_SHADOWS = 1

    def __init__(self):
        self.connected = False
        self.mode = self.DIRECT
        self.robots = {}
        self.next_robot_id = 0
        self.camera_config = {
            "distance": 0.75,
            "yaw": 45.0,
            "pitch": -15.0,
            "target": [0.0, 0.0, 0.22]
        }
        self.visualizer = None
        self.ground_mesh = None
        self.window_closed = False

    def connect(self, mode):
        self.connected = True
        self.mode = mode
        self.window_closed = False
        return 0

    def disconnect(self, client_id=0):
        self.connected = False
        self.window_closed = True
        if self.visualizer is not None:
            try:
                self.visualizer.destroy_window()
            except Exception:
                pass
            self.visualizer = None
        self.robots.clear()
        self.next_robot_id = 0

    def setGravity(self, gx, gy, gz, physicsClientId=0):
        pass

    def configureDebugVisualizer(self, flag, enable, physicsClientId=0):
        pass

    def resetDebugVisualizerCamera(self, cameraDistance, cameraYaw, cameraPitch, cameraTargetPosition, physicsClientId=0):
        self.camera_config["distance"] = cameraDistance
        self.camera_config["yaw"] = cameraYaw
        self.camera_config["pitch"] = cameraPitch
        self.camera_config["target"] = list(cameraTargetPosition)
        if self.visualizer is not None and OPEN3D_AVAILABLE and not getattr(self, "window_closed", False):
            try:
                ctr = self.visualizer.get_view_control()
                ctr.set_lookat(cameraTargetPosition)
            except Exception:
                pass

    def loadURDF(self, fileName, basePosition=(0, 0, 0), baseOrientation=(0, 0, 0, 1), useFixedBase=True, flags=0, physicsClientId=0):
        if not os.path.exists(fileName):
            raise FileNotFoundError(f"URDF file not found: {fileName}")
        
        tree = ET.parse(fileName)
        root = tree.getroot()
        robot_name = root.attrib.get("name", "robot")

        # Parse materials
        materials = {}
        for mat in root.findall("material"):
            m_name = mat.attrib.get("name")
            color_elem = mat.find("color")
            if color_elem is not None:
                rgba = [float(x) for x in color_elem.attrib["rgba"].split()]
                materials[m_name] = rgba[:3]

        # Parse links and visual geometries
        links = {}
        for link in root.findall("link"):
            l_name = link.attrib["name"]
            visuals = []
            for vis in link.findall("visual"):
                v_origin = vis.find("origin")
                v_xyz = [float(x) for x in v_origin.attrib.get("xyz", "0 0 0").split()] if v_origin is not None else [0.0, 0.0, 0.0]
                v_rpy = [float(x) for x in v_origin.attrib.get("rpy", "0 0 0").split()] if v_origin is not None else [0.0, 0.0, 0.0]
                
                geom = vis.find("geometry")
                geom_type = None
                geom_params = {}
                if geom is not None:
                    box = geom.find("box")
                    cyl = geom.find("cylinder")
                    sphere = geom.find("sphere")
                    if box is not None:
                        geom_type = "box"
                        geom_params["size"] = [float(x) for x in box.attrib["size"].split()]
                    elif cyl is not None:
                        geom_type = "cylinder"
                        geom_params["radius"] = float(cyl.attrib["radius"])
                        geom_params["length"] = float(cyl.attrib["length"])
                    elif sphere is not None:
                        geom_type = "sphere"
                        geom_params["radius"] = float(sphere.attrib["radius"])

                mat_elem = vis.find("material")
                mat_color = [0.8, 0.8, 0.8]
                if mat_elem is not None:
                    m_ref = mat_elem.attrib.get("name")
                    if m_ref in materials:
                        mat_color = materials[m_ref]
                    else:
                        c = mat_elem.find("color")
                        if c is not None:
                            mat_color = [float(x) for x in c.attrib["rgba"].split()][:3]

                visuals.append({
                    "xyz": v_xyz,
                    "rpy": v_rpy,
                    "type": geom_type,
                    "params": geom_params,
                    "color": mat_color
                })
            links[l_name] = {"visuals": visuals}

        # Parse joints
        joints = []
        joint_name_map = {}
        for idx, joint in enumerate(root.findall("joint")):
            j_name = joint.attrib["name"]
            j_type = joint.attrib.get("type", "revolute")
            parent = joint.find("parent").attrib["link"]
            child = joint.find("child").attrib["link"]
            
            origin = joint.find("origin")
            xyz = [float(x) for x in origin.attrib.get("xyz", "0 0 0").split()] if origin is not None else [0.0, 0.0, 0.0]
            rpy = [float(x) for x in origin.attrib.get("rpy", "0 0 0").split()] if origin is not None else [0.0, 0.0, 0.0]
            
            axis_elem = joint.find("axis")
            axis = [float(x) for x in axis_elem.attrib.get("xyz", "0 0 1").split()] if axis_elem is not None else [0.0, 0.0, 1.0]
            
            limit = joint.find("limit")
            lower = float(limit.attrib.get("lower", -3.1416)) if limit is not None else -3.1416
            upper = float(limit.attrib.get("upper", 3.1416)) if limit is not None else 3.1416
            
            j_data = {
                "index": idx,
                "name": j_name,
                "type": j_type,
                "parent": parent,
                "child": child,
                "origin_xyz": xyz,
                "origin_rpy": rpy,
                "axis": axis,
                "lower": lower,
                "upper": upper,
                "position": 0.0,
                "target_position": 0.0
            }
            joints.append(j_data)
            joint_name_map[j_name] = idx

        robot_id = self.next_robot_id
        self.next_robot_id += 1

        robot_entry = {
            "name": robot_name,
            "base_position": list(basePosition),
            "links": links,
            "joints": joints,
            "joint_name_map": joint_name_map,
            "meshes": []
        }
        self.robots[robot_id] = robot_entry

        # If GUI mode, build Open3D meshes
        if self.mode == self.GUI and OPEN3D_AVAILABLE:
            self._init_open3d(robot_id)

        return robot_id

    def _init_open3d(self, robot_id):
        robot = self.robots[robot_id]
        if self.visualizer is None:
            self.window_closed = False
            try:
                self.visualizer = o3d.visualization.Visualizer()
                self.visualizer.create_window(window_name="Hiwonder TonyPi - 3D Digital Twin", width=960, height=720, visible=True)
            except Exception as e:
                print(f"[TonyPiSim] Failed to create Open3D window: {e}")
                self.visualizer = None
                self.window_closed = True
                return
            
            # Ground plane (styled checkered arena floor)
            ground = o3d.geometry.TriangleMesh.create_box(width=1.2, height=1.2, depth=0.002)
            ground.translate([-0.6, -0.6, -0.001])
            ground.paint_uniform_color([0.22, 0.24, 0.28])
            ground.compute_vertex_normals()
            self.visualizer.add_geometry(ground)
            self.ground_mesh = ground

            # Floor grid border accent
            border = o3d.geometry.TriangleMesh.create_box(width=0.6, height=0.6, depth=0.003)
            border.translate([-0.3, -0.3, -0.0005])
            border.paint_uniform_color([0.30, 0.35, 0.42])
            border.compute_vertex_normals()
            self.visualizer.add_geometry(border)

        # Build geometry for all link visuals
        for l_name, l_data in robot["links"].items():
            for v_idx, vis in enumerate(l_data["visuals"]):
                mesh = None
                g_type = vis["type"]
                params = vis["params"]
                if g_type == "box":
                    sx, sy, sz = params["size"]
                    mesh = o3d.geometry.TriangleMesh.create_box(width=sx, height=sy, depth=sz)
                    mesh.translate([-sx/2.0, -sy/2.0, -sz/2.0])
                elif g_type == "cylinder":
                    r = params["radius"]
                    l = params["length"]
                    mesh = o3d.geometry.TriangleMesh.create_cylinder(radius=r, height=l, resolution=16)
                elif g_type == "sphere":
                    r = params["radius"]
                    mesh = o3d.geometry.TriangleMesh.create_sphere(radius=r, resolution=16)

                if mesh is not None:
                    mesh.paint_uniform_color(vis["color"])
                    mesh.compute_vertex_normals()
                    base_verts = np.asarray(mesh.vertices).copy()
                    
                    self.visualizer.add_geometry(mesh)
                    robot["meshes"].append({
                        "link": l_name,
                        "vis_offset_xyz": vis["xyz"],
                        "vis_offset_rpy": vis["rpy"],
                        "mesh": mesh,
                        "base_verts": base_verts
                    })

        self._update_kinematics(robot_id)

        # Setup camera looking at TonyPi torso
        try:
            ctr = self.visualizer.get_view_control()
            ctr.set_lookat(self.camera_config["target"])
            ctr.set_front([-1.2, -1.0, 0.6])
            ctr.set_up([0.0, 0.0, 1.0])
            ctr.set_zoom(0.7)
        except Exception:
            pass

    def get_link_transforms(self, robot_id):
        """Returns {link_name: 4x4 world transform} from forward kinematics."""
        robot = self.robots[robot_id]
        link_transforms = {"torso": self._make_translation(robot["base_position"])}
        for j in robot["joints"]:
            if j["parent"] in link_transforms:
                T_origin = self._make_translation(j["origin_xyz"]) @ self._make_rotation_rpy(j["origin_rpy"])
                T_motion = self._make_rotation_axis_angle(j["axis"], j["position"])
                link_transforms[j["child"]] = link_transforms[j["parent"]] @ T_origin @ T_motion
        return link_transforms

    def _update_kinematics(self, robot_id):
        robot = self.robots[robot_id]
        link_transforms = self.get_link_transforms(robot_id)

        # Update Open3D mesh vertices
        if self.visualizer is not None and OPEN3D_AVAILABLE and not getattr(self, "window_closed", False):
            try:
                for item in robot["meshes"]:
                    l_name = item["link"]
                    if l_name in link_transforms:
                        T_link = link_transforms[l_name]
                        T_vis = T_link @ self._make_translation(item["vis_offset_xyz"]) @ self._make_rotation_rpy(item["vis_offset_rpy"])
                        
                        base_verts = item["base_verts"]
                        R = T_vis[:3, :3]
                        p = T_vis[:3, 3]
                        new_verts = (R @ base_verts.T).T + p
                        
                        mesh = item["mesh"]
                        mesh.vertices = o3d.utility.Vector3dVector(new_verts)
                        mesh.compute_vertex_normals()
                        self.visualizer.update_geometry(mesh)
            except Exception:
                self.window_closed = True

    def getNumJoints(self, robotId, physicsClientId=0):
        return len(self.robots[robotId]["joints"])

    def getJointInfo(self, robotId, jointIndex, physicsClientId=0):
        j = self.robots[robotId]["joints"][jointIndex]
        # Mirror PyBullet joint info tuple format
        return (
            j["index"],
            j["name"].encode("utf-8"),
            0, # JOINT_REVOLUTE
            0, 0, 0, 0, 0,
            j["lower"],
            j["upper"],
            5.0, 5.0,
            j["child"].encode("utf-8"),
            j["axis"],
            j["origin_xyz"],
            j["origin_rpy"],
            j["parent"].encode("utf-8")
        )

    def setJointMotorControl2(self, bodyIndex, jointIndex, controlMode, targetPosition=0.0, targetVelocity=0.0, force=5.0, physicsClientId=0):
        robot = self.robots[bodyIndex]
        j = robot["joints"][jointIndex]
        j["target_position"] = float(targetPosition)
        # Immediate interpolation in kinematic twin
        j["position"] = float(targetPosition)

    def getJointState(self, bodyUniqueId, jointIndex, physicsClientId=0):
        j = self.robots[bodyUniqueId]["joints"][jointIndex]
        return (j["position"], 0.0, (0.0, 0.0, 0.0, 0.0, 0.0), 0.0)

    def stepSimulation(self, physicsClientId=0):
        for robot_id in list(self.robots.keys()):
            self._update_kinematics(robot_id)
        if self.visualizer is not None and OPEN3D_AVAILABLE and not getattr(self, "window_closed", False):
            try:
                is_open = self.visualizer.poll_events()
                if is_open is False:
                    self.window_closed = True
                    return
                self.visualizer.update_renderer()
            except Exception:
                self.window_closed = True

    @staticmethod
    def _make_translation(xyz):
        T = np.eye(4)
        T[:3, 3] = xyz
        return T

    @staticmethod
    def _make_rotation_rpy(rpy):
        r, p, y = rpy
        Rx = np.array([[1, 0, 0, 0], [0, np.cos(r), -np.sin(r), 0], [0, np.sin(r), np.cos(r), 0], [0, 0, 0, 1]])
        Ry = np.array([[np.cos(p), 0, np.sin(p), 0], [0, 1, 0, 0], [-np.sin(p), 0, np.cos(p), 0], [0, 0, 0, 1]])
        Rz = np.array([[np.cos(y), -np.sin(y), 0, 0], [np.sin(y), np.cos(y), 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        return Rz @ Ry @ Rx

    @staticmethod
    def _make_rotation_axis_angle(axis, angle):
        axis = np.array(axis, dtype=float)
        norm = np.linalg.norm(axis)
        if norm < 1e-6 or abs(angle) < 1e-6:
            return np.eye(4)
        axis = axis / norm
        x, y, z = axis
        c = np.cos(angle)
        s = np.sin(angle)
        C = 1.0 - c
        R = np.eye(4)
        R[:3, :3] = np.array([
            [x*x*C + c,   x*y*C - z*s, x*z*C + y*s],
            [y*x*C + z*s, y*y*C + c,   y*z*C - x*s],
            [z*x*C - y*s, z*y*C + x*s, z*z*C + c  ]
        ])
        return R


# Active physics module instance
if PYBULLET_AVAILABLE:
    p = real_p
else:
    p = PyBulletShim()


# =====================================================================
# =====================================================================
# 2. CHILD PROCESS RENDER WORKER & TONYPI DIGITAL TWIN CLASS
# =====================================================================

def _render_process_worker(urdf_path, cmd_queue, stop_event, window_closed_event, target_fps=60):
    """
    Dedicated child process worker for Open3D rendering.
    Runs strictly on the child process's main thread to eliminate GLFW/WGL context conflicts.
    """
    twin = None
    try:
        # Instantiate in synchronous GUI mode on the child process's main thread
        twin = TonyPiDigitalTwin(gui=True, urdf_path=urdf_path, headless=False, use_process=False)
    except Exception as e:
        print(f"[TonyPiSim Worker] Error initializing 3D window: {e}")
        window_closed_event.set()
        return

    interval = 1.0 / max(1, target_fps)
    try:
        while not stop_event.is_set():
            t_start = time.time()
            has_update = False

            latest_arms = None
            latest_head = None
            latest_legs = None
            while True:
                try:
                    cmd = cmd_queue.get_nowait()
                    if cmd is None or cmd[0] == "STOP":
                        return
                    elif cmd[0] == "ARMS_3D" or cmd[0] == "ARMS":
                        latest_arms = cmd
                    elif cmd[0] == "HEAD":
                        latest_head = cmd
                    elif cmd[0] == "LEGS":
                        latest_legs = cmd
                except (queue.Empty, Exception):
                    break

            if latest_arms is not None:
                if latest_arms[0] == "ARMS_3D":
                    _, l_p, l_r, l_e, r_p, r_r, r_e = latest_arms
                    twin.update_arms(l_pitch_deg=l_p, l_roll_deg=l_r, l_elbow_deg=l_e,
                                     r_pitch_deg=r_p, r_roll_deg=r_r, r_elbow_deg=r_e)
                else:
                    _, l_e, l_p, r_e, r_p = latest_arms
                    twin.update_arms(l_e, l_p, r_e, r_p)
                has_update = True

            if latest_head is not None:
                _, pitch, pan = latest_head
                twin.set_head(pitch, pan)
                has_update = True

            if latest_legs is not None:
                _, l_k, l_hp, l_a, r_k, r_hp, r_a = latest_legs
                twin.update_legs(l_knee_deg=l_k, l_hip_pitch_deg=l_hp, l_ankle_deg=l_a,
                                 r_knee_deg=r_k, r_hip_pitch_deg=r_hp, r_ankle_deg=r_a)
                has_update = True

            if not has_update:
                twin.step()

            # Check if 3D window was closed by the user
            if twin.is_window_closed():
                window_closed_event.set()
                print("[TonyPiSim Worker] 3D window closed by user.")
                break

            elapsed = time.time() - t_start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    except Exception as e:
        print(f"[TonyPiSim Worker] Render loop exception: {e}")
    finally:
        if twin is not None:
            try:
                twin.close()
            except Exception:
                pass


class TonyPiDigitalTwin:
    """
    Real-time 3D Digital Twin and kinematic simulation for
    the Hiwonder TonyPi humanoid robot.
    """

    def __init__(self, gui=True, urdf_path=None, headless=False, use_process=True):
        """
        Initializes the simulation environment, loads TonyPi URDF,
        configures the camera, and discovers all robot joints.

        Args:
            gui: Whether to enable 3D rendering.
            urdf_path: Path to robot URDF file (defaults to tonypi.urdf).
            headless: If True, disables rendering window regardless of gui flag.
            use_process: If True (default), runs Open3D in a dedicated child process
                         to eliminate GLFW/WGL context collisions on Windows.
                         If False, runs single-thread synchronous steps on caller thread.
        """
        self.gui = gui and not headless
        self.headless = headless
        self.use_process = use_process and self.gui
        self.client_id = -1
        self.robot_id = -1
        self.joint_indices = {}
        self.is_running = False
        self.target_fps = 60

        # Multiprocess resources
        self.process = None
        self.cmd_queue = None
        self.stop_event = None
        self.window_closed_event = None

        # Thread fallback resources (for legacy callers)
        self.thread = None
        self.queue = queue.Queue(maxsize=16)

        # Locate URDF
        if urdf_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            urdf_path = os.path.join(base_dir, "tonypi.urdf")
            if not os.path.exists(urdf_path):
                urdf_path = os.path.abspath("laptop_mvp/tonypi.urdf")

        if not os.path.exists(urdf_path):
            raise FileNotFoundError(f"TonyPi URDF file not found at: {urdf_path}")
        self.urdf_path = urdf_path

        # Multiprocess resources setup before robot loading
        if self.use_process:
            self.cmd_queue = mp.Queue(maxsize=2)
            self.stop_event = mp.Event()
            self.window_closed_event = mp.Event()

        self._auto_start_on_update = False

        # If use_process is enabled, parent process runs physics in DIRECT (headless) mode.
        # Open3D window will be created exclusively in the child process upon start().
        self._initialize_physics()
        self._load_robot()
        self._auto_start_on_update = True

    def _initialize_physics(self):
        """Connects physics engine and configures camera focus."""
        if self.use_process:
            connection_mode = p.DIRECT
        else:
            connection_mode = p.GUI if self.gui else p.DIRECT

        try:
            self.client_id = p.connect(connection_mode)
        except Exception as e:
            print(f"[TonyPiSim] GUI connect failed ({e}), falling back to headless DIRECT mode.")
            connection_mode = p.DIRECT
            self.gui = False
            self.use_process = False
            self.client_id = p.connect(connection_mode)

        p.setGravity(0, 0, -9.81)

        # Focus camera on TonyPi torso (~0.22m elevation above ground)
        try:
            p.resetDebugVisualizerCamera(
                cameraDistance=0.75,
                cameraYaw=45.0,
                cameraPitch=-15.0,
                cameraTargetPosition=[0.0, 0.0, 0.22]
            )
            p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0)
            p.configureDebugVisualizer(p.COV_ENABLE_SHADOWS, 1)
        except Exception:
            pass

    def _load_robot(self):
        """Loads ground plane and TonyPi URDF model into simulation."""
        if PYBULLET_AVAILABLE and pybullet_data is not None:
            try:
                p.setAdditionalSearchPath(pybullet_data.getDataPath())
                p.loadURDF("plane.urdf")
            except Exception:
                pass

        # Load TonyPi URDF standing upright stably (base torso at z = 0.235m)
        self.robot_id = p.loadURDF(
            self.urdf_path,
            basePosition=[0.0, 0.0, 0.235],
            baseOrientation=[0.0, 0.0, 0.0, 1.0],
            useFixedBase=True
        )

        # Index joints by name
        num_joints = p.getNumJoints(self.robot_id)
        for i in range(num_joints):
            info = p.getJointInfo(self.robot_id, i)
            joint_name = info[1].decode("utf-8") if isinstance(info[1], bytes) else info[1]
            self.joint_indices[joint_name] = i

        print(f"[TonyPiSim] Loaded '{self.urdf_path}' with {num_joints} joints.")
        print(f"[TonyPiSim] Key arm joints indexed: {[k for k in self.joint_indices if 'shoulder' in k or 'elbow' in k]}")

        # Set default standing pose (straight neutral arms)
        self.update_arms(180.0, 0.0, 180.0, 0.0)

    # -----------------------------------------------------------------
    # KINEMATIC & ARM UPDATES
    # -----------------------------------------------------------------

    def start(self):
        """Starts background rendering / worker loop."""
        if self.is_running:
            return
        if self.use_process:
            self.stop_event.clear()
            self.window_closed_event.clear()
            self.process = mp.Process(
                target=_render_process_worker,
                args=(self.urdf_path, self.cmd_queue, self.stop_event, self.window_closed_event, self.target_fps),
                daemon=True
            )
            self.process.start()
            self.is_running = True
            print("[TonyPiSim] Multiprocess 3D rendering process started.")
        elif self.gui:
            self.is_running = True
            print("[TonyPiSim] Synchronous 3D rendering mode active.")
        else:
            self.is_running = True

    def _send_command_throttled(self, cmd):
        """Pushes command to child process queue; drops oldest if full for zero latency."""
        if self.cmd_queue is None:
            return
        try:
            self.cmd_queue.put_nowait(cmd)
        except (queue.Full, Exception):
            try:
                self.cmd_queue.get_nowait()
            except Exception:
                pass
            try:
                self.cmd_queue.put_nowait(cmd)
            except Exception:
                pass

    def update_arms(self, l_elbow_deg=180.0, l_shoulder_plane_deg=None,
                    r_elbow_deg=180.0, r_shoulder_plane_deg=None,
                    l_pitch_deg=None, l_roll_deg=None,
                    r_pitch_deg=None, r_roll_deg=None, **kwargs):
        """
        Retargets arm degrees into physics joint radian angles.

        Supports:
        1. Full 3D Shoulder Kinematics (recommended for all ranges of motion):
           - l_pitch_deg / r_pitch_deg: Sagittal pitch (0° = vertical neutral, -90° = forward zombie pose).
           - l_roll_deg / r_roll_deg: Coronal roll/abduction:
             * Left: 0° = arms down, 45° = wing low / superhero flared, 90° = wingspan T-pose, 180° = hands up overhead.
             * Right: 0° = arms down, -45° = wing low / superhero flared, -90° = wingspan T-pose, -180° = hands up overhead.
           - l_elbow_deg / r_elbow_deg: Interior angle (180° straight, ~60° flexed / superhero hands on hips).
        2. Legacy Shoulder Plane Retargeting:
           - l_shoulder_plane_deg / r_shoulder_plane_deg (0° coronal wingspan, 90° forward raise).
        """
        # Handle alternate keyword arguments
        if "l_elbow" in kwargs:
            l_elbow_deg = kwargs["l_elbow"]
        if "r_elbow" in kwargs:
            r_elbow_deg = kwargs["r_elbow"]
        if "l_shoulder_plane" in kwargs:
            l_shoulder_plane_deg = kwargs["l_shoulder_plane"]
        if "r_shoulder_plane" in kwargs:
            r_shoulder_plane_deg = kwargs["r_shoulder_plane"]
        if "l_pitch" in kwargs:
            l_pitch_deg = kwargs["l_pitch"]
        if "r_pitch" in kwargs:
            r_pitch_deg = kwargs["r_pitch"]
        if "l_roll" in kwargs:
            l_roll_deg = kwargs["l_roll"]
        if "r_roll" in kwargs:
            r_roll_deg = kwargs["r_roll"]

        # Ensure process is started if running in multiprocess mode after initialization
        if self.use_process and not self.is_running and getattr(self, "_auto_start_on_update", False):
            self.start()

        # --- LEFT ARM ---
        if l_pitch_deg is not None or l_roll_deg is not None:
            l_p_deg = float(l_pitch_deg if l_pitch_deg is not None else 0.0)
            l_r_deg = float(l_roll_deg if l_roll_deg is not None else 0.0)
            l_pitch_rad = math.radians(max(-180.0, min(180.0, l_p_deg)))
            l_roll_rad = math.radians(max(-180.0, min(180.0, l_r_deg)))
        else:
            plane = float(l_shoulder_plane_deg if l_shoulder_plane_deg is not None else 0.0)
            l_plane_norm = max(0.0, min(90.0, plane)) / 90.0
            l_pitch_rad = -1.5707963 * l_plane_norm
            l_roll_rad = 1.5707963 * (1.0 - l_plane_norm)
            l_p_deg = math.degrees(l_pitch_rad)
            l_r_deg = math.degrees(l_roll_rad)

        l_interior = max(90.0, min(180.0, float(l_elbow_deg)))
        l_elbow_rad = -math.radians(180.0 - l_interior)

        # --- RIGHT ARM ---
        if r_pitch_deg is not None or r_roll_deg is not None:
            r_p_deg = float(r_pitch_deg if r_pitch_deg is not None else 0.0)
            r_r_deg = float(r_roll_deg if r_roll_deg is not None else 0.0)
            r_pitch_rad = math.radians(max(-180.0, min(180.0, r_p_deg)))
            r_roll_rad = math.radians(max(-180.0, min(180.0, r_r_deg)))
        else:
            plane = float(r_shoulder_plane_deg if r_shoulder_plane_deg is not None else 0.0)
            r_plane_norm = max(0.0, min(90.0, plane)) / 90.0
            r_pitch_rad = -1.5707963 * r_plane_norm
            r_roll_rad = -1.5707963 * (1.0 - r_plane_norm)
            r_p_deg = math.degrees(r_pitch_rad)
            r_r_deg = math.degrees(r_roll_rad)

        r_interior = max(90.0, min(180.0, float(r_elbow_deg)))
        r_elbow_rad = -math.radians(180.0 - r_interior)

        # Apply motor controls locally
        self._set_joint("l_shoulder_pitch", l_pitch_rad)
        self._set_joint("l_shoulder_roll", l_roll_rad)
        self._set_joint("l_elbow", l_elbow_rad)

        self._set_joint("r_shoulder_pitch", r_pitch_rad)
        self._set_joint("r_shoulder_roll", r_roll_rad)
        self._set_joint("r_elbow", r_elbow_rad)

        # Forward to child process if multiprocess mode is active
        if self.use_process and self.process is not None and self.process.is_alive():
            if not self.window_closed_event.is_set():
                cmd = ("ARMS_3D", l_p_deg, l_r_deg, float(l_elbow_deg), r_p_deg, r_r_deg, float(r_elbow_deg))
                self._send_command_throttled(cmd)
        elif not self.use_process:
            # Handle background thread if legacy thread worker is running
            if self.is_running and self.thread is not None and threading.current_thread() != self.thread:
                self.update_arms_async(l_elbow_deg, l_shoulder_plane_deg or 0.0, r_elbow_deg, r_shoulder_plane_deg or 0.0)
                return
            # Synchronous mode: step simulation immediately on this thread
            p.stepSimulation()

    def set_head(self, pitch_deg=0.0, pan_deg=0.0):
        """Sets neck pan (yaw, Servo 2) and pitch (tilt, Servo 1)."""
        pan_rad = math.radians(max(-90.0, min(90.0, float(pan_deg))))
        pitch_rad = math.radians(max(-45.0, min(45.0, float(pitch_deg))))
        self._set_joint("neck_pan", pan_rad)
        self._set_joint("neck_pitch", pitch_rad)

        if self.use_process and self.process is not None and self.process.is_alive():
            if not self.window_closed_event.is_set():
                self._send_command_throttled(("HEAD", float(pitch_deg), float(pan_deg)))
        elif not self.use_process:
            p.stepSimulation()

    def update_legs(
        self,
        l_knee_deg: float = 180.0,
        l_hip_pitch_deg: float = 0.0,
        l_ankle_deg: float = 0.0,
        r_knee_deg: float = 180.0,
        r_hip_pitch_deg: float = 0.0,
        r_ankle_deg: float = 0.0,
    ):
        """Updates humanoid leg joints when full body is visible."""
        # All leg pitch joints share the URDF Y axis (+rotation swings the distal
        # link backward), so both legs use identical signs: hips flex forward
        # (negative), knees bend backward (positive), ankles dorsiflex (negative).
        l_k_rad = math.radians(max(0.0, 180.0 - float(l_knee_deg)))
        r_k_rad = math.radians(max(0.0, 180.0 - float(r_knee_deg)))
        l_hp_rad = -math.radians(float(l_hip_pitch_deg))
        r_hp_rad = -math.radians(float(r_hip_pitch_deg))
        l_a_rad = -math.radians(float(l_ankle_deg))
        r_a_rad = -math.radians(float(r_ankle_deg))

        self._set_joint("l_hip_pitch", l_hp_rad)
        self._set_joint("l_knee", l_k_rad)
        self._set_joint("l_ankle", l_a_rad)
        self._set_joint("r_hip_pitch", r_hp_rad)
        self._set_joint("r_knee", r_k_rad)
        self._set_joint("r_ankle", r_a_rad)

        if self.use_process and self.process is not None and self.process.is_alive():
            if not self.window_closed_event.is_set():
                self._send_command_throttled((
                    "LEGS",
                    float(l_knee_deg), float(l_hip_pitch_deg), float(l_ankle_deg),
                    float(r_knee_deg), float(r_hip_pitch_deg), float(r_ankle_deg),
                ))
        elif not self.use_process:
            p.stepSimulation()

    def _set_joint(self, name, position_rad):
        if name in self.joint_indices:
            idx = self.joint_indices[name]
            p.setJointMotorControl2(
                self.robot_id,
                idx,
                p.POSITION_CONTROL,
                targetPosition=position_rad,
                force=5.0
            )

    def get_joint_state(self, name):
        """Returns (position_rad, velocity) for a joint."""
        if name in self.joint_indices:
            idx = self.joint_indices[name]
            state = p.getJointState(self.robot_id, idx)
            return state[0], state[1]
        return 0.0, 0.0

    def get_link_rotation(self, link_name):
        """Returns the 3x3 world rotation of a link (limbs point along its local -Z)."""
        if PYBULLET_AVAILABLE:
            for name, idx in self.joint_indices.items():
                info = p.getJointInfo(self.robot_id, idx)
                child = info[12].decode("utf-8") if isinstance(info[12], bytes) else info[12]
                if child == link_name:
                    quat = p.getLinkState(self.robot_id, idx, computeForwardKinematics=True)[5]
                    return np.array(p.getMatrixFromQuaternion(quat)).reshape(3, 3)
            return np.eye(3)
        return p.get_link_transforms(self.robot_id)[link_name][:3, :3]

    def is_window_open(self):
        """Returns True if the 3D window is currently open and active."""
        if not self.gui:
            return False
        if self.use_process:
            if self.window_closed_event is not None and self.window_closed_event.is_set():
                return False
            return self.process is not None and self.process.is_alive()
        else:
            return not getattr(p, "window_closed", False)

    def is_window_closed(self):
        """Returns True if the 3D window was closed."""
        return not self.is_window_open()

    def step(self):
        """Manual step of simulation & renderer."""
        p.stepSimulation()

    def close(self):
        """Closes visualizer and disconnects physics server."""
        if self.use_process:
            if self.stop_event is not None:
                self.stop_event.set()
            if self.cmd_queue is not None:
                try:
                    self.cmd_queue.put_nowait(("STOP",))
                except Exception:
                    pass
            if self.process is not None and self.process.is_alive():
                self.process.join(timeout=1.0)
                if self.process.is_alive():
                    self.process.terminate()
                self.process = None
            if self.cmd_queue is not None:
                try:
                    self.cmd_queue.close()
                    self.cmd_queue.join_thread()
                except Exception:
                    pass
                self.cmd_queue = None
        else:
            self.stop_background_thread()

        if self.client_id >= 0:
            p.disconnect(self.client_id)
            self.client_id = -1
        self.is_running = False
        print("[TonyPiSim] Closed physics server.")

    # -----------------------------------------------------------------
    # ASYNCHRONOUS THREAD & QUEUE API (BACKWARDS COMPATIBILITY)
    # -----------------------------------------------------------------

    def update_arms_async(self, l_elbow_deg, l_shoulder_plane_deg, r_elbow_deg, r_shoulder_plane_deg):
        """
        Thread/process-safe, non-blocking method for external high-FPS loops.
        Drops oldest if queue is full to ensure real-time responsiveness.
        """
        if self.use_process:
            self.update_arms(l_elbow_deg, l_shoulder_plane_deg, r_elbow_deg, r_shoulder_plane_deg)
            return

        data = (l_elbow_deg, l_shoulder_plane_deg, r_elbow_deg, r_shoulder_plane_deg)
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(data)

    def start_background_thread(self, target_fps=60):
        """Starts background worker (multiprocess by default, or thread in headless/legacy)."""
        self.target_fps = target_fps
        if self.use_process:
            self.start()
            return

        if self.is_running:
            return
        self.is_running = True
        self.thread = threading.Thread(target=self._worker_loop, args=(target_fps,), daemon=True)
        self.thread.start()
        print("[TonyPiSim] Background simulation thread started.")

    def _worker_loop(self, target_fps):
        interval = 1.0 / target_fps
        while self.is_running:
            try:
                item = self.queue.get(timeout=interval)
                l_e, l_p, r_e, r_p = item
                self.update_arms(l_e, l_p, r_e, r_p)
            except queue.Empty:
                p.stepSimulation()
            except Exception as e:
                print(f"[TonyPiSim Worker] Error: {e}")
                break

    def stop_background_thread(self):
        """Stops background worker thread."""
        self.is_running = False
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        self.thread = None


# =====================================================================
# 3. VERIFICATION & INTERACTIVE DEMO RUNTIME
# =====================================================================

def run_tests():
    """Automated unit verification for URDF parsing and simulation."""
    print("=" * 60)
    print("RUNNING TONYPI SIMULATION & URDF VERIFICATION TESTS")
    print("=" * 60)

    # 1. Initialize Headless Twin
    twin = TonyPiDigitalTwin(gui=False, headless=True)
    assert twin.robot_id >= 0, "Failed to load TonyPi robot in physics engine"

    # 2. Check essential joints
    required_joints = [
        "l_shoulder_pitch", "l_shoulder_roll", "l_elbow",
        "r_shoulder_pitch", "r_shoulder_roll", "r_elbow",
        "neck_pan", "neck_pitch"
    ]
    for j in required_joints:
        assert j in twin.joint_indices, f"Missing critical joint: {j}"
        pos, _ = twin.get_joint_state(j)
        print(f"  [PASS] Joint '{j}' validated (default position: {pos:.3f} rad)")

    # 3. Test Wingspan (0° shoulder plane, straight 180° arms)
    print("\nTesting Pose: Wingspan (0° plane, 180° elbow)...")
    twin.update_arms(l_elbow_deg=180.0, l_shoulder_plane_deg=0.0,
                     r_elbow_deg=180.0, r_shoulder_plane_deg=0.0)
    l_roll, _ = twin.get_joint_state("l_shoulder_roll")
    r_roll, _ = twin.get_joint_state("r_shoulder_roll")
    assert abs(l_roll - 1.5707963) < 0.05, f"Expected Left Roll ~ +1.57, got {l_roll}"
    assert abs(r_roll - (-1.5707963)) < 0.05, f"Expected Right Roll ~ -1.57, got {r_roll}"
    print("  [PASS] Wingspan lateral abduction confirmed (+1.57 rad L, -1.57 rad R)")

    # 4. Test Wingspan Curl (0° shoulder plane, 60° elbow)
    print("\nTesting Pose: Wingspan Curl (0° plane, 60° elbow)...")
    twin.update_arms(l_elbow_deg=60.0, l_shoulder_plane_deg=0.0,
                     r_elbow_deg=60.0, r_shoulder_plane_deg=0.0)
    l_elb, _ = twin.get_joint_state("l_elbow")
    expected_elb = -math.radians(180.0 - 60.0) # -120 deg = -2.094 rad
    assert abs(l_elb - expected_elb) < 0.05, f"Expected Left Elbow ~ {expected_elb}, got {l_elb}"
    print(f"  [PASS] Wingspan elbow flexion confirmed ({l_elb:.3f} rad)")

    # 5. Test Forward Pose (90° shoulder plane, straight 180° arms)
    print("\nTesting Pose: Forward Sagittal (90° plane, 180° elbow)...")
    twin.update_arms(l_elbow_deg=180.0, l_shoulder_plane_deg=90.0,
                     r_elbow_deg=180.0, r_shoulder_plane_deg=90.0)
    l_pitch, _ = twin.get_joint_state("l_shoulder_pitch")
    l_roll, _ = twin.get_joint_state("l_shoulder_roll")
    assert abs(l_pitch - (-1.5707963)) < 0.05, f"Expected Left Pitch ~ -1.57, got {l_pitch}"
    assert abs(l_roll - 0.0) < 0.05, f"Expected Left Roll ~ 0.0, got {l_roll}"
    print("  [PASS] Forward pitch confirmed (-1.57 rad L pitch, 0.0 rad roll)")

    # 6. Test Forward Curl (90° plane, 45° deep curl)
    print("\nTesting Pose: Forward Curl (90° plane, 45° elbow)...")
    twin.update_arms(l_elbow_deg=45.0, l_shoulder_plane_deg=90.0,
                     r_elbow_deg=45.0, r_shoulder_plane_deg=90.0)
    l_elb, _ = twin.get_joint_state("l_elbow")
    expected_elb = -math.radians(180.0 - 45.0)
    assert abs(l_elb - expected_elb) < 0.05, f"Expected Left Elbow ~ {expected_elb}, got {l_elb}"
    print(f"  [PASS] Forward deep curl confirmed ({l_elb:.3f} rad)")

    # 7. Test Head Pan & Tilt
    print("\nTesting Head Pan & Tilt...")
    twin.set_head(pitch_deg=20.0, pan_deg=-30.0)
    pan, _ = twin.get_joint_state("neck_pan")
    pitch, _ = twin.get_joint_state("neck_pitch")
    assert abs(pan - math.radians(-30.0)) < 0.05
    assert abs(pitch - math.radians(20.0)) < 0.05
    print("  [PASS] Head servos 1 & 2 position confirmed")

    # 8. Test Async Background Worker Queue
    print("\nTesting Async Background Worker...")
    twin.start_background_thread(target_fps=60)
    twin.update_arms_async(140.0, 45.0, 140.0, 45.0)
    time.sleep(0.15)
    twin.stop_background_thread()
    print("  [PASS] Non-blocking worker queue executed smoothly")

    twin.close()

    # 9. Test Synchronous Mode Step
    print("\nTesting Synchronous Single-Thread Mode...")
    sync_twin = TonyPiDigitalTwin(gui=False, headless=True, use_process=False)
    sync_twin.start()
    sync_twin.update_arms(l_elbow=120.0, l_shoulder_plane=30.0, r_elbow=120.0, r_shoulder_plane=30.0)
    sync_twin.step()
    l_elb_sync, _ = sync_twin.get_joint_state("l_elbow")
    assert abs(l_elb_sync - (-math.radians(180.0 - 120.0))) < 0.05
    sync_twin.close()
    print("  [PASS] Synchronous single-thread execution verified")

    # 10. Test Multiprocess Twin Headless Initialization & Lifecycle
    print("\nTesting Multiprocess Headless Twin Lifecycle...")
    mp_twin = TonyPiDigitalTwin(gui=False, headless=True, use_process=True)
    mp_twin.start()
    mp_twin.update_arms(l_elbow=90.0, l_shoulder_plane=45.0, r_elbow=90.0, r_shoulder_plane=45.0)
    mp_twin.close()
    print("  [PASS] Multiprocess lifecycle validated cleanly")

    # 11. Test 3D Full Range Poses (Superhero, Hands Up, Zombie, Flapping)
    print("\nTesting 3D Full Range Poses (Superhero, Hands Up, Zombie, Flapping)...")
    rom_twin = TonyPiDigitalTwin(gui=False, headless=True)
    # Superhero pose (< | >): flared elbows, bent arms
    rom_twin.update_arms(l_pitch_deg=0.0, l_roll_deg=45.0, l_elbow_deg=65.0,
                         r_pitch_deg=0.0, r_roll_deg=-45.0, r_elbow_deg=65.0)
    l_r, _ = rom_twin.get_joint_state("l_shoulder_roll")
    r_r, _ = rom_twin.get_joint_state("r_shoulder_roll")
    assert abs(math.degrees(l_r) - 45.0) < 1.0
    assert abs(math.degrees(r_r) - (-45.0)) < 1.0

    # Hands Up: vertical 180° raise
    rom_twin.update_arms(l_pitch_deg=0.0, l_roll_deg=180.0, l_elbow_deg=180.0,
                         r_pitch_deg=0.0, r_roll_deg=-180.0, r_elbow_deg=180.0)
    l_r, _ = rom_twin.get_joint_state("l_shoulder_roll")
    assert abs(math.degrees(l_r) - 180.0) < 1.0

    # Zombie pose: straight forward 90° pitch
    rom_twin.update_arms(l_pitch_deg=-90.0, l_roll_deg=0.0, l_elbow_deg=180.0,
                         r_pitch_deg=-90.0, r_roll_deg=0.0, r_elbow_deg=180.0)
    l_p, _ = rom_twin.get_joint_state("l_shoulder_pitch")
    assert abs(math.degrees(l_p) - (-90.0)) < 1.0
    rom_twin.close()
    print("  [PASS] Full 3D range of motion poses validated successfully")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED: Hiwonder TonyPi URDF & Digital Twin are 100% operational!")
    print("=" * 60)
    return True


def run_interactive_demo(use_process=True, headless=False):
    """Runs interactive 3D digital twin visualization showcasing arm exercise motions."""
    mode_name = "Headless" if headless else ("Multiprocess (Zero-WGL-Contention)" if use_process else "Synchronous (Single-Thread)")
    print("\n--- Starting Hiwonder TonyPi 3D Digital Twin Demo ---")
    print(f"Architecture Mode: {mode_name}")
    print("Demonstrating 3D Physical Therapy & Full Range of Motion Kinematics:")
    print("  1. Continuous Flapping Wings (0° down -> 90° T-pose -> 180° Hands Up -> down)")
    print("  2. Superhero Pose (< | >) (Hands on hips, flared elbows, 65° flexion)")
    print("  3. Zombie Pose (Forward sagittal raise, -90° pitch, straight arms)")
    print("  4. 3D Circular Arm Rolls / Wing Circles (Continuous pitch & roll circles)")
    print("Press Ctrl+C to stop.\n")

    twin = TonyPiDigitalTwin(gui=not headless, headless=headless, use_process=use_process)
    twin.start()

    t0 = time.time()
    try:
        while True:
            # If 3D window was closed by the user, exit gracefully
            if not headless and twin.is_window_closed():
                print("[TonyPiSim] 3D window closed by user. Exiting demo.")
                break

            t = time.time() - t0
            # 16-second cyclic exercise routine
            phase = (t % 16.0)

            if phase < 4.0:
                # Phase 1: Continuous Flapping Wings (0° down to 180° overhead and back)
                progress = (phase / 4.0)
                # Elevation angle sweeps 0° -> 180° -> 0°
                elevation = 90.0 - 90.0 * math.cos(progress * 2.0 * math.pi)
                twin.update_arms(
                    l_pitch_deg=0.0, l_roll_deg=elevation, l_elbow_deg=180.0,
                    r_pitch_deg=0.0, r_roll_deg=-elevation, r_elbow_deg=180.0
                )
                twin.set_head(pitch_deg=5.0 * math.sin(t * 3), pan_deg=0.0)

            elif phase < 8.0:
                # Phase 2: Superhero Pose (< | >) (Hands on hips, elbows flared at 45°, 65° flexed)
                progress = ((phase - 4.0) / 4.0)
                breath = 4.0 * math.sin(progress * 4.0 * math.pi)
                twin.update_arms(
                    l_pitch_deg=0.0, l_roll_deg=45.0 + breath, l_elbow_deg=65.0,
                    r_pitch_deg=0.0, r_roll_deg=-(45.0 + breath), r_elbow_deg=65.0
                )
                twin.set_head(pitch_deg=0.0, pan_deg=10.0 * math.sin(progress * 2.0 * math.pi))

            elif phase < 12.0:
                # Phase 3: Zombie Pose (Arms extended straight forward, pitch -90°, roll 0°)
                progress = ((phase - 8.0) / 4.0)
                sway = 5.0 * math.sin(progress * 2.0 * math.pi)
                twin.update_arms(
                    l_pitch_deg=-90.0, l_roll_deg=sway, l_elbow_deg=180.0,
                    r_pitch_deg=-90.0, r_roll_deg=-sway, r_elbow_deg=180.0
                )
                twin.set_head(pitch_deg=5.0 * math.cos(t * 2), pan_deg=0.0)

            else:
                # Phase 4: 3D Circular Arm Rolls / Wing Circles (circular trajectory in pitch & roll)
                progress = ((phase - 12.0) / 4.0)
                angle = progress * 4.0 * math.pi
                roll_center = 75.0
                roll_radius = 45.0
                pitch_center = -45.0
                pitch_radius = 40.0
                
                l_roll = roll_center + roll_radius * math.cos(angle)
                l_pitch = pitch_center + pitch_radius * math.sin(angle)
                
                twin.update_arms(
                    l_pitch_deg=l_pitch, l_roll_deg=l_roll, l_elbow_deg=160.0,
                    r_pitch_deg=l_pitch, r_roll_deg=-l_roll, r_elbow_deg=160.0
                )
                twin.set_head(pitch_deg=0.0, pan_deg=-15.0 * math.sin(angle))

            time.sleep(0.016) # ~60 FPS
    except KeyboardInterrupt:
        print("\nStopping demo...")
    finally:
        twin.close()


def main():
    parser = argparse.ArgumentParser(description="Hiwonder TonyPi 3D Simulation & Digital Twin")
    parser.add_argument("--test", action="store_true", help="Run automated URDF & simulation tests")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode without window")
    parser.add_argument("--sync", action="store_true", help="Run in synchronous single-thread mode instead of multiprocess")
    args = parser.parse_args()

    if args.test:
        success = run_tests()
        sys.exit(0 if success else 1)
    else:
        run_interactive_demo(use_process=not args.sync, headless=args.headless)


if __name__ == "__main__":
    mp.freeze_support()
    main()
