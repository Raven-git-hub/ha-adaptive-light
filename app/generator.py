"""
Adaptive Light - Home Assistant artefact generator.

Turns a validated config document into the complete set of helpers and
automations that Home Assistant needs. Output is deterministic: the same
config always produces byte-identical files, so regeneration is a safe,
repeatable overwrite rather than a merge.

Deliberately NOT built with a template engine. The output contains Jinja
that Home Assistant must receive verbatim; rendering Jinja to produce
Jinja invites the outer pass to consume the inner one. Instead the
artefacts are assembled as Python data structures and serialised by
PyYAML, so HA templates are inert strings throughout.

Two rules the generated automations must obey:

  * Scene automations carry `conditions: []`, always. They are fired by
    the container through automation.trigger, which defaults to
    skip_condition: true - any condition placed there would be silently
    ignored, which is worse than not having one.

  * Nothing is hardcoded from the almanac. Forced-off groups are not
    baked in as light.turn_off; the automation reads the value at
    runtime and branches. Flipping a group to 'off' in the UI therefore
    needs only an almanac republish, never a regeneration.
"""

from __future__ import annotations

import io
from typing import Any

import yaml

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")
GUARD_WATCHDOG_MINUTES = 10
STALE_SCENE_HOURS = 10  # longest legitimate gap is sleep -> sunrise (~7.5h)


# ---------------------------------------------------------------------
# YAML serialisation
# ---------------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    """Block scalars for multi-line strings; no anchors or aliases."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _str_representer(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_representer)


def dump(obj: Any) -> str:
    buf = io.StringIO()
    yaml.dump(obj, buf, Dumper=_Dumper, sort_keys=False,
              default_flow_style=False, allow_unicode=True, width=100)
    return buf.getvalue()


# ---------------------------------------------------------------------
# Naming - single source of truth for every generated entity id
# ---------------------------------------------------------------------

def guard_id(room: str) -> str:      return f"input_boolean.al_active_{room}"
def hold_id(room: str) -> str:       return f"input_boolean.al_hold_{room}"
def scene_select_id(room: str) -> str: return f"input_select.al_scene_{room}"
def almanac_id(room: str) -> str:    return f"sensor.al_almanac_{room}"

def scene_automation_id(room: str, section: str) -> str:
    return f"al_scene_{room}_{section}"

def maintenance_automation_id(room: str) -> str:
    return f"al_maintenance_{room}"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def build_helpers(config: dict) -> dict[str, str]:
    booleans: dict[str, Any] = {}
    selects: dict[str, Any] = {}

    for room in config["rooms"]:
        if not room.get("enabled", True):
            continue
        rid, name = room["id"], room["name"]
        sections = _sections_for(config, room)

        booleans[f"al_active_{rid}"] = {
            "name": f"AL Active - {name}",
            "icon": "mdi:lightbulb-auto",
        }
        booleans[f"al_hold_{rid}"] = {
            "name": f"AL Maintenance Hold - {name}",
            "icon": "mdi:hand-back-right",
        }
        selects[f"al_scene_{rid}"] = {
            "name": f"AL Scene - {name}",
            "icon": "mdi:theme-light-dark",
            "options": [s["name"] for s in sections],
        }

    return {
        "input_boolean/adaptive_light.yaml": dump(booleans),
        "input_select/adaptive_light.yaml": dump(selects),
    }


def _sections_for(config: dict, room: dict) -> list[dict]:
    profiles = {p["id"]: p for p in config.get("schedule_profiles", [])}
    profile = profiles.get(room.get("schedule_profile", "default")) \
              or profiles.get("default")
    by_id = {s["id"]: s for s in profile["sections"]}
    return [by_id[s] for s in SECTIONS]


# ---------------------------------------------------------------------
# Scene automations
# ---------------------------------------------------------------------

def _section_name_map(sections: list[dict]) -> dict[str, str]:
    """Display name -> canonical id, so the input_select can show the
    user's own naming while templates stay on stable identifiers."""
    return {s["name"]: s["id"] for s in sections}


def build_scene_automation(config: dict, room: dict, section: dict) -> dict:
    rid, sid = room["id"], section["id"]
    scene_cfg = room["scenes"][sid]
    transition = scene_cfg.get("transition_seconds", 120)
    alm = almanac_id(rid)

    actions: list[dict] = [
        {"action": "input_boolean.turn_on",
         "target": {"entity_id": guard_id(rid)}},
        # A crossover always clears any maintenance hold: the user's
        # intervention stands for the section, and the section is over.
        {"action": "input_boolean.turn_off",
         "target": {"entity_id": hold_id(rid)}},
        {"action": "input_select.select_option",
         "target": {"entity_id": scene_select_id(rid)},
         "data": {"option": section["name"]}},
        {"variables": {"alm": "{{ state_attr('%s', '%s') }}" % (alm, sid)}},
    ]

    for group in room["groups"]:
        gid, entity = group["id"], group["entity_id"]
        mode_g = scene_cfg.get("groups", {}).get(gid, {}).get("mode", "auto")

        if mode_g == "off":
            # An explicit override, not a learned fact. Baked in directly
            # so it holds from the very first deploy - it must not wait
            # on an almanac existing, or on the analyser having run.
            # Mirrors what the analyser later publishes (0), so once an
            # almanac exists the two agree; this just does not depend on
            # one being there yet.
            actions.append({
                "alias": f"{group['name']} (off)",
                "action": "light.turn_off",
                "target": {"entity_id": entity},
                "data": {"transition": transition},
            })
            continue

        has_value = (
            "{{ alm is not none and alm.get('%s') is not none }}" % gid
        )
        is_off = "{{ alm['%s'] | int(-1) == 0 }}" % gid

        actions.append({
            "alias": f"{group['name']}",
            "if": [{"condition": "template", "value_template": has_value}],
            "then": [{
                "if": [{"condition": "template", "value_template": is_off}],
                "then": [{
                    "action": "light.turn_off",
                    "target": {"entity_id": entity},
                    "data": {"transition": transition},
                }],
                "else": [{
                    "action": "light.turn_on",
                    "target": {"entity_id": entity},
                    "data": {
                        "transition": transition,
                        "brightness": "{{ alm['%s'] | int }}" % gid,
                    },
                }],
            }],
            # No else: a null brightness means the almanac has nothing to
            # say about this group yet, so the light is left exactly as
            # it is. The learner never decides a light should be off.
        })

    actions += [
        {"delay": {"seconds": transition + 30}},
        {"action": "input_boolean.turn_off",
         "target": {"entity_id": guard_id(rid)}},
    ]

    return {
        "id": scene_automation_id(rid, sid),
        "alias": f"AL SET SCENE - {room['name']} - {section['name']}",
        "description": (
            "Generated by Adaptive Light. Fired by the container via "
            "automation.trigger; do not add triggers or conditions."
        ),
        "mode": "single",
        "max_exceeded": "silent",
        "triggers": [],
        "conditions": [],
        "actions": actions,
    }


# ---------------------------------------------------------------------
# Lux maintenance
# ---------------------------------------------------------------------

def build_maintenance_automation(config: dict, room: dict) -> dict:
    rid = room["id"]
    alm = almanac_id(rid)
    interval = config.get("system", {}).get("heartbeat_interval_minutes", 10)
    sections = _sections_for(config, room)
    name_map = _section_name_map(sections)

    brightness_vars = {}
    for group in room["groups"]:
        brightness_vars[f"b_{group['id']}"] = (
            "{%% if is_state('%s','on') %%}"
            "{{ state_attr('%s','brightness') | int(0) }}"
            "{%% else %%}0{%% endif %%}" % (group["entity_id"], group["entity_id"])
        )

    variables = {
        "section_map": name_map,
        "section": (
            "{{ section_map.get(states('%s'), 'none') }}" % scene_select_id(rid)
        ),
        "alm": "{{ state_attr('%s', section) if section != 'none' else none }}" % alm,
        "enabled": "{{ alm is not none and alm.get('maintenance_enabled', true) }}",
        "lux_target": "{{ alm.get('lux_target') if alm is not none else none }}",
        "lux_margin": "{{ alm.get('lux_margin', 5) | float(5) if alm is not none else 5 }}",
        "max_step_pct": (
            "{{ alm.get('max_step_pct', 0.04) | float(0.04) if alm is not none else 0.04 }}"
        ),
        "current_lux": _lux_expression(room),
        "nudge": (
            "{% if not enabled or lux_target is none %}none"
            "{% elif current_lux < (lux_target | float) - lux_margin %}up"
            "{% elif current_lux > (lux_target | float) + lux_margin %}down"
            "{% else %}none{% endif %}"
        ),
        **brightness_vars,
    }

    actions: list[dict] = [
        {"variables": variables},
        {"condition": "template", "value_template": "{{ nudge != 'none' }}"},
        {"action": "input_boolean.turn_on", "target": {"entity_id": guard_id(rid)}},
    ]

    for group in room["groups"]:
        gid, entity = group["id"], group["entity_id"]
        step = (
            "{%% set step = [[(b_%s * max_step_pct) | int, 1] | max, 10] | min %%}"
            "{%% if nudge == 'up' %%}{{ [b_%s + step, 255] | min }}"
            "{%% else %%}{{ [b_%s - step, 1] | max }}{%% endif %%}"
            % (gid, gid, gid)
        )
        actions.append({
            "alias": group["name"],
            # Maintenance only touches lights that are already on, and
            # floors at 1 rather than 0: it can dim a light but only a
            # user can turn one off.
            "if": [{"condition": "template",
                    "value_template": "{{ b_%s > 0 }}" % gid}],
            "then": [{
                "action": "light.turn_on",
                "target": {"entity_id": entity},
                "data": {"brightness": step, "transition": 30},
            }],
        })

    actions += [
        {"delay": {"seconds": 35}},
        {"action": "input_boolean.turn_off", "target": {"entity_id": guard_id(rid)}},
    ]

    return {
        "id": maintenance_automation_id(rid),
        "alias": f"AL Lux Maintenance - {room['name']}",
        "description": (
            "Generated by Adaptive Light. Runs on HA's own clock so light "
            "correction continues even if the container is down."
        ),
        "mode": "single",
        "max_exceeded": "silent",
        "triggers": [{"trigger": "time_pattern", "minutes": f"/{interval}"}],
        "conditions": [
            # Guard: an AL automation is mid-change, ignore this tick.
            {"condition": "state", "entity_id": guard_id(rid), "state": "off"},
            # Hold: the user intervened this section, stay out of the way.
            {"condition": "state", "entity_id": hold_id(rid), "state": "off"},
        ],
        "actions": actions,
    }


def _lux_expression(room: dict) -> str:
    """Mean of the room's illuminance sensors, ignoring any that are
    unavailable rather than counting them as zero."""
    sensors = room["lux_sensors"]
    if len(sensors) == 1:
        return "{{ states('%s') | float(0) }}" % sensors[0]
    listed = ", ".join("'%s'" % s for s in sensors)
    return (
        "{%% set vals = [%s] | map('states') | reject('in', "
        "['unknown','unavailable','none']) | map('float') | list %%}"
        "{{ (vals | sum / vals | count) if vals | count > 0 else 0 }}" % listed
    )


# ---------------------------------------------------------------------
# Watchdogs
# ---------------------------------------------------------------------

def build_watchdogs(config: dict) -> list[dict]:
    out: list[dict] = []
    for room in config["rooms"]:
        if not room.get("enabled", True):
            continue
        rid, name = room["id"], room["name"]

        # The guard is short-lived by design. If it is still on ten
        # minutes later, something died mid-run - and a stuck guard
        # silently disables both maintenance and observation.
        out.append({
            "id": f"al_watchdog_guard_{rid}",
            "alias": f"AL Watchdog - Guard Stuck - {name}",
            "description": "Generated by Adaptive Light.",
            "mode": "single",
            "triggers": [{
                "trigger": "state",
                "entity_id": guard_id(rid),
                "to": "on",
                "for": {"minutes": GUARD_WATCHDOG_MINUTES},
            }],
            "conditions": [],
            "actions": [
                {"action": "input_boolean.turn_off",
                 "target": {"entity_id": guard_id(rid)}},
                {"action": "persistent_notification.create",
                 "data": {
                     "title": "Adaptive Light",
                     "message": (
                         f"Guard for {name} was stuck on and has been released. "
                         "A scene or maintenance run probably failed part-way."
                     ),
                 }},
            ],
        })

        # No watchdog on the hold boolean: it is meant to stay on for the
        # rest of a section, and the crossover clears it.
        out.append({
            "id": f"al_watchdog_stale_{rid}",
            "alias": f"AL Watchdog - Scene Stale - {name}",
            "description": "Generated by Adaptive Light.",
            "mode": "single",
            "triggers": [{
                "trigger": "state",
                "entity_id": scene_select_id(rid),
                "for": {"hours": STALE_SCENE_HOURS},
            }],
            "conditions": [],
            "actions": [{
                "action": "persistent_notification.create",
                "data": {
                    "title": "Adaptive Light",
                    "message": (
                        f"{name} has not changed section for "
                        f"{STALE_SCENE_HOURS} hours. The container may be down."
                    ),
                },
            }],
        })
    return out


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def generate(config: dict) -> dict[str, str]:
    files = build_helpers(config)

    for room in config["rooms"]:
        if not room.get("enabled", True):
            continue
        rid = room["id"]
        scenes = [
            build_scene_automation(config, room, section)
            for section in _sections_for(config, room)
        ]
        files[f"automations_adaptive_light/{rid}_scenes.yaml"] = dump(scenes)
        files[f"automations_adaptive_light/{rid}_maintenance.yaml"] = dump(
            [build_maintenance_automation(config, room)]
        )

    files["automations_adaptive_light/watchdogs.yaml"] = dump(build_watchdogs(config))
    return files
