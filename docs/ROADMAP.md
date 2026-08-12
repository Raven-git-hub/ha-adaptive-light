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
| 9 | UI — Almanac: per-section lux target with a settling-trend sparkline | **done** |
| 10 | Home Assistant add-on packaging | next |

## Deployed

Cut over to the containerised system on 28 July 2026, running against Home
Assistant 2026.7.4. The n8n prototype and its automations were removed. The
container schedules crossovers, observes via heartbeats, detects manual
adjustments, and maintains lux against the learned target; the Now, Analysis,
Almanac, Config and Log views are in use.

Issues found and fixed during and after cutover:

- WebSocket reconnect loop that left the event stream dead while the log
  reported "connected" — the read loop must start before any subscription.
- Reactive detection now ignores automation-caused changes (via the guard
  boolean, `context.parent_id`, and configurable `external_guards`), so a
  coexisting system's nudges are not learned as user interventions.
- Off-mode groups are baked into the scene automation directly, so an explicit
  "off" holds from first deploy instead of waiting on an almanac to exist.
- Section runs are closed when the next fires, so Analysis target bands stop at
  the real boundary; band drawing is clamped to the plot area.
- Maintenance nudges now appear in the Log. The maintenance loop runs inside HA
  under the guard, which the reactive detector ignores, so nudges were never
  observed; the runtime now attributes each guard window (scene vs maintenance)
  and writes one `maintenance` event per nudge run without changing what is
  learned. See DESIGN #22.

## Future ideas

- **Presence Rules.** Configurable actions when no presence is detected in a
  section — dimming or turning off after a period unoccupied, restoring the
  scene when presence returns — set per section. A collapsed **Presence rules**
  slot is already reserved per room in the Config UI.
- **Custom Time Sections.** Let a profile add or remove sections rather than
  being fixed at the canonical six (e.g. a baby-room nap window, a focus block,
  a dinner or wind-down section). The larger change: the analyser, generator
  and almanac currently assume the six-section vocabulary and would need to
  work from the profile's section list instead.

## Open items

- **Coordinates.** The year sweep has been validated against the real
  installation via `tools/doctor.py`; zero collapses at Hong Kong latitude,
  as expected below ~45°.
- **Scheduler `ReadTimeout` noise.** A routine retry when Home Assistant is
  briefly slow logs a full stack trace at error level. Cosmetic; worth
  quietening so a transient blip does not look alarming.
- **Static-file cache busting.** UI updates currently need a hard refresh; a
  version query string on the script tags would remove that.
- **`external_guards`.** Added for coexistence with the prototype during
  cutover; now dead weight, harmless, removable.
