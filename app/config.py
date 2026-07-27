"""
Adaptive Light - configuration.

Loads, validates and defaults the config document, then exposes it as
typed objects. The JSON file is the single source of truth: the UI
writes it, the runtime reads it, the generator derives every Home
Assistant artefact from it.

Validation happens in two passes, because they catch different things:

  * JSON Schema checks shape - types, patterns, required keys, ranges.

  * Cross-reference checks catch what a schema cannot express: a scene
    referring to a group the room does not have, a duplicate room id, a
    section missing from a schedule. These are collected and reported
    together rather than one at a time, so a user fixing a config in the
    UI sees every problem at once.

Secrets never live in the file. The access token is read from the
environment and redacted on save.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")

# Sections whose lux readings sit at the sensor's noise floor, where
# maintenance cannot work. Previously inert by arithmetic accident;
# now explicit.
NO_MAINTENANCE_BY_DEFAULT = ("night", "sleep")

DEFAULT_SCHEDULE: dict[str, Any] = {
    "collision_policy": "collapse",
    "min_section_minutes": 30,
    "sections": [
        {"id": "sunrise", "name": "Sunrise", "priority": 100,
         "trigger": {"type": "earliest", "of": [
             {"type": "sun", "event": "sunrise", "offset_minutes": 0},
             {"type": "clock", "time": "05:30"}]}},
        {"id": "day", "name": "Day", "priority": 50,
         "trigger": {"type": "sun", "event": "sunrise", "offset_minutes": 180}},
        {"id": "afternoon", "name": "Afternoon", "priority": 50,
         "trigger": {"type": "sun", "event": "sunset", "offset_minutes": -180}},
        {"id": "sunset", "name": "Sunset", "priority": 50,
         "trigger": {"type": "sun", "event": "sunset", "offset_minutes": 0}},
        {"id": "night", "name": "Night", "priority": 100,
         "trigger": {"type": "clock", "time": "20:30"}},
        {"id": "sleep", "name": "Sleep", "priority": 100,
         "trigger": {"type": "clock", "time": "22:00"}},
    ],
}


class ConfigError(Exception):
    """Carries every problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))

    def __str__(self) -> str:
        return "\n".join(f"  - {p}" for p in self.problems)


# ---------------------------------------------------------------------
# Typed view
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Group:
    id: str
    name: str
    entity_id: str


@dataclass(frozen=True)
class SceneConfig:
    lux_target_seed: float | None
    lux_margin: float
    max_step_pct: float
    maintenance_enabled: bool
    transition_seconds: int
    group_modes: dict[str, str]      # group id -> 'auto' | 'off'


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    enabled: bool
    lux_sensors: list[str]
    presence_sensors: list[str]
    groups: list[Group]
    schedule: dict[str, Any]         # resolved: override if set, else global
    scenes: dict[str, SceneConfig]

    @property
    def group_ids(self) -> list[str]:
        return [g.id for g in self.groups]

    @property
    def entity_ids(self) -> list[str]:
        """Every entity this room depends on, for the health check."""
        return [g.entity_id for g in self.groups] + \
               list(self.lux_sensors) + list(self.presence_sensors)

    @property
    def always_occupied(self) -> bool:
        """No presence sensor means the room is never gated on occupancy,
        rather than never eligible."""
        return not self.presence_sensors

    def scene_config_for_analyser(self) -> dict[str, dict]:
        """Shape the analyser expects."""
        return {
            section: {
                "lux_margin": sc.lux_margin,
                "max_step_pct": sc.max_step_pct,
                "maintenance_enabled": sc.maintenance_enabled,
                "groups": {gid: {"mode": mode}
                           for gid, mode in sc.group_modes.items()},
            }
            for section, sc in self.scenes.items()
        }


@dataclass(frozen=True)
class HomeAssistant:
    base_url: str
    token: str
    verify_ssl: bool
    reconnect_backoff: tuple[int, ...]


@dataclass(frozen=True)
class SystemConfig:
    heartbeat_interval_minutes: int
    reactive_window_seconds: int
    reactive_min_delta: int
    reactive_suspends_maintenance: bool
    data_dir: str
    csv_enabled: bool
    event_log_retention_days: int


@dataclass(frozen=True)
class Config:
    version: int
    homeassistant: HomeAssistant
    system: SystemConfig
    schedule: dict[str, Any]
    learning: dict[str, Any]
    rooms: list[Room]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def room(self, room_id: str) -> Room | None:
        return next((r for r in self.rooms if r.id == room_id), None)

    @property
    def active_rooms(self) -> list[Room]:
        return [r for r in self.rooms if r.enabled]


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------

def _cross_check(doc: dict) -> list[str]:
    problems: list[str] = []
    seen_rooms: set[str] = set()

    for index, room in enumerate(doc.get("rooms", [])):
        where = f"room[{index}]" + (f" '{room['id']}'" if "id" in room else "")
        rid = room.get("id")

        if rid in seen_rooms:
            problems.append(f"{where}: duplicate room id")
        seen_rooms.add(rid)

        group_ids = [g["id"] for g in room.get("groups", [])]
        if len(group_ids) != len(set(group_ids)):
            problems.append(f"{where}: duplicate group ids")

        entities = [g["entity_id"] for g in room.get("groups", [])]
        if len(entities) != len(set(entities)):
            problems.append(f"{where}: the same entity_id is used by two groups")

        scenes = room.get("scenes", {})
        for section in SECTIONS:
            if section not in scenes:
                problems.append(f"{where}: missing scene '{section}'")
                continue
            configured = set(scenes[section].get("groups", {}))
            missing = set(group_ids) - configured
            extra = configured - set(group_ids)
            if missing:
                problems.append(
                    f"{where} scene '{section}': no mode set for "
                    f"{', '.join(sorted(missing))}")
            if extra:
                problems.append(
                    f"{where} scene '{section}': refers to unknown group(s) "
                    f"{', '.join(sorted(extra))}")

        schedule = room.get("schedule_override") or doc.get("schedule", {})
        ids = [s["id"] for s in schedule.get("sections", [])]
        if sorted(ids) != sorted(SECTIONS):
            problems.append(
                f"{where}: schedule must define all six sections, found "
                f"{', '.join(ids) or 'none'}")

    return problems


def _apply_defaults(doc: dict) -> dict:
    """JSON Schema documents defaults but does not apply them."""
    doc.setdefault("version", 1)
    doc.setdefault("schedule", json.loads(json.dumps(DEFAULT_SCHEDULE)))
    doc.setdefault("rooms", [])

    ha = doc.setdefault("homeassistant", {})
    ha.setdefault("verify_ssl", True)
    ha.setdefault("reconnect_backoff_seconds", [1, 2, 5, 10, 30, 60])

    sysc = doc.setdefault("system", {})
    sysc.setdefault("heartbeat_interval_minutes", 10)
    sysc.setdefault("reactive_window_seconds", 120)
    sysc.setdefault("reactive_min_delta", 5)
    sysc.setdefault("reactive_suspends_maintenance", True)
    sysc.setdefault("data_dir", "/data")
    sysc.setdefault("csv_enabled", True)
    sysc.setdefault("event_log_retention_days", 90)

    for schedule in [doc["schedule"]] + [
        r["schedule_override"] for r in doc["rooms"] if r.get("schedule_override")
    ]:
        schedule.setdefault("collision_policy", "collapse")
        schedule.setdefault("min_section_minutes", 30)
        for section in schedule.get("sections", []):
            if "priority" not in section:
                # A fixed clock time states something about the user's
                # routine that holds whatever the sun is doing; a
                # sun-relative time encodes an assumption a collision has
                # already falsified. A composite is as firm as its
                # firmest component.
                section["priority"] = _trigger_priority(section["trigger"])

    for room in doc["rooms"]:
        room.setdefault("enabled", True)
        room.setdefault("presence_sensors", [])
        room.setdefault("schedule_override", None)
        for section, scene in room.get("scenes", {}).items():
            scene.setdefault("lux_target_seed", None)
            scene.setdefault("lux_margin", 5)
            scene.setdefault("max_step_pct", 0.04)
            scene.setdefault("transition_seconds", 120)
            scene.setdefault("maintenance_enabled",
                             section not in NO_MAINTENANCE_BY_DEFAULT)
            for mode in scene.get("groups", {}).values():
                mode.setdefault("mode", "auto")

    return doc


def _trigger_priority(trigger: dict) -> int:
    if trigger["type"] in ("earliest", "latest"):
        return max(_trigger_priority(sub) for sub in trigger["of"])
    return 100 if trigger["type"] == "clock" else 50


def _build(doc: dict) -> Config:
    rooms: list[Room] = []
    for room in doc["rooms"]:
        schedule = room.get("schedule_override") or doc["schedule"]
        rooms.append(Room(
            id=room["id"],
            name=room["name"],
            enabled=room.get("enabled", True),
            lux_sensors=list(room["lux_sensors"]),
            presence_sensors=list(room.get("presence_sensors", [])),
            groups=[Group(g["id"], g["name"], g["entity_id"])
                    for g in room["groups"]],
            schedule=schedule,
            scenes={
                section: SceneConfig(
                    lux_target_seed=scene.get("lux_target_seed"),
                    lux_margin=scene.get("lux_margin", 5),
                    max_step_pct=scene.get("max_step_pct", 0.04),
                    maintenance_enabled=scene.get(
                        "maintenance_enabled",
                        section not in NO_MAINTENANCE_BY_DEFAULT),
                    transition_seconds=scene.get("transition_seconds", 120),
                    group_modes={gid: g.get("mode", "auto")
                                 for gid, g in scene.get("groups", {}).items()},
                )
                for section, scene in room["scenes"].items()
            },
        ))

    ha = doc["homeassistant"]
    sysc = doc["system"]
    return Config(
        version=doc["version"],
        homeassistant=HomeAssistant(
            base_url=os.environ.get("AL_HA_URL") or ha.get("base_url", ""),
            # Never read from the file if the environment supplies it:
            # compose passes the token, the file should not hold it.
            token=os.environ.get("AL_HA_TOKEN") or ha.get("access_token", ""),
            verify_ssl=_env_bool("AL_HA_VERIFY_SSL", ha.get("verify_ssl", True)),
            reconnect_backoff=tuple(ha.get("reconnect_backoff_seconds",
                                           [1, 2, 5, 10, 30, 60])),
        ),
        system=SystemConfig(
            heartbeat_interval_minutes=sysc["heartbeat_interval_minutes"],
            reactive_window_seconds=sysc["reactive_window_seconds"],
            reactive_min_delta=sysc["reactive_min_delta"],
            reactive_suspends_maintenance=sysc["reactive_suspends_maintenance"],
            data_dir=os.environ.get("AL_DATA_DIR") or sysc["data_dir"],
            csv_enabled=sysc["csv_enabled"],
            event_log_retention_days=sysc["event_log_retention_days"],
        ),
        schedule=doc["schedule"],
        learning=doc.get("learning", {}),
        rooms=rooms,
        raw=doc,
    )


def _env_bool(name: str, fallback: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return fallback
    return value.lower() not in ("false", "0", "no")


def load(path: str | Path, schema_path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError([f"no config at {path}"])
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError([f"{path} is not valid JSON: {exc}"]) from exc
    return loads(doc, schema_path)


def loads(doc: dict, schema_path: str | Path) -> Config:
    doc = _apply_defaults(json.loads(json.dumps(doc)))
    schema = json.loads(Path(schema_path).read_text())

    problems = [
        f"{'/'.join(str(p) for p in e.absolute_path) or 'root'}: {e.message}"
        for e in sorted(
            jsonschema.Draft202012Validator(schema).iter_errors(doc),
            key=lambda e: list(e.absolute_path),
        )
    ]
    problems += _cross_check(doc)
    if problems:
        raise ConfigError(problems)
    return _build(doc)


def save(config_doc: dict, path: str | Path) -> None:
    """Write the config, with the token stripped."""
    doc = json.loads(json.dumps(config_doc))
    doc.get("homeassistant", {}).pop("access_token", None)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)          # atomic: a crash mid-write cannot truncate


def blank() -> dict:
    """A fresh install: connection details only, no rooms yet."""
    return {
        "version": 1,
        "homeassistant": {"base_url": "", "verify_ssl": True},
        "system": {},
        "schedule": json.loads(json.dumps(DEFAULT_SCHEDULE)),
        "rooms": [],
    }
