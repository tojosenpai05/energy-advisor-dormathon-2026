"""Learned detectors: each learns one appliance's normal 24-hour pattern from its own history and scores how
unlike that pattern the latest 24 hours are. Pure Python, no libraries.

  NearestPattern : instance-based. Score = distance to the most similar past day-pattern.
  TinyAutoencoder: a small neural network (25 -> 5 -> 25) trained to reconstruct normal patterns.
                   Score = reconstruction error.
Both only DETECT ("this is unlike your normal"); naming the type is done by core.analyze().
"""
import math
import random

HOURS = 24
NN_HOURS = 3      # the network must find the pattern unusual on 3 hourly checks in a row before it counts


def profile(rows, now, scale):
    """24 hourly mean watts (by hour of day, from the last 24 h) scaled to about 0..1, plus a weekend flag."""
    tot, cnt = [0.0] * HOURS, [0] * HOURS
    for t, w in rows:
        if t > now - 86400:
            h = (t % 86400) // 3600
            tot[h] += w; cnt[h] += 1
    return [tot[h] / cnt[h] / scale if cnt[h] else 0.0 for h in range(HOURS)] + [1.0 if ((now // 86400 + 3) % 7) >= 5 else 0.0]


def training_set(rows, step_h=2):
    """(scale, [(ts, vector)]) from an appliance's history: one pattern every `step_h` hours."""
    if len(rows) < 3 * 1440:
        return None, []
    hourly = {}
    for t, w in rows:
        k = t // 3600
        a = hourly.setdefault(k, [0.0, 0]); a[0] += w; a[1] += 1
    scale = max(max(a[0] / a[1] for a in hourly.values()), 1.0)
    out, i, t = [], 0, rows[0][0] + 86400
    t -= t % 3600
    while t <= rows[-1][0]:
        while rows[i][0] <= t - 86400:
            i += 1
        j = i
        seg = []
        while j < len(rows) and rows[j][0] <= t:
            seg.append(rows[j]); j += 1
        out.append((t, profile(seg, t, scale)))
        t += step_h * 3600
    return scale, out


def _d2(a, b):
    return sum((x - y) * (x - y) for x, y in zip(a, b))


class NearestPattern:
    name = "nearest past pattern"

    def __init__(self, train, margin=1.5):
        self.V = train
        # threshold from the data itself: how far each normal pattern is from its nearest OTHER day (not its own overlap)
        loo = [min(_d2(v, u) for s, u in train if abs(s - t) >= 36 * 3600) for t, v in train[::3]]
        self.thr = margin * max(loo)

    def score(self, v):
        return min(_d2(v, u) for _, u in self.V)


class TinyAutoencoder:
    name = "small neural network"

    def __init__(self, train, hidden=5, epochs=120, lr=0.05, seed=0, margin=1.5):
        rng = random.Random(seed)
        X = [v for _, v in train]
        n = len(X[0])
        cut = int(len(X) * 0.8)                      # last fifth of history is held out to set the threshold
        self.W1 = [[rng.uniform(-0.3, 0.3) for _ in range(n)] for _ in range(hidden)]
        self.b1 = [0.0] * hidden
        self.W2 = [[rng.uniform(-0.3, 0.3) for _ in range(hidden)] for _ in range(n)]
        self.b2 = [0.0] * n
        order = list(range(cut))
        for _ in range(epochs):
            rng.shuffle(order)
            for i in order:
                x = X[i]
                h = [math.tanh(sum(w * xi for w, xi in zip(row, x)) + b) for row, b in zip(self.W1, self.b1)]
                y = [sum(w * hi for w, hi in zip(row, h)) + b for row, b in zip(self.W2, self.b2)]
                e = [yi - xi for yi, xi in zip(y, x)]
                gh = [(1 - h[j] * h[j]) * sum(e[k] * self.W2[k][j] for k in range(n)) for j in range(hidden)]
                for k in range(n):
                    ek = lr * e[k]; row = self.W2[k]
                    for j in range(hidden):
                        row[j] -= ek * h[j]
                    self.b2[k] -= ek
                for j in range(hidden):
                    g = lr * gh[j]; row = self.W1[j]
                    for k in range(n):
                        row[k] -= g * x[k]
                    self.b1[j] -= g
        self.train_err = sum(self.score(x) for x in X[:cut]) / cut
        self.thr = margin * max(self.score(x) for x in X[cut:])

    def reconstruct(self, x):
        h = [math.tanh(sum(w * xi for w, xi in zip(row, x)) + b) for row, b in zip(self.W1, self.b1)]
        return [sum(w * hi for w, hi in zip(row, h)) + b for row, b in zip(self.W2, self.b2)]

    def score(self, x):
        return sum((y - xi) ** 2 for y, xi in zip(self.reconstruct(x), x))

    def explain(self, x, scale):
        """Where the last 24 h differ most from what the network expects: (hour, used W, expected W, extra kWh/day)."""
        y = self.reconstruct(x)
        h = max(range(HOURS), key=lambda i: abs(x[i] - y[i]))
        return h, x[h] * scale, max(y[h] * scale, 0.0), sum(x[i] - y[i] for i in range(HOURS)) * scale / 1000
