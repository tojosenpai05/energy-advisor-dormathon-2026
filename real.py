"""The same detectors on REAL appliance recordings instead of the simulator.

Data: AMPds2 (Makonin et al., Scientific Data 2016, CC BY 4.0): one house in Burnaby, Canada, one meter per appliance,
1-minute readings, April 2012 to March 2014. Files expected in ../../data/ampds2/ (see data/DATA.md).
The recordings are real. The faults are still injected by us, onto the real readings.

Split: year 1 is the only year settings may be tuned on; year 2 gives the reported numbers.

  python3 real.py          # year 2 -> real_proof.json (about 10 minutes: also year 1, no-calibration and less-history runs)
  python3 real.py tune     # year 1, prints only
  python3 real.py traces   # cut the slice the live simulation replays -> traces.json (about 15 seconds)
"""
import bisect
import calendar
import csv
import datetime as dt
import functools
import json
import multiprocessing
import random
import statistics
import sys
import zlib
from pathlib import Path
from zoneinfo import ZoneInfo

import core
import evaluate

DAY = 86400
DATA = Path(__file__).resolve().parents[2] / "data" / "ampds2"
DETECTORS = evaluate.DETECTORS
# our appliance type -> (file, what the meter was really on)
TRACES = {"fridge": ("Electricity_FGE.csv", "Kitchen fridge"),
          "aircon": ("Electricity_HPE.csv", "Heat pump (heats and cools; the closest thing to an air conditioner)"),
          "tv": ("Electricity_TVE.csv", "TV + recorder + amplifier"),
          "washer": ("Electricity_CWE.csv", "Clothes washer")}
# No real water heater: the house's hot water unit is gas, its meter only sees a 74 W controller.
FAULTS = {"Left on": ["aircon", "tv"], "Power up 15-60%": ["aircon", "fridge", "tv"], "Drifting up 25%": ["aircon", "fridge"],
          "Fridge working harder": ["fridge"], "Running weak -40%": ["aircon", "fridge"],
          "Standby creep +25 W": ["aircon", "fridge", "tv"], "Runs 3 h longer": ["aircon", "tv"]}
BLOCK, TRAIN, TEST = 42, 35, 7          # days: each 42-day block = up to 35 days of history, then a 7-day test week


@functools.lru_cache(None)
def load(key):
    """[(local_ts, watts)] for one appliance. Timestamps are shifted to local time, as the rest of the app assumes."""
    tz, off, out = ZoneInfo("America/Vancouver"), 0, []
    with open(DATA / TRACES[key][0]) as f:
        r = csv.reader(f); next(r)
        for row in r:
            t = int(row[0])
            if t % 3600 == 0:
                off = int(dt.datetime.fromtimestamp(t, tz).utcoffset().total_seconds())
            if not out or t + off > out[-1][0]:          # the hour repeated when clocks go back is dropped
                out.append((t + off, float(row[6])))     # column P: real power, watts
    return out


def cut(rows, a, b):
    i = bisect.bisect_left(rows, (a,)); return rows[i:bisect.bisect_left(rows, (b,), i)]


def inject(kind, rows, change, B, x=None):
    """Apply a fault to real readings from `change` on. Each mirrors the simulator's version of that fault."""
    w, n = [v for _, v in rows], len(rows)
    f = {"Left on": lambda i: max(w[i], B.level),                                 # held at its running watts
         "Power up 15-60%": lambda i: w[i] * x if w[i] > B.thr else w[i],
         "Drifting up 25%": lambda i: w[i] * 1.25,
         "Fridge working harder": lambda i: max(w[i:i + 7]),                      # every run lasts 6 minutes longer
         "Running weak -40%": lambda i: w[i] * 0.6 if w[i] > 15 else w[i],
         "Standby creep +25 W": lambda i: w[i] + 25,
         "Runs 3 h longer": lambda i: max(w[i], w[min(i + 180, n - 1)])}[kind]    # starts 3 hours early
    return [(t, f(i) if t >= change else v) for i, (t, v) in enumerate(rows)]


def run(m, tail, test):
    """{detector: [time each alarm started]} over `test`, with the same rules as evaluate.run_all."""
    rows = tail + test
    ts = [t for t, _ in rows]
    prev, alarm, state = dict.fromkeys(DETECTORS, False), dict.fromkeys(DETECTORS, False), {}
    on = {d: [] for d in DETECTORS}
    for i in range(len(tail), len(rows)):
        now = ts[i] + 60
        if (now // 60) % 5:
            continue
        r = rows[bisect.bisect_right(ts, now - DAY, 0, i):i + 1]
        for d, c in evaluate.conditions(m, r, now, state).items():
            a = c and prev[d]                                    # two checks in a row
            prev[d] = c
            if a and not alarm[d]:
                on[d].append(now)
            alarm[d] = a
    return on


def block(args):
    """One appliance, one test week: a clean pass (nothing injected), then one pass per applicable fault."""
    key, year, k, train_days, calibrate = args
    rows = load(key)
    t0 = rows[0][0] + (year * 365 + k * BLOCK + TRAIN) * DAY                # start of the test week
    seed = zlib.crc32(f"{key}:{year}:{k}".encode())
    rng = random.Random(seed)
    train = cut(rows, t0 - train_days * DAY, t0)
    m = evaluate.models(train, seed, core.learn if calibrate else core.Baseline)
    ref = core.Baseline(cut(rows, t0 - TRAIN * DAY, t0))                    # the injector's idea of "running", same for any train_days
    test = cut(rows, t0, t0 + TEST * DAY)
    clean = run(m, train[-1500:], test)
    out = dict(key=key, clean={d: len(v) for d, v in clean.items()}, faults=[])
    for kind, keys in FAULTS.items():
        if key in keys:
            change = t0 + DAY + rng.randrange(24) * 3600
            x = round(rng.uniform(1.15, 1.6), 2)
            seg = inject(kind, cut(rows, t0, change + 2 * DAY + 3 * 3600), change, ref, x)
            hit = run(m, train[-1500:], cut(seg, t0, change + 2 * DAY))
            win = lambda v: [t for t in v if change <= t]
            out["faults"].append(dict(kind=kind, first={d: round((win(v)[0] - change) / 60) if win(v) else None for d, v in hit.items()},
                                      anyway={d: any(change <= t < change + 2 * DAY for t in clean[d]) for d in DETECTORS}))
    return out


def measure(year, train_days=TRAIN, blocks=8, calibrate=True):
    tasks = [(key, year, k, train_days, calibrate) for key in TRACES for k in range(blocks)]
    with multiprocessing.Pool() as pool:
        res = pool.map(block, tasks)
    days = blocks * TEST
    out = dict(year=year + 1, train_days=train_days, clean_days_per_appliance=days, detectors={},
               appliances={k: v[1] for k, v in TRACES.items()})
    for d in DETECTORS:
        f = [x for r in res for x in r["faults"]]
        hits = [x["first"][d] for x in f if x["first"][d] is not None]
        kinds = {}
        for x in f:
            a = kinds.setdefault(x["kind"], [0, 0]); a[0] += x["first"][d] is not None; a[1] += 1
        false = {key: sum(r["clean"][d] for r in res if r["key"] == key) for key in TRACES}
        out["detectors"][d] = dict(
            caught=len(hits), total=len(f), median_minutes=round(statistics.median(hits)) if hits else None,
            # the untouched recording also raised an alarm in the same two days: that catch may be luck
            caught_but_clean_alarmed_too=sum(x["first"][d] is not None and x["anyway"][d] for x in f),
            false_per_day=round(sum(false.values()) / (days * len(TRACES)), 3),
            false_per_day_by_appliance={k: round(v / days, 3) for k, v in false.items()}, by_kind=kinds)
    return out


def show(r):
    print(f"year {r['year']}, {r['train_days']} days of history, {r['clean_days_per_appliance']} untouched days per appliance")
    for d, v in r["detectors"].items():
        print(f"{d:28s} caught {v['caught']:3d}/{v['total']} (clean alarmed too: {v['caught_but_clean_alarmed_too']:2d})  "
              f"median {str(v['median_minutes']):>5s} min  false alerts/day {v['false_per_day']:.3f}  "
              + " ".join(f"{k}:{x}" for k, x in v["false_per_day_by_appliance"].items()))
        print("     " + "  ".join(f"{k}: {a}/{b}" for k, (a, b) in v["by_kind"].items()))


def export(start=dt.datetime(2013, 11, 12), weeks=19, path=Path(__file__).resolve().parent / "traces.json"):
    """Cut the slice of each recording that sim.py replays: one value per local minute, written to traces.json.
    The start is a Tuesday like sim.START, so weekdays line up, and it is winter: the heat pump hardly runs in summer.
    It is in year 2, the year the reported numbers come from. Untouched, the app raises 2 alerts in the 30 days after
    the simulation's starting point (first on day 15); the year-1 slice from 2012-10-30 raised 8."""
    a = calendar.timegm(start.timetuple())
    out = dict(source="AMPds2 (Makonin et al., Scientific Data 2016, CC BY 4.0): one house in Burnaby, Canada",
               start=start.date().isoformat(), weeks=weeks, appliances={})
    for key, (_, what) in TRACES.items():
        rows = cut(load(key), a, a + weeks * 7 * DAY)
        B, d, last, w = core.Baseline(rows), dict(rows), 0.0, []
        for t in range(a, a + weeks * 7 * DAY, 60):          # the hour skipped when clocks go forward repeats the last value
            last = d.get(t, last); w.append(round(last))
        out["appliances"][key] = dict(what=what, level=round(B.level), thr=round(B.thr), w=w)
    path.write_text(json.dumps(out, separators=(",", ":")))
    print({k: (v["what"], v["level"], len(v["w"])) for k, v in out["appliances"].items()})


if __name__ == "__main__":
    if sys.argv[1:] == ["traces"]:
        export()
    elif sys.argv[1:] == ["tune"]:
        show(measure(0))
    else:
        r = measure(1)
        both = "statistics + neural network"
        r["tuning_year"] = measure(0)["detectors"]                              # year 1: settings were chosen on this one
        r["without_calibration"] = measure(1, calibrate=False)["detectors"]     # year 2 with the checks as first written
        r["learning"] = {d: measure(1, d)["detectors"][both] for d in (3, 7, 14, 21)}   # same test weeks, less history
        (Path(__file__).resolve().parent / "real_proof.json").write_text(json.dumps(r, indent=1))
        show(r)
        for label, v in [("year 1 (tuning year)", r["tuning_year"][both]), ("year 2 without calibration", r["without_calibration"][both])] + \
                        [(f"year 2 with {d} days of history", v) for d, v in r["learning"].items()]:
            print(f"{label:32s} caught {v['caught']:3d}/{v['total']} (clean alarmed too: {v['caught_but_clean_alarmed_too']:2d})  false alerts/day {v['false_per_day']:.3f}")
