"""
Adaptive Light - connection doctor.

Points at a real Home Assistant and reports whether everything the
runtime depends on actually works there. Run it before building on any
of it.

    export AL_HA_URL=http://192.168.1.251:8123
    export AL_HA_TOKEN=...
    PYTHONPATH=. python tools/doctor.py

It answers the two open questions in docs/ROADMAP.md:

  * whether a template variable assigned from state_attr() survives as a
    dict, or is stringified - if stringified, the generated maintenance
    automation fails silently and never nudges;

  * whether helpers can be created over the WebSocket API, which decides
    whether deployment is one click or a copy-paste step.

Everything it creates, it removes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ha import HAAuthError, HARest, HAWebSocket  # noqa: E402

PROBE_SENSOR = "sensor.al_doctor_probe"
PROBE_HELPER = "al_doctor_probe"

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "
results: list[tuple[str, str, str]] = []


def record(status: str, check: str, detail: str = "") -> None:
    results.append((status, check, detail))
    print(f"[{status}] {check}" + (f"\n         {detail}" if detail else ""))


async def main() -> int:
    url = os.environ.get("AL_HA_URL")
    token = os.environ.get("AL_HA_TOKEN")
    verify = os.environ.get("AL_HA_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

    if not url or not token:
        print("Set AL_HA_URL and AL_HA_TOKEN first.")
        return 2

    print(f"\nAdaptive Light doctor -> {url}\n" + "=" * 62)
    rest = HARest(url, token, verify_ssl=verify)
    ws = HAWebSocket(url, token, verify_ssl=verify)
    ws_task: asyncio.Task | None = None

    try:
        # -- REST ------------------------------------------------------
        try:
            await rest.ping()
            record(OK, "REST API reachable and token accepted")
        except HAAuthError:
            record(FAIL, "Token rejected", "Create a new long-lived token in HA.")
            return 1
        except Exception as exc:
            record(FAIL, "REST API unreachable", f"{type(exc).__name__}: {exc}")
            return 1

        cfg = await rest.config()
        record(OK, f"Home Assistant {cfg.get('version')}",
               f"timezone {cfg.get('time_zone')}, "
               f"lat {cfg.get('latitude')}, lon {cfg.get('longitude')}, "
               f"elevation {cfg.get('elevation')}m")

        if cfg.get("latitude") in (None, 0) and cfg.get("longitude") in (None, 0):
            record(WARN, "No coordinates set in Home Assistant",
                   "Sun-relative sections cannot be computed. Set them in "
                   "Settings -> System -> General.")

        # -- sun.sun ---------------------------------------------------
        sun = await rest.state("sun.sun")
        if sun is None:
            record(FAIL, "sun.sun not found",
                   "The sun integration is required for sun-relative sections.")
        else:
            a = sun.get("attributes", {})
            record(OK, "sun.sun present",
                   f"next rising {a.get('next_rising')}, "
                   f"next setting {a.get('next_setting')}")

        # -- WebSocket -------------------------------------------------
        ws_task = asyncio.create_task(ws.run())
        try:
            await asyncio.wait_for(ws._connected.wait(), timeout=15)
            record(OK, f"WebSocket authenticated (HA {ws.ha_version})")
        except asyncio.TimeoutError:
            record(FAIL, "WebSocket did not authenticate within 15s")
            return 1

        # -- template native types (ROADMAP open item) -----------------
        await rest.set_state(
            PROBE_SENSOR, "2026-07-27",
            {"day": {"lux_target": 42.5, "tv_light": 128,
                     "maintenance_enabled": True}},
        )
        await asyncio.sleep(0.4)

        direct = (await rest.render_template(
            "{%% set alm = state_attr('%s','day') %%}"
            "{{ alm is mapping }}|{{ alm.get('lux_target') }}" % PROBE_SENSOR
        )).strip()

        roundtrip = (await rest.render_template(
            "{%% set raw = '{{ state_attr(\"%s\",\"day\") }}' %%}{{ raw }}" % PROBE_SENSOR
        )).strip()

        if direct.startswith("True|42.5"):
            record(OK, "state_attr() returns a mapping inside a template",
                   f"rendered: {direct}")
        else:
            record(FAIL, "state_attr() did not return a usable mapping",
                   f"rendered: {direct!r} - the generated maintenance "
                   "automation would fail silently. Report this.")

        record(OK if "lux_target" in roundtrip else WARN,
               "Template variable round-trip",
               f"rendered: {roundtrip[:90]}")

        # -- helper creation over WebSocket (ROADMAP open item) ---------
        helper_ok = False
        try:
            created = await ws.create_helper(
                "input_boolean",
                {"name": "AL Doctor Probe", "icon": "mdi:test-tube"},
            )
            helper_ok = True
            record(OK, "Helpers can be created over the WebSocket API",
                   "Deployment can be fully automatic - no copy-paste step.")
            hid = (created or {}).get("id")
            if hid:
                await ws.delete_helper("input_boolean", hid)
                record(OK, "Probe helper removed")
        except Exception as exc:
            record(WARN, "Helper creation over WebSocket unavailable",
                   f"{type(exc).__name__}: {exc}\n         "
                   "Falls back to generated YAML plus a paste step.")

        # -- existing entities that would collide ----------------------
        states = await rest.states()
        legacy = sorted(
            s["entity_id"] for s in states
            if s["entity_id"].startswith((
                "input_boolean.scene_", "input_boolean.adaptive_light",
                "timer.adaptive_light", "input_number.reactive_",
                "input_text.reactive_", "sensor.adaptive_light_almanac",
            ))
        )
        if legacy:
            record(WARN, f"{len(legacy)} prototype entities still present",
                   "Delete these before deploying, or HA will create "
                   "_2-suffixed duplicates:\n         " + "\n         ".join(legacy))
        else:
            record(OK, "No prototype entities to clean up")

        lights = sum(1 for s in states if s["entity_id"].startswith("light."))
        lux = sum(1 for s in states
                  if s.get("attributes", {}).get("device_class") == "illuminance")
        record(OK, "Entity inventory",
               f"{lights} lights, {lux} illuminance sensors, {len(states)} total")

    finally:
        try:
            await rest.delete_state(PROBE_SENSOR)
        except Exception:
            pass
        if ws_task:
            await ws.stop()
            ws_task.cancel()
        await rest.close()

    print("=" * 62)
    fails = sum(1 for s, _, _ in results if s == FAIL)
    warns = sum(1 for s, _, _ in results if s == WARN)
    print(f"{len(results)} checks, {fails} failed, {warns} warnings "
          f"({datetime.now():%H:%M:%S})\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
