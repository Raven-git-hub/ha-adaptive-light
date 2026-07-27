"""Year sweep: how often does each section collapse, and when."""
import json, os, sys
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from app.scheduler import AstralSunProvider, compute_day

SITES = [
    ("Equatorial  (1.35N)",  1.35, 103.82, "Asia/Singapore"),
    ("Perth      (31.95S)", -31.95, 115.86, "Australia/Perth"),
    ("London     (51.51N)",  51.51,  -0.13, "Europe/London"),
    ("Helsinki   (60.17N)",  60.17,  24.94, "Europe/Helsinki"),
    ("Tromso     (69.65N)",  69.65,  18.96, "Europe/Oslo"),
]
sched = json.load(open(os.environ.get("AL_CONFIG", "examples/config.example.json")))["schedule"]

print(f"{'site':<22} {'collapsed section-days out of 2190':<38}")
print("-"*78)
for label, lat, lon, tz in SITES:
    tzi = ZoneInfo(tz)
    prov = AstralSunProvider(lat, lon, tzi)
    counts, spans = defaultdict(int), defaultdict(list)
    d = date(2026,1,1)
    while d <= date(2026,12,31):
        for b in compute_day(sched, prov, d, tzi):
            if not b.ran:
                counts[b.section] += 1
                spans[b.section].append(d)
        d += timedelta(days=1)
    if not counts:
        print(f"{label:<22} none")
    else:
        parts = []
        for s, n in sorted(counts.items(), key=lambda x:-x[1]):
            lo, hi = spans[s][0], spans[s][-1]
            parts.append(f"{s} x{n} ({lo:%d %b}-{hi:%d %b})")
        print(f"{label:<22} " + "; ".join(parts))
