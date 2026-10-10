"""Virtual smart plugs: replayed real recordings (traces.json) or appliance models (ASSUMED values), + fault injection.

This module stands in for the physical house. It only emits readings through `sink(device_id, ts, watts)`,
the same call a real plug's HTTP POST ends in. The fault log lives here and is never exposed to the app API,
so the analyzer cannot be "told" what was injected.
"""
import calendar
import datetime as dt
import functools
import json
import random
import threading
import time
import zlib
from pathlib import Path

START = calendar.timegm(dt.datetime(2026, 9, 1).timetuple())      # sim clock origin
HISTORY_END = calendar.timegm(dt.datetime(2026, 10, 14, 12).timetuple())   # "now" at startup: mid-month

# ASSUMED appliance table (not measured). id: (name, type, class, on-watts, paired at start)
APPLIANCES = {
    "plug-fridge": ("Fridge", "fridge", "must-run", 140, True),
    "plug-aircon": ("Bedroom air cond", "aircon", "adjustable", 1150, True),
    "plug-heater": ("Water heater", "heater", "adjustable", 3200, True),
    "plug-tv": ("TV + router", "tv", "switchable", 95, True),
    "plug-washer": ("Washing machine", "washer", "switchable", 450, False),   # discoverable, not paired yet
}
FAULTS = {"fridge": ["cycling", "drift", "weak", "standby", "offline"], "aircon": ["drift", "longer", "weak", "standby", "offline"],
          "heater": ["overload", "weak", "offline"], "tv": ["longer", "standby", "offline"], "washer": ["offline"]}
# drift: +25% when running. cycling: fridge compressor on more of the time. overload: heater +60%.
# weak: -40% when running (failing element / low refrigerant). standby: +25 W all the time (phantom load).
# longer: starts 3 hours earlier every day. offline: the plug stops reporting.
MODES = ("auto", "on", "off")     # manual control per appliance: follow its schedule, force on, or switch off


@functools.lru_cache(None)
def load_traces(path=Path(__file__).with_name("traces.json")):
    """Real recordings to replay, {appliance type: dict(what, level, thr, w=[watts per minute])}; written by
    `python3 real.py traces`. No file: every plug falls back to its assumed model."""
    return json.loads(path.read_text())["appliances"] if path.exists() else {}


def _hm(ts):
    g = time.gmtime(ts)
    return g.tm_hour + g.tm_min / 60, g.tm_wday, ts // 86400


class Plug:
    """One appliance. `watts(ts)` is called once per simulated minute, in order."""

    def __init__(self, pid, seed, trace=None):
        self.id = pid
        self.name, self.type, self.klass, self.rated, self.paired = APPLIANCES[pid]
        self.trace, self.source = None, "Assumed model, not a measurement"
        if trace:                                 # replay a real recording; switch, power slider and faults act on top of it
            self.trace, self.rated, self.thr = trace["w"], trace["level"], trace["thr"]
            self.source = "Real recording: " + trace["what"]
        self.default, self.mode = self.rated, "auto"
        self.relay = True                         # the plug's own switch, set remotely from the app: open = no power
        self.rng = random.Random(seed)
        self.phase, self.left = "off", 0          # fridge / aircon compressor cycle state
        self.day, self.jit = None, {}

    def _jitter(self, day):
        if day != self.day:                       # new per-day schedule offsets (hours)
            r = random.Random(zlib.crc32(f"{self.id}:{day}".encode()))   # stable across runs (str hash is not)
            self.day, self.jit = day, {k: r.uniform(-0.3, 0.3) for k in ("a", "b", "c")}
        return self.jit

    def _cycle(self, on_len, off_len):
        if self.left <= 0:
            self.phase = "on" if self.phase == "off" else "off"
            m, s = on_len if self.phase == "on" else off_len
            self.left = max(2, round(self.rng.gauss(m, s)))
        self.left -= 1
        return self.phase == "on"

    def watts(self, ts, fault=None):
        h, wd, day = _hm(ts)
        j = self._jitter(day)
        t, R, forced = self.type, self.rated, self.mode == "on"
        if self.trace:                            # the faults mirror real.inject()
            tr = self.trace
            i = (ts - START) // 60 % len(tr)      # a whole number of weeks, so wrapping keeps the weekday
            w = tr[i]
            if fault == "cycling":
                w = max(tr[i:i + 7])                                   # every run lasts 6 minutes longer
            if fault == "longer":
                w = max(w, tr[(i + 180) % len(tr)])                    # starts 3 hours early
            if w > self.thr:
                w *= R / self.default                                  # power slider: scales it while running
            if forced:
                w = max(w, R)
        elif t == "fridge":
            on = self._cycle((18, 2), (14, 2)) if fault == "cycling" else self._cycle((12, 1.5), (22, 3))
            w = R if on else 2
        elif t == "aircon":
            sched = h >= 21.5 + j["a"] - (3 if fault == "longer" else 0) or h < 6.5 + j["b"] or (wd >= 5 and 13 + j["c"] <= h < 16 + j["c"])
            if sched or forced:
                w = R if self._cycle((10, 1), (6, 1)) else 120       # compressor on / fan only
            else:
                w, self.phase, self.left = 3, "off", 0
        elif t == "heater":
            burst = (6.6 + j["a"] <= h < 6.6 + j["a"] + 7 / 60) or (20.2 + j["b"] <= h < 20.2 + j["b"] + 9 / 60)
            w = R if (burst or forced) else 0
            if fault == "overload" and w:
                w *= 1.6
        elif t == "tv":
            tv_on = 19 + j["a"] - (3 if fault == "longer" else 0) <= h < 23.2 + j["b"] or (wd >= 5 and 10 <= h < 12) or forced
            w = 11 + (R if tv_on else 3)                               # router always on + TV / standby
        else:                                                          # washer
            run = (wd in (1, 5) and 9 + j["a"] <= h < 9.9 + j["a"]) or forced
            w = R * (1.6 if run and self.rng.random() < 0.15 else 1) if run else 1
        if fault == "drift":
            w *= 1.25
        if fault == "weak" and w > 15:
            w *= 0.6
        if fault == "standby":
            w += 25
        if self.mode == "off" or not self.relay:   # off at the appliance or at the plug: it still reports, and reports 0 W
            w = 0
        return float(w)


class Simulator:
    def __init__(self, sink, seed=7, traces=None):
        """traces: load_traces() to replay real recordings; None keeps every plug on its assumed model."""
        self.sink, self.lock = sink, threading.Lock()
        self.plugs = {pid: Plug(pid, seed + i, (traces or {}).get(APPLIANCES[pid][1])) for i, pid in enumerate(APPLIANCES)}
        self.rng = random.Random(seed)
        self.t = START
        self.speed, self.paused, self.noise, self.dropout = 60, False, True, False
        self.faults, self.log = {}, []            # device -> fault type ; injection log (sim screen only)
        self.now_w = {}

    def step_minute(self):
        """Advance one simulated minute and emit one reading per plug."""
        for pid, p in self.plugs.items():
            f = self.faults.get(pid)
            w = p.watts(self.t, f)
            if self.noise and not p.trace:        # a recording already carries its meter's own noise
                w = max(0.0, w * (1 + self.rng.gauss(0, 0.02)) + self.rng.gauss(0, 0.3))
            if f == "offline" or (self.dropout and self.rng.random() < 0.05):
                w = None
            self.now_w[pid] = w
            self.sink(pid, self.t, w)
        self.t += 60

    def generate_history(self, until=HISTORY_END):
        while self.t < until:
            self.step_minute()

    def inject(self, pid, fault):
        with self.lock:
            if fault and fault in FAULTS[self.plugs[pid].type]:
                self.faults[pid] = fault
                self.log.insert(0, dict(t=self.t, device=self.plugs[pid].name, fault=fault))
            else:
                if self.faults.pop(pid, None):
                    self.log.insert(0, dict(t=self.t, device=self.plugs[pid].name, fault="cleared"))
            del self.log[50:]

    def control(self, pid, mode=None, rated=None):
        """Manual per-appliance control from the simulation screen."""
        p = self.plugs[pid]
        with self.lock:
            if mode in MODES and mode != p.mode:
                p.mode = mode
                self.log.insert(0, dict(t=self.t, device=p.name, fault="mode:" + mode))
            if isinstance(rated, (int, float)) and not isinstance(rated, bool):
                p.rated = min(max(float(rated), p.default * 0.25), p.default * 2)
                self.log.insert(0, dict(t=self.t, device=p.name, fault=f"power:{round(p.rated)}"))
            del self.log[50:]

    def run(self):
        """Real-time loop: `speed` simulated seconds per real second."""
        acc, last = 0.0, time.monotonic()
        while True:
            time.sleep(0.25)
            now = time.monotonic(); dtm, last = now - last, now
            if self.paused:
                continue
            acc += dtm * self.speed
            with self.lock:
                while acc >= 60:
                    self.step_minute(); acc -= 60
