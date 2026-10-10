"""Run: python -m unittest -v   (standard library only)"""
import json
import tempfile
import unittest
from pathlib import Path

import core
import evaluate
import house as housemod
import learned
import server
import sim as simmod

DAY = 86400


def run_days(days, fault=None, pid="plug-aircon", seed=7, mode=None, rated=None):
    """Simulate `days` and return kWh per plug (noise off for exact comparisons)."""
    tot = {}
    def sink(d, ts, w):
        if w is not None:
            tot[d] = tot.get(d, 0) + w / 60 / 1000
    s = simmod.Simulator(sink, seed); s.noise = False
    if fault:
        s.faults[pid] = fault
    if mode or rated:
        s.control(pid, mode, rated)
    s.generate_history(simmod.START + days * DAY)
    return tot


class SimTests(unittest.TestCase):
    def test_daily_energy_is_plausible(self):
        k = {p: v / 14 for p, v in run_days(14).items()}      # kWh per day
        self.assertTrue(0.8 < k["plug-fridge"] < 1.8, k)
        self.assertTrue(4 < k["plug-aircon"] < 9, k)
        self.assertTrue(0.5 < k["plug-heater"] < 1.3, k)
        self.assertTrue(0.4 < k["plug-tv"] < 1.2, k)

    def test_faults_change_consumption(self):
        base = run_days(7)
        self.assertAlmostEqual(run_days(7, "drift")["plug-aircon"] / base["plug-aircon"], 1.25, delta=0.02)
        self.assertGreater(run_days(7, "cycling", "plug-fridge")["plug-fridge"], base["plug-fridge"] * 1.2)

    def test_manual_control_per_appliance(self):
        base = run_days(7)
        self.assertGreater(run_days(7, mode="on")["plug-aircon"], base["plug-aircon"] * 1.5)       # forced on all day
        self.assertEqual(run_days(7, mode="off")["plug-aircon"], 0)                                 # still reports, 0 W
        self.assertEqual(run_days(7, mode="off")["plug-fridge"], base["plug-fridge"])               # others untouched
        half = run_days(7, rated=1150 / 2)["plug-aircon"]
        self.assertTrue(0.45 < half / base["plug-aircon"] < 0.62, half / base["plug-aircon"])       # fan-only watts do not scale
        s = simmod.Simulator(lambda *a: None); s.control("plug-tv", rated=99999)
        self.assertEqual(s.plugs["plug-tv"].rated, 95 * 2)                                          # clamped to 2x default
        s.control("plug-tv", mode="nonsense"); self.assertEqual(s.plugs["plug-tv"].mode, "auto")

    def test_offline_emits_nothing(self):
        self.assertNotIn("plug-tv", run_days(2, "offline", "plug-tv"))

    def test_same_seed_is_reproducible(self):
        self.assertEqual(run_days(3), run_days(3))


class ReplayTests(unittest.TestCase):
    """Plugs that replay a real recording (traces.json); switch, slider and faults act on top of it."""
    TR = dict(fridge=dict(what="test fridge", level=100, thr=20, w=[2, 2, 100, 100, 2, 2, 2, 2, 2, 2] * 1008))   # one week

    def plug(self):
        return simmod.Simulator(lambda *a: None, traces=self.TR).plugs

    def test_replays_the_recording_and_wraps(self):
        p, t = self.plug()["plug-fridge"], simmod.START
        self.assertEqual([p.watts(t + 60 * i) for i in range(10)], self.TR["fridge"]["w"][:10])
        self.assertEqual(p.watts(t + 7 * DAY + 120), 100)                       # past the end: starts again
        self.assertTrue(p.source.startswith("Real recording"))
        self.assertIsNone(self.plug()["plug-heater"].trace)                     # no recording for it: assumed model

    def test_faults_switch_and_slider_act_on_the_recording(self):
        p, t = self.plug()["plug-fridge"], simmod.START
        self.assertEqual((p.watts(t, "drift"), p.watts(t + 120, "drift")), (2.5, 125))
        self.assertEqual((p.watts(t + 120, "weak"), p.watts(t, "standby")), (60, 27))
        self.assertEqual([p.watts(t + 60 * i, "cycling") for i in (0, 4, 6)], [100, 2, 100])   # looks 6 minutes ahead
        p.mode = "on"; self.assertEqual(p.watts(t), 100)
        p.mode = "off"; self.assertEqual(p.watts(t + 120), 0)
        p.mode, p.rated = "auto", 150
        self.assertEqual((p.watts(t), p.watts(t + 120)), (2, 150))              # scales running watts, not standby

    def test_shipped_slice_lines_up_with_the_simulated_calendar(self):
        tr = simmod.load_traces()
        self.assertEqual(set(tr), {"fridge", "aircon", "tv", "washer"})
        for v in tr.values():
            self.assertEqual(len(v["w"]) % (7 * 1440), 0)                       # whole weeks, so wrapping keeps the weekday
            self.assertGreater(len(v["w"]), 60 * 1440)


class AppTests(unittest.TestCase):
    """The product, fed only through the calls a plug or the app page makes (the house's `send` is app.post)."""

    @classmethod
    def setUpClass(cls):
        cls.app = server.App(); cls.app.auto_train = False        # no background training: tests place their own model
        cls.house = housemod.House(cls.app.post, simmod.load_traces())
        cls.house.install_family()
        cls.id = cls.house.ids                                     # appliance in the house -> the id its plug gave itself
        cls.app.check()                                            # first pass: learns each plug's normal

    def run_minutes(self, n):
        for _ in range(n):
            self.house.step()
            if (self.app.now // 60) % 5 == 0:
                self.app.check()

    @classmethod
    def tearDownClass(cls):
        cls.app.store.db.close()

    def test_history_loaded_and_totals_match_store(self):
        now = self.app.now
        self.assertEqual(now, self.house.sim.t)                # the product's clock comes from the readings alone
        k = self.app.store.kwh(self.id["plug-fridge"], now - 7 * DAY, now)
        self.assertTrue(5 < k < 13, k)                         # about 0.8-1.8 kWh/day
        d = {x["name"]: x for x in self.app.get("/api/devices", {})["devices"]}
        self.assertEqual(set(d), {"Fridge", "Bedroom air cond", "Water heater", "TV + router", "My laptop"})
        self.assertEqual((d["Fridge"]["type"], d["Fridge"]["klass"], d["Fridge"]["room"], d["Fridge"]["learning"]), ("fridge", "must-run", "Kitchen", None))
        self.assertEqual((d["Fridge"]["state"], d["Fridge"]["voltage_V"]), ("ON", 230.0))
        self.assertAlmostEqual(d["Fridge"]["current_A"] * 230, d["Fridge"]["watts"], delta=0.2)
        self.assertNotIn("fridge", " ".join(x["id"] for x in d.values()))      # ids say nothing about the appliance

    def test_app_api_never_reveals_injected_faults(self):
        sim, ac = self.house.sim, self.id["plug-aircon"]
        sim.inject("plug-aircon", "drift")
        text = json.dumps([self.app.get("/api/devices", {}), self.app.get("/api/device", {"id": [ac]})])
        self.assertNotIn("fault", text); self.assertNotIn("drift", text); self.assertIn('"state"', text)
        self.assertEqual(self.house.get("/api/sim", {})["plugs"][1]["fault"], "drift")   # the house's own screen does know
        self.assertIsNone(self.app.get("/api/sim", {}))                                   # and the product has no such screen
        sim.control("plug-aircon", mode="on")
        text = json.dumps([self.app.get("/api/devices", {}), self.app.get("/api/device", {"id": [ac]})])
        self.assertNotIn('"mode"', text)                                                  # nor manual control
        sim.control("plug-aircon", mode="auto")
        sim.inject("plug-aircon", None)

    def test_new_plug_pairs_by_password_then_learns_before_it_is_judged(self):
        a, h, w = self.app, self.house, self.house.plugs["plug-washer"]
        ids = lambda: [d["id"] for d in a.get("/api/devices", {})["devices"]]
        found = lambda: [f["id"] for f in a.get("/api/discover", {})["found"]]
        h.step(); self.assertNotIn(w["id"], ids()); self.assertEqual(found(), [])        # no plug fitted: silent
        self.assertEqual(a.readings(dict(deviceId=w["id"], power_W=5))["unknown"], [w["id"]])   # and not listened to
        self.assertEqual(h.post("/api/sim", dict(action="plug", device="plug-washer")), dict(ok=True))
        h.beacon(); self.assertEqual((w["state"], found()), ("BROADCASTING_WIFI", [w["id"]]))
        self.assertIsNone(a.post("/api/pair", dict(deviceId="plug_nobody", password="x")))
        a.post("/api/pair", dict(deviceId=w["id"], password="00000000", name="Washer", type="washer", room="Laundry"))
        h.beacon(); self.assertEqual(w["state"], "CONNECTING")
        h.beacon(); self.assertEqual((w["state"], a.get("/api/pair", {"id": [w["id"]]})["status"]), ("BROADCASTING_WIFI", "wrong_password"))
        self.assertNotIn(w["id"], ids())
        a.post("/api/pair", dict(deviceId=w["id"], password=w["password"], name="  Washer  ", type="washer", room="Laundry"))
        h.beacon(); h.beacon()
        self.assertEqual((w["state"], a.get("/api/pair", {"id": [w["id"]]})["status"], found()), ("PAIRED_ONLINE", "paired", []))
        self.assertEqual((w["label"], h.plugs["plug-fridge"]["label"]), ("Washer", "Fridge"))   # the plug is told its name
        h.step()
        d = a.get("/api/device", {"id": [w["id"]]})
        self.assertEqual((d["name"], d["type"], d["klass"], d["room"], d["learning"]), ("Washer", "washer", "switchable", "Laundry", dict(day=0, of=14)))
        self.assertTrue(d["online"]); self.assertNotIn(w["id"], a.base)                   # reporting, not judged yet
        with self.assertRaises(ValueError):
            a.post("/api/device", dict(id=w["id"], klass="whatever"))
        self.assertIsNone(a.post("/api/device", dict(id="nope", name="x")))

    def test_remote_switch_reaches_the_plug_on_its_next_report(self):
        a, h, tv = self.app, self.house, self.id["plug-tv"]
        self.assertIsNone(a.post("/api/command", dict(id="nope", state="OFF")))
        self.assertIsNone(a.post("/api/command", dict(id=tv, state="BLUE")))
        self.assertEqual(a.post("/api/command", dict(id=tv, state="OFF")), dict(ok=True))
        h.step(); h.step()                                                               # picks it up, then reports it
        d = a.get("/api/device", {"id": [tv]})
        self.assertEqual((d["state"], d["watts"], h.sim.plugs["plug-tv"].relay), ("OFF", 0.0, False))
        a.post("/api/command", dict(id=tv, state="ON")); h.step(); h.step()
        d = a.get("/api/device", {"id": [tv]})
        self.assertEqual(d["state"], "ON"); self.assertGreater(d["watts"], 5)

    def test_bill_view_is_consistent(self):
        b = self.app.get("/api/bill", {})
        self.assertAlmostEqual(sum(r["kwh"] for r in b["rows"]), b["mtd_kwh"], delta=0.3)
        self.assertAlmostEqual(sum(r["rm"] for r in b["rows"]), b["proj_rm"], delta=0.05)
        self.assertTrue(b["low_rm"] <= b["proj_rm"] <= b["high_rm"])
        self.assertTrue(200 < b["proj_kwh"] < 400 and 200 < b["last_kwh"] < 400, b)   # the replayed house
        self.assertNotIn("laptop", [r["id"] for r in b["rows"]])
        self.assertEqual(self.app.post("/api/settings", dict(discount=False)), dict(ok=True))
        self.assertGreater(self.app.get("/api/bill", {})["proj_rm"], b["proj_rm"])
        self.app.post("/api/settings", dict(discount=True))

    def test_live_app_raises_one_alert_and_shows_it(self):
        a, heater, fridge = self.app, self.id["plug-heater"], self.id["plug-fridge"]
        self.house.sim.control("plug-heater", mode="on")
        self.run_minutes(60)
        v = a.get("/api/alerts", {})
        self.assertEqual([(i["device"], i["kind"]) for i in v["open"]], [(heater, "unusual_time")])
        self.assertGreater(v["open"][0]["rm_month"], 50)                          # a 3.2 kW heater left on is expensive
        dev = {d["id"]: d for d in a.get("/api/devices", {})["devices"]}
        self.assertEqual((dev[heater]["alerts"], dev[fridge]["alerts"]), (1, 0))
        self.assertEqual(a.post("/api/alerts", dict(ack=v["open"][0]["id"])), dict(ok=True))
        self.assertTrue(a.get("/api/alerts", {"device": [heater]})["open"][0]["ack"])
        self.assertEqual(a.get("/api/alerts", {"device": [fridge]})["open"], [])
        self.house.sim.control("plug-heater", mode="auto")

    def test_advisor_never_cuts_must_run_and_lands_inside_the_target(self):
        b = self.app.bill()
        for target in (40, 50, 55, 60, 65):
            p = core.advise(b, target, run_w=self.app.run_w())
            fridge = next(r for r in p["rows"] if r["id"] == self.id["plug-fridge"])
            self.assertEqual((fridge["klass"], fridge["cut_kwh"]), ("must-run", 0))
            for r in p["rows"]:                                           # cutting x% cannot free more than x% of a day
                self.assertLessEqual(r["hours_less"] or 0, 24 * r["cut_pct"] / 100 + 0.1, r)
            if p["status"] == "plan":
                self.assertLessEqual(p["new_rm"], target)
                self.assertGreater(p["new_rm"], target - 8)              # does not over-cut (band cliffs allowed for)
                pcts = {r["cut_pct"] for r in p["rows"] if 0 < r["cut_pct"] < 50}
                self.assertLessEqual(len(pcts), 1)                        # same % for every appliance not at its cap
        low = core.advise(b, 10)
        self.assertEqual(low["status"], "unreachable"); self.assertGreater(low["new_rm"], 10)
        self.assertTrue(all(r["cut_pct"] <= 100 * core.MAX_CUT[r["klass"]] for r in low["rows"]))
        self.assertIn(core.advise(b, 500)["status"], ("under", "on_track"))
        self.assertIsNone(self.app.get("/api/advisor", {"target": ["abc"]}))
        self.assertEqual(self.app.get("/api/advisor", {"target": ["60"]})["target"], 60)

    def test_chat_is_scripted_without_a_key_and_survives_a_dead_language_model(self):
        def boom(*a, **k):
            raise OSError("no internet")
        calls = []
        class Fake:
            def __init__(self, req, timeout=0):
                calls.append(json.loads(req.data))
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(dict(choices=[dict(message=dict(content=" Nicely phrased. "))])).encode()
        plain = self.app.chat("I want to pay RM45", key="", opener=boom)
        self.assertEqual((plain["source"], plain["intent"], plain["note"]), ("scripted", "target", None))
        self.assertIn("Fridge", plain["reply"]); self.assertLessEqual(plain["facts"]["plan"]["new_rm"], 45)
        dead = self.app.chat("I want to pay RM45", key="k", opener=boom)
        self.assertEqual((dead["source"], dead["reply"]), ("scripted", plain["reply"])); self.assertIn("unavailable", dead["note"])
        live = self.app.chat("I want to pay RM45", key="k", opener=Fake)
        self.assertEqual((live["source"], live["reply"], live["draft"]), ("deepseek", "Nicely phrased.", plain["reply"]))
        self.assertEqual(live["facts"], plain["facts"])                   # the model cannot change the figures shown
        self.assertEqual(calls[0]["model"], "deepseek-flash"); self.assertIn("DRAFT ANSWER", calls[0]["messages"][1]["content"])
        self.assertEqual(calls[0]["thinking"], dict(type="disabled"))    # reasoning would eat max_tokens and return nothing
        self.assertEqual(self.app.chat("hello", key="k", opener=boom)["intent"], "help")   # nothing to rephrase: no call
        self.app.llm_n = server.LLM_DAILY_CAP                              # a public page cannot spend the key without limit
        capped = self.app.chat("I want to pay RM45", key="k", opener=boom)
        self.assertEqual((capped["source"], capped["reply"]), ("scripted", plain["reply"])); self.assertIn("limit", capped["note"])
        self.app.llm_n = 0
        for q, intent in (("why is my bill higher?", "why"), ("what uses the most?", "most"), ("is anything wrong?", "alerts"),
                          ("ok so why is my bill RM 10 higher?", "why"), ("why did my bill go up 58 kWh", "why"),
                          ("is the fridge ok?", "alerts"), ("can I get the bill under 50", "target")):
            self.assertEqual(self.app.chat(q, key="")["intent"], intent, q)

    def test_chat_explains_the_change_in_hours_and_offers_the_next_question(self):
        b, run_w = self.app.bill(), self.app.run_w()
        b["last_kwh"], b["last_rm"] = b["proj_kwh"] - 60, core.bill(b["proj_kwh"] - 60, b["discount"])   # a month 60 kWh lower
        ac = next(r for r in b["rows"] if r["klass"] != "must-run" and run_w.get(r["id"], 0) > 20)
        for r in b["rows"]:
            r["delta_kwh"] = 60.0 if r is ac else 0.0
        text, facts, intent = core.answer("why is my bill higher?", b, [], run_w)
        h = facts["changes"][0]["hours_day"]
        self.assertAlmostEqual(h * b["days"] * run_w[ac["id"]] / 1000, 60, delta=0.05 * b["days"] * run_w[ac["id"]] / 1000 + 0.01)
        self.assertIn(f"Running the {ac['name']}", text); self.assertIn("less a day would take back its 60.0 kWh", text)
        nxt = core.followups(intent, facts)
        self.assertEqual(nxt[0], f"Get back to RM {round(b['last_rm'])}"); self.assertNotIn(core.ASK["why"], nxt)
        self.assertEqual(core.answer(nxt[0], b, [], run_w)[2], "target")              # the chip is a question the chat understands
        hello, facts, intent = core.answer("hello", b, [], run_w)
        self.assertEqual(intent, "help"); self.assertIn(f"RM {b['proj_rm']:.2f}", hello); self.assertIn("more than last month", hello)
        ac["klass"] = "must-run"                                                       # never advise cutting what must stay on
        self.assertNotIn("Running the", core.answer("why is my bill higher?", b, [], run_w)[0])

    def test_learned_layer_only_speaks_after_hours_and_when_named_checks_are_silent(self):
        a, tv = self.app, self.id["plug-tv"]
        class Odd:                                    # stand-in network that always finds the pattern unusual
            thr = 0.0
            def score(self, v): return 1.0
            def explain(self, v, scale): return 14, 700.0, 100.0, 2.5
        a.models[tv] = (100.0, Odd()); a.streak[tv] = 0
        kinds = lambda: [i["kind"] for i in a.get("/api/alerts", {"device": [tv]})["open"]]
        self.run_minutes(60 * (learned.NN_HOURS - 1))
        self.assertEqual(kinds(), [])                 # not yet: fewer than NN_HOURS hourly checks
        self.run_minutes(60 * 3)
        self.assertEqual(kinds(), ["pattern"])
        self.assertIn("14:00", a.get("/api/alerts", {"device": [tv]})["open"][0]["detail"])
        del a.models[tv]
        p = a.get("/api/proof", {})
        self.assertEqual(sorted(p)[:3], ["bill_error", "comparison", "counts"]); self.assertGreaterEqual(p["counts"]["incidents"], 1)
        fr = next(x for x in p["plugs"] if x["name"] == "Fridge")               # running power is learned, not listed anywhere
        self.assertTrue(fr["info"].startswith("Real recording") and 100 < fr["watts"] < 200, fr)

    def test_bad_readings_rejected(self):
        post = lambda **r: self.app.post("/api/readings", r)
        self.assertEqual(post(deviceId=self.id["plug-tv"], power_W=-5)["accepted"], 0)
        self.assertEqual(post(deviceId=self.id["plug-tv"], power_W=True)["accepted"], 0)
        self.assertEqual((post(deviceId="ghost", power_W=5)["accepted"], post(deviceId="ghost", power_W=5)["unknown"]), (0, ["ghost"]))
        self.assertEqual(post(readings=["junk", dict(deviceId=7)])["accepted"], 0)
        now = self.app.now
        self.assertEqual(post(deviceId="laptop", power_W=30)["accepted"], 1)
        self.assertEqual(self.app.now, now)                                    # the laptop's real clock does not move the plugs' clock


class TariffTests(unittest.TestCase):
    def test_matches_sarawak_energy_worked_examples(self):
        self.assertEqual(core.bill(450), 132.75)                      # FAQ: 450 kWh x RM0.295
        self.assertEqual(core.bill(1000), 317.44)                     # FAQ: RM310 + 6% tax on 400 kWh
        self.assertEqual(core.bill(1000, 0.25), 239.94)               # FAQ: after the 25% discount

    def test_band_rate_applies_to_all_units_so_boundaries_are_cliffs(self):
        self.assertEqual((core.bill(150), core.bill(151)), (27.00, 33.22))
        self.assertEqual((core.bill(300), core.bill(301)), (75.00, 81.27))
        self.assertEqual(core.bill(10), 5.00)                         # minimum monthly charge

    def test_budget_and_band_watch(self):
        self.assertEqual(core.kwh_for_budget(100), 370)               # 370 x 0.27 = 99.90; 371 would be 100.17
        self.assertLessEqual(core.bill(core.kwh_for_budget(100, 0.25), 0.25), 100)
        w = core.band_watch(319)
        self.assertEqual((w["limit"], w["cut_kwh"], w["saving"]), (300, 19, round(319 * 0.27 - 75, 2)))
        self.assertIsNone(core.band_watch(120))

    def test_band_above_is_the_published_cliff(self):
        a = core.band_above(285)                                       # Sarawak Energy: 300 units = RM75.00, 301 = RM81.27
        self.assertEqual((a["limit"], a["away_kwh"], a["rate_above"], a["rm_at"], a["rm_over"]), (300, 15, 0.27, 75.00, 81.27))
        self.assertEqual(core.band_above(300)["limit"], 300)           # exactly on the line is still inside the band
        self.assertIsNone(core.band_above(1301))                       # top band: nothing above it

    def test_error_band_shrinks_through_the_month(self):
        e = [core.err_pct(d) for d in (3, 7, 10, 15, 20, 28, 31)]
        self.assertEqual(e[:5], [12.5, 12.5, 10.2, 8.3, 4.8])
        self.assertEqual(e, sorted(e, reverse=True)); self.assertEqual(e[-1], 0)


class AnalyzerTests(unittest.TestCase):
    """Simulation replays: the analyzer is given readings only, never the injected change."""
    kw = dict(train_days=21, clean_days=2, fault_days=2)

    def test_no_alerts_on_normal_days_even_with_noise_and_dropouts(self):
        r = evaluate.replay(train_days=21, clean_days=6, fault_days=0, dropout=True)
        self.assertEqual((r["false"], r["counts"]["incidents"]), ([], 0))

    def test_each_change_gives_exactly_one_alert_of_the_right_kind(self):
        for label, dev, kw, want in evaluate.SCENARIOS:
            with self.subTest(label):
                r = evaluate.replay(dev, **kw, **self.kw)
                self.assertEqual([k for _, k, _ in r["opened"]], [want], r)
                self.assertEqual(r["false"], [])
                self.assertGreater(r["counts"]["raw_detections"], 50 * r["counts"]["incidents"])   # one alert, not a buzz
                self.assertLessEqual(r["counts"]["notifications"], 4)

    def test_alert_clears_itself_and_ack_stops_escalation(self):
        inc = core.Incidents()
        f = [("drift", "x", 1.0)]
        for t in (0, 300, 600):
            inc.update("d", "D", f, t, 0.2)
        self.assertEqual((len(inc.open), inc.notifications), (1, 1))
        self.assertTrue(inc.ack(1)); inc.update("d", "D", f, 4000, 0.2)
        self.assertEqual(inc.notifications, 1)                                    # acknowledged: no 1-hour reminder
        inc.update("d", "D", [], 4300, 0.2); self.assertEqual(len(inc.open), 1)   # not clear for 30 min yet
        inc.update("d", "D", [], 6000, 0.2); self.assertEqual((len(inc.open), len(inc.closed)), (0, 1))

    def test_running_level_ignores_a_standby_it_sits_in_all_day(self):
        # found on real recordings: 40 W standby for 23 h, 1800 W for 1 h. "Running" is 1800 W, not 40 W
        rows = [(d * 86400 + m * 60, 1800.0 if 600 <= m < 660 else 40.0) for d in range(6) for m in range(1440)]
        self.assertEqual(core.Baseline(rows).level, 1800.0)

    def test_checks_are_calibrated_on_the_appliances_own_history(self):
        # a fridge (12 min on / 22 off) whose defrost heater draws 400 W for 15 minutes every day: normal, not an overload
        w = lambda m: 400.0 if 300 <= m % 1440 < 315 else 140.0 if m % 34 < 12 else 2.0
        B = core.learn([(m * 60, w(m)) for m in range(20 * 1440)])
        self.assertEqual(B.level, 140.0)
        self.assertGreater(B.hold["overload"], 15 * 60)             # must outlast every defrost it has seen
        inc, f = core.Incidents(), [("overload", "x", 1.0)]
        t = 0
        while not inc.open:
            inc.update("d", "D", f, t, 0.2, B.hold); t += 300
        self.assertGreaterEqual(t - 300, B.hold["overload"])        # opened only after holding that long
        inc = core.Incidents()
        for t in (0, 300):
            inc.update("d", "D", f, t, 0.2)                         # no calibration: two checks in a row, as before
        self.assertEqual(len(inc.open), 1)


class LearnedModelTests(unittest.TestCase):
    """The small neural network on a synthetic appliance: on 18:00-22:00 every day."""
    @classmethod
    def setUpClass(cls):
        day = lambda lvl=800, start=18, end=22: [lvl if start <= h < end else 5 for h in range(24)]
        cls.rows = lambda self, days: [(d * 86400 + h * 3600 + m * 60, w) for d, prof in enumerate(days) for h, w in enumerate(prof) for m in range(0, 60, 5)]
        cls.day = staticmethod(day)
        hist = cls.rows(cls, [day() for _ in range(20)])
        cls.scale, tr = learned.training_set(hist)
        cls.net = learned.TinyAutoencoder(tr, epochs=60)

    def _score(self, last_day):
        rows = self.rows([self.day()] * 20 + [last_day])
        now = rows[-1][0] + 300
        return self.net.score(learned.profile(rows, now, self.scale)), learned.profile(rows, now, self.scale)

    def test_normal_day_passes_and_changed_days_are_flagged(self):
        self.assertLess(self._score(self.day())[0], self.net.thr)
        self.assertGreater(self._score(self.day(lvl=480))[0], self.net.thr)          # running weak: no rule covers this
        self.assertGreater(self._score(self.day(start=10))[0], self.net.thr)         # on for 8 extra hours

    def test_explain_points_at_the_changed_hour_and_direction(self):
        _, v = self._score(self.day(lvl=480))
        h, used, normal, extra = self.net.explain(v, self.scale)
        self.assertTrue(18 <= h < 22 and used < normal and extra < 0, (h, used, normal, extra))

    def test_too_little_history_gives_no_model(self):
        self.assertEqual(learned.training_set(self.rows([self.day()] * 2))[1], [])


class LaptopReaderTests(unittest.TestCase):
    def _tree(self, status, hwmon=True):
        root = Path(tempfile.mkdtemp())
        b = root / "power_supply" / "BAT1"; b.mkdir(parents=True)
        (b / "status").write_text(status + "\n"); (b / "current_now").write_text("2000000\n"); (b / "voltage_now").write_text("12000000\n")
        h = root / "hwmon" / "hwmon0"; h.mkdir(parents=True)
        (h / "name").write_text("amdgpu\n" if hwmon else "nvme\n"); (h / "power1_input").write_text("14000000\n")
        return root

    def test_battery_used_when_discharging(self):
        self.assertEqual(server.laptop_watts(self._tree("Discharging")), (24.0, "battery"))     # 2 A x 12 V

    def test_chip_sensor_used_when_plugged_in(self):
        self.assertEqual(server.laptop_watts(self._tree("Full")), (14.0, "chip"))

    def test_none_when_no_sensor(self):
        self.assertEqual(server.laptop_watts(self._tree("Full", hwmon=False)), (None, None))


class RelayTests(unittest.TestCase):
    """One public port serves both programs: the product passes /sim and /api/sim on to the house."""

    def test_house_screen_is_reachable_through_the_product_port(self):
        import threading, urllib.request, urllib.error
        from http.server import ThreadingHTTPServer

        class FakeHouse:
            got = []
            def get(self, path, q): return dict(now=1, app="http://127.0.0.1:1/app") if path == "/api/sim" else None
            def post(self, path, b): self.got.append(b); return dict(ok=True)

        class Product:
            house_url = None
            def get(self, path, q): return None
            def post(self, path, b): return None
        product = Product()
        house = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(FakeHouse(), {"/sim": "sim.html"}))
        prod = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(product, {}))
        for s in (house, prod):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close); self.addCleanup(s.shutdown)
        base = f"http://127.0.0.1:{prod.server_address[1]}"
        call = lambda path, data=None: urllib.request.urlopen(urllib.request.Request(base + path, data), timeout=5)
        with self.assertRaises(urllib.error.HTTPError):                    # no house started: nothing to pass on
            call("/api/sim")
        product.house_url = f"http://127.0.0.1:{house.server_address[1]}/"
        self.assertEqual(json.loads(call("/api/sim").read()), dict(now=1, app="/app"))      # the link a visitor can follow
        self.assertIn(b"<html", call("/sim").read().lower())
        self.assertEqual(json.loads(call("/api/sim", b'{"action":"paused","value":true}').read()), dict(ok=True))
        self.assertEqual(FakeHouse.got, [dict(action="paused", value=True)])
        product.house_url = "http://127.0.0.1:9/"                          # house gone: a clear error, not a hang or a crash
        with self.assertRaises(urllib.error.HTTPError) as e:
            call("/api/sim")
        self.assertEqual(e.exception.code, 502)


if __name__ == "__main__":
    unittest.main()
