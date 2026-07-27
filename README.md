# Adaptive Light

Adaptive Light targets a **light level**, not a brightness value. It learns
what ambient lux you actually prefer in each part of the day, and drives your
lights toward it — so you stop reaching for the dimmer.

It is a back-end system, meant to run unseen. It is not a replacement for
Home Assistant's own light controls.

> **Status: in development.** The concept has been running in production
> through n8n for months and is well proven. This repository is the rebuild:
> a containerised application with a proper UI. Phases 1–5a are implemented;
> the runtime and UI are not yet built. See `docs/ROADMAP.md`.

## How it works

A day is divided into six **sections**:

| Section | Fires at |
|---|---|
| Sunrise | sunrise, or 05:30, whichever is earlier |
| Day | sunrise + 3h |
| Afternoon | sunset − 3h |
| Sunset | sunset |
| Night | 20:30 |
| Sleep | 22:00 |

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

## Architecture

**Home Assistant owns behaviour. The container owns observation.**

Home Assistant runs the scene automations and the maintenance loop, so the
parts you tune and debug stay visible in the UI with traces intact. The
container observes over the WebSocket API, learns, and pushes almanacs back
over REST. It never touches Home Assistant's filesystem.

Home Assistant needs only three helpers per room, all generated:

- `input_select.al_scene_<room>` — the active section
- `input_boolean.al_active_<room>` — an AL automation is mid-change; ignore
- `input_boolean.al_hold_<room>` — the user intervened; maintenance stands down

## Layout

```
app/          scheduler, analyser, generator, FastAPI entry point
schema/       config JSON Schema, SQLite DDL
examples/     worked configuration
tools/        offline validation harnesses
docs/         design decisions and roadmap
```

## Quick start

Requires a **Docker host**. Home Assistant OS cannot run arbitrary containers,
so this needs a separate machine or VM on the same network.

```bash
git clone https://github.com/Raven-git-hub/ha-adaptive-light.git
cd ha-adaptive-light

cp .env.example .env && $EDITOR .env    # HA URL and long-lived token

mkdir -p data && sudo chown 10001 data  # container runs unprivileged
docker compose up -d

curl -s localhost:8099/healthz
```

Then open `http://<host>:8099`, add your rooms and entities, and deploy the
generated helpers and automations to Home Assistant.

## Validation

Both harnesses run without Docker or Home Assistant:

```bash
python tools/doctor.py                             # check a real Home Assistant
python tools/sweep.py                              # a year of section boundaries
python tools/compare_analyser.py heartbeat.csv     # diff two analyser versions
```
