"""Adaptive Light - application entry point."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config, ConfigError, blank, load, loads, save
from app.deploy import check as deploy_check
from app.deploy import deploy
from app.runtime import Runtime
from app.store import Store

logging.basicConfig(
    level=os.environ.get("AL_LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("adaptive_light")

DATA_DIR = Path(os.environ.get("AL_DATA_DIR", "/data"))
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
CONFIG_PATH = DATA_DIR / "config" / "config.json"


@dataclass
class State:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    config: Config | None = None
    store: Store | None = None
    runtime: Runtime | None = None
    error: str | None = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state.store = Store(DATA_DIR, SCHEMA_DIR / "storage.schema.sql")

    if not CONFIG_PATH.exists():
        # A fresh install starts with no rooms and does nothing until
        # the UI adds one. It must still come up and serve.
        save(blank(), CONFIG_PATH)
        log.info("wrote a blank config to %s", CONFIG_PATH)

    try:
        state.config = load(CONFIG_PATH, SCHEMA_DIR / "config.schema.json")
        state.store.save_config_version(state.config.raw)
    except ConfigError as exc:
        state.error = str(exc)
        log.error("configuration is not usable:\n%s", exc)

    if state.config and state.config.active_rooms and state.config.homeassistant.token:
        try:
            state.runtime = Runtime(state.config, state.store)
            await state.runtime.start()
        except Exception as exc:
            state.error = f"{type(exc).__name__}: {exc}"
            log.exception("runtime failed to start")
    elif state.config and not state.config.active_rooms:
        log.info("no rooms configured yet; idle until one is added")

    yield

    if state.runtime:
        await state.runtime.stop()
    if state.store:
        state.store.close()


app = FastAPI(title="Adaptive Light", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> JSONResponse:
    # Degraded when disconnected from HA: a process that is alive but
    # cannot see Home Assistant is the silent failure worth catching.
    # A container with no rooms yet is healthy - it is simply waiting.
    idle = bool(state.config) and not state.config.active_rooms
    connected = bool(state.runtime and state.runtime.ws.connected)
    ok = idle or connected
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "idle": idle,
            "ha_connected": connected,
            "error": state.error,
            "uptime_seconds": int(
                (datetime.now(timezone.utc) - state.started_at).total_seconds()),
        },
    )


@app.get("/api/status")
def api_status() -> dict:
    from datetime import date as _date
    today = _date.today().isoformat()
    rooms = {}
    if state.runtime:
        for room_id, rs in state.runtime.rooms.items():
            entry = {
                "name": rs.room.name,
                "section": rs.fired_section,
                "since": rs.fired_at.isoformat() if rs.fired_at else None,
                "guard": rs.guard_on,
                "hold": rs.hold_on,
                "groups": len(rs.room.groups),
            }
            if state.store:
                entry.update(state.store.activity(room_id, today))
            rooms[room_id] = entry
    return {
        "ha_connected": bool(state.runtime and state.runtime.ws.connected),
        "ha_version": state.runtime.ws.ha_version if state.runtime else None,
        # Rising while nothing else happens is the proof that the
        # subscription is live rather than silently dead.
        "events_seen": state.runtime.events_seen if state.runtime else 0,
        "last_event_at": (state.runtime.last_event_at.isoformat()
                          if state.runtime and state.runtime.last_event_at else None),
        "error": state.error,
        "rooms": rooms,
    }


@app.get("/api/events")
def api_events(limit: int = 100, room_id: str | None = None,
               category: str | None = None, min_severity: str | None = None) -> dict:
    """Backs the UI log - the visible record of what happened and when."""
    if not state.store:
        return {"events": []}
    return {"events": state.store.recent_events(limit, room_id, category, min_severity)}


@app.get("/api/almanac/{room_id}")
def api_almanac(room_id: str) -> dict:
    if not state.store:
        return {}
    return state.store.current_almanac(room_id) or {}


@app.post("/api/analysis/run")
async def api_run_analysis() -> dict:
    """Rebuild almanacs now rather than waiting for the nightly run."""
    if not state.runtime:
        return {"ok": False, "error": "runtime not started"}
    await state.runtime.run_analysis()
    return {"ok": True}


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

async def _restart_runtime() -> None:
    """Apply a config change without restarting the container."""
    if state.runtime:
        await state.runtime.stop()
        state.runtime = None
    if state.config and state.config.active_rooms and state.config.homeassistant.token:
        state.runtime = Runtime(state.config, state.store)
        await state.runtime.start()


@app.get("/api/config")
def api_get_config() -> dict:
    """The stored document. The token is never included - it comes from
    the environment and is stripped on save."""
    if not CONFIG_PATH.exists():
        return blank()
    doc = json.loads(CONFIG_PATH.read_text())
    doc.get("homeassistant", {}).pop("access_token", None)
    return doc


@app.put("/api/config")
async def api_put_config(doc: dict = Body(...)) -> dict:
    """Validate, persist, then reload the runtime in place.

    Validation happens before anything is written: a config that would
    not load must not be able to replace one that does.
    """
    try:
        config = loads(doc, SCHEMA_DIR / "config.schema.json")
    except ConfigError as exc:
        raise HTTPException(status_code=422,
                            detail={"problems": exc.problems}) from exc

    save(doc, CONFIG_PATH)
    state.config = config
    state.error = None
    if state.store:
        state.store.save_config_version(config.raw)
    await _restart_runtime()
    return {"ok": True, "rooms": len(config.active_rooms)}


# ---------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------

@app.get("/api/deploy/check")
async def api_deploy_check() -> dict:
    """Does Home Assistant already hold what the config expects?
    Read-only; drives the "deploy needed" indicator in the UI."""
    if not state.config:
        raise HTTPException(status_code=409, detail="no usable configuration")
    if not state.runtime:
        raise HTTPException(status_code=409,
                            detail="not connected to Home Assistant")
    return await deploy_check(state.config, state.runtime.rest)


@app.post("/api/deploy")
async def api_deploy() -> dict:
    """Create the helpers and automations, and remove orphans."""
    if not state.config:
        raise HTTPException(status_code=409, detail="no usable configuration")
    if not state.runtime:
        raise HTTPException(status_code=409,
                            detail="not connected to Home Assistant")
    report = await deploy(state.config, state.runtime.rest,
                          state.runtime.ws, state.store)
    # Newly created automations need mapping before they can be fired.
    await state.runtime._map_automations()
    if report.ok:
        # Clear the fired marker so the scheduler applies the CURRENT
        # section on its next tick. Without this the room stays dark
        # until the next boundary, which after a 22:00 deploy means
        # nothing visibly happens until 05:30.
        for room_state in state.runtime.rooms.values():
            room_state.fired_section = None
    return report.as_dict()


# ---------------------------------------------------------------------
# Entity discovery - backs the config pickers
# ---------------------------------------------------------------------

@app.get("/api/entities")
async def api_entities() -> dict:
    """Lights, illuminance sensors and presence sensors, from Home
    Assistant itself.

    Pickers rather than free text: a typo'd entity id does not error in
    Home Assistant, it silently resolves to unknown forever. Choosing
    from a list makes that class of failure impossible.
    """
    if not state.runtime:
        raise HTTPException(status_code=409, detail="not connected to Home Assistant")

    lights, lux, presence = [], [], []
    for s in await state.runtime.rest.states():
        entity = s["entity_id"]
        attrs = s.get("attributes", {})
        item = {"entity_id": entity,
                "name": attrs.get("friendly_name") or entity}
        if entity.startswith("light."):
            lights.append(item)
        elif attrs.get("device_class") == "illuminance":
            lux.append(item)
        elif attrs.get("device_class") in ("occupancy", "motion", "presence"):
            presence.append(item)

    key = lambda i: i["name"].lower()
    return {"lights": sorted(lights, key=key),
            "illuminance": sorted(lux, key=key),
            "presence": sorted(presence, key=key)}


@app.get("/api/schedule/preview")
def api_schedule_preview(room_id: str | None = None, days: int = 365) -> dict:
    """Today's computed boundaries, plus a year-ahead collision scan.

    Section collisions are seasonal - a schedule that behaves perfectly
    in July can silently lose a section in December. Showing the year
    means a collision is something you see coming rather than discover.
    """
    from collections import Counter
    from datetime import date as _date
    from datetime import timedelta as _td

    from app.scheduler import compute_day

    if not state.runtime or not state.runtime.sun:
        raise HTTPException(status_code=409, detail="not connected to Home Assistant")

    room = (state.config.room(room_id) if room_id and state.config
            else (state.config.active_rooms[0] if state.config
                  and state.config.active_rooms else None))
    schedule = room.schedule if room else state.config.schedule
    tz = state.runtime.tz
    today = datetime.now(tz).date()

    boundaries = [
        {"section": b.section, "name": b.name,
         "at": b.planned.strftime("%H:%M") if b.planned else None,
         "ends": b.ends.strftime("%H:%M") if b.ends else None,
         "outcome": b.outcome, "reason": b.reason}
        for b in compute_day(schedule, state.runtime.sun, today, tz)
    ]

    collisions: Counter = Counter()
    for offset in range(days):
        for b in compute_day(schedule, state.runtime.sun,
                             today + _td(days=offset), tz):
            if not b.ran:
                collisions[b.section] += 1

    return {"date": today.isoformat(), "boundaries": boundaries,
            "collisions": dict(collisions), "days_scanned": days}


# ---------------------------------------------------------------------
# Now - the live comparison the dashboard renders
# ---------------------------------------------------------------------

@app.get("/api/now/{room_id}")
async def api_now(room_id: str) -> dict:
    """What the almanac wants versus what the room is actually doing,
    right now. One HA read per group plus the sensors, joined to the
    current almanac section."""
    if not state.runtime:
        raise HTTPException(status_code=409, detail="not connected to Home Assistant")
    rs = state.runtime.rooms.get(room_id)
    if rs is None:
        raise HTTPException(status_code=404, detail=f"no such room: {room_id}")

    section = rs.fired_section
    almanac = state.store.current_almanac(room_id) if state.store else None
    entry = (almanac or {}).get(section) if section else None

    lux, sensor_n = await state.runtime._read_lux(rs.room)
    occupied = await state.runtime._read_presence(rs.room)

    groups = []
    for group in rs.room.groups:
        st = await state.runtime.rest.state(group.entity_id)
        on = bool(st and st.get("state") == "on")
        brightness = None
        if on:
            b = st.get("attributes", {}).get("brightness")
            brightness = int(b) if b is not None else None
        target = entry.get(group.id) if isinstance(entry, dict) else None
        mode = "auto"
        if isinstance(entry, dict):
            mode = "off" if target == 0 else "auto"
        groups.append({
            "id": group.id, "name": group.name, "entity_id": group.entity_id,
            "on": on, "brightness": brightness,
            "target": target, "mode": mode,
            "on_fraction": (entry.get("on_fraction", {}) or {}).get(group.id)
                           if isinstance(entry, dict) else None,
        })

    lux_target = entry.get("lux_target") if isinstance(entry, dict) else None
    lux_margin = entry.get("lux_margin", 5) if isinstance(entry, dict) else 5
    maintenance = entry.get("maintenance_enabled") if isinstance(entry, dict) else None

    in_band = None
    if lux is not None and lux_target is not None:
        in_band = abs(lux - lux_target) <= lux_margin

    # The next boundary, so the page can count down to it.
    next_at = next_name = None
    if state.runtime.sun and state.runtime.tz:
        from datetime import datetime as _dt
        from app.scheduler import compute_day
        now = _dt.now(state.runtime.tz)
        for b in compute_day(rs.room.schedule, state.runtime.sun, now.date(),
                             state.runtime.tz):
            if b.ran and b.planned and b.planned > now:
                next_at, next_name = b.planned.isoformat(), b.name
                break

    return {
        "room_id": room_id, "name": rs.room.name,
        "section": section, "guard": rs.guard_on, "hold": rs.hold_on,
        "occupied": occupied,
        "lux": lux, "lux_sensors": sensor_n,
        "lux_target": lux_target, "lux_margin": lux_margin, "in_band": in_band,
        "maintenance_enabled": maintenance,
        "almanac_mode": (almanac or {}).get("_meta", {}).get("mode"),
        "groups": groups,
        "next_section": next_name, "next_at": next_at,
    }


# ---------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
