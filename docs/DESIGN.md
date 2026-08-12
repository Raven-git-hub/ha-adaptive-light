# Design decisions

Why the system is built the way it is. Each entry records a decision and the
reasoning behind it, so that a future change is made deliberately rather than
by accident.

---

## 1. Six sections, one canonical vocabulary

`sunrise, day, afternoon, sunset, night, sleep`.

The prototype carried three incompatible vocabularies — the spec's names, the
analyser's (`evening`, `late`), and Home Assistant's helper names (`scene_morning`,
`scene_day_time`). Worse, `night` meant 20:30–22:00 in one and 00:00–05:30 in
another. The identifiers above are the only truth; display names live in config
and may be changed freely.

**Consequence:** the historical almanac is not migrated. The system starts fresh.

---

## 2. The section is recorded, never inferred

The analyser previously derived a heartbeat's section from hardcoded clock
boundaries while Home Assistant switched sections on real sun times. Those
agree in July and diverge in December, so every sample near a boundary was
misfiled — and the error was seasonal, so it drifted rather than averaging out.

The active section is now written into every heartbeat at the moment of
observation.

---

## 3. Fixed-clock sections outrank sun-relative ones

Four of the six boundaries are sun-relative and two are fixed, so they can
cross. A UK midsummer sunset at 21:21 falls after `night` at 20:30; a Nordic
December has `sunrise + 3h` overtaking `sunset − 3h`.

A fixed time states something about the user's routine that holds whatever the
sun is doing. A sun-relative time encodes an assumption about daylight — and a
collision is precisely the case where that assumption has broken. So fixed wins
(priority 100 vs 50), ties break toward the earlier section in canonical order,
and the loser **collapses**: it is skipped for that day and recorded with a
reason, rather than being silently distorted.

A composite trigger takes the priority of its firmest component:
`earliest(sunrise, 05:30)` has a clock floor, so it ranks 100.

**Two kinds of conflict, and both matter.** A *gap* conflict is two boundaries
closer together than `min_section_minutes`. An *order* conflict is a section
running out of canonical sequence — which a legal gap will not catch, and which
clamping cannot fix, since shifting the sunset scene later still leaves it
running after the night scene. Order inversions always collapse.

Measured over 2026: zero collapses at equatorial and Perth latitudes, 131 at
London, 508 at Tromsø. This only bites above roughly 45°.

---

## 4. A group's brightness is learned only from samples where it was on

The original averaged a group's brightness across on and off states, producing
values the light never actually held. `tv_light` sat at 142 for the Day
section — an average of "mostly off" and "occasionally 216". Setting that at a
crossover turns on a light the user normally wants off.

Brightness is now averaged over on-samples only, and `on_fraction` is recorded
separately. Two honest facts instead of one misleading number.

---

## 5. Only a user turns a light off

The learner never emits 0 for a group in `auto` mode. Off is a user decision,
made in this app's UI, and is then permanent for that section.

Turning a light off in Home Assistant is a different thing: a manual
intervention that stands until the next crossover, excluded from learning.
Only that group's brightness is excluded — the resulting room lux is still a
real measurement and still counts.

**Accepted consequence:** a group that is never on during a section learns
`null`, which means "leave the light exactly as it is". If it happens to be on
at the crossover it can stay on for the whole section. `on_fraction` is
surfaced in the UI so this is visible, but the system will not act on it.

---

## 6. A reactive event suspends maintenance for the rest of the section

Without this, maintenance fights the user: turn one group off, room lux drops
below the band, and ten minutes later the system nudges the remaining lights up
to compensate for exactly what was just done.

A converged almanac hides this — manual changes stay inside the margin. A new
user in week one would see it constantly, and it is the most likely reason
someone abandons the system.

**This needs its own helper.** The guard boolean cannot carry it: the guard also
gates heartbeat deferral, so holding it for a whole section would silently stop
observation for hours. Hence `al_hold_<room>` alongside `al_active_<room>`.

---

## 7. Reactive events ignore occupancy

The analyser dropped any reactive event where the presence sensor read false.
But a human moving a dimmer is definitive proof of presence — the sensor merely
had not tripped. Given reactive events carry five times the weight and are the
only signal that is not the system reading back its own output, discarding them
is expensive.

Heartbeats still require occupancy. A room with no presence sensor configured is
treated as permanently occupied.

---

## 8. Zero lux is a floor, not a measurement

The sensor reads 0.0 for much of the night while lights are visibly on, so a
reading of 0 is never treated as a real lux measurement. That is unchanged.

What changed: such rows are excluded from the *lux* average but still contribute
*brightness*. Previously they were dropped entirely, which is why `night` and
`sleep` learned almost nothing — yet those are exactly the sections where
maintenance is disabled and the scene brightness is the entire behaviour.

Maintenance being inert at night used to happen by arithmetic accident (a margin
of 5 around a target of 3 puts every reading in band). It is now an explicit
`maintenance_enabled` flag.

---

## 9. Home Assistant owns behaviour, the container owns observation

Scene automations and the maintenance loop stay in Home Assistant, where they
can be inspected and traced. Heartbeat logging and reactive detection move into
the container.

This removes: the heartbeat automation, three reactive automations, two shell
command scripts, the daily NAS dump, the dynamic CSV header problem, and — per
room — six `input_number`s, two `input_text`s and a timer. Per-room helpers drop
from about thirteen to three.

It also fixes two bugs structurally rather than by patching: the
`input_text` `max: 10` truncation that would silently lose reactive events in a
room with six groups, and transition-echo phantom events, neither of which can
exist once the snapshot happens in memory.

Maintenance stays on Home Assistant's own clock, so light correction continues
even when the container is down. The container's heartbeat is a separate timer
that will drift against it — so a heartbeat is **deferred**, not skipped, while
the guard is on, and never samples a light mid-transition.

---

## 10. The container is the scheduler

Scene automations carry no triggers. The container computes boundaries and
fires them over `automation.trigger`.

Collision logic then lives in one place, a time change in the UI takes effect
immediately with no regeneration, and the UI cannot drift from what Home
Assistant does.

**Costs, accepted:** container down means sections stop changing, so a staleness
watchdog runs in Home Assistant; and on startup or reconnect the container must
compute which section *should* be active and fire the crossover if it disagrees,
or a thirty-second restart at the wrong moment costs a whole section.

**Trap:** `automation.trigger` defaults to `skip_condition: true`. Any
`conditions:` block in a scene automation would be silently ignored, so
generated scene automations always carry `conditions: []`.

Sun times come from Home Assistant's `sun.sun` so the two can never disagree.
Local astral computation is used only for the UI's forward-looking preview,
which `sun.sun` cannot answer.

---

## 11. The almanac is pushed, not read from disk

Previously a `command_line` sensor `cat`-ed a JSON file. That required a
configuration.yaml edit, a restart, and a `json_attributes` allow-list that
silently drops any attribute not named — so renaming a section would have made
`state_attr()` return `None` with no error anywhere.

The container now POSTs directly to `/api/states/sensor.al_almanac_<room>`. No
YAML, no restart, no allow-list, no `/share` mount, and no scan-interval lag.

**Cost:** API-set states do not survive a Home Assistant restart, so the
container re-pushes on a timer and on `homeassistant_start`.

The sensor's state is `valid_from`, not the schema version — a constant state
means a stalled analyser is invisible.

---

## 12. Three almanac states

| State | Source | Maintenance |
|---|---|---|
| `provisional` | ambient lux observed at the first crossover into each section; brightness null | off |
| `bootstrap` | analysed, fewer than `bootstrap_min_days`; published immediately | on |
| `learning` | analysed, enough days; published with the two-day validity delay | on |

The delay ensures only complete days feed a published almanac and prevents large
swings. Skipping it during bootstrap means a fresh install comes alive on day
two rather than appearing broken for three days.

The provisional seed averages three readings over two minutes — a single
instantaneous read is hostage to one cloud or one opened door.

---

## 13. Generated artefacts contain no baked-in state

The generator emits no literal brightness values. Forced-off groups are not
written as `light.turn_off`; the automation reads the almanac at runtime and
branches on `== 0`.

So changing a group's mode, a lux margin, or a maintenance flag needs only an
almanac republish. Regeneration is required only when *entities* change.

Generation uses PyYAML over Python data structures, never a template engine:
rendering Jinja to produce Jinja means the outer pass consumes the inner one.

Output is deterministic, so regeneration is a safe overwrite rather than a merge.

---

## 14. Entity IDs only, never device IDs

The prototype mixed both. Device IDs are opaque UUIDs that break on re-pairing
and cannot be validated by an entity-existence check.

---

## 15. One `input_select`, not six booleans

The prototype's scene automations turned off exactly one predecessor boolean. A
restart or manual trigger could leave two on at once, and the maintenance
template's if/elif ladder would then silently resolve to whichever appeared
first rather than the one that actually fired.

A single select cannot desynchronise, and it collapses an eleven-line template
to one line.

---

## 16. CSV and SQLite, with a clear split

Home Assistant-era CSVs were the archive; that property is kept. The container
writes CSV as the human-readable record and ingests into SQLite as the query
layer for the analyser and the live graph.

SQLite is always rebuildable from CSV, so the container holds no irreplaceable
state.

Group readings are stored **long**, not wide. Rooms have a variable number of
groups, so fixed `group_1..group_4` columns cannot survive — and long format
turns "average this group only over samples where it was on" into a `WHERE`
clause rather than a special case.

---

## 17. The event log is a feature, not debug output

The container is the only component that sees the whole picture, so every action
it takes is recorded and surfaced in the UI: crossovers with computed boundaries,
collapses with reasons, nudges, reactive events, almanac publishes, disconnects,
deferred heartbeats.

`section_run` records what was *planned* against what *happened*, so a collapsed
or missed section appears as a row with a reason rather than as silence.

---

## 18. Reactive detection ignores automation-caused changes

The runtime treats a brightness change as a human intervention only when
it was not caused by an automation. Two independent signals establish
this, either sufficient: the guard boolean being on (our own automation
is mid-change), or the change carrying a `context.parent_id` (Home
Assistant threads one through any change an automation or script
triggers; a physical switch press or direct user action has none).

Without the second signal, any other automation touching these lights is
learned from at 5x weight. During the transition that means a still-running
prototype's ten-minute maintenance nudge is recorded as a user
intervention every ten minutes; after cutover it would mean our own
maintenance loop being learned from as if it were the user. Neither is a
real preference, and both would corrupt the almanac.

---

## 19. Off-mode groups are baked into the automation, not read from the almanac

An earlier version encoded "off" for a group by relying on the almanac
storing a brightness of 0 for it, with the scene automation checking
`alm[group] == 0` at runtime. That made an explicit user override silently
depend on an almanac existing at all - on the first night after deploy,
with no almanac yet, the whole conditional was skipped and the light was
left exactly as it was, contradicting the override entirely.

Off is now baked directly into the generated automation as an
unconditional `light.turn_off` for any group whose scene config has
`mode: "off"`, bypassing the almanac lookup completely. It takes effect
from the moment it is deployed, independent of whether an almanac exists,
how old it is, or whether the analyser has ever run. The analyser still
writes 0 for that group so the almanac and the deployed behaviour agree,
but the deployed behaviour no longer depends on it.


---

## 20. Almanac history comes free from insert-only snapshots

The nightly analysis inserts a new `almanac` row each time rather than updating
one in place. That was originally just simplicity — `current_almanac` reads the
latest row — but it means every almanac the system has ever computed is still
on disk, timestamped. The Almanac page's trend sparklines are therefore a pure
read over data that already existed; no history table, no schema change, was
needed to show how a target settled over three weeks.

`trust_weight` — the accumulated recency×confidence×settled weight behind a
section — is the one value added for this. The analyser already computed it
internally to bucket confidence into low/medium/high; it was simply not being
persisted. Storing the raw number lets the UI show trust *approaching* a
threshold rather than only the step when it crosses one. An idempotent
migration adds the column to databases created before it existed.

## 21. The Almanac view is a glance, not a second editor

An earlier version of the page was a full matrix — sections down, groups
across — with per-group brightness, `on_fraction` bars and the auto/off toggle
duplicated from Config, plus a separate column of full charts. It was accurate
but did too much: two side-by-side panels whose rows had to be kept aligned by
measuring one against the other, and a second place to edit auto/off.

It was cut back to one table, one row per section: the section, its learned lux
target, and a small axis-less sparkline of how that target and its trust have
settled. Alignment stopped being a problem the moment the trend lived in the
same table row as its numbers rather than in a parallel panel. Editing auto/off
belongs on Config, where the scene matrix already is; the Almanac page only
needs to *show* what was learned. The sparkline has no axis deliberately —
direction at a glance is the whole job, and an absent axis cannot be clipped.

## 22. Maintenance nudges are observed by attributing the guard window

Decision #17 promised the log records nudges, but nothing ever wrote a
`maintenance` event. The reason is decision #9: maintenance runs entirely
inside Home Assistant on its own clock, precisely so correction survives the
container being down. It raises the guard (`al_active_<room>`) and steps
brightness, and the container's reactive detector treats a raised guard as
"an AL automation is mid-change, ignore this" and returns early — so the very
mechanism that keeps maintenance from being *mis-learned* also kept it from
being *seen*.

The container never initiates a maintenance run, so it cannot log one at the
source. Instead it attributes the guard window at the edges. The guard is only
ever raised two ways: by a scene crossover the container fires itself
(`_fire_crossover`), or by the maintenance `time_pattern` it does not. So the
crossover stamps `own_crossover_at`; when the guard's off→on edge arrives, a
stamp within the last few seconds means "scene", its absence means
"maintenance". During a maintenance window the guard-on branch collects each
group's before/after brightness (the same changes it still ignores for
learning), and the on→off edge flushes exactly one `info`/`maintenance` event
with direction, per-group deltas, and the current lux against the section
target.

This adds observation only inside the guard-on path; the reactive detector's
guard / `context.parent_id` / `external_guards` logic (#18) is untouched, so
no nudge is ever learned as a preference. A window already open at container
startup has no attribution and is not logged, and a user tweak landing inside a
maintenance window is mis-attributed as a nudge — but that input is already
discarded for learning today, so the only cost is one possibly-mislabelled log
line, not a corrupted almanac.
