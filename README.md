# Adaptive Light

Adaptive Light targets a **light level**, not a brightness value. It learns
what ambient lux you actually prefer in each part of the day, and drives your
lights toward it — so you stop reaching for the dimmer.

It is a back-end system, meant to run largely unseen, with a web UI for
configuration and insight. It is not a replacement for Home Assistant's own
light controls; your switches keep working exactly as before.

> **Status: live.** The concept ran in production through n8n for months and
> is well proven. This repository is the rebuild: a containerised application
> with a proper UI, deployed and running against Home Assistant 2026.7.4. The
> full pipeline — scheduling, observation, learning, deployment, and the Now,
> Analysis, Almanac, Config and Log views — is built and in use. The only
> remaining planned work is Home Assistant add-on packaging; see
> `docs/ROADMAP.md`.

## How it works

A day is divided into six **sections**. Their times come from a **time
profile**, which a room selects — several rooms can share one profile, so a
single edit updates them all. The default profile:

| Section | Fires at |
|---|---|
| Sunrise | sunrise, or 05:30, whichever is earlier |
| Day | sunrise + 3h |
| Afternoon | sunset − 3h |
| Sunset | sunset |
| Night | 20:30 |
| Sleep | 22:00 |

Each section's trigger is a fixed clock time, an offset from sunrise or sunset,
or a composite that takes the earliest or latest of several — Sunrise above is
the earliest of "sunrise" and "05:30". When a sun-relative and a fixed time
collide, the fixed one wins and the loser is skipped for that day, because a
fixed time states something about your routine that holds whatever the sun
does. Collisions are seasonal; the Config UI scans a year ahead so you see one
coming rather than discover it.

At each crossover, brightness is set from the room's **almanac** — the learned
model of your preferences. Between crossovers a **maintenance** loop compares
measured lux against the section's target every ten minutes and nudges the
lights that are already on, by no more than a few percent at a time.

Every ten minutes a **heartbeat** records ambient lux, per-group brightness and
occupancy. When you adjust a light by hand, that **reactive** event is captured
at five times the weight of a heartbeat — you correcting the system is the most
valuable signal it gets. Overnight, a rolling three-week weighted analysis
rebuilds the almanac, favouring recent days and high-confidence samples.

The goal is convergence: a system that has learned enough to provoke no
reactions at all.

## Learning states

A room's almanac moves through three states, so the system is useful
immediately without pretending to know more than it does:

- **Provisional** — on the first crossover into a section, ambient lux is
  sampled and used as a target straight away, but maintenance stays out. This
  is an observation, not learning.
- **Bootstrap** — under seven days of data; the almanac is published
  immediately so behaviour appears quickly.
- **Learning** — seven days or more; new almanacs take effect after a short
  validity delay, so a single odd day cannot swing behaviour.

A group can also be set to **Off** for a section in the Config UI. That is an
explicit override, not something the learner decides — it is baked directly
into the generated automation and takes effect from the moment it is deployed,
regardless of whether an almanac exists yet.

## Architecture

**Home Assistant owns behaviour. The container owns observation.**

Home Assistant runs the scene automations and the maintenance loop, so the
parts you tune and debug stay visible in the UI with traces intact. The
container observes over the WebSocket API, learns, schedules the crossovers,
and pushes almanacs back over REST. It never touches Home Assistant's
filesystem, reaching it only over the network API.

Scheduling is **level-triggered**: rather than a timer per boundary, a slow
loop asks "which section should be active now?" and fires a crossover when the
answer changes. Startup after downtime, a dropped WebSocket, a clock change and
a paused VM all take the same path, so there is no separate catch-up routine to
get wrong.

Sun times are computed locally with `astral`, seeded from Home Assistant's own
latitude, longitude and elevation, and verified against `sun.sun` at startup
(a warning is logged if they drift more than a minute apart). This is because
`sun.sun` only exposes the *next* event, so it cannot say when today's sunrise
was — which catch-up needs — nor answer questions about future dates for the
year-ahead preview.

Home Assistant needs only three helpers per room, all generated:

- `input_select.al_scene_<room>` — the active section
- `input_boolean.al_active_<room>` — an AL automation is mid-change; ignore
- `input_boolean.al_hold_<room>` — the user intervened; maintenance stands down

Helpers are named from the room's stable **id**, not its display name, so the
object id Home Assistant derives always matches what the generated automations
reference. Deployment verifies this and refuses rather than deploy a broken
reference.

## The web UI

Served by the container at `http://<host>:8099`. Dark, desktop-oriented, no
login (it lives on your LAN).

- **Now** — the live picture per room: measured lux against the target band,
  each group's actual brightness versus its learned target, occupancy, the
  section timeline and a countdown to the next crossover.
- **Analysis** — one day on a shared time axis: measured lux, the target band
  per section, per-group brightness, section boundaries and markers wherever
  you intervened. A day with no markers is a day the system got right. Step
  back through history with the date controls.
- **Almanac** — the learned model per room: one row per section showing the
  learned lux target and a compact sparkline of how that target and the
  system's confidence in it have settled over recent nights. A rising, then
  flattening line is convergence. A **Re-run analysis** button rebuilds the
  almanac on demand rather than waiting for the nightly job.
- **Config** — rooms, groups and sensors chosen from live entity pickers (a
  typo cannot silently resolve to `unknown`); the scene matrix of auto/off per
  group per section; and **Time Profiles**, with a per-section trigger editor,
  a live boundary preview and the year-ahead collision scan. Rooms and profiles
  are collapsible.
- **Log** — everything the container did and when, filterable by room, category
  and severity. Heartbeats are recorded at debug level so they do not bury the
  rest.

Saving configuration starts observation. Deploying creates the helpers and
automations in Home Assistant. A change to section *times* is live from Save
alone; a change to what a scene *does* (a group's auto/off mode, transition
time) needs a redeploy.

## Layout

```
app/          scheduler, analyser, generator, runtime, deploy, FastAPI entry
app/static/   the web UI (vanilla JS, vendored uPlot; no build step)
schema/       config JSON Schema, SQLite DDL
examples/     worked configuration
tools/        offline validation harnesses
docs/         design decisions and roadmap
```

## Requirements

A **Docker host** on the same network as Home Assistant. Home Assistant OS
cannot run arbitrary containers, so this needs a separate machine or VM. The
three roles — the Docker host, Home Assistant, and (optionally) a git remote —
can be entirely separate machines; nothing assumes they are co-located.

## Quick start

```bash
git clone https://github.com/Raven-git-hub/ha-adaptive-light.git
cd ha-adaptive-light

cp .env.example .env && $EDITOR .env    # HA URL and long-lived token

mkdir -p data && sudo chown 10001 data  # container runs unprivileged
docker compose up -d

curl -s localhost:8099/healthz
```

The container comes up **idle** with no rooms and stays healthy — it does
nothing until you add one. Then open `http://<host>:8099`, add your room, its
light groups, and its lux and presence sensors from the pickers, and press
**Deploy to Home Assistant**.

If you are replacing an existing lighting system, disable or delete it first —
two systems driving the same lights will fight. Adaptive Light distinguishes
its own changes (and, during a transition, another system's) from your manual
ones, but only one should be actively driving the lights.

The first almanac builds from the nightly analysis at 00:15; until then the
system observes and seeds provisional targets, and maintenance begins the
following day.

## Validation

The harnesses run without Docker or Home Assistant:

```bash
python tools/doctor.py                              # check a real Home Assistant
python tools/sweep.py                               # a year of section boundaries
python tools/compare_analyser.py heartbeat.csv      # diff two analyser versions
```

## Roadmap and future ideas

Planned next: **Home Assistant add-on packaging**, which would remove the need
for a separate Docker host.

Three larger ideas under consideration:

- **Presence Rules** — configurable actions when no presence is detected in a
  section. Today occupancy only gates whether a heartbeat is eligible for
  learning; this would let a room *act* on emptiness — for example dimming or
  turning off after a period unoccupied, and restoring the section's scene when
  presence returns — set per section, since "empty at Day" and "empty at Sleep"
  may warrant different responses. The Config UI already reserves a collapsed
  **Presence rules** slot per room for this.

- **Custom Time Sections** — letting a profile add or remove sections rather
  than being fixed at the canonical six. The motivating case is a **baby-room
  nap window** — a bounded afternoon section with its own target and its own
  behaviour — but the same mechanism serves a work-from-home "focus" block, a
  "dinner" section, a "wind-down" hour before Sleep, or a short "away" section
  tied to a routine. This is the largest change of the three: the six-section
  vocabulary is currently assumed by the analyser, generator and almanac, so
  custom sections would need those to work from the profile's section list
  rather than a fixed set.

- **Almanac import** — seeding a room's almanac from an uploaded set of values
  (from another install, or drafted by hand or by an AI) so a new room does not
  start from nothing. It would land as a bootstrap almanac — an explicit
  starting guess, not learned data — that real observation is then free to
  correct from the first night, rather than an override that sticks. The
  **Re-run analysis** button is unrelated to this: it re-runs the existing
  nightly analysis on the data already collected, it does not import anything.

See `docs/ROADMAP.md` for status and `docs/DESIGN.md` for the reasoning behind
the twenty-one key decisions.
