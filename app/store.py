"""
Adaptive Light - storage.

CSV on disk is the archive; SQLite is the query layer. Every observation
is written to both, CSV first: if the database write fails the data is
still on disk and recoverable, whereas the reverse is not true. SQLite
can always be rebuilt from CSV with ingest_csv(), so the container holds
no irreplaceable state.

All methods are synchronous. Writes are a few rows at a time, every ten
minutes - measured in microseconds, and not worth the complexity of
offloading to a thread. A lock guards concurrent access from the
scheduler, the observer and the HTTP layer.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("adaptive_light.store")


@dataclass(frozen=True)
class GroupSample:
    """One group's reading within a heartbeat."""
    is_on: bool
    brightness: int | None


@dataclass(frozen=True)
class ReactiveGroupSample:
    is_on_before: bool
    is_on_after: bool
    brightness_before: int | None
    brightness_after: int | None
    changed: bool


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Store:
    def __init__(self, data_dir: str | Path, schema_path: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.csv_dir = self.data_dir / "csv"
        self.db_path = self.data_dir / "db" / "adaptive_light.db"

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_dir.mkdir(parents=True, exist_ok=True)

        fresh = not self.db_path.exists() or self.db_path.stat().st_size == 0
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if fresh:
            self._conn.executescript(Path(schema_path).read_text())
            self._conn.commit()
            log.info("initialised database at %s", self.db_path)
        else:
            self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -----------------------------------------------------------------
    # CSV
    # -----------------------------------------------------------------

    def _csv_path(self, kind: str, room_id: str, local_date: str,
                  header: list[str]) -> Path:
        """Path for today's file, rotating if the columns have changed.

        Adding a group mid-day changes the header. Rather than corrupt
        the file or silently drop the new column, start a numbered
        successor and say so.
        """
        room_dir = self.csv_dir / room_id
        room_dir.mkdir(parents=True, exist_ok=True)
        base = room_dir / f"{kind}_{local_date}.csv"

        candidate, suffix = base, 0
        while candidate.exists():
            with candidate.open(newline="") as fh:
                existing = next(csv.reader(fh), None)
            if existing == header:
                return candidate
            suffix += 1
            candidate = room_dir / f"{kind}_{local_date}.{suffix}.csv"
            if not candidate.exists():
                log.warning("columns changed for %s/%s; starting %s",
                            room_id, local_date, candidate.name)
        return candidate

    def _append_csv(self, path: Path, header: list[str], row: list[Any]) -> None:
        new = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(header)
            writer.writerow(row)

    @staticmethod
    def _heartbeat_header(group_ids: Iterable[str]) -> list[str]:
        cols = ["timestamp", "section", "ambient_lux", "occupied", "any_light_on"]
        for gid in group_ids:
            cols += [f"{gid}_on", f"{gid}_brightness"]
        return cols

    @staticmethod
    def _reactive_header(group_ids: Iterable[str]) -> list[str]:
        cols = ["timestamp", "section", "window_seconds",
                "lux_before", "lux_after", "occupied"]
        for gid in group_ids:
            cols += [f"{gid}_before", f"{gid}_after", f"{gid}_changed"]
        return cols

    # -----------------------------------------------------------------
    # Observations
    # -----------------------------------------------------------------

    def record_heartbeat(
        self,
        room_id: str,
        ts: datetime,
        section: str,
        ambient_lux: float | None,
        lux_sensor_n: int,
        occupied: bool,
        groups: dict[str, GroupSample],
        group_order: list[str],
        deferred_ms: int = 0,
        write_csv: bool = True,
    ) -> int | None:
        local_date = ts.strftime("%Y-%m-%d")
        any_on = any(g.is_on for g in groups.values())
        header = self._heartbeat_header(group_order)

        row: list[Any] = [ts.isoformat(), section, ambient_lux,
                          occupied, any_on]
        for gid in group_order:
            g = groups.get(gid)
            row += [g.is_on if g else "", g.brightness if g and g.is_on else ""]

        # CSV first: it is the archive, and a database failure must not
        # cost an observation. Skipped during ingest - re-reading the
        # archive must never append to it.
        if write_csv:
            self._append_csv(
                self._csv_path("heartbeat", room_id, local_date, header), header, row
            )

        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO heartbeat(room_id,ts,ts_utc,local_date,"
                    "section,ambient_lux,lux_sensor_n,occupied,any_light_on,deferred_ms)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (room_id, ts.isoformat(),
                     ts.astimezone(timezone.utc).isoformat(), local_date, section,
                     ambient_lux, lux_sensor_n, int(occupied), int(any_on),
                     deferred_ms),
                )
                if cur.rowcount == 0:
                    return None  # duplicate timestamp; already recorded
                hb_id = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO heartbeat_group VALUES(?,?,?,?)",
                    [(hb_id, gid, int(g.is_on), g.brightness)
                     for gid, g in groups.items()],
                )
                self._conn.commit()
                return hb_id
            except sqlite3.Error:
                self._conn.rollback()
                log.exception("heartbeat db write failed; CSV is intact")
                return None

    def record_reactive(
        self,
        room_id: str,
        ts: datetime,
        section: str,
        window_seconds: int,
        lux_before: float | None,
        lux_after: float | None,
        occupied: bool,
        suspended_maintenance: bool,
        groups: dict[str, ReactiveGroupSample],
        group_order: list[str],
        write_csv: bool = True,
    ) -> int | None:
        local_date = ts.strftime("%Y-%m-%d")
        header = self._reactive_header(group_order)

        row: list[Any] = [ts.isoformat(), section, window_seconds,
                          lux_before, lux_after, occupied]
        for gid in group_order:
            g = groups.get(gid)
            row += [
                (g.brightness_before if g.is_on_before else 0) if g else "",
                (g.brightness_after if g.is_on_after else 0) if g else "",
                g.changed if g else "",
            ]

        if write_csv:
            self._append_csv(
                self._csv_path("reactive", room_id, local_date, header), header, row
            )

        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO reactive(room_id,ts,ts_utc,local_date,"
                    "section,window_seconds,lux_before,lux_after,occupied,"
                    "suspended_maint) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (room_id, ts.isoformat(),
                     ts.astimezone(timezone.utc).isoformat(), local_date, section,
                     window_seconds, lux_before, lux_after, int(occupied),
                     int(suspended_maintenance)),
                )
                if cur.rowcount == 0:
                    return None
                rx_id = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO reactive_group VALUES(?,?,?,?,?,?,?)",
                    [(rx_id, gid, int(g.is_on_before), int(g.is_on_after),
                      g.brightness_before, g.brightness_after, int(g.changed))
                     for gid, g in groups.items()],
                )
                self._conn.commit()
                return rx_id
            except sqlite3.Error:
                self._conn.rollback()
                log.exception("reactive db write failed; CSV is intact")
                return None

    # -----------------------------------------------------------------
    # Section runs
    # -----------------------------------------------------------------

    def record_section_run(
        self, room_id: str, local_date: str, section: str,
        planned_start: datetime | None, actual_start: datetime | None,
        outcome: str = "ran", outcome_reason: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO section_run(room_id,local_date,section,planned_start,"
                "actual_start,outcome,outcome_reason) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(room_id,local_date,section) DO UPDATE SET "
                "planned_start=excluded.planned_start,"
                "actual_start=excluded.actual_start,"
                "outcome=excluded.outcome,outcome_reason=excluded.outcome_reason",
                (room_id, local_date, section,
                 planned_start.isoformat() if planned_start else None,
                 actual_start.isoformat() if actual_start else None,
                 outcome, outcome_reason),
            )
            self._conn.commit()

    def close_section_run(self, room_id: str, local_date: str,
                          section: str, ended_at: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE section_run SET ended_at=? WHERE room_id=? AND "
                "local_date=? AND section=?",
                (ended_at.isoformat(), room_id, local_date, section),
            )
            self._conn.commit()

    # -----------------------------------------------------------------
    # Event log
    # -----------------------------------------------------------------

    def log_event(
        self, severity: str, category: str, message: str,
        room_id: str | None = None, detail: dict | None = None,
        ts: datetime | None = None,
    ) -> None:
        ts = ts or datetime.now().astimezone()
        with self._lock:
            self._conn.execute(
                "INSERT INTO event(ts,ts_utc,room_id,severity,category,message,detail)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts.isoformat(), ts.astimezone(timezone.utc).isoformat(),
                 room_id, severity, category, message,
                 json.dumps(detail) if detail else None),
            )
            self._conn.commit()

    def recent_events(
        self, limit: int = 100, room_id: str | None = None,
        category: str | None = None, min_severity: str | None = None,
    ) -> list[dict]:
        order = ["debug", "info", "warning", "error"]
        sql = "SELECT * FROM event WHERE 1=1"
        args: list[Any] = []
        if room_id:
            sql += " AND room_id = ?"; args.append(room_id)
        if category:
            sql += " AND category = ?"; args.append(category)
        if min_severity:
            allowed = order[order.index(min_severity):]
            sql += f" AND severity IN ({','.join('?' * len(allowed))})"
            args += allowed
        sql += " ORDER BY ts_utc DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args)]

    def prune_events(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM event WHERE ts_utc < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # -----------------------------------------------------------------
    # Almanacs
    # -----------------------------------------------------------------

    def save_almanac(self, room_id: str, payload: dict) -> int:
        meta = payload.get("_meta", {})
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR REPLACE INTO almanac(room_id,generated_at,valid_from,"
                "mode,days_analysed,payload) VALUES(?,?,?,?,?,?)",
                (room_id, datetime.now().astimezone().isoformat(),
                 meta.get("valid_from", ""), meta.get("mode", "provisional"),
                 meta.get("days_analysed", 0), json.dumps(payload)),
            )
            almanac_id = cur.lastrowid

            for section, entry in payload.items():
                if section == "_meta" or not isinstance(entry, dict):
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO almanac_scene(almanac_id,section,"
                    "lux_target,lux_margin,max_step_pct,maintenance_enabled,"
                    "days_contributing,high_confidence_days) VALUES(?,?,?,?,?,?,?,?)",
                    (almanac_id, section, entry.get("lux_target"),
                     entry.get("lux_margin", 5), entry.get("max_step_pct", 0.04),
                     int(entry.get("maintenance_enabled", True)),
                     entry.get("days_contributing", 0),
                     entry.get("high_confidence_days", 0)),
                )
                fractions = entry.get("on_fraction", {})
                for key, value in entry.items():
                    if key in ("lux_target", "lux_margin", "max_step_pct",
                               "maintenance_enabled", "days_contributing",
                               "high_confidence_days", "on_fraction"):
                        continue
                    self._conn.execute(
                        "INSERT OR REPLACE INTO almanac_group(almanac_id,section,"
                        "group_id,mode,brightness,on_fraction,sample_weight)"
                        " VALUES(?,?,?,?,?,?,0)",
                        (almanac_id, section, key,
                         "off" if value == 0 else "auto", value,
                         fractions.get(key)),
                    )
            self._conn.commit()
            return almanac_id

    def mark_published(self, almanac_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE almanac SET published_at=? WHERE id=?",
                (datetime.now().astimezone().isoformat(), almanac_id),
            )
            self._conn.commit()

    def current_almanac(self, room_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM almanac WHERE room_id=? AND valid_from<=? "
                "ORDER BY valid_from DESC, generated_at DESC LIMIT 1",
                (room_id, datetime.now().strftime("%Y-%m-%d")),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    # -----------------------------------------------------------------
    # Config versions
    # -----------------------------------------------------------------

    def save_config_version(self, config: dict) -> int | None:
        payload = json.dumps(config, sort_keys=True)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO config_version(applied_at,payload,payload_sha)"
                " VALUES(?,?,?)",
                (datetime.now().astimezone().isoformat(), payload, _sha(payload)),
            )
            self._conn.commit()
            return cur.lastrowid if cur.rowcount else None

    # -----------------------------------------------------------------
    # CSV re-ingest
    # -----------------------------------------------------------------

    def ingest_csv(self, path: str | Path, room_id: str) -> tuple[int, int]:
        """Rebuild database rows from an archived CSV.

        Idempotent: unchanged files are skipped by content hash, and rows
        that already exist are ignored on their unique timestamp.
        Returns (ingested, skipped).
        """
        path = Path(path)
        text = path.read_text()
        digest = _sha(text)
        kind = "reactive" if path.name.startswith("reactive") else "heartbeat"

        with self._lock:
            prior = self._conn.execute(
                "SELECT content_sha FROM ingest_file WHERE path=?", (str(path),)
            ).fetchone()
        if prior and prior["content_sha"] == digest:
            return (0, 0)

        rows = list(csv.DictReader(text.splitlines()))
        ingested = skipped = 0

        for row in rows:
            ts = datetime.fromisoformat(row["timestamp"])
            if kind == "heartbeat":
                # Keyed off _brightness, not _on: the room-level
                # 'any_light_on' column also ends in _on and would
                # otherwise be ingested as a phantom light group.
                groups = {}
                for col in row:
                    if col.endswith("_brightness"):
                        gid = col[: -len("_brightness")]
                        b = row.get(col, "")
                        groups[gid] = GroupSample(
                            is_on=row.get(f"{gid}_on") in ("True", "true", "1"),
                            brightness=int(b) if b not in ("", None) else None,
                        )
                lux = row.get("ambient_lux", "")
                result = self.record_heartbeat(
                    room_id, ts, row["section"],
                    float(lux) if lux not in ("", None) else None,
                    1, row.get("occupied") in ("True", "true", "1"),
                    groups, list(groups), 0, write_csv=False,
                )
            else:
                groups = {}
                for col in row:
                    if col.endswith("_changed"):
                        gid = col[:-8]
                        before = row.get(f"{gid}_before", "")
                        after = row.get(f"{gid}_after", "")
                        bi = int(before) if before not in ("", None) else None
                        ai = int(after) if after not in ("", None) else None
                        groups[gid] = ReactiveGroupSample(
                            is_on_before=bool(bi), is_on_after=bool(ai),
                            brightness_before=bi, brightness_after=ai,
                            changed=row[col] in ("True", "true", "1"),
                        )
                lb, la = row.get("lux_before", ""), row.get("lux_after", "")
                result = self.record_reactive(
                    room_id, ts, row["section"], int(row["window_seconds"]),
                    float(lb) if lb not in ("", None) else None,
                    float(la) if la not in ("", None) else None,
                    row.get("occupied") in ("True", "true", "1"),
                    False, groups, list(groups), write_csv=False,
                )
            if result:
                ingested += 1
            else:
                skipped += 1

        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ingest_file(path,kind,room_id,local_date,"
                "content_sha,rows_ingested,rows_skipped,last_ingest_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (str(path), kind, room_id, path.stem.split("_")[-1].split(".")[0],
                 digest, ingested, skipped,
                 datetime.now().astimezone().isoformat()),
            )
            self._conn.commit()
        return (ingested, skipped)

    def rebuild_from_csv(self) -> tuple[int, int]:
        total_in = total_skip = 0
        for room_dir in sorted(self.csv_dir.iterdir()):
            if not room_dir.is_dir():
                continue
            for path in sorted(room_dir.glob("*.csv")):
                a, b = self.ingest_csv(path, room_dir.name)
                total_in += a
                total_skip += b
        return (total_in, total_skip)

    def activity(self, room_id: str, local_date: str) -> dict[str, Any]:
        """Counts and last-seen times for the status view, so 'is it
        actually working' is answerable without opening a CSV."""
        with self._lock:
            hb = self._conn.execute(
                "SELECT COUNT(*), MAX(ts) FROM heartbeat WHERE room_id=? AND local_date=?",
                (room_id, local_date)).fetchone()
            rx = self._conn.execute(
                "SELECT COUNT(*), MAX(ts) FROM reactive WHERE room_id=? AND local_date=?",
                (room_id, local_date)).fetchone()
        return {"heartbeats_today": hb[0], "last_heartbeat": hb[1],
                "reactive_today": rx[0], "last_reactive": rx[1]}

    @property
    def connection(self) -> sqlite3.Connection:
        """For the analyser, which reads directly."""
        return self._conn
