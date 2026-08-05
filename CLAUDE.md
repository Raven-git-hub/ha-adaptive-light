# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Adaptive Light — a self-hosted container that drives adaptive lighting in
Home Assistant. It replaced an earlier n8n-based prototype (cut over 28 July
2026). The container is the single source of truth for config; Home
Assistant only ever holds the helpers/automations this app generates and
deploys to it.

Core loop: a validated JSON config describes rooms, light groups, sensors,
and six fixed daily sections (`sunrise, day, afternoon, sunset, night,
sleep`). The scheduler fires scene automations at section crossovers, the
runtime observes via heartbeats and reactive (manual-change) detection, and
a nightly analyser turns observations into a learned "almanac" of per-group
targets that the generated automations read at runtime.

## Layout

```
app/           FastAPI service — see module map below
app/static/    web UI: vanilla JS, vendored uPlot, no build step
schema/        config.schema.json (JSON Schema) + storage.schema.sql (SQLite DDL)
examples/      worked example configuration
tools/         offline validation harnesses (doctor.py, sweep.py, compare_analyser.py)
docs/          ROADMAP.md (status) and DESIGN.md (the reasoning behind key decisions)
```

### Module map (`app/`)

- `config.py` — load/validate config against the JSON Schema, apply
  defaults, cross-check references (profiles, groups, entity ids), migrate
  legacy `schedule`/`schedule_override` shapes into `schedule_profiles`.
- `analyser.py` — nightly learning pipeline, ported from the n8n prototype.
  Behavioural changes from the legacy pipeline are gated behind
  `AnalyserFlags` so old and new can be diffed on identical input.
- `generator.py` — turns a validated config into deterministic HA helper
  and automation YAML. No template engine — HA's own Jinja must survive as
  inert strings, not be rendered by ours.
- `scheduler.py` — computes section boundaries (sun-relative and fixed) and
  applies collision policy (`collapse` or `clamp`) when they cross.
- `ha.py` — Home Assistant transport layer only (REST + WebSocket). Knows
  nothing about rooms, sections, or almanacs — that's `runtime.py`'s job.
- `runtime.py` — the live loop: scheduler, heartbeat observer, reactive
  (manual-change) detector, almanac push.
- `store.py` — dual-write storage layer (CSV archive + SQLite query layer),
  event log, CSV re-ingest.
- `deploy.py` — reconciles generated artefacts against what's actually in
  Home Assistant; creates/removes helpers and automations.
- `main.py` — FastAPI app and HTTP endpoints (`/api/config`, `/api/deploy`,
  `/api/now`, `/api/analysis`, etc.).

## Running things

No test suite (`pytest`) exists yet — validation is via the harnesses in
`tools/`, which run without Docker or a real Home Assistant except where
noted:

```bash
python tools/doctor.py                          # needs AL_HA_URL / AL_HA_TOKEN env vars; probes a real HA instance and cleans up after itself
python tools/sweep.py                            # a year of section-boundary computations, checking for unexpected collapses
python tools/compare_analyser.py heartbeat.csv    # diffs legacy vs current analyser output on identical input
```

Local dev container:

```bash
cp .env.example .env && $EDITOR .env    # HA base URL + long-lived token
mkdir -p data && sudo chown 10001 data  # container runs as uid 10001, unprivileged
docker compose up -d
curl -s localhost:8099/healthz
```

The UI is served at `http://<host>:8099` with no build step — edit
`app/static/` files directly and refresh (see "Open items" in
`docs/ROADMAP.md`: there's no cache-busting yet, so a hard refresh may be
needed).

## Conventions and hard rules

- **CSV is the archival source of truth; SQLite is a rebuildable query
  layer.** Never treat `data/*.db` as irreplaceable — it must always be
  reconstructable from the CSV archive. Don't add state that only lives in
  SQLite.
- **The generator has no template engine, by design.** Generated
  automations embed Home Assistant Jinja verbatim. Don't introduce Jinja
  rendering into `generator.py` — assemble Python data structures and let
  `yaml.dump` serialise them; Jinja stays an inert string throughout.
- **Scene automations always carry `conditions: []`.** They're fired via
  `automation.trigger`, which defaults to `skip_condition: true`; a
  condition placed there would be silently ignored. Don't add conditions to
  generated scene automations.
- **Nothing from the almanac is hardcoded into generated YAML.** Forced-off
  groups are not baked in as `light.turn_off` — the automation reads the
  almanac value at runtime and branches, so flipping a group to "off" in
  the UI needs only a republish, not a regeneration.
- **The six sections are currently a fixed vocabulary** (`sunrise, day,
  afternoon, sunset, night, sleep`), assumed by the analyser, generator,
  and almanac alike. If you're asked to add/remove sections, this
  assumption runs through multiple modules — see "Custom Time Sections" in
  `docs/ROADMAP.md` before starting.
- **Reactive detection must ignore the container's own changes.** It
  distinguishes user-originated brightness changes from automation-caused
  ones via a guard boolean, `context.parent_id`, and the configurable
  `external_guards` list. Don't regress this when touching `runtime.py`.
- **The WebSocket read loop must start before subscribing.** Subscribing
  first deadlocks until timeout and leaves the event stream dead while the
  log still reports "connected" — this bit the project once already (see
  `docs/ROADMAP.md`).
- **Token handling:** the HA access token is never persisted in the config
  document — it's stripped on save/export (`config.py: save()`) and comes
  from the environment (`AL_HA_TOKEN`) or a separate secrets file at
  runtime.
- **Config writes are validate-then-write, never the reverse** — a config
  that wouldn't load must never be able to replace one that does
  (`app/main.py: api_put_config`).
- **Deterministic generation:** the same config must always produce
  byte-identical generated YAML. Avoid anything nondeterministic (dict
  ordering, set iteration, timestamps) leaking into `generator.py` output.
- Comments in this codebase tend to explain *why*, especially around past
  bugs (e.g. the WebSocket race, the per-group eligibility fix in the
  analyser). Preserve that style — when fixing a non-obvious bug, leave a
  comment explaining the failure mode, not just the fix.

## Where to look first

- `docs/DESIGN.md` — reasoning behind the ~19 key design decisions; read
  before proposing a structural change.
- `docs/ROADMAP.md` — phase status, what's deployed, known open items, and
  two larger features under consideration (Presence Rules, Custom Time
  Sections). Check here before assuming something is unbuilt or before
  duplicating a fix that's already listed as done.
- `schema/config.schema.json` — the contract everything downstream derives
  from; its `description` fields double as the authoritative behavioural
  spec for config options.
