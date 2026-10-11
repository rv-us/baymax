// Baymax TonyPi digital-twin viewer. Self-contained ES module: embed it in any dashboard.
//
//   import { BaymaxTwin } from "http://<twin-host>:8765/web/twin.js";
//   const twin = new BaymaxTwin(document.getElementById("view"));
//   await twin.init();      // fetches the URDF model
//   twin.connect();         // live Server-Sent Events stream (auto-reconnects)
//
// Or feed it yourself: twin.setState(snapshot) with the JSON from GET /api/state.
import * as THREE from "./vendor/three.module.min.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

const DEFAULT_BASE = new URL("../", import.meta.url).href.replace(/\/$/, "");
const STAND_HEIGHT = 0.251; // torso height above the floor in the stand pose (metres)

const CAMERAS = {
  iso:   [0.75, 0.42, 0.75],
  front: [1.05, 0.28, 0.0],
  side:  [0.0, 0.28, 1.05],
  back:  [-1.05, 0.28, 0.0],
  top:   [0.05, 1.15, 0.0],
};

function colorOf(model, name) {
  const c = model.materials[name] || [0.8, 0.8, 0.85, 1];
  return new THREE.Color(c[0], c[1], c[2]);
}

// One articulated copy of the URDF. `ghost` renders it as a translucent overlay.
class RobotView {
  constructor(model, { ghost = false, tint = 0xff9f43 } = {}) {
    this.ghost = ghost;
    this.joints = {};
    this.angles = {};   // current (smoothed) degrees
    this.target = {};
    this.base = new THREE.Group();     // floor-contact transform (ROS frame: x fwd, y left, z up)
    this.basePos = new THREE.Vector3(0, 0, STAND_HEIGHT);
    this.baseQuat = new THREE.Quaternion();
    this.primed = false;
    this.group = new THREE.Group();    // ROS -> three.js (Y up): rotate -90 deg about X
    this.group.rotation.x = -Math.PI / 2;
    this.group.add(this.base);

    const mats = new Map();
    const material = (name) => {
      const key = this.ghost ? "ghost" : name;
      if (!mats.has(key)) {
        mats.set(key, this.ghost
          ? new THREE.MeshBasicMaterial({ color: tint, transparent: true, opacity: 0.28, depthWrite: false })
          : new THREE.MeshStandardMaterial({ color: colorOf(model, name), roughness: 0.55, metalness: 0.12 }));
      }
      return mats.get(key);
    };

    const links = {};
    for (const [lname, visuals] of Object.entries(model.links)) {
      const g = new THREE.Group();
      g.name = lname;
      for (const v of visuals) {
        let geom;
        if (v.type === "box") geom = new THREE.BoxGeometry(v.size[0], v.size[1], v.size[2]);
        else if (v.type === "cylinder") { geom = new THREE.CylinderGeometry(v.radius, v.radius, v.length, 24); geom.rotateX(Math.PI / 2); }
        else if (v.type === "sphere") geom = new THREE.SphereGeometry(v.radius, 24, 16);
        else continue;
        const mesh = new THREE.Mesh(geom, material(v.material));
        mesh.castShadow = !this.ghost;
        mesh.receiveShadow = !this.ghost;
        mesh.position.set(...v.xyz);
        mesh.rotation.set(v.rpy[0], v.rpy[1], v.rpy[2], "ZYX");
        g.add(mesh);
      }
      links[lname] = g;
    }
    for (const j of model.joints) {
      const frame = new THREE.Group(); // joint origin in the parent link
      frame.position.set(...j.xyz);
      frame.rotation.set(j.rpy[0], j.rpy[1], j.rpy[2], "ZYX");
      const pivot = new THREE.Group(); // rotates about the joint axis
      frame.add(pivot);
      pivot.add(links[j.child]);
      links[j.parent].add(frame);
      if (j.type === "revolute" || j.type === "continuous") {
        this.joints[j.name] = { pivot, axis: new THREE.Vector3(...j.axis).normalize() };
        this.angles[j.name] = 0;
        this.target[j.name] = 0;
      }
    }
    this.base.add(links[model.root]);
    this.applyBase(1);
    this.applyAngles();
  }

  setTarget(angles, base, ground) {
    for (const [n, a] of Object.entries(angles || {})) if (n in this.target) this.target[n] = a;
    if (ground && base) {
      this.basePos.set(base.pos[0], base.pos[1], base.pos[2]);
      this.baseQuat.set(base.quat[0], base.quat[1], base.quat[2], base.quat[3]);
    } else {
      this.basePos.set(0, 0, STAND_HEIGHT);
      this.baseQuat.identity();
    }
    if (!this.primed) {
      this.primed = true;
      for (const n in this.target) this.angles[n] = this.target[n];
      this.applyBase(1);
      this.applyAngles();
    }
  }

  applyBase(k) {
    this.base.position.lerp(this.basePos, k);
    this.base.quaternion.slerp(this.baseQuat, k);
  }

  applyAngles() {
    const q = new THREE.Quaternion();
    for (const [n, j] of Object.entries(this.joints)) {
      q.setFromAxisAngle(j.axis, THREE.MathUtils.degToRad(this.angles[n]));
      j.pivot.quaternion.copy(q);
    }
  }

  step(dt) {
    const k = 1 - Math.exp(-dt * 16); // ~60 ms time constant: hides 4 Hz robot polling and 30 Hz jitter
    for (const n in this.angles) this.angles[n] += (this.target[n] - this.angles[n]) * k;
    this.applyBase(k);
    this.applyAngles();
  }
}

export class BaymaxTwin {
  constructor(container, opts = {}) {
    this.container = container;
    this.baseUrl = (opts.baseUrl ?? DEFAULT_BASE).replace(/\/$/, "");
    this.options = { ghost: opts.ghost ?? true, ground: opts.ground ?? true };
    this.onState = opts.onState || (() => {});
    this.onConnection = opts.onConnection || (() => {});
    this.model = null;
    this.state = null;
    this.main = null;
    this.ghostView = null;
    this._es = null;
    this._raf = 0;
    this._last = performance.now();
    this._initThree();
  }

  _initThree() {
    const el = this.container;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    el.appendChild(renderer.domElement);
    renderer.domElement.style.cssText = "display:block;width:100%;height:100%;touch-action:none";
    this.renderer = renderer;

    const scene = new THREE.Scene();
    const bg = getComputedStyle(el).getPropertyValue("--twin-bg").trim() || "#0f1319";
    scene.background = new THREE.Color(bg);
    scene.fog = new THREE.Fog(bg, 2.2, 5);
    this.scene = scene;

    const camera = new THREE.PerspectiveCamera(38, 1, 0.02, 20);
    camera.position.set(...CAMERAS.iso);
    this.camera = camera;
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(0, 0.12, 0);
    controls.enableDamping = true;
    controls.minDistance = 0.3;
    controls.maxDistance = 3;
    controls.maxPolarAngle = Math.PI * 0.495;
    this.controls = controls;

    scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x3a3f4a, 0.9));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(0.8, 1.4, 0.6);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -0.6, right: 0.6, top: 0.6, bottom: -0.6, near: 0.2, far: 4 });
    sun.shadow.bias = -0.0004;
    scene.add(sun);

    const floor = new THREE.Mesh(new THREE.CircleGeometry(2.4, 64),
      new THREE.MeshStandardMaterial({ color: 0x1a2029, roughness: 0.95 }));
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    scene.add(floor);
    const grid = new THREE.GridHelper(2, 20, 0x3b4656, 0x262e3a);
    grid.position.y = 0.0005;
    scene.add(grid);
    // "front" arrow on the floor: the robot faces +X
    const arrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(0.14, 0.001, 0), 0.12, 0x4cc3ff, 0.035, 0.025);
    scene.add(arrow);

    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(el);
    this._resize();
  }

  _resize() {
    const w = Math.max(1, this.container.clientWidth), h = Math.max(1, this.container.clientHeight);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  async init() {
    const res = await fetch(`${this.baseUrl}/api/model`);
    if (!res.ok) throw new Error(`model: HTTP ${res.status}`);
    this.model = await res.json();
    this.main = new RobotView(this.model);
    this.ghostView = new RobotView(this.model, { ghost: true });
    this.scene.add(this.main.group, this.ghostView.group);
    this.ghostView.group.visible = false;
    if (this.state) this.setState(this.state);
    this._last = performance.now();
    const loop = (now) => {
      this._raf = requestAnimationFrame(loop);
      const dt = Math.min(0.1, (now - this._last) / 1000);
      this._last = now;
      this.main.step(dt);
      this.ghostView.step(dt);
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    };
    this._raf = requestAnimationFrame(loop);
    return this.model;
  }

  setOptions(o) {
    Object.assign(this.options, o);
    if (this.state) this.setState(this.state);
  }

  setCameraPreset(name) {
    const p = CAMERAS[name];
    if (!p) return;
    this.camera.position.set(...p);
    this.controls.target.set(0, 0.12, 0);
  }

  // Which channel is drawn as the robot, and which (if any) as the ghost.
  static pickChannels(state) {
    const ch = state.channels;
    let main = ch[state.display] ? state.display : (ch.command ? "command" : "actual");
    const other = main === "command" ? "actual" : "command";
    return { main, ghost: ch[other] ? other : null };
  }

  setState(state) {
    this.state = state;
    if (!this.main) return;
    const { main, ghost } = BaymaxTwin.pickChannels(state);
    const m = state.channels[main];
    this.main.setTarget(m.angles, m.base, this.options.ground);
    const g = ghost && this.options.ghost ? state.channels[ghost] : null;
    this.ghostView.group.visible = !!g;
    if (g) this.ghostView.setTarget(g.angles, g.base, this.options.ground);
    this.onState(state, { main, ghost });
  }

  connect() {
    this.disconnect();
    const es = new EventSource(`${this.baseUrl}/api/stream`);
    this._es = es;
    es.onopen = () => this.onConnection(true);
    es.onerror = () => this.onConnection(false); // EventSource retries by itself
    es.onmessage = (ev) => { try { this.setState(JSON.parse(ev.data)); } catch (e) { console.warn("bad twin frame", e); } };
  }

  disconnect() {
    if (this._es) { this._es.close(); this._es = null; }
  }

  async post(path, body) {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  dispose() {
    this.disconnect();
    cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
