"""Replay harness: run the analyzer over simulated days, with or without a change it is not told about.
Used by the tests and by the Proof page. Simulation only: this measures the pipeline, not real-world accuracy."""
import bisect

import core
import sim as simmod

DAY = 86400


def replay(device=None, fault=None, mode=None, rated=None, seed=7, train_days=35, clean_days=3, fault_days=2,
           noise=True, dropout=False, analyze=core.analyze):
    """Returns dict(opened=[(device, kind, minutes_after_change)], false=[...], counts=..., change_at=ts)."""
    rows = {p: [] for p in simmod.APPLIANCES}
    ts = {p: [] for p in simmod.APPLIANCES}

    def sink(d, t, w):
        if w is not None:
            rows[d].append((t, w)); ts[d].append(t)

    s = simmod.Simulator(sink, seed); s.noise, s.dropout = noise, dropout
    s.generate_history(simmod.START + train_days * DAY)
    base = {p: core.Baseline(rows[p]) for p in rows}
    inc, change_at, end = core.Incidents(), s.t + clean_days * DAY, s.t + (clean_days + fault_days) * DAY
    seen, changed = {}, False
    while s.t < end:
        if not changed and s.t >= change_at and device:
            changed = True
            if fault:
                s.faults[device] = fault
            if mode or rated:
                s.control(device, mode, rated)
        s.step_minute()
        if (s.t // 60) % 5 == 0:
            for p in rows:
                lo = bisect.bisect_left(ts[p], s.t - DAY)
                inc.update(p, p, analyze(rows[p][lo:], base[p], s.t, inc.kinds(p)), s.t, 0.2)
            for i in inc.open.values():
                seen.setdefault(i["id"], (i["device"], i["kind"], i["opened"]))
    opened = [(d, k, round((t - change_at) / 60)) for d, k, t in seen.values()]
    return dict(opened=[o for o in opened if o[2] >= 0 and o[0] == device], change_at=change_at,
                false=[o for o in opened if o[2] < 0 or o[0] != device], counts=inc.view()["counts"])


SCENARIOS = [  # (label, device, kwargs, expected kind)
    ("Air cond left on", "plug-aircon", dict(mode="on"), "unusual_time"),
    ("Air cond drifting up 25%", "plug-aircon", dict(fault="drift"), "drift"),
    ("Fridge working harder", "plug-fridge", dict(fault="cycling"), "duty"),
    ("Fridge drifting up 25%", "plug-fridge", dict(fault="drift"), "drift"),
    ("Water heater overload", "plug-heater", dict(fault="overload"), "overload"),
    ("Water heater left on", "plug-heater", dict(mode="on"), "unusual_time"),
    ("TV left on", "plug-tv", dict(mode="on"), "unusual_time"),
    ("TV plug goes offline", "plug-tv", dict(fault="offline"), "offline"),
]

# ---------------------------------------------------------------------------------------------
# Comparison: the same randomized changes, four detectors, one pass. None of them is told what changed.
# ---------------------------------------------------------------------------------------------
import copy
import json
import random
import statistics
import time
from pathlib import Path

import learned

DETECTORS = ["fixed watt limit", "simple statistics", "nearest past pattern", "small neural network",
             "statistics + neural network"]
NN_HOURS = learned.NN_HOURS

DEVICES = ["plug-fridge", "plug-aircon", "plug-heater", "plug-tv"]
_prep = {}


def models(rows, seed=0, learn=core.learn):
    """Baseline, fixed limit and the two learned models for one appliance's history [(ts, watts)]."""
    scale, tr = learned.training_set(rows)
    ok = tr and tr[-1][0] - tr[0][0] >= 3 * DAY       # the learned models need about 4 days before they can set a threshold
    return dict(base=learn(rows), limit=1.15 * max(w for _, w in rows), scale=scale,
                nn=learned.NearestPattern(tr) if ok else None, ae=learned.TinyAutoencoder(tr, seed=seed) if ok else None)


def conditions(m, r, now, state):
    """What each detector thinks of one appliance right now. r: its last 24 h of rows; state: a dict kept per appliance.
    The learned models only look once an hour, so their keys are absent in between."""
    c = {"fixed watt limit": sum(w > m["limit"] for _, w in r[-15:]) >= 3,      # 3+ readings over the limit in 15 min
         "simple statistics": False}
    since, hold = state.setdefault("since", {}), m["base"].hold
    found = {k for k, _, _ in core.analyze(r, m["base"], now)} - {"offline"}
    for k in [k for k in since if k not in found]:
        del since[k]
    for k in found:                                   # a named check counts once it has lasted as long as that appliance needs
        c["simple statistics"] |= now - since.setdefault(k, now) >= hold.get(k, core.HOLD_S) - core.HOLD_S
    if (now // 60) % 60 == 0 and m["ae"]:
        v = learned.profile(r, now, m["scale"])
        c["nearest past pattern"] = m["nn"].score(v) > m["nn"].thr
        odd = c["small neural network"] = m["ae"].score(v) > m["ae"].thr
        state["streak"] = state.get("streak", 0) + 1 if odd else 0
    c["statistics + neural network"] = c["simple statistics"] or state.get("streak", 0) >= NN_HOURS
    return c


def prepare(seed, train_days=35):
    """History, baselines and trained models for one simulated house (cached)."""
    if (seed, train_days) not in _prep:
        rows = {p: [] for p in simmod.APPLIANCES}
        s = simmod.Simulator(lambda d, t, w: w is not None and rows[d].append((t, w)), seed)
        s.generate_history(simmod.START + train_days * DAY)
        _prep[seed, train_days] = (s, {p: rows[p][-1500:] for p in rows}, {p: models(rows[p], seed) for p in DEVICES})
    return _prep[seed, train_days]


def run_all(seed, device=None, fault=None, mode=None, rated_x=None, offset_h=0, clean_days=1, fault_days=2, train_days=35):
    """One scenario through all four detectors. Returns {detector: dict(first=minutes or None, false=count)}."""
    s0, tail, m = prepare(seed, train_days)
    rows = {p: list(tail[p]) for p in tail}
    s = simmod.Simulator(lambda d, t, w: w is not None and rows[d].append((t, w)), seed)
    s.plugs, s.t = copy.deepcopy(s0.plugs), s0.t
    s.rng.setstate(s0.rng.getstate())
    change_at = s.t + clean_days * DAY + offset_h * 3600
    end, changed = change_at + fault_days * DAY, False
    prev = {(d, p): False for d in DETECTORS for p in DEVICES}       # condition true at the previous check
    alarm = {(d, p): False for d in DETECTORS for p in DEVICES}      # currently alarming (after persistence)
    res = {d: dict(first=None, false=0) for d in DETECTORS}
    state = {p: {} for p in DEVICES}

    def step(det, p, cond):
        on = cond and prev[(det, p)]                                 # same rule for everyone: two checks in a row
        prev[(det, p)] = cond
        if on and not alarm[(det, p)]:
            if changed and p == device:
                if res[det]["first"] is None:
                    res[det]["first"] = round((s.t - change_at) / 60)
            else:
                res[det]["false"] += 1
        alarm[(det, p)] = on

    while s.t < end:
        if not changed and s.t >= change_at and device:
            changed = True
            if fault:
                s.faults[device] = fault
            if mode or rated_x:
                s.control(device, mode, s.plugs[device].default * rated_x if rated_x else None)
        s.step_minute()
        if (s.t // 60) % 5 == 0:
            for p in DEVICES:
                r = [x for x in rows[p][-1500:] if x[0] > s.t - DAY]
                for det, cond in conditions(m[p], r, s.t, state[p]).items():
                    step(det, p, cond)
    return res


def scenarios(n, seed=1):
    rng = random.Random(seed)
    kinds = [("Left on", ["plug-aircon", "plug-heater", "plug-tv"], lambda: dict(mode="on")),
             ("Power up 15-60%", ["plug-aircon", "plug-fridge", "plug-tv"], lambda: dict(rated_x=round(rng.uniform(1.15, 1.6), 2))),
             ("Drifting up 25%", ["plug-aircon", "plug-fridge"], lambda: dict(fault="drift")),
             ("Fridge working harder", ["plug-fridge"], lambda: dict(fault="cycling")),
             ("Heater overload", ["plug-heater"], lambda: dict(fault="overload")),
             # the three below were NOT anticipated when the statistics rules were written
             ("Running weak -40% (new)", ["plug-aircon", "plug-fridge", "plug-heater"], lambda: dict(fault="weak")),
             ("Standby creep +25 W (new)", ["plug-aircon", "plug-fridge", "plug-tv"], lambda: dict(fault="standby")),
             ("Runs 3 h longer (new)", ["plug-aircon", "plug-tv"], lambda: dict(fault="longer"))]
    out = []
    for i in range(n):
        label, devs, kw = kinds[i % len(kinds)]
        out.append(dict(label=label, seed=100 + i % 4, device=rng.choice(devs), offset_h=rng.randrange(24), **kw()))
    return out


def compare(n=64, clean_runs=4, clean_days=6):
    t0 = time.time()
    per = {d: {} for d in DETECTORS}
    false_n = {d: 0 for d in DETECTORS}
    for sc in scenarios(n):
        label = sc.pop("label")
        r = run_all(**sc)
        for d in DETECTORS:
            per[d].setdefault(label, []).append(r[d]["first"]); false_n[d] += r[d]["false"]
    clean = {d: 0 for d in DETECTORS}
    for i in range(clean_runs):
        r = run_all(100 + i, clean_days=clean_days, fault_days=0)
        for d in DETECTORS:
            clean[d] += r[d]["false"]
    days = clean_runs * clean_days
    out = dict(scenarios=n, clean_days=days, detectors={}, seconds=None, labels=sorted({l for d in per.values() for l in d}))
    for d in DETECTORS:
        hits = [x for v in per[d].values() for x in v if x is not None]
        out["detectors"][d] = dict(
            caught=len(hits), total=n, median_minutes=round(statistics.median(hits)) if hits else None,
            false_during_scenarios=false_n[d], false_clean=clean[d], false_per_day=round(clean[d] / days, 2),
            by_kind={l: [sum(x is not None for x in v), len(v)] for l, v in per[d].items()})
    out["seconds"] = round(time.time() - t0)
    return out


if __name__ == "__main__":
    import sys
    r = compare(int(sys.argv[1]) if len(sys.argv) > 1 else 64)
    (Path(__file__).resolve().parent / "proof.json").write_text(json.dumps(r, indent=1))
    print(f"{r['scenarios']} randomized changes, {r['clean_days']} clean days, {r['seconds']} s")
    for d, v in r["detectors"].items():
        print(f"{d:22s} caught {v['caught']:2d}/{v['total']}  median {str(v['median_minutes']):>5s} min  false alarms/clean day {v['false_per_day']:.2f}  during scenarios {v['false_during_scenarios']}")
        print("   " + "  ".join(f"{k}: {a}/{b}" for k, (a, b) in v["by_kind"].items()))
