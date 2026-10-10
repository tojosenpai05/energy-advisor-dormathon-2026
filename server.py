"""Home energy advisor: the product. Server, storage, analyzer loop, laptop reader. Standard library only.

Run:  python server.py            -> http://127.0.0.1:8765/app, and starts the simulated house (house.py) as a
                                     second program on the next port: http://127.0.0.1:8766/
      python server.py --no-house -> the product alone (for real plugs, or a house started by hand)
      python server.py --lan      -> also reachable from a phone on the same Wi-Fi
      python server.py --port N   -> use another port
The product knows no appliances. A plug (virtual or a real ESP32) announces itself on /api/plug/beacon, is paired
from the app, then POSTs JSON readings to /api/readings; replies carry any on/off command waiting for it.
"""
import atexit
import calendar
import collections
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core
import learned

HERE = Path(__file__).resolve().parent
OFFLINE_AFTER = 5 * 60          # seconds without a reading (on that device's own clock)
LAPTOP = "laptop"
LEARN_DAYS = 14                 # history a plug needs before its alerts start. Measured on real recordings: 0.64 false
                                # alerts per appliance per day with 3 days of history, 0.13 with 14 (real_proof.json)
# appliance type the user picks when pairing -> (label, how the advisor treats it unless the user changes it)
TYPES = {"fridge": ("Fridge", "must-run"), "aircon": ("Air conditioner", "adjustable"), "heater": ("Water heater", "adjustable"),
         "tv": ("TV / entertainment", "switchable"), "washer": ("Washing machine", "switchable"), "fan": ("Fan", "switchable"),
         "lamp": ("Lamp", "switchable"), "other": ("Other", "switchable")}


class Store:
    """1-minute rows in SQLite; the latest reading and a short recent window in memory."""

    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, name TEXT, type TEXT, klass TEXT, paired INT, real INT, room TEXT, info TEXT);
            CREATE TABLE IF NOT EXISTS minute(device TEXT, ts INT, w REAL, PRIMARY KEY(device, ts));""")
        self.recent = collections.defaultdict(lambda: collections.deque(maxlen=1500))  # last 25 h of minutes
        self.last = {}                                                                 # device -> (ts, watts)
        self.first = {}                                                                # device -> ts of its first reading
        self.pending = []

    def add_device(self, did, name, typ, klass, paired, real=0, room="", info=""):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO devices VALUES(?,?,?,?,?,?,?,?)", (did, name, typ, klass, int(paired), real, room, info))
            self.db.commit()

    def ingest(self, did, ts, watts):
        """One reading. watts=None means the plug sent nothing (offline or dropped)."""
        if watts is None:
            return
        with self.lock:
            self.recent[did].append((ts, watts)); self.last[did] = (ts, watts); self.first.setdefault(did, ts)
            self.pending.append((did, ts - ts % 60, watts))
            if len(self.pending) >= 2000:
                self._flush()

    def _flush(self):
        self.db.executemany("INSERT OR REPLACE INTO minute VALUES(?,?,?)", self.pending)
        self.db.commit(); self.pending.clear()

    def flush(self):
        with self.lock:
            self._flush()

    def kwh(self, did, since, until):
        with self.lock:
            self._flush()
            r = self.db.execute("SELECT COALESCE(SUM(w),0) FROM minute WHERE device=? AND ts>=? AND ts<?", (did, since, until)).fetchone()
        return r[0] / 60 / 1000          # sum of 1-minute average watts -> kWh

    def rows(self, did, since, until):
        with self.lock:
            self._flush()
            return self.db.execute("SELECT ts,w FROM minute WHERE device=? AND ts>=? AND ts<? ORDER BY ts", (did, since, until)).fetchall()

    def devices(self, paired=None):
        with self.lock:
            rows = self.db.execute("SELECT id,name,type,klass,paired,real,room,info FROM devices").fetchall()
        out = [dict(id=r[0], name=r[1], type=r[2], klass=r[3], paired=bool(r[4]), real=bool(r[5]), room=r[6], info=r[7]) for r in rows]
        return [d for d in out if paired is None or d["paired"] == paired]

    def update_device(self, did, **kw):
        cols = {k: v for k, v in kw.items() if k in ("name", "klass", "room") and v is not None}
        if "klass" in cols and cols["klass"] not in ("must-run", "adjustable", "switchable"):
            raise ValueError("bad class")
        if "name" in cols:
            cols["name"] = str(cols["name"]).strip()[:40] or "Unnamed"
        if "room" in cols:
            cols["room"] = str(cols["room"]).strip()[:30]
        if not cols:
            return
        with self.lock:
            self.db.execute(f"UPDATE devices SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), did))
            self.db.commit()


def laptop_watts(root=Path("/sys/class")):
    """(watts, source) for this machine, or (None, None).
    "battery": whole-laptop draw (current x voltage), only available while discharging.
    "chip": processor + graphics chip power from the AMD graphics driver, available when plugged in.
    The chip figure excludes screen, storage, Wi-Fi and charger losses, so it is lower than the wall draw."""
    for b in sorted((root / "power_supply").glob("BAT*")):
        try:
            rd = lambda f: (b / f).read_text().strip()
            if rd("status") == "Discharging":
                w = int(rd("power_now")) / 1e6 if (b / "power_now").exists() else int(rd("current_now")) * int(rd("voltage_now")) / 1e12
                if w > 0:
                    return w, "battery"
        except (OSError, ValueError):
            continue
    for h in sorted((root / "hwmon").glob("hwmon*")):
        try:
            if (h / "name").read_text().strip() == "amdgpu":
                return int((h / "power1_input").read_text()) / 1e6, "chip"
        except (OSError, ValueError):
            continue
    return None, None


LAPTOP_SOURCE = {"v": None}


def laptop_loop(store):
    while True:
        w, LAPTOP_SOURCE["v"] = laptop_watts()
        store.ingest(LAPTOP, int(time.time()), w)
        time.sleep(2)


LLM_URL, LLM_MODEL = "https://api.deepseek.com/chat/completions", "deepseek-flash"
LLM_DAILY_CAP = int(os.environ.get("LLM_DAILY_CAP", "300"))      # a public page must not be able to spend the key without limit
LLM_SYSTEM = ("You are the assistant inside a home energy app for a household in Sarawak, Malaysia. "
              "Rewrite the DRAFT ANSWER so it reads naturally and answers the user's question. Use ONLY the figures in the "
              "draft and the FACTS. Do not invent numbers, appliances, causes or advice. Never suggest switching off an "
              "appliance whose class is must-run. If the facts do not answer the question, say so. Under 120 words, plain "
              "language, RM for money, no markdown tables.")


def llm_rewrite(question, draft, facts, key, opener=urllib.request.urlopen):
    """Ask DeepSeek to phrase the scripted draft. Returns text, or raises; the caller falls back to the draft.
    Checked against the live service 2026-10-11. Thinking is switched off: it is on by default, tripled the wait and
    spent most of max_tokens on reasoning, which would leave a long answer empty."""
    body = dict(model=LLM_MODEL, stream=False, max_tokens=600, thinking=dict(type="disabled"), messages=[
        dict(role="system", content=LLM_SYSTEM),
        dict(role="user", content=f"QUESTION: {question}\n\nDRAFT ANSWER:\n{draft}\n\nFACTS (JSON):\n{json.dumps(facts)[:6000]}")])
    req = urllib.request.Request(LLM_URL, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with opener(req, timeout=25) as r:
        text = json.loads(r.read())["choices"][0]["message"]["content"]
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty reply")
    return text.strip()


def day_start(ts):
    return ts - ts % 86400


def month_start(ts):
    g = time.gmtime(ts)
    return calendar.timegm((g.tm_year, g.tm_mon, 1, 0, 0, 0))


class App:
    def __init__(self):
        self.store = Store()
        self.store.add_device(LAPTOP, "My laptop", "laptop", "switchable", True, real=1)
        self.llm_day, self.llm_n = 0, 0      # real day, language-model calls made on it
        self.discount = True          # 25% domestic discount, Apr-Dec 2026 (the simulated date is Oct 2026)
        self.inc, self.inc_lock = core.Incidents(), threading.Lock()
        self.base, self.base_day, self.last_check = {}, None, 0
        self.models, self.streak, self.model_hour = {}, {}, None      # learned detector, trained in the background
        self.auto_train, self.training = True, False
        self.cost = lambda e: 0.0
        self.plug_now, self.syncing = 0, False    # the plugs' clock; True while a plug is uploading stored readings
        self.air, self.pairing = {}, {}           # plugs heard announcing themselves; pairing requests from the app
        self.commands, self.live = {}, {}         # on/off waiting for a plug; its latest state / volts / amps
        self.house_url = None                     # set when this process started a simulated house: only for the link on /

    @property
    def now(self):
        """The plugs' clock: the end of the newest minute any plug reported (the simulated house runs fast, so this is
        not the wall clock). Before any plug has reported: the wall clock."""
        return self.plug_now or int(time.time())

    def ready(self, d):
        """Has this plug reported for long enough to be judged?"""
        first = self.store.first.get(d["id"])
        return first is not None and self.now - first >= LEARN_DAYS * 86400

    def train_models(self):
        """Train one small neural network per appliance on its own history (a few seconds each, off the main path)."""
        now = self.now
        try:
            for d in self.sim_devices():
                if d["id"] in self.base and d["id"] not in self.models:
                    scale, tr = learned.training_set(self.store.rows(d["id"], now - 37 * 86400, now))
                    if tr:
                        self.models[d["id"]] = (scale, learned.TinyAutoencoder(tr))
        finally:
            self.training = False

    def pattern_finding(self, did, rows, now):
        """Second layer: only speaks when the named checks found nothing and the network has disagreed for hours."""
        if did not in self.models or self.streak.get(did, 0) < learned.NN_HOURS:
            return []
        scale, net = self.models[did]
        h, used, normal, extra = net.explain(learned.profile(rows, now, scale), scale)
        return [("pattern", f"Around {h:02d}:00 it averaged about {round(used)} W; the learned normal for that hour is about {round(normal)} W. "
                 + ("Over the day it is using more than normal." if extra > 0.05 else "Over the day it is using less than normal, which can mean it is not doing its job." if extra < -0.05 else ""),
                 max(extra, 0.0))]

    def sim_devices(self):
        return [d for d in self.store.devices(paired=True) if not d["real"]]

    def learn(self):
        """Refresh each appliance's normal from its last 5 weeks, leaving out the latest 2 days and any
        appliance with an open alert (so a problem is not learned as the new normal)."""
        now = self.now
        for d in self.sim_devices():
            if not self.ready(d) or (d["id"] in self.base and self.inc.kinds(d["id"])):
                continue
            self.base[d["id"]] = core.learn(self.store.rows(d["id"], now - 37 * 86400, now - 2 * 86400))
        if self.auto_train and not self.training and any(k not in self.models for k in self.base):
            self.training = True
            threading.Thread(target=self.train_models, daemon=True).start()
        b = core.bill_view(now, self.sim_devices(), self.store.kwh, core.DISCOUNT_2026 if self.discount else 0.0)
        # cost of an alert = what last month's bill would become with the extra usage (bands included)
        self.cost = lambda e, k=b["last_kwh"], d=b["discount"]: core.bill(k + e * 30, d) - core.bill(k, d)
        self.base_day = now // 86400

    def check(self):
        """One analyzer pass over every paired appliance that is past its learning period (every 5 minutes of plug time)."""
        now = self.now
        if now // 86400 != self.base_day:
            self.learn()
        hourly = now // 3600 != self.model_hour
        self.model_hour = now // 3600
        with self.inc_lock:
            for d in self.sim_devices():
                if d["id"] not in self.base:
                    continue
                rows = [r for r in list(self.store.recent[d["id"]]) if r[0] > now - 86400]
                if hourly and d["id"] in self.models:
                    scale, net = self.models[d["id"]]
                    odd = net.score(learned.profile(rows, now, scale)) > net.thr
                    self.streak[d["id"]] = self.streak.get(d["id"], 0) + 1 if odd else 0
                found = core.analyze(rows, self.base[d["id"]], now, self.inc.kinds(d["id"]))
                if not found:
                    found = self.pattern_finding(d["id"], rows, now)
                self.inc.update(d["id"], d["name"], found, now, self.cost, self.base[d["id"]].hold)
        self.last_check = now

    def check_loop(self):
        while True:
            time.sleep(0.2)
            if self.plug_now and not self.syncing and self.now - self.last_check >= 300:
                self.check()

    def bill(self):
        return core.bill_view(self.now, self.sim_devices(), self.store.kwh, core.DISCOUNT_2026 if self.discount else 0.0)

    def run_w(self):
        """Watts while really running, for turning kWh into hours. The learned level, not the average of every reading
        above 15 W: standby dragged that average down and made a 23% cut read as about 7 hours a day."""
        return {k: v.level or v.run_w for k, v in self.base.items()}

    def chat(self, question, key=None, opener=urllib.request.urlopen):
        """Numbers always come from the calculation (the scripted draft). A language model only rephrases it."""
        question = str(question).strip()[:500]
        text, facts, intent = core.answer(question, self.bill(), self.alerts()["open"], self.run_w())
        out = dict(reply=text, facts=facts, intent=intent, source="scripted", note=None, next=core.followups(intent, facts))
        key = key if key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        if key and intent != "help" and facts:
            day = int(time.time() // 86400)
            if day != self.llm_day:
                self.llm_day, self.llm_n = day, 0
            if self.llm_n >= LLM_DAILY_CAP:  # ponytail: one counter for everyone, and two requests at once can overshoot by one. Upgrade: per-visitor limits
                out["note"] = "Daily language-model limit reached; showing the scripted answer."
                return out
            self.llm_n += 1
            try:
                out.update(reply=llm_rewrite(question, text, facts, key, opener), source="deepseek", draft=text)
            except Exception as e:          # no internet, bad key, timeout, odd reply: keep the scripted answer
                out["note"] = f"Language model unavailable ({type(e).__name__}); showing the scripted answer."
        return out

    def alerts(self, device=None):
        names = {d["id"]: d["name"] for d in self.store.devices()}
        with self.inc_lock:
            v = self.inc.view()
        for k in ("open", "closed"):
            v[k] = [dict(i, name=names.get(i["device"], i["name"])) for i in v[k] if device in (None, i["device"])]
        return dict(v, now=self.now)

    def clock(self, d):
        return int(time.time()) if d["real"] else self.now

    def device_view(self, d):
        now = self.clock(d)
        ts, w = self.store.last.get(d["id"], (None, None))
        online = ts is not None and now - ts <= (10 if d["real"] else OFFLINE_AFTER)
        v = dict(d, online=online, watts=round(w, 1) if online else None, alerts=len(self.inc.kinds(d["id"])))
        if d["real"]:
            v["source"] = LAPTOP_SOURCE["v"] if online else None
        else:      # the laptop runs on the real clock; it is not blended into the simulated month
            v["today_kwh"] = round(self.store.kwh(d["id"], day_start(now), now + 60), 3)
            v["month_kwh"] = round(self.store.kwh(d["id"], month_start(now), now + 60), 2)
            first = self.store.first.get(d["id"], now)
            v["learning"] = None if self.ready(d) else dict(day=int((now - first) // 86400), of=LEARN_DAYS)
            v.update(self.live.get(d["id"], {}))
        return v

    def on_air(self):
        """Plugs heard announcing themselves in the last few seconds that are not added yet."""
        return {k: v for k, v in self.air.items() if time.time() - v["seen"] < 5}

    def readings(self, b):
        """Readings from one plug or a batch of them. Only a paired plug is listened to."""
        rs = b["readings"] if isinstance(b.get("readings"), list) else [b]
        known = {d["id"]: d for d in self.store.devices(paired=True)}
        ok, unknown, cmds, plug_time = 0, set(), {}, False
        for r in rs:
            did, w = (r.get("deviceId"), r.get("power_W")) if isinstance(r, dict) else (None, None)
            if did not in known:
                if isinstance(did, str):
                    unknown.add(did[:40])
                continue
            if isinstance(w, bool) or not isinstance(w, (int, float)) or not 0 <= w < 20000:
                continue
            ts = int(r.get("timestamp") or time.time())
            self.store.ingest(did, ts, float(w)); ok += 1
            if not known[did]["real"]:
                plug_time = True
                self.plug_now = max(self.plug_now, ts + 60)
                self.live[did] = {k: r.get(k) for k in ("state", "voltage_V", "current_A")}
                if did in self.commands:
                    cmds[did] = self.commands.pop(did)
        if plug_time:
            self.syncing = bool(b.get("backlog"))
        return dict(ok=ok > 0, accepted=ok, unknown=sorted(unknown)[:50], commands=cmds)

    def beacon(self, b):
        """A plug in pairing mode announcing itself. The reply hands it any pairing request made in the app; the plug
        checks the password (only it knows it) and reports the result on its next call."""
        did = b["deviceId"][:40]
        if did not in self.air and len(self.air) >= 50:
            return dict(ok=False)
        self.air[did] = dict(ssid=str(b.get("ssid", ""))[:40], info=str(b.get("info", ""))[:120], seen=time.time())
        p = self.pairing.get(did)
        if not p or p["status"] != "pending":
            return dict(ok=True)
        if b.get("result") is None:
            return dict(pair=dict(password=p["password"]))
        p["status"] = "paired" if b["result"] == "ok" else "wrong_password"
        if p["status"] == "paired":
            self.store.add_device(did, p["name"], p["type"], TYPES[p["type"]][1], True, room=p["room"], info=self.air.pop(did)["info"])
            return dict(ok=True, name=p["name"])
        return dict(ok=True)

    def pair(self, b):
        """The app asks to add a plug it can see: Wi-Fi password, then the name, type and room the user gave it."""
        did, typ = b["deviceId"], b.get("type") if b.get("type") in TYPES else "other"
        self.pairing[did] = dict(status="pending", password=str(b.get("password", ""))[:64], type=typ,
                                 name=str(b.get("name") or "").strip()[:40] or TYPES[typ][0], room=str(b.get("room") or "").strip()[:30])
        return dict(ok=True, status="pending")

    # ---- API ----
    def get(self, path, q):
        if path == "/api/devices":           # app-facing: paired plugs only, NO fault information
            return dict(now=self.now, devices=[self.device_view(d) for d in self.store.devices(paired=True)])
        if path == "/api/discover":
            return dict(found=[dict(id=k, ssid=v["ssid"]) for k, v in self.on_air().items()], types={k: v[0] for k, v in TYPES.items()})
        if path == "/api/pair":
            p = self.pairing.get(q.get("id", [""])[0])
            return dict(status=p["status"]) if p else None
        if path == "/api/device":
            d = next((d for d in self.store.devices() if d["id"] == q.get("id", [""])[0]), None)
            if not d:
                return None
            return dict(self.device_view(d), now=self.now, series=list(self.store.recent[d["id"]])[-1440:])
        if path == "/api/proof":
            f, rf = HERE / "proof.json", HERE / "real_proof.json"
            with self.inc_lock:
                counts = self.inc.view()["counts"]
            return dict(comparison=json.loads(f.read_text()) if f.exists() else None, bill_error=core.MEASURED_ERR,
                        real=json.loads(rf.read_text()) if rf.exists() else None,
                        counts=counts, models_ready=len(self.models), laptop=LAPTOP_SOURCE["v"],
                        plugs=[dict(name=d["name"], info=d["info"], watts=round(self.base[d["id"]].level) if d["id"] in self.base and self.base[d["id"]].ok else None)
                               for d in self.sim_devices()], learn_days=LEARN_DAYS,
                        max_cut=core.MAX_CUT)
        if path == "/api/alerts":            # the analyzer's own conclusions (from readings only)
            return self.alerts(q.get("device", [None])[0])
        if path == "/api/bill":              # simulated plugs only: the real laptop runs on a different clock
            return self.bill()
        if path == "/api/advisor":
            try:
                target = float(q.get("target", ["100"])[0])
            except ValueError:
                return None
            if not 5 <= target <= 5000:
                return None
            return core.advise(self.bill(), target, run_w=self.run_w(), alerts=self.alerts()["open"])
        return None

    def post(self, path, b):
        if path == "/api/readings":          # what a plug calls
            return self.readings(b)
        if path == "/api/plug/beacon" and isinstance(b.get("deviceId"), str) and b["deviceId"]:
            return self.beacon(b)
        if path == "/api/pair" and b.get("deviceId") in self.on_air():
            return self.pair(b)
        if path == "/api/command" and b.get("state") in ("ON", "OFF") and any(d["id"] == b.get("id") for d in self.sim_devices()):
            self.commands[b["id"]] = b["state"]          # delivered in the reply to that plug's next readings
            return dict(ok=True)
        if path == "/api/device" and any(d["id"] == b.get("id") for d in self.store.devices()):
            self.store.update_device(b["id"], name=b.get("name"), klass=b.get("klass"), room=b.get("room"))
            return dict(ok=True)
        if path == "/api/alerts" and isinstance(b.get("ack"), int):
            with self.inc_lock:
                return dict(ok=True) if self.inc.ack(b["ack"]) else None
        if path == "/api/chat" and isinstance(b.get("text"), str) and b["text"].strip():
            return self.chat(b["text"])
        if path == "/api/settings" and isinstance(b.get("discount"), bool):
            self.discount = b["discount"]
            return dict(ok=True)
        return None


PAGES = {"/": "landing.html", "/app": "app.html"}


def make_handler(app, pages=PAGES):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(data)

        def _relay(self, url, data=None):
            """Pass a request on to the simulated house (a second program on its own port), so one public port serves both."""
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data, {"Content-Type": "application/json"}), timeout=5) as r:
                    body, ctype = r.read(), r.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as e:
                return self._send(e.code, e.read())
            except OSError:
                return self._send(502, dict(error="house not reachable"))
            if data is None and url.endswith("/api/sim"):       # seen through this port, the app is next door, not on 127.0.0.1
                body = json.dumps(dict(json.loads(body), app="/app")).encode()
            self._send(200, body, ctype)

        def do_GET(self):
            u = urlparse(self.path)
            house = getattr(app, "house_url", None)
            if house and u.path in ("/sim", "/api/sim"):
                return self._relay(house + u.path[1:])
            if u.path in pages:
                return self._send(200, (HERE / pages[u.path]).read_bytes(), "text/html; charset=utf-8")
            if u.path == "/sim":                 # reached only when this process started no house (see _relay above)
                return self._send(200, '<meta charset="utf-8"><p>No simulated house was started with this server: run python3 house.py. <a href="/">Back</a></p>'.encode(), "text/html; charset=utf-8")
            r = app.get(u.path, parse_qs(u.query))
            self._send(200, r) if r is not None else self._send(404, dict(error="not found"))

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > (4_000_000 if self.path == "/api/readings" else 10_000):      # a plug may upload a day of stored readings
                    return self._send(413, dict(error="too large"))
                if self.path == "/api/sim" and getattr(app, "house_url", None):
                    return self._relay(app.house_url + "api/sim", self.rfile.read(n))
                body = json.loads(self.rfile.read(n) or b"{}")
                r = app.post(urlparse(self.path).path, body if isinstance(body, dict) else {})
            except (ValueError, KeyError) as e:
                return self._send(400, dict(error=str(e)))
            self._send(200, r) if r is not None else self._send(400, dict(error="bad request"))

        def log_message(self, *a):
            pass
    return H


if __name__ == "__main__":
    app = App()
    threading.Thread(target=laptop_loop, args=(app.store,), daemon=True).start()
    threading.Thread(target=app.check_loop, daemon=True).start()
    host = "0.0.0.0" if "--lan" in sys.argv or "PORT" in os.environ else "127.0.0.1"      # a host sets PORT and needs every interface
    port = int(sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else os.environ.get("PORT", 8765))
    print(f"App: http://{host}:{port}/app")
    if not os.environ.get("DEEPSEEK_API_KEY"):      # else the nearest .env above this folder: the key is never copied in here
        env = next((d / ".env" for d in HERE.parents if (d / ".env").is_file()), None)
        for line in env.read_text().splitlines() if env else ():
            k, _, v = line.partition("=")
            if k.strip() == "DEEPSEEK_API_KEY":
                os.environ["DEEPSEEK_API_KEY"] = v.strip().strip("\"'")
    print("Chat: " + ("phrased by DeepSeek" if os.environ.get("DEEPSEEK_API_KEY") else "scripted (no DEEPSEEK_API_KEY found)"))
    if "--no-house" not in sys.argv:
        app.house_url = f"http://127.0.0.1:{port + 1}/"
        house = subprocess.Popen([sys.executable, str(HERE / "house.py"), "--app", f"http://127.0.0.1:{port}", "--port", str(port + 1)])
        atexit.register(house.terminate)                              # the house stops with the product, on Ctrl-C or kill
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        print(f"House simulation: {app.house_url}")
    ThreadingHTTPServer((host, port), make_handler(app)).serve_forever()
