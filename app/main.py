"""Adaptive Light - application entry point."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Config, ConfigError, blank, load, save
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
    rooms = {}
    if state.runtime:
        for room_id, rs in state.runtime.rooms.items():
            rooms[room_id] = {
                "name": rs.room.name,
                "section": rs.fired_section,
                "since": rs.fired_at.isoformat() if rs.fired_at else None,
                "guard": rs.guard_on,
                "hold": rs.hold_on,
                "groups": len(rs.room.groups),
            }
    return {
        "ha_connected": bool(state.runtime and state.runtime.ws.connected),
        "ha_version": state.runtime.ws.ha_version if state.runtime else None,
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
