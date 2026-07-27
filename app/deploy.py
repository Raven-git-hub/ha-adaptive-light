"""
Adaptive Light - deployment.

Creates everything Adaptive Light needs inside Home Assistant: three
helpers per room, six scene automations per room, one maintenance
automation per room, and the watchdogs. Removes anything it previously
created that the config no longer calls for.

Two things make this trickier than it looks.

**Helpers do not get the entity id you ask for.** In YAML the dict key
is the object id, so `al_hold_main_room` is guaranteed. Over the
WebSocket there is no key: Home Assistant slugifies the friendly name.
"AL Maintenance Hold - Main Room" would become
`input_boolean.al_maintenance_hold_main_room`, while the generated
automations reference `al_hold_main_room` - every condition would then
evaluate against a non-existent entity and maintenance would silently
never run. Helpers are therefore named from the room *id*, which is
already a valid slug, and the result is verified against what Home
Assistant actually created.

**Deployment must be repeatable.** Adding a room, renaming a group or
changing a section time all re-run it. Existing helpers are reused
rather than duplicated, automations are overwritten by id, and orphans
from a deleted room are removed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Config, Room
from app.generator import (build_maintenance_automation, build_scene_automation,
                           build_watchdogs, guard_id, hold_id,
                           maintenance_automation_id, scene_automation_id,
                           scene_select_id, _sections_for)
from app.ha import HAError, HARest, HAWebSocket
from app.store import Store

log = logging.getLogger("adaptive_light.deploy")


def slugify(text: str) -> str:
    """Home Assistant's own object-id derivation, closely enough to
    predict what a created helper will be called."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


@dataclass
class DeployReport:
    helpers_created: list[str] = field(default_factory=list)
    helpers_reused: list[str] = field(default_factory=list)
    automations_written: list[str] = field(default_factory=list)
    automations_removed: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "helpers_created": self.helpers_created,
            "helpers_reused": self.helpers_reused,
            "automations_written": self.automations_written,
            "automations_removed": self.automations_removed,
            "missing_entities": self.missing_entities,
            "problems": self.problems,
        }


# ---------------------------------------------------------------------
# Helper naming
#
# The friendly name must slugify to the object id the generated
# automations reference. Derived from room.id, never room.name: a room
# called "Room 2" with id "room_two" would otherwise produce
# al_active_room_2 and quietly break every reference.
# ---------------------------------------------------------------------

def helper_specs(room: Room, sections: list[dict]) -> list[tuple[str, str, dict]]:
    """(domain, required_object_id, create payload)."""
    rid = room.id
    return [
        ("input_boolean", f"al_active_{rid}",
         {"name": f"AL Active {rid}", "icon": "mdi:lightbulb-auto"}),
        ("input_boolean", f"al_hold_{rid}",
         {"name": f"AL Hold {rid}", "icon": "mdi:hand-back-right"}),
        ("input_select", f"al_scene_{rid}",
         {"name": f"AL Scene {rid}", "icon": "mdi:theme-light-dark",
          "options": [s["name"] for s in sections]}),
    ]


async def _ensure_helper(
    ws: HAWebSocket, existing: dict[str, dict], domain: str,
    object_id: str, payload: dict, report: DeployReport,
) -> None:
    current = existing.get(object_id)

    if current is not None:
        # An input_select whose options no longer match the configured
        # section names cannot represent the active section.
        if domain == "input_select" and current.get("options") != payload["options"]:
            try:
                await ws.send({"type": "input_select/update",
                               "input_select_id": object_id, **payload})
                report.helpers_created.append(f"{domain}.{object_id} (options updated)")
            except HAError as exc:
                report.problems.append(
                    f"{domain}.{object_id}: could not update options - {exc}")
            return
        report.helpers_reused.append(f"{domain}.{object_id}")
        return

    predicted = slugify(payload["name"])
    if predicted != object_id:
        report.problems.append(
            f"{domain}.{object_id}: name {payload['name']!r} would create "
            f"{domain}.{predicted} instead - refusing to deploy a broken reference")
        return

    try:
        created = await ws.create_helper(domain, payload)
    except HAError as exc:
        report.problems.append(f"{domain}.{object_id}: creation failed - {exc}")
        return

    actual = (created or {}).get("id")
    if actual != object_id:
        # Usually a name collision: HA appends a suffix silently, and
        # every generated reference would then point at nothing.
        report.problems.append(
            f"{domain}.{object_id}: Home Assistant created "
            f"{domain}.{actual} instead - resolve the name collision")
        return

    report.helpers_created.append(f"{domain}.{object_id}")


# ---------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------

async def deploy(
    config: Config, rest: HARest, ws: HAWebSocket, store: Store | None = None,
) -> DeployReport:
    report = DeployReport()
    rooms = config.active_rooms

    if not rooms:
        report.problems.append("no enabled rooms to deploy")
        return report

    # -- entities the config depends on must actually exist ------------
    present = {s["entity_id"] for s in await rest.states()}
    for room in rooms:
        for entity in room.entity_ids:
            if entity not in present:
                report.missing_entities.append(entity)
    if report.missing_entities:
        report.problems.append(
            f"{len(report.missing_entities)} configured entities do not exist "
            "in Home Assistant")
        return report

    # -- helpers -------------------------------------------------------
    for domain in ("input_boolean", "input_select"):
        try:
            listed = await ws.list_helpers(domain)
        except HAError as exc:
            report.problems.append(f"cannot list {domain} helpers: {exc}")
            return report
        existing = {item["id"]: item for item in listed or []}

        for room in rooms:
            sections = _sections_for(config.raw, _room_doc(config, room))
            for spec_domain, object_id, payload in helper_specs(room, sections):
                if spec_domain != domain:
                    continue
                await _ensure_helper(ws, existing, domain, object_id, payload, report)

    if report.problems:
        return report

    # -- automations ---------------------------------------------------
    wanted: dict[str, dict] = {}
    for room in rooms:
        room_doc = _room_doc(config, room)
        for section in _sections_for(config.raw, room_doc):
            body = build_scene_automation(config.raw, room_doc, section)
            wanted[body["id"]] = body
        body = build_maintenance_automation(config.raw, room_doc)
        wanted[body["id"]] = body
    for body in build_watchdogs(config.raw):
        wanted[body["id"]] = body

    for automation_id, body in wanted.items():
        try:
            await rest._request(
                "POST", f"/api/config/automation/config/{automation_id}", json=body)
            report.automations_written.append(automation_id)
        except Exception as exc:
            report.problems.append(
                f"automation {automation_id}: {type(exc).__name__}: {exc}")

    # -- remove ours that the config no longer calls for ----------------
    for state in await rest.states():
        if not state["entity_id"].startswith("automation."):
            continue
        existing_id = state.get("attributes", {}).get("id", "")
        if not existing_id.startswith(("al_scene_", "al_maintenance_", "al_watchdog_")):
            continue
        if existing_id in wanted:
            continue
        try:
            await rest._request(
                "DELETE", f"/api/config/automation/config/{existing_id}")
            report.automations_removed.append(existing_id)
        except Exception as exc:
            report.problems.append(f"could not remove {existing_id}: {exc}")

    if store:
        store.log_event(
            "info" if report.ok else "error", "deploy",
            f"Deployed {len(report.automations_written)} automations, "
            f"{len(report.helpers_created)} helpers created, "
            f"{len(report.helpers_reused)} reused"
            + (f", {len(report.problems)} problems" if report.problems else ""),
            detail=report.as_dict())

    return report


def _room_doc(config: Config, room: Room) -> dict:
    """The generator works from the raw document; find this room in it."""
    for candidate in config.raw.get("rooms", []):
        if candidate["id"] == room.id:
            return candidate
    raise KeyError(room.id)


# ---------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------

async def check(config: Config, rest: HARest) -> dict[str, Any]:
    """Does Home Assistant currently hold everything the config expects?

    Read-only: used by the UI to show whether a deploy is needed.
    """
    states = {s["entity_id"]: s for s in await rest.states()}
    automation_ids = {
        s.get("attributes", {}).get("id")
        for s in states.values() if s["entity_id"].startswith("automation.")
    }

    missing_entities: list[str] = []
    missing_helpers: list[str] = []
    missing_automations: list[str] = []

    for room in config.active_rooms:
        missing_entities += [e for e in room.entity_ids if e not in states]
        for entity in (guard_id(room.id), hold_id(room.id), scene_select_id(room.id)):
            if entity not in states:
                missing_helpers.append(entity)
        for section in room.scenes:
            aid = scene_automation_id(room.id, section)
            if aid not in automation_ids:
                missing_automations.append(aid)
        if maintenance_automation_id(room.id) not in automation_ids:
            missing_automations.append(maintenance_automation_id(room.id))

    return {
        "deployed": not (missing_entities or missing_helpers or missing_automations),
        "missing_entities": missing_entities,
        "missing_helpers": missing_helpers,
        "missing_automations": missing_automations,
    }
