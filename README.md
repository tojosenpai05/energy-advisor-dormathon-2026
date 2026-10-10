# Energy advisor (prototype)

A per-appliance home energy advisor for a Sarawak household. Two programs: **the product** (`server.py`), which knows no appliances, and **a simulated house** (`house.py`) whose plugs replay real appliance recordings and reach the product only over HTTP, as real plugs would. Plus one live device (this laptop).
Python standard library only; nothing to install.

## Run
```bash
python3 server.py                 # product + simulated house (house.py on the next port), then open the two screens below
python3 server.py --no-house      # product alone      |  python3 house.py   house alone (--app URL --port N)
python3 server.py --port 8799     # another port      |  --lan  to reach it from a phone on the same Wi-Fi
python3 -m unittest               # 36 tests, about 30 s
python3 evaluate.py               # reruns the detector comparison (about 7 min), writes proof.json
python3 real.py                   # same detectors on the real recordings (about 10 min), writes real_proof.json; needs the AMPds2 files, see Data
DEEPSEEK_API_KEY=... python3 server.py   # chat is rephrased by DeepSeek (also read from a .env above this folder); without it the chat is scripted
```
- Start page (what it is, live figures, how to try it): http://127.0.0.1:8765/
- App (what the user sees): http://127.0.0.1:8765/app
- Simulation (stands in for the house): http://127.0.0.1:8765/sim, passed through from the house program on http://127.0.0.1:8766/

## Deploy (Render, free plan)
`render.yaml` describes one web service. In Render: New > Blueprint, pick this repo, then type `DEEPSEEK_API_KEY` when asked (leave it empty for a scripted chat).
- A host sets `PORT`; the server then binds every interface and serves all three pages on that one port.
- Anyone with the link can use every control, including the fault buttons. Language-model calls are capped at `LLM_DAILY_CAP` a day (default 300), then the chat answers from the script.
- The free plan stops the service after 15 minutes without visitors. The next visit waits about a minute, and the house then replays its history from the start. Nothing is stored between runs.
- "This laptop" shows no reading on a host: a server has no laptop power sensor.

## Two screens
**Simulation.** Rooms, each holding its appliances: live watts, the smart plug's state (no plug, ready to pair with its Wi-Fi name and password, connecting, paired), a switch (Auto / On / Off) and a power slider. Fault buttons and the log of what you changed sit in the Developer & Fault Testing Panel at the bottom; the app only ever receives each plug's readings.

**App**, one topic per page:
| Page | What it does |
|---|---|
| Home | Paired plugs by room, live watts, today's usage, online/offline, alerts that need attention. Add a plug: pick its Wi-Fi, enter its password, give it a name, type and room. A new plug shows "Learning: day N of 14" before it is judged. |
| Appliance (a sheet over Home) | Live chart, today and month so far, its alerts with explanation and cost, rename, set its room and class. The app does not switch appliances on or off. |
| Bill | Month estimate as a range, split by appliance, compared with last month, and the band watch. |
| Advisor | "I want to pay RM X": what to cut and what must stay on, plus a chat. |
| How we know (a panel, opened from the app's footer) | What is real, simulated or assumed, with the numbers. |

## How it works
| Part | File | What it really is |
|---|---|---|
| Virtual plugs, faults, manual control | `sim.py` | Fridge, air cond, TV and washer replay real recordings (`traces.json`: AMPds2, one house in Canada, winter 2013-14; the air cond is its heat pump). Water heater: assumed model |
| The house as a program: plug pairing states, HTTP client, simulation screen | `house.py` | Posts JSON readings (`deviceId, state, voltage_V, current_A, power_W, timestamp`) to the product; current is derived from watts at a nominal 230 V |
| Server, storage, pairing, laptop reader, DeepSeek call | `server.py` | http.server + SQLite (in memory) |
| Tariff, bill, named checks, alerts, advisor, scripted chat | `core.py` | Arithmetic, statistics and rules |
| Learned detector | `learned.py` | A small neural network (25-5-25 autoencoder) per appliance, hand-written |
| Replay and detector comparison | `evaluate.py` | Simulation harness |

Detect: named statistical checks first (fast, they say what kind), then the neural network as a second layer for patterns the checks did not anticipate. One alert per problem: it updates, clears itself, and can be acknowledged.

## Results (the "How we know" panel has the full tables)
- **Real data:** month-estimate error on a real household (UCI household power data, one home in France): 12.5% at day 7, 8.3% at day 15, 4.8% at day 20. No model beat this arithmetic.
- **Real rule:** the tariff code reproduces Sarawak Energy's three published examples to the cent. A band's rate applies to every unit.
- **Simulation, 64 randomized hidden changes + 24 normal days:** fixed watt limit 23/64; simple statistics 54/64; small neural network 62/64; both together (what the app runs) 62/64 at a median 3.3 h, about one false alert per 12 normal days.
- **Real recordings, injected faults (`real_proof.json`):** AMPds2, four appliances, settings tuned on year 1 and reported on year 2. Of 120 injected faults: fixed watt limit 7; simple statistics 57; small neural network 40; both together 66 at a median 4.1 h, with about 0.054 false alerts per appliance per day (one per appliance every 18 days). 4 of the 66 are doubtful: the untouched recording alarmed in the same two days. Weakest: running weak 2/16.

## Data credit
`traces.json` is cut from AMPds2 (Makonin et al., Scientific Data 2016), released under CC BY 4.0. `python3 real.py traces` recuts it from the original files, which are not in this repo.

## Limits
- We wrote both the simulated changes and the detectors: the comparison is not real-world accuracy.
- No real smart plug. The laptop reading is processor + graphics chip power when plugged in (lower than wall draw), whole laptop from the battery when unplugged (that path is tested with fake sensor files only).
- The water heater's wattage and schedule and the advisor's cut limits are assumptions. The replayed house is Canadian, not a Sarawak home, and its faults are injected by us.
- The language model (DeepSeek) only rephrases an answer the calculation already produced; it was checked against the live service on 2026-10-11. Nothing checks that its wording keeps every figure unchanged.
- Storage is in memory: restarting the server regenerates the same simulated history.

## Data
- `traces.json` is a 19-week cut of **AMPds2**, licensed CC BY 4.0: Makonin, S., Ellert, B., Bajić, I. V. & Popowich, F. "Electricity, water, and natural gas consumption of a residential house in Canada from 2012 to 2014." *Scientific Data* 3, 160037 (2016). https://doi.org/10.1038/sdata.2016.37. Changes: only the power column of four meters (fridge, heat pump, TV, washer), cut to 19 weeks from 2013-11-12 and rounded to whole watts.
- `python3 real.py` and `python3 real.py traces` need the original files (`Electricity_FGE.csv`, `HPE`, `TVE`, `CWE`), which are too large for this repo. Download them from https://doi.org/10.7910/DVN/FIE0S4 into `data/ampds2/`, two folders above this one. The app and the tests do not need them.
