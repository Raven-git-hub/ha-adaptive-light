"""
Adaptive Light - runtime.

Owns the moving parts: the Home Assistant connection, section
scheduling, heartbeat observation, reactive detection, nightly analysis
and almanac publication.

Three things are worth knowing before reading the code.

**Scheduling is level-triggered, not edge-triggered.** Rather than
setting a timer for each boundary, a slow loop asks "which section
should be active right now?" and fires a crossover whenever the answer
differs from what was last fired. The same code path therefore handles
normal operation, startup after downtime, a dropped websocket, a clock
change and a paused VM. There is no separate catch-up routine to get
wrong.

**Two booleans, two meanings.** `al_active_<room>` says an Adaptive
Light automation is changing lights right now, so ignore what you see;
it lives for seconds. `al_hold_<room>` says a human intervened, so
maintenance stands down until the next crossover; it lives for hours.
Conflating them would mean a reactive event silently stopping
observation for the rest of the section.

**Heartbeats defer, they do not skip.** Home Assistant runs maintenance
on its own ten-minute clock and the container polls on its own; the two
drift and will eventually collide. Sampling during a transition would
record a brightness that was never a settled state, so a heartbeat waits
for the guard to clear and records how long it waited.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.analyser import AnalyserFlags, LearningConfig, analyse_room
from app.config import Config, Room
from app.generator import (almanac_id, guard_id, hold_id, scene_automation_id,
                           scene_select_id)
from app.ha import HARest, HAWebSocket
from app.scheduler import (AstralSunProvider, Boundary, active_section_at,
                           compute_day)
from app.store import GroupSample, ReactiveGroupSample, Store

log = logging.getLogger("adaptive_light.runtime")

SCHEDULER_TICK_SECONDS = 20
GUARD_WAIT_TIMEOUT_SECONDS = 90
PROVISIONAL_SAMPLES = 3
PROVISIONAL_SPACING_SECONDS = 40


# ---------------------------------------------------------------------
# Per-room state
# ---------------------------------------------------------------------

@dataclass
class ReactiveWindow:
    """Collects a burst of user changes into one event.

    Someone adjusting four groups by hand produces a dozen state changes
    over several seconds; that is one intervention, not twelve.
    """
    opened_at: datetime
    deadline: datetime
    before: dict[str, tuple[bool, int | None]]
    after: dict[str, tuple[bool, int | None]] = field(default_factory=dict)
    lux_before: float | None = None


@dataclass
class RoomState:
    room: Room
    guard_on: bool = False
    hold_on: bool = False
    fired_section: str | None = None
    fired_at: datetime | None = None
    boundaries: list[Boundary] = field(default_factory=list)
    boundaries_for: date | None = None
    window: ReactiveWindow | None = None
    last_states: dict[str, tuple[bool, int | None]] = field(default_factory=dict)
    seeded_sections: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------

class Runtime:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.rest = HARest(config.homeassistant.base_url,
                           config.homeassistant.token,
                           verify_ssl=config.homeassistant.verify_ssl)
        self.ws = HAWebSocket(config.homeassistant.base_url,
                              config.homeassistant.token,
                              verify_ssl=config.homeassistant.verify_ssl,
                              backoff=config.homeassistant.reconnect_backoff)
        self.rooms: dict[str, RoomState] = {
            r.id: RoomState(room=r) for r in config.active_rooms
        }
        self.tz: ZoneInfo | None = None
        self.sun: AstralSunProvider | None = None
        self.automation_entities: dict[str, str] = {}   # automation id -> entity_id
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # -- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        await self._load_location()
        self.ws.on_event("state_changed", self._on_state_changed)
        self.ws.on_event("homeassistant_start", self._on_ha_restart)

        self._tasks.append(asyncio.create_task(self.ws.run(), name="ws"))
        await asyncio.wait_for(self.ws._connected.wait(), timeout=30)

        await self._map_automations()
        await self._prime_states()

        for name, coro in (("scheduler", self._scheduler_loop),
                           ("heartbeat", self._heartbeat_loop),
                           ("analysis", self._analysis_loop)):
            self._tasks.append(asyncio.create_task(coro(), name=name))

        self.store.log_event("info", "connection",
                             f"Runtime started against Home Assistant "
                             f"{self.ws.ha_version}")

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await self.ws.stop()
        await self.rest.close()
        self.store.log_event("info", "connection", "Runtime stopped")

    async def _load_location(self) -> None:
        cfg = await self.rest.config()
        self.tz = ZoneInfo(cfg["time_zone"])
        self.sun = AstralSunProvider(
            latitude=cfg["latitude"], longitude=cfg["longitude"],
            tzinfo=self.tz, elevation=cfg.get("elevation", 0) or 0,
        )
        await self._verify_sun_against_ha()

    async def _verify_sun_against_ha(self) -> None:
        """Local astral computation must agree with HA's sun integration.

        The scheduler cannot use sun.sun directly - it only knows the
        NEXT event, so it cannot say when today's sunrise was, which
        catch-up needs. Computing locally and checking the two agree
        gives correctness plus an alarm if they ever drift apart.
        """
        state = await self.rest.state("sun.sun")
        if not state or not self.sun:
            return
        attrs = state.get("attributes", {})
        for key, event in (("next_rising", "sunrise"), ("next_setting", "sunset")):
            raw = attrs.get(key)
            if not raw:
                continue
            expected = datetime.fromisoformat(raw).astimezone(self.tz)
            times = self.sun.times(expected.date())
            if event not in times:
                continue
            drift = abs((times[event] - expected).total_seconds())
            if drift > 60:
                msg = (f"local {event} differs from Home Assistant by "
                       f"{drift:.0f}s - check latitude, longitude and elevation")
                log.warning(msg)
                self.store.log_event("warning", "validation", msg)

    async def _map_automations(self) -> None:
        """Resolve automation ids to entity ids.

        Entity ids are derived from the alias and can be renamed; the id
        field is what the generator controls, so map through that rather
        than guessing at slugs.
        """
        self.automation_entities.clear()
        missing: list[str] = []
        for state in await self.rest.states():
            entity = state["entity_id"]
            if entity.startswith("automation."):
                aid = state.get("attributes", {}).get("id")
                if aid:
                    self.automation_entities[aid] = entity

        for state in self.rooms.values():
            for section in state.room.scenes:
                aid = scene_automation_id(state.room.id, section)
                if aid not in self.automation_entities:
                    missing.append(aid)
        if missing:
            msg = f"{len(missing)} scene automations not found in Home Assistant"
            log.warning("%s: %s", msg, ", ".join(missing[:6]))
            self.store.log_event("warning", "validation", msg,
                                 detail={"missing": missing})

    async def _prime_states(self) -> None:
        """Seed the in-memory mirror so the first reactive event has a
        'before' to compare against."""
        states = {s["entity_id"]: s for s in await self.rest.states()}
        for rs in self.rooms.values():
            for group in rs.room.groups:
                rs.last_states[group.entity_id] = _reading(states.get(group.entity_id))
            rs.guard_on = _is_on(states.get(guard_id(rs.room.id)))
            rs.hold_on = _is_on(states.get(hold_id(rs.room.id)))

            select = states.get(scene_select_id(rs.room.id))
            if select:
                name_to_id = {s["name"]: s["id"]
                              for s in rs.room.schedule["sections"]}
                rs.fired_section = name_to_id.get(select["state"])

    # -- events -------------------------------------------------------

    def _on_state_changed(self, event: dict) -> None:
        data = event.get("data", {})
        entity = data.get("entity_id", "")
        new = data.get("new_state")

        for rs in self.rooms.values():
            rid = rs.room.id
            if entity == guard_id(rid):
                rs.guard_on = _is_on(new)
                return
            if entity == hold_id(rid):
                rs.hold_on = _is_on(new)
                return

            group = next((g for g in rs.room.groups if g.entity_id == entity), None)
            if group is None:
                continue

            reading = _reading(new)
            previous = rs.last_states.get(entity, (False, None))
            rs.last_states[entity] = reading

            # The guard is what separates our own changes from a human's.
            # No user_id check: a physical dimmer press carries no user,
            # and the guard already excludes everything we caused.
            if rs.guard_on:
                return
            self._note_user_change(rs, group.id, previous, reading)
            return

    def _note_user_change(self, rs: RoomState, group_id: str,
                          before: tuple[bool, int | None],
                          after: tuple[bool, int | None]) -> None:
        now = datetime.now(self.tz)
        if rs.window is None:
            rs.window = ReactiveWindow(
                opened_at=now,
                deadline=now + timedelta(
                    seconds=self.config.system.reactive_window_seconds),
                before={group_id: before},
            )
            asyncio.create_task(self._close_window_later(rs))
        else:
            rs.window.before.setdefault(group_id, before)
        rs.window.after[group_id] = after

    def _on_ha_restart(self, event: dict) -> None:
        # States pushed over the API do not survive a restart, so every
        # almanac must be published again.
        asyncio.create_task(self._republish_all())

    # -- reactive -----------------------------------------------------

    async def _close_window_later(self, rs: RoomState) -> None:
        window = rs.window
        if window is None:
            return
        await asyncio.sleep(
            max(0.0, (window.deadline - datetime.now(self.tz)).total_seconds()))
        if rs.window is not window:
            return
        rs.window = None
        await self._commit_reactive(rs, window)

    async def _commit_reactive(self, rs: RoomState, window: ReactiveWindow) -> None:
        threshold = self.config.system.reactive_min_delta
        groups: dict[str, ReactiveGroupSample] = {}
        changed_any = False

        for group in rs.room.groups:
            on_before, b_before = window.before.get(
                group.id, rs.last_states.get(group.entity_id, (False, None)))
            on_after, b_after = window.after.get(group.id, (on_before, b_before))
            delta = abs((b_after or 0) - (b_before or 0))
            changed = on_before != on_after or delta > threshold
            changed_any |= changed
            groups[group.id] = ReactiveGroupSample(
                is_on_before=on_before, is_on_after=on_after,
                brightness_before=b_before, brightness_after=b_after,
                changed=changed)

        if not changed_any:
            return  # transition echo or rounding noise

        lux, _ = await self._read_lux(rs.room)
        occupied = await self._read_presence(rs.room)
        suspend = self.config.system.reactive_suspends_maintenance

        self.store.record_reactive(
            rs.room.id, window.opened_at, rs.fired_section or "unknown",
            self.config.system.reactive_window_seconds,
            window.lux_before, lux,
            # Recorded, never filtered on: a human at a dimmer is proof
            # of presence whatever the sensor says.
            occupied, suspend, groups, rs.room.group_ids)

        touched = [g for g, s in groups.items() if s.changed]
        self.store.log_event(
            "info", "reactive",
            f"User adjusted {', '.join(touched)} during "
            f"{rs.fired_section or 'unknown'}",
            rs.room.id, {"groups": touched, "lux_after": lux})

        if suspend and not rs.hold_on:
            await self.rest.call_service(
                "input_boolean", "turn_on",
                {"entity_id": hold_id(rs.room.id)})
            rs.hold_on = True
            self.store.log_event(
                "info", "hold",
                "Maintenance suspended until the next crossover", rs.room.id)

    # -- scheduling ---------------------------------------------------

    async def _scheduler_loop(self) -> None:
        while not self._stopping:
            try:
                await self._scheduler_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)

    async def _scheduler_tick(self) -> None:
        now = datetime.now(self.tz)
        for rs in self.rooms.values():
            self._refresh_boundaries(rs, now.date())
            section, started = active_section_at(
                rs.room.schedule, self.sun, now, self.tz)
            if section != rs.fired_section:
                await self._fire_crossover(rs, section, started, now)

    def _refresh_boundaries(self, rs: RoomState, today: date) -> None:
        if rs.boundaries_for == today:
            return
        rs.boundaries = compute_day(rs.room.schedule, self.sun, today, self.tz)
        rs.boundaries_for = today
        rs.seeded_sections.clear()

        for b in rs.boundaries:
            self.store.record_section_run(
                rs.room.id, today.isoformat(), b.section,
                b.planned, None, b.outcome, b.reason)
            if not b.ran:
                self.store.log_event(
                    "warning", "scene_collapsed",
                    f"{b.name} skipped today: {b.reason}", rs.room.id)

    async def _fire_crossover(self, rs: RoomState, section: str,
                              started: datetime, now: datetime) -> None:
        aid = scene_automation_id(rs.room.id, section)
        entity = self.automation_entities.get(aid)
        late = (now - started).total_seconds()

        if entity is None:
            self.store.log_event(
                "error", "scene_change",
                f"Cannot start {section}: automation {aid} not found",
                rs.room.id)
            rs.fired_section = section
            return

        await self.rest.trigger_automation(entity)
        rs.fired_section = section
        rs.fired_at = now

        # A crossover always releases the hold: the user's intervention
        # stood for the section, and the section is over.
        if rs.hold_on:
            await self.rest.call_service("input_boolean", "turn_off",
                                         {"entity_id": hold_id(rs.room.id)})
            rs.hold_on = False

        self.store.record_section_run(
            rs.room.id, now.date().isoformat(), section, started, now,
            "caught_up" if late > 120 else "ran",
            f"fired {late:.0f}s after boundary" if late > 120 else None)
        self.store.log_event(
            "info", "scene_change",
            f"{section} began" + (f" ({late:.0f}s late)" if late > 120 else ""),
            rs.room.id, {"planned": started.isoformat()})

        asyncio.create_task(self._seed_provisional(rs, section))

    async def _seed_provisional(self, rs: RoomState, section: str) -> None:
        """On the first crossover into a section with no learned target,
        observe the ambient level and use that.

        Averaged over several readings: one instantaneous sample is
        hostage to a passing cloud or an opened door.
        """
        if section in rs.seeded_sections:
            return
        almanac = self.store.current_almanac(rs.room.id) or {}
        entry = almanac.get(section)
        if entry and entry.get("lux_target") is not None:
            return
        rs.seeded_sections.add(section)

        readings: list[float] = []
        for _ in range(PROVISIONAL_SAMPLES):
            await asyncio.sleep(PROVISIONAL_SPACING_SECONDS)
            lux, n = await self._read_lux(rs.room)
            if lux is not None and n:
                readings.append(lux)
        if not readings:
            return

        target = round(sum(readings) / len(readings), 1)
        scene = rs.room.scenes[section]
        almanac.setdefault("_meta", {"version": 3, "mode": "provisional",
                                     "days_analysed": 0,
                                     "valid_from": date.today().isoformat()})
        almanac[section] = {
            "lux_target": target,
            "lux_margin": scene.lux_margin,
            "max_step_pct": scene.max_step_pct,
            # Provisional targets are observations, not learning.
            # Maintenance stays out until there is real data.
            "maintenance_enabled": False,
            "days_contributing": 0, "high_confidence_days": 0,
            "on_fraction": {},
            **{gid: None for gid in rs.room.group_ids},
        }
        self.store.save_almanac(rs.room.id, almanac)
        await self._publish(rs.room.id, almanac)
        self.store.log_event(
            "info", "almanac",
            f"Seeded provisional target for {section}: {target} lux",
            rs.room.id)

    # -- heartbeat ----------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        interval = self.config.system.heartbeat_interval_minutes * 60
        while not self._stopping:
            await asyncio.sleep(self._until_next_tick(interval))
            try:
                await asyncio.gather(*(self._heartbeat(rs)
                                       for rs in self.rooms.values()))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("heartbeat failed")

    def _until_next_tick(self, interval: int) -> float:
        now = datetime.now(self.tz)
        elapsed = now.minute * 60 + now.second
        return interval - (elapsed % interval)

    async def _heartbeat(self, rs: RoomState) -> None:
        waited = 0.0
        while rs.guard_on and waited < GUARD_WAIT_TIMEOUT_SECONDS:
            await asyncio.sleep(5)
            waited += 5
        if rs.guard_on:
            self.store.log_event(
                "warning", "heartbeat",
                f"Guard still on after {GUARD_WAIT_TIMEOUT_SECONDS}s; "
                "heartbeat skipped", rs.room.id)
            return

        now = datetime.now(self.tz)
        lux, sensor_count = await self._read_lux(rs.room)
        occupied = await self._read_presence(rs.room)

        groups: dict[str, GroupSample] = {}
        for group in rs.room.groups:
            state = await self.rest.state(group.entity_id)
            on, brightness = _reading(state)
            groups[group.id] = GroupSample(is_on=on, brightness=brightness)
            rs.last_states[group.entity_id] = (on, brightness)

        self.store.record_heartbeat(
            rs.room.id, now, rs.fired_section or "unknown", lux, sensor_count,
            occupied, groups, rs.room.group_ids, int(waited * 1000))

    # -- sensors ------------------------------------------------------

    async def _read_lux(self, room: Room) -> tuple[float | None, int]:
        """Mean of the room's sensors, ignoring any that are unavailable
        rather than counting them as zero."""
        values: list[float] = []
        for entity in room.lux_sensors:
            state = await self.rest.state(entity)
            if not state or state["state"] in ("unknown", "unavailable", "none"):
                continue
            try:
                values.append(float(state["state"]))
            except (TypeError, ValueError):
                continue
        if not values:
            return (None, 0)
        return (round(sum(values) / len(values), 1), len(values))

    async def _read_presence(self, room: Room) -> bool:
        # No presence sensor means permanently occupied, not permanently
        # ineligible.
        if room.always_occupied:
            return True
        for entity in room.presence_sensors:
            state = await self.rest.state(entity)
            if state and state["state"] == "on":
                return True
        return False

    # -- analysis and publication -------------------------------------

    async def _analysis_loop(self) -> None:
        while not self._stopping:
            now = datetime.now(self.tz)
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=15, second=0, microsecond=0)
            await asyncio.sleep((tomorrow - now).total_seconds())
            try:
                await self.run_analysis()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("nightly analysis failed")

    async def run_analysis(self) -> None:
        learning = self._learning_config()
        for rs in self.rooms.values():
            almanac = analyse_room(
                self.store.connection, rs.room.id, rs.room.group_ids,
                rs.room.scene_config_for_analyser(), learning,
                datetime.now(self.tz).date())
            meta = almanac.get("_meta", {})
            if not meta.get("days_analysed"):
                continue
            almanac_row = self.store.save_almanac(rs.room.id, almanac)
            await self._publish(rs.room.id, almanac, almanac_row)
            self.store.log_event(
                "info", "analysis",
                f"Almanac rebuilt from {meta['days_analysed']} day(s), "
                f"mode {meta['mode']}, valid from {meta['valid_from']}",
                rs.room.id)
        self.store.prune_events(self.config.system.event_log_retention_days)

    def _learning_config(self) -> LearningConfig:
        raw = self.config.learning or {}
        return LearningConfig(
            lookback_days=raw.get("lookback_days", 21),
            publish_delay_days=raw.get("publish_delay_days", 2),
            bootstrap_min_days=raw.get("bootstrap_min_days", 7),
            reactive_weight=raw.get("reactive_weight", 5),
            post_reactive_boost=tuple(raw.get("post_reactive_boost", (3, 2, 1.5))),
            flags=AnalyserFlags(),
        )

    async def _publish(self, room_id: str, almanac: dict,
                       almanac_row: int | None = None) -> None:
        """Push the almanac into Home Assistant's state machine.

        The state is valid_from rather than a schema version: a state
        that never changes makes a stalled analyser invisible.
        """
        meta = almanac.get("_meta", {})
        attributes = {k: v for k, v in almanac.items() if k != "_meta"}
        attributes["mode"] = meta.get("mode")
        attributes["days_analysed"] = meta.get("days_analysed")
        attributes["friendly_name"] = f"AL Almanac {room_id}"

        await self.rest.set_state(
            almanac_id(room_id), meta.get("valid_from", "unknown"), attributes)
        if almanac_row:
            self.store.mark_published(almanac_row)

    async def _republish_all(self) -> None:
        await asyncio.sleep(15)   # let HA finish starting
        for room_id in self.rooms:
            almanac = self.store.current_almanac(room_id)
            if almanac:
                await self._publish(room_id, almanac)
        self.store.log_event("info", "almanac",
                             "Almanacs republished after Home Assistant restart")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _is_on(state: dict | None) -> bool:
    return bool(state) and state.get("state") == "on"


def _reading(state: dict | None) -> tuple[bool, int | None]:
    if not state or state.get("state") != "on":
        return (False, None)
    brightness = state.get("attributes", {}).get("brightness")
    return (True, int(brightness) if brightness is not None else None)
