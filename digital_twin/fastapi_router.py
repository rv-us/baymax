"""Mount the twin inside an existing FastAPI dashboard (optional; needs ``fastapi``).

    from fastapi import FastAPI
    from digital_twin.fastapi_router import mount_twin

    app = FastAPI()
    twin = mount_twin(app, prefix="/twin")      # viewer at /twin/web/index.html, API at /twin/api/*
    twin.state.set_command({...}, source="dashboard")   # same TwinState the estimator can drive

Same endpoints as the standalone server (see server.py), so web/twin.js works unchanged:
``new BaymaxTwin(el, { baseUrl: "/twin" })``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .api import ApiError, TwinApi
from .server import WEB_DIR
from .state import TwinState


def _call(fn, body: Optional[dict] = None):
    try:
        return fn(body or {}) if body is not None else fn()
    except ApiError as e:
        raise HTTPException(e.status, str(e))


def make_router(api: TwinApi) -> APIRouter:
    state = api.state
    router = APIRouter()

    @router.get("/api/model")
    def model():
        return api.model()

    @router.get("/api/poses")
    def poses():
        return api.poses()

    @router.get("/api/state")
    def snapshot():
        return api.snapshot()

    @router.get("/tonypi.urdf")
    def urdf():
        return FileResponse(state.urdf_path, media_type="application/xml")

    @router.post("/api/command")
    def command(body: dict = Body(default={})):
        return _call(api.command, body)

    @router.post("/api/pose")
    def pose(body: dict = Body(default={})):
        return _call(api.pose, body)

    @router.post("/api/view")
    def view(body: dict = Body(default={})):
        return _call(api.view, body)

    @router.post("/api/robot")
    def robot(body: dict = Body(default={})):
        return _call(api.connect_robot, body)

    @router.post("/api/clone")
    def clone():
        return _call(api.clone)

    @router.get("/api/stream")
    async def stream():
        async def events():
            last = -1
            while True:
                version = await asyncio.to_thread(state.wait_for_change, last, 1.0)
                snap = state.snapshot()
                yield f"data: {json.dumps(snap)}\n\n"
                last = version
                await asyncio.sleep(1 / 30)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return router


def mount_twin(app: FastAPI, prefix: str = "/twin", state: Optional[TwinState] = None,
               allow_robot_config: bool = False) -> TwinApi:
    """Attach API + viewer to ``app``. Returns the TwinApi (``.state`` to drive it, ``.attach_robot`` to mirror)."""
    api = TwinApi(state or TwinState(), allow_robot_config=allow_robot_config)
    app.include_router(make_router(api), prefix=prefix)
    app.mount(f"{prefix}/web", StaticFiles(directory=WEB_DIR), name="twin-web")

    @app.get(prefix, include_in_schema=False)
    def _index():
        return RedirectResponse(f"{prefix}/web/index.html")

    return api
