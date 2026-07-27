"""
Adaptive Light - section scheduler.

Pure computation: coordinates and a date in, ordered section boundaries
out. No Home Assistant, no database, no I/O. Everything here is testable
offline across a full year, which matters because the failure modes are
seasonal - a rule that misbehaves in December looks perfect in July.

Two sun providers exist by design:

  * HomeAssistantSunProvider is authoritative at runtime. Sun times come
    from HA's own sun.sun entity so the container and HA can never
    disagree about when sunset is.

  * AstralSunProvider computes locally from lat/lon. Used only for the
    UI's forward-looking preview, because sun.sun only knows about the
    next event and cannot answer "what will happen in November". The two
    may differ by a few seconds; irrelevant for a preview, and never
    used to fire anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")
CANONICAL_ORDER = {s: i for i, s in enumerate(SECTIONS)}

DEFAULT_PRIORITY = {"clock": 100, "sun": 50}


class SunUnavailable(Exception):
    """No sunrise or sunset occurs on this date at this latitude."""


# ---------------------------------------------------------------------
# Sun providers
# ---------------------------------------------------------------------

class SunProvider(Protocol):
    def times(self, on: date) -> dict[str, datetime]: ...


@dataclass
class AstralSunProvider:
    latitude: float
    longitude: float
    tzinfo: object
    elevation: float = 0.0

    def times(self, on: date) -> dict[str, datetime]:
        from astral import LocationInfo, sun as astral_sun

        loc = LocationInfo(
            latitude=self.latitude, longitude=self.longitude,
        ).observer
        loc.elevation = self.elevation
        out: dict[str, datetime] = {}
        for key, fn in (("sunrise", astral_sun.sunrise), ("sunset", astral_sun.sunset)):
            try:
                out[key] = fn(loc, on, tzinfo=self.tzinfo)
            except ValueError:
                pass  # polar day or polar night: event does not occur
        return out


@dataclass
class FixedSunProvider:
    """Test double: supply sun times explicitly."""
    sunrise: time | None
    sunset: time | None
    tzinfo: object = None

    def times(self, on: date) -> dict[str, datetime]:
        out = {}
        if self.sunrise:
            out["sunrise"] = datetime.combine(on, self.sunrise, tzinfo=self.tzinfo)
        if self.sunset:
            out["sunset"] = datetime.combine(on, self.sunset, tzinfo=self.tzinfo)
        return out


# ---------------------------------------------------------------------
# Trigger resolution
# ---------------------------------------------------------------------

def resolve_trigger(trigger: dict, sun: dict[str, datetime], on: date, tzinfo=None) -> datetime:
    kind = trigger["type"]

    if kind == "clock":
        hh, mm = (int(x) for x in trigger["time"].split(":"))
        return datetime.combine(on, time(hh, mm), tzinfo=tzinfo)

    if kind == "sun":
        event = trigger["event"]
        if event not in sun:
            raise SunUnavailable(f"no {event} on {on}")
        return sun[event] + timedelta(minutes=trigger.get("offset_minutes", 0))

    if kind in ("earliest", "latest"):
        resolved = []
        for sub in trigger["of"]:
            try:
                resolved.append(resolve_trigger(sub, sun, on, tzinfo))
            except SunUnavailable:
                continue
        if not resolved:
            raise SunUnavailable(f"no component of {kind} resolved on {on}")
        return min(resolved) if kind == "earliest" else max(resolved)

    raise ValueError(f"unknown trigger type: {kind}")


def default_priority(trigger: dict) -> int:
    kind = trigger["type"]
    if kind in ("earliest", "latest"):
        # A composite is as firm as its firmest component: 'sunrise or
        # 05:30, whichever is earlier' has a clock floor and should not
        # be treated as purely sun-relative.
        return max(default_priority(sub) for sub in trigger["of"])
    return DEFAULT_PRIORITY.get(kind, 50)


# ---------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------

@dataclass
class Boundary:
    section: str
    name: str
    planned: datetime | None
    priority: int
    outcome: str = "ran"          # ran | collapsed
    reason: str | None = None
    ends: datetime | None = None

    @property
    def ran(self) -> bool:
        return self.outcome == "ran"


def compute_day(
    schedule: dict,
    provider: SunProvider,
    on: date,
    tzinfo=None,
) -> list[Boundary]:
    """Return all six sections for `on`, chronologically ordered, each
    marked as having run or collapsed with a stated reason."""
    policy = schedule.get("collision_policy", "collapse")
    min_gap = timedelta(minutes=schedule.get("min_section_minutes", 30))
    sun = provider.times(on)

    candidates: list[Boundary] = []
    for section in schedule["sections"]:
        trigger = section["trigger"]
        priority = section.get("priority", default_priority(trigger))
        try:
            planned = resolve_trigger(trigger, sun, on, tzinfo)
        except SunUnavailable as exc:
            candidates.append(Boundary(
                section["id"], section["name"], None, priority,
                outcome="collapsed", reason=str(exc),
            ))
            continue
        # A sun offset can push a boundary outside the day; clamp rather
        # than let a section leak into yesterday or tomorrow.
        lo = datetime.combine(on, time(0, 0), tzinfo=tzinfo)
        hi = datetime.combine(on, time(23, 59), tzinfo=tzinfo)
        planned = min(max(planned, lo), hi)
        candidates.append(Boundary(section["id"], section["name"], planned, priority))

    resolvable = [c for c in candidates if c.planned is not None]
    resolvable.sort(key=lambda b: (b.planned, CANONICAL_ORDER[b.section]))

    kept: list[Boundary] = []
    for cand in resolvable:
        while True:
            blocker, violation = _find_conflict(cand, kept, min_gap)
            if blocker is None:
                break

            cand_wins = (
                cand.priority > blocker.priority
                or (cand.priority == blocker.priority
                    and CANONICAL_ORDER[cand.section] < CANONICAL_ORDER[blocker.section])
            )

            # Clamping can rescue a section that merely starts too soon,
            # but it cannot rescue one that starts in the wrong order:
            # shifting the sunset scene later still leaves it running
            # after the night scene. Order inversions always collapse.
            if policy == "clamp" and violation == "gap":
                cand.planned = blocker.planned + min_gap
                cand.reason = f"clamped to {min_gap} after {blocker.section}"
                break

            loser, winner = (blocker, cand) if cand_wins else (cand, blocker)
            loser.outcome = "collapsed"
            loser.reason = (
                f"{violation} conflict: {winner.section} (priority {winner.priority}) "
                f"takes precedence over {loser.section} (priority {loser.priority})"
            )
            if cand_wins:
                kept.remove(blocker)
            else:
                break

        if cand.ran:
            kept.append(cand)

    for i, b in enumerate(kept):
        b.ends = kept[i + 1].planned if i + 1 < len(kept) else None

    collapsed = [c for c in candidates if not c.ran]
    return sorted(
        kept + collapsed,
        key=lambda b: (b.planned or datetime.combine(on, time(23, 59), tzinfo=tzinfo)),
    )


def _find_conflict(
    cand: Boundary, kept: list[Boundary], min_gap: timedelta
) -> tuple[Boundary | None, str | None]:
    """Two ways a candidate can conflict with what has already been kept.

    'order'  - a section later in canonical order has already started, so
               this one would run out of sequence. A UK midsummer sunset
               at 21:21 falling after night at 20:30 is this case: the gap
               is legal, the ordering is not.
    'gap'    - it starts within min_section_minutes of the previous
               section, leaving too little time for the crossover
               transition to finish before being overridden.
    """
    inversions = [
        k for k in kept
        if CANONICAL_ORDER[k.section] > CANONICAL_ORDER[cand.section]
    ]
    if inversions:
        return max(inversions, key=lambda k: CANONICAL_ORDER[k.section]), "order"
    if kept and (cand.planned - kept[-1].planned) < min_gap:
        return kept[-1], "gap"
    return None, None


def active_section_at(
    schedule: dict, provider: SunProvider, when: datetime, tzinfo=None
) -> tuple[str, datetime]:
    """Which section should be active right now, and when it started.

    Looks back a day, because between midnight and the first boundary
    the active section is the last one from yesterday. This is what the
    container uses on startup to catch up after downtime.
    """
    for delta in (0, 1, 2):
        day = when.date() - timedelta(days=delta)
        ran = [b for b in compute_day(schedule, provider, day, tzinfo) if b.ran]
        past = [b for b in ran if b.planned <= when]
        if past:
            last = past[-1]
            return last.section, last.planned
    raise SunUnavailable("could not determine an active section")
