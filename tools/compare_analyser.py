"""Run the legacy and current analyser over the same heartbeat CSV and
diff the resulting almanacs.

    python tools/compare_analyser.py path/to/heartbeat_YYYY-MM-DD.csv

Used to prove that a change to the learning maths does what was intended
and nothing else. Accepts the historical fixed-4-group CSV format, which
predates the section column, so sections are assigned from assumed sun
times - fine for comparing two analysers against identical input, not a
statement about any real installation.
"""
import csv, sqlite3, sys
from datetime import date
from app.analyser import analyse_room, LearningConfig, AnalyserFlags

BOUNDS = [("sunrise","05:30"),("day","10:00"),("afternoon","16:00"),
          ("sunset","19:00"),("night","20:30"),("sleep","22:00")]
GROUPS = ["tv_light","play_area","dining_area","bar_lights"]
LEGACY = AnalyserFlags(per_group_eligibility=False,
                       per_group_stability=False,
                       brightness_requires_measurable_lux=True)

def section_for(hhmm):
    cur = "sleep"
    for name, start in BOUNDS:
        if hhmm >= start:
            cur = name
    return cur if hhmm >= "05:30" else "sleep"

def main(path, schema="schema/storage.schema.sql"):
    conn = sqlite3.connect(":memory:")
    conn.executescript(open(schema).read())
    for r in csv.DictReader(open(path)):
        ts = r["timestamp"]
        cur = conn.execute(
            "INSERT INTO heartbeat(room_id,ts,ts_utc,local_date,section,ambient_lux,"
            "lux_sensor_n,occupied,any_light_on) VALUES(?,?,?,?,?,?,1,?,?)",
            ("main_room", ts, ts, ts[:10], section_for(ts[11:16]),
             float(r["ambient_lux"]),
             1 if r["motion_detected"] == "True" else 0,
             1 if r["any_light_on"] == "True" else 0))
        for i, g in enumerate(GROUPS, start=1):
            b = int(r[f"group_{i}_brightness"])
            conn.execute("INSERT INTO heartbeat_group VALUES(?,?,?,?)",
                         (cur.lastrowid, g, 1 if b > 0 else 0, b))
    conn.commit()

    scene_cfg = {s: {"lux_margin": 5, "max_step_pct": 0.04,
                     "maintenance_enabled": s not in ("night", "sleep"),
                     "groups": {g: {"mode": "auto"} for g in GROUPS}}
                 for s, _ in BOUNDS}

    A = analyse_room(conn, "main_room", GROUPS, scene_cfg,
                     LearningConfig(flags=LEGACY), date.today())
    B = analyse_room(conn, "main_room", GROUPS, scene_cfg,
                     LearningConfig(), date.today())

    print(f"{'section':<10} {'group':<12} {'legacy':>8} {'current':>8} {'on_frac':>8}")
    print("-" * 52)
    for s, _ in BOUNDS:
        a, b = A.get(s), B.get(s)
        if not b:
            print(f"{s:<10} (no data)")
            continue
        print(f"{s:<10} {'LUX TARGET':<12} "
              f"{str(a['lux_target'] if a else '-'):>8} {str(b['lux_target']):>8}")
        for g in GROUPS:
            of = b["on_fraction"][g]
            print(f"{'':<10} {g:<12} {str(a[g] if a else '-'):>8} {str(b[g]):>8} "
                  f"{('' if of is None else format(of, '.0%')):>8}")

if __name__ == "__main__":
    main(sys.argv[1])
