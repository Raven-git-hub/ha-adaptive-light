"""
Network-free test for maintenance-nudge observation (runtime.py).

There is no pytest suite yet, so this doubles as a directly runnable
script: `python tests/test_maintenance_nudge.py` from the repo root.

Runtime.__init__ does no IO (the HA clients are created lazily), so we
construct a real Runtime around a fake Store and a minimal single-room
Config, monkeypatch the two async sensor reads, and drive
`_on_state_changed` with synthetic `state_changed` events.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (Config, Group, HomeAssistant, Room,  # noqa: E402
                        SystemConfig)
from app.generator import guard_id  # noqa: E402
from app.runtime import Runtime  # noqa: E402


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------

class FakeStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_event(self, severity, category, message, room_id=None,
                  detail=None, ts=None) -> None:
        self.events.append({"severity": severity, "category": category,
                            "message": message, "room_id": room_id,
                            "detail": detail})


def _make_runtime() -> tuple[Runtime, FakeStore]:
    groups = [Group(id="tv_light", name="TV", entity_id="light.tv"),
              Group(id="dining_area", name="Dining", entity_id="light.dining")]
    room = Room(id="lounge", name="Lounge", enabled=True,
                lux_sensors=["sensor.lux"], presence_sensors=[],
                groups=groups, schedule={"sections": []}, scenes={})
    system = SystemConfig(
        heartbeat_interval_minutes=10, reactive_window_seconds=120,
        reactive_min_delta=3, reactive_suspends_maintenance=True,
        data_dir="data", csv_enabled=False, event_log_retention_days=30,
        external_guards=())
    ha = HomeAssistant(base_url="http://ha.local", token="x",
                       verify_ssl=False, reconnect_backoff=(1,))
    config = Config(version=1, homeassistant=ha, system=system,
                    schedule_profiles={}, learning={}, rooms=[room])

    store = FakeStore()
    rt = Runtime(config, store)
    rt.tz = ZoneInfo("UTC")

    async def _fake_lux(_room):
        return (72.0, 1)

    rt._read_lux = _fake_lux                               # type: ignore[assignment]
    store.current_almanac = lambda rid: {"day": {"lux_target": 78.8}}
    return rt, store


# ---------------------------------------------------------------------
# Synthetic events
# ---------------------------------------------------------------------

def _guard(state: str) -> dict:
    return {"data": {"entity_id": guard_id("lounge"),
                     "new_state": {"state": state}}}


def _bright(entity: str, brightness: int) -> dict:
    # context.parent_id present: a maintenance run is automation-caused.
    return {"data": {"entity_id": entity, "new_state": {
        "state": "on",
        "attributes": {"brightness": brightness},
        "context": {"parent_id": "abc"}}}}


async def _drain() -> None:
    # Let the task _close_guard_window spawned run to completion.
    for _ in range(3):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------

async def test_maintenance_window_logs_one_event() -> None:
    rt, store = _make_runtime()
    rs = rt.rooms["lounge"]
    rs.fired_section = "day"
    rs.last_states = {"light.tv": (True, 72), "light.dining": (True, 50)}

    # No crossover we fired -> the guard-on edge attributes this to
    # maintenance.
    rt._on_state_changed(_guard("on"))
    assert rs.guard_reason == "maintenance"

    rt._on_state_changed(_bright("light.tv", 75))
    rt._on_state_changed(_bright("light.dining", 53))
    rt._on_state_changed(_guard("off"))
    await _drain()

    maint = [e for e in store.events if e["category"] == "maintenance"]
    assert len(maint) == 1, store.events
    ev = maint[0]
    assert ev["severity"] == "info"
    assert ev["room_id"] == "lounge"
    assert ev["detail"]["direction"] == "up"
    assert ev["detail"]["groups"] == {
        "tv_light": {"before": 72, "after": 75},
        "dining_area": {"before": 50, "after": 53}}
    assert ev["detail"]["lux"] == 72.0
    assert ev["detail"]["lux_target"] == 78.8
    assert "target 78.8 lux" in ev["message"]
    assert "up" in ev["message"]
    print("ok: maintenance window logs exactly one info event")


async def test_scene_window_logs_nothing() -> None:
    rt, store = _make_runtime()
    rs = rt.rooms["lounge"]
    rs.fired_section = "day"
    rs.last_states = {"light.tv": (True, 72), "light.dining": (True, 50)}

    # A crossover we fired just ran: the guard it raises is a scene window,
    # not a nudge, so brightness changes must not produce a maintenance event.
    rs.own_crossover_at = datetime.now(rt.tz)

    rt._on_state_changed(_guard("on"))
    assert rs.guard_reason == "scene"

    rt._on_state_changed(_bright("light.tv", 120))
    rt._on_state_changed(_bright("light.dining", 90))
    rt._on_state_changed(_guard("off"))
    await _drain()

    assert not [e for e in store.events if e["category"] == "maintenance"], \
        store.events
    print("ok: scene window logs no maintenance event")


async def test_no_deltas_logs_nothing() -> None:
    rt, store = _make_runtime()
    rs = rt.rooms["lounge"]
    rs.fired_section = "day"
    rs.last_states = {"light.tv": (False, None), "light.dining": (False, None)}

    # Guard raised by maintenance but every target light is off -> no
    # brightness events arrive -> nothing to flush.
    rt._on_state_changed(_guard("on"))
    rt._on_state_changed(_guard("off"))
    await _drain()

    assert not [e for e in store.events if e["category"] == "maintenance"], \
        store.events
    print("ok: maintenance run touching no lit groups logs nothing")


async def _main() -> None:
    await test_maintenance_window_logs_one_event()
    await test_scene_window_logs_nothing()
    await test_no_deltas_logs_nothing()
    print("\nall maintenance-nudge tests passed")


if __name__ == "__main__":
    asyncio.run(_main())
