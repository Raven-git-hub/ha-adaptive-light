-- =====================================================================
-- Adaptive Light - SQLite storage schema (v1)
--
-- CSV on disk is the archival source of truth; this database is the
-- query layer for the analyser and the UI. It is always rebuildable
-- from CSV, so the container holds no irreplaceable state.
--
-- Design notes:
--   * Group readings are stored LONG, not wide. Rooms have a variable
--     number of groups, so the old fixed group_1..group_4 columns
--     cannot survive. Long format also makes "average this group only
--     over samples where it was on" a WHERE clause rather than a
--     special case.
--   * Section is RECORDED at observation time, never inferred later
--     from the clock. This removes the seasonal misfiling that hard-
--     coded hour boundaries caused.
--   * All timestamps are ISO-8601 with UTC offset, stored as TEXT,
--     exactly as written to CSV. Sorting is lexicographic and correct
--     within a fixed offset; ts_utc is carried alongside for safety
--     across DST or relocation.
-- =====================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------------
-- Config snapshots
-- The live config is a JSON file; this table keeps every version that
-- was ever active, so a historical row can always be interpreted with
-- the config that produced it (e.g. what light.x was called last month).
-- ---------------------------------------------------------------------
CREATE TABLE config_version (
    id            INTEGER PRIMARY KEY,
    applied_at    TEXT    NOT NULL,
    payload       TEXT    NOT NULL,          -- full config JSON
    payload_sha   TEXT    NOT NULL UNIQUE
);


-- ---------------------------------------------------------------------
-- Section runs
-- One row per room per section per day, written at crossover.
-- Records what was PLANNED versus what ACTUALLY happened, so a collapsed
-- or missed section is visible rather than merely absent.
-- ---------------------------------------------------------------------
CREATE TABLE section_run (
    id              INTEGER PRIMARY KEY,
    room_id         TEXT    NOT NULL,
    local_date      TEXT    NOT NULL,        -- YYYY-MM-DD, local
    section         TEXT    NOT NULL
                    CHECK (section IN ('sunrise','day','afternoon','sunset','night','sleep')),
    planned_start   TEXT,                    -- computed boundary
    actual_start    TEXT,                    -- when the scene was really fired
    ended_at        TEXT,
    outcome         TEXT    NOT NULL DEFAULT 'ran'
                    CHECK (outcome IN ('ran','collapsed','missed','caught_up')),
    outcome_reason  TEXT,                    -- e.g. 'priority: night(100) > sunset(50)'
    UNIQUE (room_id, local_date, section)
);
CREATE INDEX idx_section_run_lookup ON section_run(room_id, local_date);


-- ---------------------------------------------------------------------
-- Heartbeats - room level
-- ---------------------------------------------------------------------
CREATE TABLE heartbeat (
    id              INTEGER PRIMARY KEY,
    room_id         TEXT    NOT NULL,
    ts              TEXT    NOT NULL,        -- local ISO-8601 with offset
    ts_utc          TEXT    NOT NULL,
    local_date      TEXT    NOT NULL,
    section         TEXT    NOT NULL,        -- recorded, not inferred
    ambient_lux     REAL,                    -- NULL if every lux sensor was unavailable
    lux_sensor_n    INTEGER NOT NULL DEFAULT 0,   -- how many sensors contributed to the mean
    occupied        INTEGER NOT NULL,        -- 1 if any presence sensor on, or room has none configured
    any_light_on    INTEGER NOT NULL,
    deferred_ms     INTEGER NOT NULL DEFAULT 0,   -- >0 if delayed waiting for the guard to clear
    UNIQUE (room_id, ts)
);
CREATE INDEX idx_heartbeat_scan ON heartbeat(room_id, local_date, section);
CREATE INDEX idx_heartbeat_time ON heartbeat(room_id, ts_utc);


-- ---------------------------------------------------------------------
-- Heartbeats - per group
-- is_on is stored explicitly rather than derived from brightness>0,
-- because a light can legitimately be on at brightness 1.
-- ---------------------------------------------------------------------
CREATE TABLE heartbeat_group (
    heartbeat_id    INTEGER NOT NULL REFERENCES heartbeat(id) ON DELETE CASCADE,
    group_id        TEXT    NOT NULL,
    is_on           INTEGER NOT NULL,
    brightness      INTEGER CHECK (brightness BETWEEN 0 AND 255),  -- NULL if unavailable
    PRIMARY KEY (heartbeat_id, group_id)
);
CREATE INDEX idx_hbgroup_group ON heartbeat_group(group_id, is_on);


-- ---------------------------------------------------------------------
-- Reactive events - room level
-- One row per consolidated window, not per state change.
-- ---------------------------------------------------------------------
CREATE TABLE reactive (
    id                INTEGER PRIMARY KEY,
    room_id           TEXT    NOT NULL,
    ts                TEXT    NOT NULL,      -- start of window
    ts_utc            TEXT    NOT NULL,
    local_date        TEXT    NOT NULL,
    section           TEXT    NOT NULL,
    window_seconds    INTEGER NOT NULL,
    lux_before        REAL,
    lux_after         REAL,
    occupied          INTEGER NOT NULL,      -- recorded for information only:
                                             -- reactive events are NEVER filtered on
                                             -- occupancy, because a human touching a
                                             -- dimmer is definitive proof of presence
    suspended_maint   INTEGER NOT NULL DEFAULT 0,   -- did this set the hold boolean
    UNIQUE (room_id, ts)
);
CREATE INDEX idx_reactive_scan ON reactive(room_id, local_date, section);


CREATE TABLE reactive_group (
    reactive_id         INTEGER NOT NULL REFERENCES reactive(id) ON DELETE CASCADE,
    group_id            TEXT    NOT NULL,
    is_on_before        INTEGER NOT NULL,
    is_on_after         INTEGER NOT NULL,
    brightness_before   INTEGER CHECK (brightness_before BETWEEN 0 AND 255),
    brightness_after    INTEGER CHECK (brightness_after  BETWEEN 0 AND 255),
    changed             INTEGER NOT NULL,    -- passed system.reactive_min_delta
    PRIMARY KEY (reactive_id, group_id)
);


-- ---------------------------------------------------------------------
-- Almanacs
-- Full published payload kept verbatim for audit; scene/group rows
-- normalised so the UI can chart target history without JSON parsing.
-- ---------------------------------------------------------------------
CREATE TABLE almanac (
    id              INTEGER PRIMARY KEY,
    room_id         TEXT    NOT NULL,
    generated_at    TEXT    NOT NULL,
    valid_from      TEXT    NOT NULL,        -- local date this becomes active
    mode            TEXT    NOT NULL
                    CHECK (mode IN ('provisional','bootstrap','learning')),
    days_analysed   INTEGER NOT NULL DEFAULT 0,
    published_at    TEXT,                    -- when pushed to HA; NULL = not yet live
    payload         TEXT    NOT NULL,        -- exact JSON pushed to sensor.al_almanac_<room>
    UNIQUE (room_id, valid_from, generated_at)
);
CREATE INDEX idx_almanac_active ON almanac(room_id, valid_from DESC);


CREATE TABLE almanac_scene (
    almanac_id            INTEGER NOT NULL REFERENCES almanac(id) ON DELETE CASCADE,
    section               TEXT    NOT NULL,
    lux_target            REAL,
    lux_margin            REAL    NOT NULL,
    max_step_pct          REAL    NOT NULL,
    maintenance_enabled   INTEGER NOT NULL,
    days_contributing     INTEGER NOT NULL DEFAULT 0,
    high_confidence_days  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (almanac_id, section)
);


CREATE TABLE almanac_group (
    almanac_id      INTEGER NOT NULL REFERENCES almanac(id) ON DELETE CASCADE,
    section         TEXT    NOT NULL,
    group_id        TEXT    NOT NULL,
    mode            TEXT    NOT NULL CHECK (mode IN ('auto','off')),
    brightness      INTEGER,                 -- NULL = leave the light exactly as it is
    on_fraction     REAL,                    -- share of eligible samples where it was on;
                                             -- informational, surfaced in the UI so the user
                                             -- can decide to force a rarely-used light off
    sample_weight   REAL    NOT NULL DEFAULT 0,   -- per-group divisor: only on-samples count
    PRIMARY KEY (almanac_id, section, group_id)
);


-- ---------------------------------------------------------------------
-- Event log - the visible record of what the system did and when
-- ---------------------------------------------------------------------
CREATE TABLE event (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    ts_utc      TEXT    NOT NULL,
    room_id     TEXT,                        -- NULL for system-wide events
    severity    TEXT    NOT NULL
                CHECK (severity IN ('debug','info','warning','error')),
    category    TEXT    NOT NULL
                CHECK (category IN (
                    'scene_change',      -- crossover fired, with computed boundaries
                    'scene_collapsed',   -- section skipped, with the reason
                    'maintenance',       -- nudge observed during a guard window
                    'reactive',          -- user intervention captured
                    'hold',              -- maintenance suspended / released
                    'heartbeat',         -- deferrals and gaps (debug level normally)
                    'analysis',          -- analyser run, inputs and outcome
                    'almanac',           -- generated / published to HA
                    'connection',        -- HA websocket up, down, reconnecting
                    'deploy',            -- helpers and automations written
                    'config',            -- config changed, by whom
                    'validation'         -- missing or unavailable entity detected
                )),
    message     TEXT    NOT NULL,            -- human-readable, shown in the UI
    detail      TEXT                         -- optional JSON payload for expansion
);
CREATE INDEX idx_event_feed ON event(ts_utc DESC);
CREATE INDEX idx_event_filter ON event(room_id, category, severity, ts_utc DESC);


-- ---------------------------------------------------------------------
-- CSV ingest ledger
-- Makes re-ingestion idempotent and lets the UI show whether the
-- database is a faithful reflection of what is on disk.
-- ---------------------------------------------------------------------
CREATE TABLE ingest_file (
    path            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('heartbeat','reactive')),
    room_id         TEXT NOT NULL,
    local_date      TEXT NOT NULL,
    content_sha     TEXT NOT NULL,
    rows_ingested   INTEGER NOT NULL DEFAULT 0,
    rows_skipped    INTEGER NOT NULL DEFAULT 0,
    last_ingest_at  TEXT NOT NULL
);


-- ---------------------------------------------------------------------
-- Convenience view: the almanac currently in force per room
-- ---------------------------------------------------------------------
CREATE VIEW current_almanac AS
SELECT a.*
FROM almanac a
JOIN (
    SELECT room_id, MAX(valid_from) AS vf
    FROM almanac
    WHERE valid_from <= date('now','localtime')
    GROUP BY room_id
) latest
  ON latest.room_id = a.room_id AND latest.vf = a.valid_from;
