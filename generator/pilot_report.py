"""Pilot analysis behind the horizon / warm-up / window decision (reads a `dataset.py pilot` bundle, H >= 3650).
python generator/pilot_report.py PILOT_OUT
Series columns: chain.SERIES (2 = external profit, 0 = external revenue). Seasonality (annual) and trend make the
weekly series non-stationary, so MSER-5 runs on raw weekly flows and on deseasonalised + detrended ones
(annual harmonics + quadratic trend fitted on the second half, removed from the whole series)."""
import json
import math
import sys

import numpy as np

from dataset import hw, query
from chain import SERIES, mser5
from loader import LEVELS, load_series

root = sys.argv[1]
H = json.load(open(f"{root}/meta.json"))["horizon"]
runs = [r for r in query(root).to_pylist() if r["status"] == "ok"]
s = load_series(root)  # daily flows -> cumulative (levels stay), as chain.simulate returns them
V = dict(zip(s["run_id"].tolist(), np.stack([s[c] if c in LEVELS else s[c].cumsum(1, dtype=float) for c in SERIES], 2).astype(float)))
cfg = {}
for r in runs:
    cfg.setdefault(r["config_id"], []).append(r)
Y = 365
q = lambda x, p=(50, 90, 95): " ".join(f"p{a} {b:.0f}" for a, b in zip(p, np.percentile(x, p)))


def deseason(x):
    t = np.arange(len(x)) / (Y / 7)  # years
    X = np.column_stack([np.ones_like(t), t, t * t] + [f(2 * math.pi * k * t) for k in (1, 2) for f in (np.sin, np.cos)])
    h = len(x) // 2
    b = np.linalg.lstsq(X[h:], x[h:], rcond=None)[0]
    return x - X[:, 1:] @ b[1:]


# 1. Warm-up: MSER-5 cut (days), median over replications per config.
cuts = {k: [] for k in ("profit raw", "profit deseas.", "inventory raw", "inventory deseas.", "cover deseas.")}
for rs in cfg.values():
    c = {k: [] for k in cuts}
    for r in rs:
        w = V[r["run_id"]][: H // 7 * 7: 7]
        f = np.diff(w, axis=0, prepend=0)
        cover = w[:, 5] / np.maximum(f[:, 3] / 7, 1e-9)  # days of demand on hand
        for k, x in (("profit raw", f[:, 2]), ("profit deseas.", deseason(f[:, 2])), ("inventory raw", w[:, 5]),
                     ("inventory deseas.", deseason(w[:, 5])), ("cover deseas.", deseason(cover))):
            c[k].append(7 * mser5(x))
    for k in cuts:
        cuts[k].append(np.median(c[k]))
print(f"{len(cfg)} configs, {len(runs)} runs, horizon {H} d")
print("MSER-5 warm-up (days, median over reps per config):")
for k, v in cuts.items():
    print(f"  {k:18s} {q(v, (50, 75, 90, 95, 99))}  share <= 365 d {np.mean(np.array(v) <= 365):.2f}, <= 730 d {np.mean(np.array(v) <= 730):.2f}")

# Transient by year: config means of fill, margin (profit / ext revenue), cover; paired difference to years 4-6.
def yearly(y, k):
    e = y[np.arange(Y - 1, H, Y)]
    d = np.diff(e, axis=0, prepend=0)
    return d[:, 4] / d[:, 3], d[:, 2] / d[:, 0], np.array([y[a:a + Y, 5].mean() for a in range(0, len(e) * Y, Y)]) / (d[:, 3] / Y)


A = np.array([np.mean([yearly(V[r["run_id"]], 0) for r in rs], 0) for rs in cfg.values()])  # [cfg, 3, years]
ny = A.shape[2]
ref = A[:, :, 3:6].mean(2, keepdims=True)
print("\nyear-by-year (config means of reps): mean and 95% CI of (year - mean of years 4-6)")
for j, name in enumerate(("fill", "margin", "cover d")):
    dd = A[:, j] - ref[:, j]
    rel = dd / np.abs(ref[:, j]) if name == "cover d" else dd
    print(f"  {name:8s}" + " ".join(f"y{k + 1} {rel[:, k].mean():+.4f}±{1.96 * rel[:, k].std() / math.sqrt(len(rel)):.4f}"
                                    for k in range(ny)))

# 2. Precision of whole-year windows: 95% CI half-width over reps (t, df = reps - 1).
def win(y, a, b):
    d = y[b - 1] - y[a - 1]
    return d[4] / d[3], d[2] / (b - a), d[0] / (b - a)


print("\nprecision over reps: profit hw / ext revenue, fill hw (abs.)")
for W in (Y, 2 * Y):
    for T in range(1, (H - W) // Y + 1):
        pr, fh = [], []
        for rs in cfg.values():
            x = np.array([win(V[r["run_id"]], W, W + T * Y) for r in rs])
            pr.append(hw(x[:, 1]) / x[:, 2].mean())
            fh.append(hw(x[:, 0]))
        pr, fh = np.array(pr), np.array(fh)
        print(f"  W={W // Y}y T={T}y: profit p50 {np.median(pr):.4f} p90 {np.percentile(pr, 90):.4f} share<=1% {np.mean(pr <= .01):.2f}"
              f" | fill p50 {np.median(fh):.4f} p90 {np.percentile(fh, 90):.4f} share<=0.01 {np.mean(fh <= .01):.2f} <=0.02 {np.mean(fh <= .02):.2f}")

# Configs with outages: precision vs the rest (rare events make short windows noisy).
W = Y
for T in (2, 3, 5):
    for lab, m in (("outages", lambda r: r["outage_p"] > 0), ("none", lambda r: r["outage_p"] == 0)):
        pr = [hw([win(V[r["run_id"]], W, W + T * Y)[1] for r in rs]) / np.mean([win(V[r["run_id"]], W, W + T * Y)[2] for r in rs])
              for rs in cfg.values() if m(rs[0])]
        print(f"  W=1y T={T}y {lab:8s} configs {len(pr):3d} profit hw/rev p50 {np.median(pr):.4f} p90 {np.percentile(pr, 90):.4f}")

# 3. Seasonal bias of a non-whole-year window: expected demand rate over [W, W+T] / annual-mean rate (trend off).
de = query(root, table="demands", columns=["season_amp", "season_phase"]).to_pydict()
amp, ph = np.array(de["season_amp"]), np.array(de["season_phase"])
print("\nseasonal bias of the window's mean demand rate (per stream, |E rate / annual rate - 1|):")
for T in (0.5, 1, 1.5, 2, 2.5, 3):
    a, b = 2 * math.pi * Y / Y, 2 * math.pi * (Y + T * Y) / Y
    bias = np.abs(amp * (np.cos(a + ph) - np.cos(b + ph)) / (b - a))
    print(f"  T={T}y: p50 {np.median(bias):.4f} p90 {np.percentile(bias, 90):.4f} max {bias.max():.4f}")

# 4. Trend: label drift between the two halves after a 1-year warm-up, vs the config trend.
c = {r["config_id"]: r for r in query(root, table="configs").to_pylist()}
print("\ntrend pivot(s) in configs:", sorted({x["trend_pivot"] for x in c.values()}), "(main window", (0, H), ")")
mid = (Y + H) // 2
dr = np.array([(c[k]["trend"], np.mean([win(V[r["run_id"]], mid, H)[0] - win(V[r["run_id"]], Y, mid)[0] for r in rs]),
                np.mean([win(V[r["run_id"]], mid, H)[1] / win(V[r["run_id"]], mid, H)[2] - win(V[r["run_id"]], Y, mid)[1] / win(V[r["run_id"]], Y, mid)[2] for r in rs]))
               for k, rs in cfg.items()])
for lo, hi in ((-0.11, 0), (0, 0.1), (0.1, 0.21)):
    m = (dr[:, 0] >= lo) & (dr[:, 0] < hi)
    print(f"  trend [{lo:+.2f},{hi:+.2f}) configs {m.sum():3d}: fill 2nd-1st half {dr[m, 1].mean():+.4f} (p10 {np.percentile(dr[m, 1], 10):+.4f}),"
          f" margin {dr[m, 2].mean():+.4f}")
print("  corr(trend, fill drift) %.2f, corr(trend, margin drift) %.2f" % (np.corrcoef(dr[:, 0], dr[:, 1])[0, 1], np.corrcoef(dr[:, 0], dr[:, 2])[0, 1]))

# 5. Label distributions per run over the window [W, W + T] (argv: W T in years; default 1 5).
W, T = (int(x) * Y for x in (sys.argv[2:4] if len(sys.argv) > 3 else (1, 5)))
g = {r["graph_id"]: r for r in query(root, table="graphs", columns=["graph_id", "topology", "n"]).to_pylist()}
print(f"\nlabels over days [{W}, {W + T}): fill <0.7 <0.5 p5 p50 | margin <0 p5 p50 | runs")
X = [(r["scenario"], g[r["graph_id"]]["topology"], r["n"], *win(V[r["run_id"]], W, W + T)) for r in runs]
for lab, m in ([(s, lambda x, s=s: x[0] == s) for s in ("fmcg", "electronics", "pharma", "industrial")] +
               [(t, lambda x, t=t: x[1] == t) for t in ("echelon", "random_tree")] +
               [("n<30", lambda x: x[2] < 30), ("n 30-80", lambda x: 30 <= x[2] < 80), ("n>=80", lambda x: x[2] >= 80), ("ALL", lambda x: True)]):
    f = np.array([x[3] for x in X if m(x)])
    mg = np.array([x[4] / x[5] for x in X if m(x)])
    print(f"  {lab:12s} fill {np.mean(f < .7):.3f} {np.mean(f < .5):.3f} {np.percentile(f, 5):.3f} {np.median(f):.3f} | "
          f"margin {np.mean(mg < 0):.3f} {np.percentile(mg, 5):+.3f} {np.median(mg):.3f} | {len(f)}")
wall, n = np.array([r["wall_seconds"] for r in runs]), np.array([r["n"] for r in runs])
print("\nwall s/run over %d d ~ %.4f n + %.3f (mean %.2f s, contended pilot timing)" % (H, *np.polyfit(n, wall, 1), wall.mean()))
