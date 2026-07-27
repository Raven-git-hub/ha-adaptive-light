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
| 5b-ii | Runtime: catch-up, observer, reactive detector, almanac push | next |
| 6 | UI page 2 — configuration | |
| 7 | UI page 1 — live analysis graph | |
| 8 | UI page 3 — deploy and entity health check | |
| 9 | Home Assistant add-on packaging | |

## Verified against Home Assistant 2026.7.4

Run of `tools/doctor.py`, 27 July 2026:

- **Coordinates** 22.354 N, 114.061 E, `Asia/Hong_Kong`. Year sweep: **zero
  collapsed sections** across 2026. Note that `earliest(sunrise, 05:30)` resolves
  to 05:30 every day of the year here, since local sunrise never falls before
  05:39 - the sun component never wins, and in late December the Sunrise section
  begins some 93 minutes before it is actually light.
- **Template native types** confirmed. `state_attr()` returns a mapping inside a
  template, and a template variable round-trips through `literal_eval` back into
  a dict. The generator's `variables:` approach is safe as written.
- **Helper creation over WebSocket** confirmed working.
- **Automation creation over REST** confirmed working.

Deployment is therefore fully automatic: no YAML export, no copy-paste, no
`configuration.yaml` edit, no restart.

## Verified against Home Assistant 2026.7.4

Run of `tools/doctor.py`, 27 July 2026:

- **Coordinates** 22.354 N, 114.061 E, `Asia/Hong_Kong`. Year sweep: **zero
  collapsed sections** across 2026. Note that `earliest(sunrise, 05:30)` resolves
  to 05:30 every day of the year here, since local sunrise never falls before
  05:39 - the sun component never wins, and in late December the Sunrise section
  begins some 93 minutes before it is actually light.
- **Template native types** confirmed. `state_attr()` returns a mapping inside a
  template, and a template variable round-trips through `literal_eval` back into
  a dict. The generator's `variables:` approach is safe as written.
- **Helper creation over WebSocket** confirmed working.
- **Automation creation over REST** confirmed working.

Deployment is therefore fully automatic: no YAML export, no copy-paste, no
`configuration.yaml` edit, no restart.

## Open items

- **Cutover.** The prototype's automations and the n8n workflow are still live.
  They must be disabled before the container takes over, or two systems will
  drive the same lights from two different almanacs. The prototype's 16 helpers
  do not clash with the new names and can be removed afterwards at leisure.
- **One illuminance sensor for 24 lights.** Each room needs its own, so only one
  room can be configured until more are added.
