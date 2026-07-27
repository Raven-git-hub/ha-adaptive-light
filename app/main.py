"""Adaptive Light - application entry point.

Skeleton only: wires up the FastAPI app and the health endpoint so the
compose stack can be brought up and verified before the scheduler,
observer and analyser are attached.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse


@dataclass
class Status:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ha_connected: bool = False
    ha_last_seen: datetime | None = None
    last_heartbeat: datetime | None = None
    last_analysis: datetime | None = None
    rooms: dict[str, dict] = field(default_factory=dict)


status = Status()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup. Phase 5b attaches: HA websocket client, scheduler with
    # catch-up, heartbeat observer, reactive detector, nightly analysis,
    # almanac push.
    os.makedirs(os.environ.get("AL_DATA_DIR", "/data"), exist_ok=True)
    yield
    # Shutdown. The websocket must be closed deliberately rather than
    # dropped, so Home Assistant does not hold a dead subscription.


app = FastAPI(title="Adaptive Light", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> JSONResponse:
    # Unhealthy when disconnected from HA: a process that is alive but
    # cannot see Home Assistant is exactly the silent failure this is
    # meant to catch.
    ok = status.ha_connected
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "ha_connected": status.ha_connected,
            "ha_last_seen": status.ha_last_seen.isoformat() if status.ha_last_seen else None,
            "uptime_seconds": int((datetime.now(timezone.utc) - status.started_at).total_seconds()),
        },
    )


@app.get("/api/status")
def api_status() -> dict:
    """Backs the UI status strip: connection, last heartbeat, last
    analysis, and the current section per room."""
    return {
        "ha_connected": status.ha_connected,
        "last_heartbeat": status.last_heartbeat.isoformat() if status.last_heartbeat else None,
        "last_analysis": status.last_analysis.isoformat() if status.last_analysis else None,
        "rooms": status.rooms,
    }
