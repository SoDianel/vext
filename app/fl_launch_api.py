"""Authenticated actions for FL Studio loading; GETs only inspect state."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import applog, db, fl_launch, midi_loopback, virtual_midi_service
from app.config import DB_PATH

router = APIRouter(prefix="/api/fl-launch")


class PresetBody(BaseModel):
    plugin_id: int | None = None
    key: str = Field(default="", max_length=2000)


class InstallBody(BaseModel):
    path: str
    confirmed: bool = False


@router.get("/setup")
def setup():
    with db.connect(DB_PATH) as conn:
        value = fl_launch.setup_info(conn)
    value["loopback"] = midi_loopback.inspect(value["port"])
    value["loopback"]["managed"] = virtual_midi_service.status()
    value["outputs"] = value["loopback"]["outputs"]
    return value


@router.get("/connection")
def connection():
    with db.connect(DB_PATH) as conn:
        return fl_launch.connection(conn)


@router.get("/status")
def status():
    return fl_launch.status()


@router.get("/menu")
def menu(include_plugins: bool = False):
    with db.connect(DB_PATH) as conn:
        return fl_launch.menu(conn, include_plugins=include_plugins)


@router.get("/plugins")
def menu_plugins(kind: str = "", value: str = Query("", max_length=200)):
    try:
        with db.connect(DB_PATH) as conn:
            return fl_launch.plugins_for(conn, kind, value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/preset")
def preset_by_key(key: str = Query(..., max_length=2000)):
    try:
        with db.connect(DB_PATH) as conn:
            return fl_launch.preset_by_key(conn, key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/preset-image")
def preset_image(key: str = Query(..., max_length=2000)):
    try:
        with db.connect(DB_PATH) as conn:
            image = fl_launch.preset_image(conn, key)
        if image is None:
            raise HTTPException(404, "No FL preset image.")
        return FileResponse(image, media_type="image/png")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/presets/{plugin_id}")
def presets(plugin_id: int):
    try:
        with db.connect(DB_PATH) as conn:
            return {"presets": fl_launch.presets(conn, plugin_id)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/install")
def install(body: InstallBody):
    if not body.confirmed:
        raise HTTPException(400, "Confirm the controller script installation first.")
    try:
        with db.connect(DB_PATH) as conn:
            result = fl_launch.install(conn, body.path)
        applog.log_event("FL controller installed/updated after confirmation: " + body.path)
        return result
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/ping")
def ping():
    try:
        with db.connect(DB_PATH) as conn:
            return fl_launch.send(conn)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/drag-resolve")
def drag_resolve(body: PresetBody):
    try:
        with db.connect(DB_PATH) as conn:
            return fl_launch.select_preset(conn, body.plugin_id, body.key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/load")
def load(body: PresetBody):
    try:
        with db.connect(DB_PATH) as conn:
            preset = fl_launch.select_preset(conn, body.plugin_id, body.key)
            return fl_launch.send(conn, preset)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
