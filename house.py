"""The simulated house as its own program. Its plugs reach the product only over HTTP, the way real ones would:
they announce themselves, get paired from the app, then post readings and pick up commands. Standard library only.

Run:  python3 house.py                       -> simulation screen on http://127.0.0.1:8766/
      python3 house.py --app URL --port N    -> product address (default http://127.0.0.1:8765), screen port

Plug states: UNPAIRED -> BROADCASTING_WIFI -> CONNECTING -> PAIRED_ONLINE.
"""
import json
import sys
import threading
import time
import urllib.request
import zlib
from http.server import ThreadingHTTPServer

import server
import sim as simmod

VOLTS = 230.0        # nominal Sarawak mains. The recordings give watts only, so current is derived (watts / 230), not measured
ROOMS = {"plug-fridge": "Kitchen", "plug-aircon": "Bedroom", "plug-tv": "Living room",      # where each appliance stands
         "plug-heater": "Utility area", "plug-washer": "Utility area"}
# What this household had already installed when its history begins: appliance -> (name, type) as typed into the app
FAMILY = {"plug-fridge": ("Fridge", "fridge"), "plug-aircon": ("Bedroom air cond", "aircon"),
          "plug-heater": ("Water heater", "heater"), "plug-tv": ("TV + router", "tv")}


def http_sender(base):
    def send(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    return send


class House:
    def __init__(self, send, traces=None, seed=7):
        """send(path, body) -> the product's reply. Over HTTP when run as a program; tests pass the product's handler."""
        self.send = send
        self.sim = simmod.Simulator(self._reading, seed, traces)
        self.out, self.plugs, self.note, self.app_url = [], {}, "", ""
        for pid in self.sim.plugs:                # identity comes from the plug, never from the product
            h = zlib.crc32(f"{seed}:{pid}".encode())
            self.plugs[pid] = dict(id=f"plug_{h & 0xffffff:06x}", ssid=f"SmartPlug_ESP32_{h >> 16 & 0xffff:04X}",
                                   password=f"{h % 10 ** 8:08d}", state="UNPAIRED", label="")
        self.ids = {pid: p["id"] for pid, p in self.plugs.items()}
        self.by_id = {p["id"]: pid for pid, p in self.plugs.items()}

    def _reading(self, pid, ts, w):
        """One simulated minute from one plug. Only a paired plug reports; a dropped reading is simply not sent."""
        p = self.plugs[pid]
        if p["state"] == "PAIRED_ONLINE" and w is not None:
            self.out.append(dict(deviceId=p["id"], state="ON" if self.sim.plugs[pid].relay else "OFF", voltage_V=VOLTS,
                                 current_A=round(w / VOLTS, 3), power_W=round(w, 2), timestamp=ts))

    def flush(self, backlog=False):
        """Post what the plugs measured since the last call; the reply carries any commands waiting for them."""
        with self.sim.lock:
            out, self.out = self.out, []
        if not out:
            return
        r = self.send("/api/readings", dict(readings=out, backlog=backlog)) or {}
        for did, state in (r.get("commands") or {}).items():
            if did in self.by_id and state in ("ON", "OFF"):
                self.sim.plugs[self.by_id[did]].relay = state == "ON"
                self.sim.log.insert(0, dict(t=self.sim.t, device=self.sim.plugs[self.by_id[did]].name, fault="remote:" + state))
        for did in r.get("unknown") or []:        # the product no longer knows this plug (it was restarted): pair again
            if did in self.by_id:
                self.plugs[self.by_id[did]]["state"] = "BROADCASTING_WIFI"

    def beacon(self):
        """One round of pairing signals from every plug that is waiting to be added."""
        for pid, p in self.plugs.items():
            if p["state"] not in ("BROADCASTING_WIFI", "CONNECTING"):
                continue
            body = dict(deviceId=p["id"], ssid=p["ssid"], info=self.sim.plugs[pid].source)
            if p["state"] == "CONNECTING":        # answer the pairing request heard on the last round
                body["result"] = p.pop("result")
                p["label"] = (self.send("/api/plug/beacon", body) or {}).get("name", "")      # what the user called it, as set at pairing
                p["state"] = "PAIRED_ONLINE" if body["result"] == "ok" else "BROADCASTING_WIFI"
            else:
                pair = (self.send("/api/plug/beacon", body) or {}).get("pair")
                if pair:                          # only the plug knows its password, so the plug decides
                    p["state"], p["result"] = "CONNECTING", "ok" if pair.get("password") == p["password"] else "wrong_password"

    def step(self):
        self.sim.step_minute(); self.flush()

    def install_family(self, until=simmod.HISTORY_END):
        """The plugs the household set up weeks ago: paired through the same calls the app makes, then their stored
        readings up to the present are uploaded, marked as backlog so the product does not treat them as live."""
        for pid, (name, typ) in FAMILY.items():
            p = self.plugs[pid]; p["state"] = "BROADCASTING_WIFI"
            self.beacon()
            self.send("/api/pair", dict(deviceId=p["id"], password=p["password"], name=name, type=typ, room=ROOMS[pid]))
            self.beacon(); self.beacon()
        while self.sim.t < until:
            self.sim.step_minute()
            if self.sim.t % 86400 == 0:
                self.flush(backlog=True)
        self.flush(backlog=True)

    def run(self):
        threading.Thread(target=self.sim.run, daemon=True).start()
        n = 0
        while True:
            time.sleep(0.25); n += 1
            try:
                self.flush()
                if n % 4 == 0:
                    self.beacon()
                self.note = ""
            except (OSError, ValueError) as e:    # ponytail: readings from a failed post are lost, as from a plug with no memory. Upgrade: keep and resend
                self.note = f"Cannot reach the product ({type(e).__name__}). Is server.py running?"

    # ---- simulation screen ----
    def get(self, path, q):
        if path != "/api/sim":
            return None
        s = self.sim
        return dict(now=s.t, speed=s.speed, paused=s.paused, noise=s.noise, dropout=s.dropout, log=s.log[:12], note=self.note, app=self.app_url, rooms=list(dict.fromkeys(ROOMS.values())),
                    plugs=[dict(id=p.id, name=p.name, type=p.type, watts=s.now_w.get(p.id), fault=s.faults.get(p.id),
                                faults=simmod.FAULTS[p.type], mode=p.mode, rated=round(p.rated), default=p.default, source=p.source,
                                relay=p.relay, room=ROOMS[p.id], **{k: self.plugs[p.id][k] for k in ("state", "ssid", "password", "label")})
                           for p in s.plugs.values()],
                    laptop=dict(zip(("watts", "source"), server.laptop_watts())))

    def post(self, path, b):
        s, a = self.sim, b.get("action")
        if path != "/api/sim":
            return None
        if a == "speed" and b.get("value") in (1, 60, 600, 3600):
            s.speed = b["value"]
        elif a in ("paused", "noise", "dropout"):
            setattr(s, a, bool(b.get("value")))
        elif a == "fault" and b.get("device") in s.plugs:
            s.inject(b["device"], b.get("fault"))
        elif a == "control" and b.get("device") in s.plugs:
            s.control(b["device"], b.get("mode"), b.get("rated"))
        elif a == "plug" and b.get("device") in s.plugs:       # fit a plug to this appliance, or take it off again
            p = self.plugs[b["device"]]
            p["state"] = "BROADCASTING_WIFI" if p["state"] == "UNPAIRED" else "UNPAIRED"
        else:
            return None
        return dict(ok=True)


if __name__ == "__main__":
    arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
    base, port = arg("--app", "http://127.0.0.1:8765").rstrip("/"), int(arg("--port", 8766))
    house = House(http_sender(base), simmod.load_traces())
    for attempt in range(40):                    # the product may still be starting
        try:
            house.install_family(); break
        except OSError:
            house = House(http_sender(base), simmod.load_traces()); time.sleep(0.5)
    else:
        sys.exit(f"Cannot reach the product at {base}. Start it first: python3 server.py")
    house.app_url = base + "/app"
    threading.Thread(target=house.run, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(house, {"/": "sim.html", "/sim": "sim.html"})).serve_forever()
