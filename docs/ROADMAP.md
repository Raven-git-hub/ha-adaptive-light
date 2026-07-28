# Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Stabilise the n8n system: section column, reactive motion filter, guard watchdog | superseded by the rebuild |
| 1 | Config schema — the contract everything else derives from | **done** — `schema/config.schema.json` |
| 2 | Storage: SQLite DDL, CSV layout, event log | **done** — `schema/storage.schema.sql` |
| 3 | Analyser ported from n8n with per-group eligibility | **done** — `app/analyser.py` |
| 4 | Generator for helpers and automations | **done** — `app/generator.py` |
| 5a | Scheduler: boundary computation and collapse | **done** — `app/scheduler.py` |
| 5b-i | HA client (REST + WebSocket) and connection doctor | **done** — `app/ha.py`, `tools/doctor.py` |
| 5b-ii | Storage layer: dual CSV/SQLite writes, event log, CSV re-ingest | **done** — `app/store.py` |
| 5b-iii | Config loader: schema validation, defaults, cross-reference checks | **done** — `app/config.py` |
| 5b-iv | Runtime: scheduler, observer, reactive detector, almanac push | **done** — `app/runtime.py` |
| 6a | Config API and deployment to Home Assistant | **done** — `app/deploy.py`, endpoints in `app/main.py` |
| 6b | UI shell, status strip, Config and Log | **done** — `app/static/` |
| 7a | UI — Now (live dashboard) | **done** — `/api/now`, Now view |
| 7b | UI — Analysis (day chart, uPlot) | **done** — `/api/analysis`, Analysis view, vendored uPlot |
| 8 | Time profiles: shared schedules, per-room selection, trigger editor | **done** |
| 9 | UI — Almanac, with the auto/off control | next |
| 8 | UI — Almanac, with the auto/off control | |
| 9 | Home Assistant add-on packaging | |

## Open items

- **Verified running** against Home Assistant 2026.7.4 on 28 July 2026: connected,
  computed the active section, logged heartbeats, and correctly reported the six
  scene automations as not yet deployed.


- **Coordinates.** The year sweep has been run against sample sites, not the
  real installation. Zero collapses are expected below ~45° latitude. Run
  `tools/doctor.py` to read the real coordinates from Home Assistant, then
  `AL_CONFIG=... python tools/sweep.py` with them.
- **Template native types.** Answered by `tools/doctor.py`. The maintenance automation assigns
  `state_attr()` to a variable and subscripts it. Recent Home Assistant
  renders template variables to native Python types, but this is unverified
  on hardware. If it returns a string, `alm.get('lux_target')` fails silently
  and maintenance never nudges. Check in Developer Tools → Template:
  ```jinja
  {% set alm = state_attr('sensor.al_almanac_main_room', 'day') %}
  {{ alm is mapping }} / {{ alm.get('lux_target') }}
  ```
- **Helper creation over WebSocket.** Answered by `tools/doctor.py`. Creating helpers via
  `input_boolean/create` and friends is the frontend's own path, not a
  documented public API. Needs an hour's spike. If it fails, the fallback is
  generated YAML and a copy-paste step — the generator's output is identical
  either way.
