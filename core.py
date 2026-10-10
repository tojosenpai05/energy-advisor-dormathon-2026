"""Bill logic for the energy advisor: Sarawak Energy domestic tariff, month estimate, comparison.

Tariff rule (confirmed from Sarawak Energy's own worked examples, see PLAN.md): the rate of the band your
monthly total falls in applies to ALL units, so crossing a band boundary is a cliff.
"""
import bisect
import calendar
import time

# (upper bound in kWh, RM per kWh). Tariff D-Domestic, sarawakenergy.com/customers/tariffs
BANDS = [(150, 0.18), (200, 0.22), (300, 0.25), (400, 0.27), (500, 0.295),
         (700, 0.30), (800, 0.305), (1300, 0.31), (float("inf"), 0.315)]
MIN_CHARGE = 5.00
DISCOUNT_2026 = 0.25          # domestic, April-December 2026 consumption (Sarawak Energy, 19 Apr 2026)
TAX_RATE, TAX_FREE_KWH = 0.06, 600   # from a 2020 FAQ; current rate UNVERIFIED (only matters above 600 kWh)

# Mean absolute % error of "usage so far scaled to the month", measured on a real household
# (UCI, 22 test months; research/07-bill-forecast-check.md). Day of month -> % error.
MEASURED_ERR = [(7, 12.5), (10, 10.2), (15, 8.3), (20, 4.8)]


def rate(kwh):
    return next(r for top, r in BANDS if kwh <= top)


def bill(kwh, discount=0.0):
    """RM for a month's kWh. ponytail: assumes the discount applies to the energy charge and the RM5
    minimum applies before it; only the energy-charge and tax parts are confirmed by published examples."""
    r = rate(kwh)
    energy = max(kwh * r, MIN_CHARGE)
    tax = TAX_RATE * max(kwh - TAX_FREE_KWH, 0) * r
    return round(energy * (1 - discount) + tax, 2)


def kwh_for_budget(rm, discount=0.0):
    """Largest whole kWh whose bill is within the budget (bill() only rises with usage, so scan up)."""
    k = 0
    while k < 5000 and bill(k + 1, discount) <= rm:
        k += 1
    return k


def band_watch(kwh, discount=0.0):
    """Nearest band boundary below the projected usage: how much to cut, and what it saves."""
    lower = [top for top, _ in BANDS if top < kwh]
    if not lower:
        return None
    top = lower[-1]
    return dict(limit=top, cut_kwh=round(kwh - top, 1), saving=round(bill(kwh, discount) - bill(top, discount), 2),
                rate_now=rate(kwh), rate_below=rate(top))


def err_pct(day):
    """Expected error of the month estimate on a given day. Measured between day 7 and 20;
    held flat before day 7 and tapered to 0 at month end (ASSUMED outside the measured range)."""
    pts = MEASURED_ERR
    if day <= pts[0][0]:
        return pts[0][1]
    for (d0, e0), (d1, e1) in zip(pts, pts[1:]):
        if day <= d1:
            return e0 + (e1 - e0) * (day - d0) / (d1 - d0)
    return max(0.0, pts[-1][1] * (31 - day) / (31 - pts[-1][0]))


def month_bounds(ts):
    g = time.gmtime(ts)
    start = calendar.timegm((g.tm_year, g.tm_mon, 1, 0, 0, 0))
    days = calendar.monthrange(g.tm_year, g.tm_mon)[1]
    py, pm = (g.tm_year, g.tm_mon - 1) if g.tm_mon > 1 else (g.tm_year - 1, 12)
    return start, days, calendar.timegm((py, pm, 1, 0, 0, 0)), calendar.monthrange(py, pm)[1]


def band_above(kwh, discount=0.0):
    """Nearest band boundary at or above the projected usage: how far away, and what one unit past it costs."""
    top = next(t for t, _ in BANDS if kwh <= t)
    if top == float("inf"):
        return None
    return dict(limit=top, away_kwh=round(top - kwh, 1), rate_above=rate(top + 1),
                rm_at=bill(top, discount), rm_over=bill(top + 1, discount))


def bill_view(now, devices, kwh_between, discount):
    """Everything the Bill page shows. `kwh_between(device_id, since, until)` reads the store."""
    start, days, pstart, pdays = month_bounds(now)
    elapsed = max((now - start) / 86400, 1 / 24)            # days into the month (at least an hour)
    scale = days / elapsed
    rows = []
    for d in devices:
        mtd = kwh_between(d["id"], start, now)
        last = kwh_between(d["id"], pstart, start)
        rows.append(dict(id=d["id"], name=d["name"], klass=d["klass"], kwh=mtd, proj=mtd * scale,
                         last=last * days / pdays))          # last month scaled to this month's length
    mtd, proj, last = (sum(r[k] for r in rows) for k in ("kwh", "proj", "last"))
    e = err_pct(elapsed) / 100
    lo, hi = proj * (1 - e), proj * (1 + e)
    proj_rm = bill(proj, discount)
    for r in rows:
        r["share"] = r["kwh"] / mtd if mtd else 0
        r["rm"] = round(proj_rm * (r["proj"] / proj), 2) if proj else 0      # the band rate applies to every unit
        r["delta_kwh"] = round(r["proj"] - r["last"], 1)
        r["delta_rm"] = round(r["delta_kwh"] * rate(proj) * (1 - discount), 2)
        for k in ("kwh", "proj", "last"):
            r[k] = round(r[k], 1)
    rows.sort(key=lambda r: -r["proj"])
    return dict(now=now, day=round(elapsed, 1), days=days, mtd_kwh=round(mtd, 1), so_far_rm=bill(mtd, discount),
                proj_kwh=round(proj), low_kwh=round(lo), high_kwh=round(hi), proj_rm=proj_rm,
                low_rm=bill(lo, discount), high_rm=bill(hi, discount), err_pct=round(e * 100, 1),
                rate=rate(proj), discount=discount, last_kwh=round(last), last_rm=bill(last, discount),
                rows=rows, watch=band_watch(proj, discount), above=band_above(proj, discount),
                bands=[[t, r] for t, r in BANDS if t != float("inf")],      # the last band has no upper limit
                crosses_band=rate(lo) != rate(hi))


# ---------------------------------------------------------------------------------------------
# Analyzer: learns each appliance's normal from its own history, then names what is unusual.
# It sees readings only. It is never told what the simulation injected.
# ---------------------------------------------------------------------------------------------
OFFLINE_S = 5 * 60
KINDS = {  # kind -> (title, what the user can do)
    "offline": ("Plug not reporting", "Check the plug has power and Wi-Fi. Usage is unknown while it is offline, not zero."),
    "unusual_time": ("On at an unusual time", "If nobody meant to leave it on, switch it off."),
    "drift": ("Drawing more power than usual", "A steady rise can mean the appliance is working less efficiently. Worth a service or clean."),
    "overload": ("Drawing far more power than normal", "This is well outside its normal range. Switch it off and have it checked."),
    "duty": ("Running more of the time than usual", "It is working harder to do the same job. Check door seals, filters and ventilation."),
    "pattern": ("Unusual pattern (learned model)", "Its last day does not look like its normal days. This comes from the learned model, which is less certain than the named checks: have a look at how it is running."),
}


def _hour(ts):
    return (ts % 86400) // 3600


def _weekend(ts):
    return ((ts // 86400 + 3) % 7) >= 5          # 1970-01-01 was a Thursday


def _p90(vals):
    s = sorted(vals)
    return s[min(len(s) - 1, int(0.9 * len(s)))]


class Baseline:
    """One appliance's normal behaviour, learned from its own past readings [(ts, watts), ...]."""

    def __init__(self, rows):
        on = [w for _, w in rows if w > 15]
        self.ok = len(on) >= 30 and len(rows) >= 3 * 1440        # needs a few days before it judges anything
        self.level = self.thr = None
        self.run_w = sum(on) / len(on) if on else 0.0             # average draw while switched on (for "hours less")
        self.p = [[0.0] * 24 for _ in (0, 1)]
        self.daily = 0.0
        self.hold = {}                                            # kind -> seconds it must persist (see learn())
        if not self.ok:
            return
        # typical draw while really running: the watts below which it uses 90% of its energy. Weighting by energy
        # (not by minutes) keeps a standby it sits in all day from being mistaken for "running" (found on real recordings)
        # ponytail: one cut at 90%. An appliance that spends over 10% of its energy in spikes gets the spike as its level
        # (its drift check then goes quiet). Upgrade: cluster the watt levels it actually sits at.
        acc, tot = 0.0, 0.9 * sum(on)
        for self.level in sorted(on):
            acc += self.level
            if acc >= tot:
                break
        self.thr = max(15.0, 0.2 * self.level)
        cnt = [[0] * 24 for _ in (0, 1)]; act = [[0] * 24 for _ in (0, 1)]
        for ts, w in rows:
            k, h = _weekend(ts), _hour(ts)
            cnt[k][h] += 1; act[k][h] += w > self.thr
        self.p = [[act[k][h] / cnt[k][h] if cnt[k][h] else 0.0 for h in range(24)] for k in (0, 1)]
        self.daily = sum(w for _, w in rows) / 60000 / max((rows[-1][0] - rows[0][0]) / 86400, 1)
        self.always_on = min(min(r) for r in self.p) >= 0.1      # cycles around the clock (a fridge), no off hours

    def expected(self, ts, tolerant=False):
        """Share of minutes this appliance is normally running at this hour (weekday / weekend).
        tolerant=True also accepts the hour before and after, so a slightly early start is not 'unusual'."""
        k, h = _weekend(ts), _hour(ts)
        return max(self.p[k][(h - 1) % 24], self.p[k][h], self.p[k][(h + 1) % 24]) if tolerant else self.p[k][h]


def analyze(rows, B, now, open_kinds=()):
    """rows: the appliance's last 24 h of readings, oldest first; open_kinds: alert kinds already open for it.
    Returns [(kind, detail, extra_kwh_per_day)]."""
    if not B.ok:
        return []
    if not rows or now - rows[-1][0] > OFFLINE_S:
        mins = round((now - rows[-1][0]) / 60) if rows else None
        return [("offline", f"No reading for {mins} minutes." if mins else "No readings received.", 0.0)]
    out = []
    last30 = [(t, w) for t, w in rows if t > now - 1800]
    if len(last30) >= 20:
        act = sum(w > B.thr for _, w in last30)
        exp = sum(B.expected(t, tolerant=True) for t, _ in last30) / len(last30)
        avg = sum(w for _, w in last30) / len(last30)
        powered = sum(w > 15 for _, w in last30) >= 0.5 * len(last30)
        if act >= 0.5 * len(last30) and exp < 0.05:
            out.append(("unusual_time", f"Running at a time it is normally off (about {round(avg)} W for the last half hour).",
                        max(avg * 24 / 1000 - B.daily, 0.0)))
        elif "unusual_time" in open_kinds and powered:
            # still the same problem: it has not been switched off since, even if this hour is one it normally runs in
            out.append(("unusual_time", f"Still on and has not been switched off since (about {round(avg)} W now).",
                        max(avg * 24 / 1000 - B.daily, 0.0)))
    recent_on = [w for _, w in rows if w > B.thr][-30:]
    if len(recent_on) >= 10:
        ratio = _p90(recent_on) / B.level
        if ratio >= 1.12:
            kind = "overload" if ratio >= 1.4 else "drift"
            out.append((kind, f"Drawing about {round(_p90(recent_on))} W when running, {round((ratio - 1) * 100)}% above its usual {round(B.level)} W.",
                        (ratio - 1) * B.daily))
    last6 = [(t, w) for t, w in rows if t > now - 6 * 3600]
    if B.always_on and len(last6) >= 240:      # only for appliances that cycle around the clock
        duty = sum(w > B.thr for _, w in last6) / len(last6)
        exp = sum(B.expected(t) for t, _ in last6) / len(last6)
        if exp >= 0.1 and duty / exp >= 1.35 and duty - exp >= 0.1:
            out.append(("duty", f"Running {round(duty * 100)}% of the time over the last 6 hours, up from its usual {round(exp * 100)}%.",
                        (duty / exp - 1) * B.daily))
    return out


HOLD_S = 300      # default persistence: seen on two 5-minute checks in a row


def learn(rows, cal_days=14, step=900, margin=1.5):
    """A Baseline with its checks calibrated on the appliance's own history. Normal is learned from the older part of
    the history and the named checks are replayed over the newest part, where nothing is known to be wrong: a check
    must then stay true for longer than it ever did there (x margin) before it may alert. `B.hold` = seconds per kind.
    This is what keeps a fridge's defrost or a TV's varying draw from being reported (found on real recordings)."""
    B = Baseline(rows)
    if not B.ok:
        return B
    ts = [t for t, _ in rows]
    end = ts[-1]
    start = end - min(cal_days, (end - ts[0]) // 86400 // 2) * 86400
    old = Baseline(rows[:bisect.bisect_left(ts, start)])
    if not old.ok:
        return B
    run, longest = {}, {}
    for now in range(start, end, step):
        found = {k for k, _, _ in analyze(rows[bisect.bisect_right(ts, now - 86400):bisect.bisect_right(ts, now)], old, now)}
        run = {k: run.get(k, 0) + step for k in found if k != "offline"}
        for k, v in run.items():
            longest[k] = max(longest.get(k, 0), v)
    B.hold = {k: margin * v + step for k, v in longest.items()}
    return B


class Incidents:
    """One open alert per (appliance, kind). It updates rather than repeating, clears itself, and can be acknowledged."""
    CLEAR_S, ESCALATE_S = 30 * 60, (3600, 4 * 3600, 24 * 3600)

    def __init__(self):
        self.open, self.closed, self.pending = {}, [], {}
        self.raw = self.notifications = self.seq = 0

    def update(self, device, name, findings, now, cost, hold=None):
        """cost: RM per kWh, or a function (extra kWh per day -> RM per month) that knows the tariff bands.
        hold: {kind: seconds a finding must persist before it opens an alert} (Baseline.hold); default two checks in a row."""
        seen = set()
        for kind, detail, extra in findings:
            key = (device, kind); seen.add(key); self.raw += 1
            inc = self.open.get(key)
            if inc is None:
                if now - self.pending.setdefault(key, now) < (hold or {}).get(kind, HOLD_S):    # persistence
                    continue
                self.seq += 1; self.notifications += 1
                inc = self.open[key] = dict(id=self.seq, device=device, name=name, kind=kind, opened=self.pending.pop(key),
                                            ack=False, escalations=0)
            inc.update(detail=detail, last_seen=now, extra_kwh_day=round(extra, 2),
                       rm_month=round(cost(extra) if callable(cost) else extra * 30 * cost, 2))
            age = now - inc["opened"]
            if not inc["ack"] and inc["escalations"] < len(self.ESCALATE_S) and age >= self.ESCALATE_S[inc["escalations"]]:
                inc["escalations"] += 1; self.notifications += 1
        for key in [k for k in self.pending if k[0] == device and k not in seen]:
            del self.pending[key]
        for key in [k for k in self.open if k[0] == device and k not in seen]:
            if now - self.open[key]["last_seen"] >= self.CLEAR_S:
                inc = self.open.pop(key); inc["closed"] = now
                self.closed.insert(0, inc); del self.closed[30:]

    def kinds(self, device):
        return {k for d, k in self.open if d == device}

    def view(self):
        rank = lambda i: (i["kind"] != "overload", -i["rm_month"])       # safety first, then cost
        add = lambda i: dict(i, title=KINDS[i["kind"]][0], action=KINDS[i["kind"]][1])
        return dict(open=[add(i) for i in sorted(self.open.values(), key=rank)], closed=[add(i) for i in self.closed[:5]],
                    counts=dict(raw_detections=self.raw, incidents=self.seq, notifications=self.notifications))

    def ack(self, iid):
        for i in self.open.values():
            if i["id"] == iid:
                i["ack"] = True
                return True
        return False


# ---------------------------------------------------------------------------------------------
# Advisor: "I want to pay about RM X" -> what to change. Optimisation with rules, not machine learning.
# ---------------------------------------------------------------------------------------------
MAX_CUT = {"must-run": 0.0, "adjustable": 0.5, "switchable": 1.0}   # ASSUMED: how far each class may be cut


def advise(b, target, tol=5.0, run_w=None, alerts=()):
    """b: bill_view() result. Returns the plan for the rest of this month.
    Every cuttable appliance is reduced by the same percentage (capped per class); must-run is never touched."""
    run_w, d = run_w or {}, b["discount"]
    left_days = max(b["days"] - b["day"], 0.0)
    allowed = kwh_for_budget(target, d)
    rows = [dict(id=r["id"], name=r["name"], klass=r["klass"], left=max(r["proj"] - r["kwh"], 0.0),
                 cap=MAX_CUT[r["klass"]] * max(r["proj"] - r["kwh"], 0.0)) for r in b["rows"]]
    proj = b["mtd_kwh"] + sum(r["left"] for r in rows)
    need = proj - allowed
    max_cut = sum(r["cap"] for r in rows)
    out = dict(target=target, tol=tol, allowed_kwh=allowed, proj_kwh=b["proj_kwh"], proj_rm=b["proj_rm"], left_days=round(left_days, 1),
               need_kwh=round(max(need, 0), 1), discount=d,
               fix_first=[dict(name=a["name"], title=a["title"], rm_month=a["rm_month"]) for a in alerts if a.get("rm_month", 0) >= 0.5])
    if need <= 0:
        out.update(status="on_track" if bill(proj, d) >= target - tol else "under", cut_kwh=0.0, new_rm=bill(proj, d),
                   spare_kwh=round(-need))
        cut = 0.0
    else:
        cut = min(need, max_cut)
        f, todo, active = 0.0, cut, [r for r in rows if r["cap"] > 0]      # equal % for all, capped: water-filling
        for r in rows:
            r["cut"] = 0.0
        while todo > 1e-6 and active:
            f = todo / sum(r["left"] for r in active)
            capped = [r for r in active if r["cut"] + f * r["left"] >= r["cap"] - 1e-9]
            if not capped:
                for r in active:
                    r["cut"] += f * r["left"]
                todo = 0
            else:
                for r in capped:
                    todo -= r["cap"] - r["cut"]; r["cut"] = r["cap"]
                active = [r for r in active if r not in capped]
        out.update(status="plan" if need <= max_cut + 1e-6 else "unreachable", cut_kwh=round(cut, 1), new_rm=bill(proj - cut, d))
    out["rows"] = []
    for r in rows:
        c = r.get("cut", 0.0)
        per_day = c / left_days if left_days else 0.0
        w = run_w.get(r["id"]) or 0
        out["rows"].append(dict(id=r["id"], name=r["name"], klass=r["klass"], left_kwh=round(r["left"], 1), cut_kwh=round(c, 1),
                                cut_pct=round(100 * c / r["left"]) if r["left"] else 0, per_day_kwh=round(per_day, 2),
                                hours_less=round(per_day / (w / 1000), 1) if w > 20 and c else None))
    out["rows"].sort(key=lambda r: -r["cut_kwh"])
    out["watch"] = band_watch(proj - cut, d)
    return out


def _dur(h):
    return f"about {round(h * 60)} minutes" if h < 1 else f"about {h} hours"


def advice_text(p):
    """Plain-language version of advise(); every number comes from the plan."""
    L = []
    if p["fix_first"]:
        L.append("First, fix what is wasting power: " + "; ".join(f"{a['name']} ({a['title'].lower()}, about RM {a['rm_month']:.2f} a month)" for a in p["fix_first"]) + ".")
    if p["status"] in ("on_track", "under"):
        L.append(f"You are on track for about RM {p['proj_rm']:.2f} this month ({p['proj_kwh']} kWh), which is within your RM {p['target']:.0f} target. No cuts needed."
                 + (f" You have room for about {p['spare_kwh']} kWh more." if p["status"] == "under" else ""))
    else:
        if p["status"] == "unreachable":
            L.append(f"RM {p['target']:.0f} is not reachable this month without cutting adjustable appliances by more than half or touching ones that must stay on. The lowest I can get you to is about RM {p['new_rm']:.2f}.")
        else:
            L.append(f"To pay about RM {p['target']:.0f} you can use up to {p['allowed_kwh']} kWh this month. You are on track for {p['proj_kwh']} kWh (RM {p['proj_rm']:.2f}), so cut {p['cut_kwh']} kWh over the next {p['left_days']:.0f} days. That brings the bill to about RM {p['new_rm']:.2f}.")
        for r in p["rows"]:
            if r["cut_kwh"] > 0:
                h = r["hours_less"]
                t = f", {_dur(h)} less a day" if h else ""
                L.append(f"- {r['name']}: " + ("keep it switched off" if r["cut_pct"] >= 100 else f"use {r['cut_pct']}% less") + f" ({r['per_day_kwh']} kWh a day{t}).")
    keep = [r["name"] for r in p["rows"] if r["klass"] == "must-run"]
    if keep and p["status"] in ("plan", "unreachable"):
        L.append("Left alone because it must stay on: " + ", ".join(keep) + ".")
    return "\n".join(L)


# ---------------------------------------------------------------------------------------------
# Chat: a few question types, answered from the calculations above. `facts` is everything the reply used.
# ---------------------------------------------------------------------------------------------
import re

HELP = ("Ask me for a target bill (\"I want to pay RM100\"), why the bill changed, what uses the most, "
        "or whether anything is wrong.")
ASK = dict(why="Why is my bill higher?", most="What uses the most?", alerts="Is anything wrong?")


def followups(intent, facts):
    """Questions to offer next: the ones not just asked, and a way back to last month's bill when this one is higher."""
    out = [t for k, t in ASK.items() if k != intent]
    bl = facts.get("bill")
    if bl and bl["last_rm"] >= 5 and bl["proj_rm"] - bl["last_rm"] >= 1:
        out.insert(0, f"Get back to RM {round(bl['last_rm'])}")
    return out


def answer(question, b, alerts, run_w):
    """Scripted reply. Returns (text, facts, intent)."""
    q, run_w = question.lower(), run_w or {}
    why = re.search(r"\bwhy\b|how come", q)          # "why is my bill RM10 higher" asks for a reason, not a target
    m = None if why else re.search(r"rm\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:ringgit|rm)", q) or (
        re.search(r"(\d+(?:\.\d+)?)", q) if re.search(r"pay|bill|budget|target|spend|under|below|limit", q) else None)
    if m:
        target = float(next(g for g in m.groups() if g))
        if not 5 <= target <= 5000:
            return "Give me a target between RM5 and RM5,000.", {}, "target"
        p = advise(b, target, run_w=run_w, alerts=alerts)
        return advice_text(p), dict(plan=p), "target"
    if re.search(r"wrong|problem|fault|alert|issue|broken|unusual|\bok(ay)?\W*$", q):
        if not alerts:
            return "Nothing unusual right now. Every appliance is behaving the way it normally does.", dict(alerts=[]), "alerts"
        L = [f"{len(alerts)} thing{'s' if len(alerts) > 1 else ''} need{'' if len(alerts) > 1 else 's'} attention:"]
        L += [f"- {a['name']}: {a['title'].lower()}. {a['detail']}" + (f" About RM {a['rm_month']:.2f} extra a month if it continues." if a["rm_month"] >= 0.5 else "") for a in alerts]
        return "\n".join(L), dict(alerts=alerts), "alerts"
    if why or re.search(r"higher|increase|went up|more than|compare|last month|lower|less than", q):
        d, dr = b["proj_kwh"] - b["last_kwh"], b["proj_rm"] - b["last_rm"]

        def hours(r):       # the change as running time a day at the appliance's usual draw; 0 if unknown or under 5 min
            w = run_w.get(r["id"]) or 0
            h = abs(r["delta_kwh"]) / b["days"] / (w / 1000) if w > 20 else 0
            return round(h, 1) if h * 60 >= 5 else 0
        moved = sorted([dict(r, hours_day=hours(r)) for r in b["rows"] if abs(r["delta_kwh"]) >= 1], key=lambda r: -abs(r["delta_kwh"]))
        L = [f"This month is on track for {b['proj_kwh']} kWh (about RM {b['proj_rm']:.2f}); last month was {b['last_kwh']} kWh (RM {b['last_rm']:.2f}). "
             + ("That is about the same." if abs(d) < 3 else f"That is {abs(d)} kWh {'more' if d > 0 else 'less'}, about RM {abs(dr):.2f} {'more' if dr > 0 else 'less'}.")]
        L += [f"- {r['name']}: {'+' if r['delta_kwh'] > 0 else '-'}{abs(r['delta_kwh'])} kWh ({r['last']} to {r['proj']})"
              + (f", equal to {_dur(r['hours_day'])} {'more' if r['delta_kwh'] > 0 else 'less'} a day." if r["hours_day"] else ".") for r in moved[:4]]
        if rate(b["proj_kwh"]) != rate(b["last_kwh"]):
            L.append(f"The total also crossed a tariff band, so every unit is priced at {rate(b['proj_kwh']) * 100:g} sen instead of {rate(b['last_kwh']) * 100:g} sen.")
        fix = next((r for r in moved[:4] if r["delta_kwh"] > 0 and r["hours_day"] and r["klass"] != "must-run"), None)
        if fix and d >= 3:
            L.append(f"Running the {fix['name']} {_dur(fix['hours_day'])} less a day would take back its {fix['delta_kwh']} kWh.")
        return "\n".join(L), dict(bill={k: b[k] for k in ("proj_kwh", "proj_rm", "last_kwh", "last_rm")}, changes=moved[:4]), "why"
    if re.search(r"most|biggest|top|largest|where|which appliance|using", q):
        L = ["Projected for this month, biggest first:"]
        L += [f"- {r['name']}: {round(r['proj'])} kWh, about RM {r['rm']:.2f} ({round(100 * r['proj'] / b['proj_kwh']) if b['proj_kwh'] else 0}% of the bill)." for r in b["rows"]]
        return "\n".join(L), dict(rows=[{k: r[k] for k in ("name", "proj", "rm")} for r in b["rows"]]), "most"
    dr = b["proj_rm"] - b["last_rm"]                  # anything else, greetings included: lead with the headline
    top = max(b["rows"], key=lambda r: abs(r["delta_kwh"]), default=None)
    head = f"You are on track for about RM {b['proj_rm']:.2f} this month ({b['proj_kwh']} kWh)"
    if b["last_kwh"] and abs(dr) >= 1:
        head += f", RM {abs(dr):.2f} {'more' if dr > 0 else 'less'} than last month"
        if top and top["delta_kwh"] * dr > 0:
            head += f", mostly the {top['name']}"
    return head + ". " + HELP, dict(bill={k: b[k] for k in ("proj_kwh", "proj_rm", "last_kwh", "last_rm")}), "help"
