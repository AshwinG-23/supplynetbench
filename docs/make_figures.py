"""Write the numbers and figures of the docs from a finished dataset bundle.

  generator/.venv/bin/python docs/make_figures.py data/BUNDLE

Writes docs/generated/*.md (tables pulled into the pages with --8<--) and docs/img/*.png.
Run it again on a new bundle and redeploy: no page text needs editing.
"""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pyarrow.compute as pc
import pyarrow.dataset as ds

ROOT = Path(sys.argv[1])
DOCS = Path(__file__).parent
GEN, IMG = DOCS / "generated", DOCS / "img"
GEN.mkdir(exist_ok=True)
IMG.mkdir(exist_ok=True)
sys.path.insert(0, str(DOCS.parent / "generator"))
from loader import load_series  # noqa: E402

INDUSTRY = {"fmcg": "Groceries (FMCG)", "electronics": "Electronics", "pharma": "Medicines (pharma)",
            "industrial": "Industrial spare parts"}
ROLE_NAME = {"S": "supplier", "M": "factory", "I": "warehouse / shop", "D": "customers"}
COLOR = {"S": "#8c6d31", "M": "#d6604d", "I": "#4393c3", "D": "#5aae61"}
plt.rcParams.update({"figure.dpi": 130, "font.size": 10, "axes.spines.top": False, "axes.spines.right": False})


def col(table, *names, filter=None):
    t = ds.dataset(ROOT / table).to_table(columns=list(names), filter=filter)
    return {n: t.column(n).to_numpy(zero_copy_only=False) for n in names}


def pct(x):
    return f"{100 * x:.1f}%"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


meta = json.loads((ROOT / "meta.json").read_text())
g = col("graphs", "graph_id", "n", "topology", "dc_tiers", "multi_parent", "size_band", "dup_of", "split")
c = col("configs", "config_id", "base_config_id", "scenario", "regime", "finite_supply", "outage_p", "season_amp", "variant")
r = col("runs", "run_id", "config_id", "scenario", "regime", "status", "n", "main_fill", "main_profit_day",
        "main_margin", "wall_seconds", "topology")
ok = r["status"] == "ok"
size_mb = sum(f.stat().st_size for f in ROOT.rglob("*.parquet")) / 1e6
series_mb = sum(f.stat().st_size for f in (ROOT / "series").rglob("*.parquet")) / 1e6 if (ROOT / "series").exists() else 0
base = c["config_id"] == c["base_config_id"]
distinct = g["dup_of"] == g["graph_id"]

# 1. Key numbers.
rows = [
    ("Networks (distinct shapes)", f"{len(g['graph_id']):,} ({distinct.sum():,})"),
    ("Setups (configs)", f"{len(c['config_id']):,}, of which {base.sum():,} base and {(~base).sum():,} what-if copies"),
    ("Simulations (runs)", f"{len(r['run_id']):,} ({(~ok).sum():,} failed)"),
    ("Network size", f"{g['n'].min()}–{g['n'].max()} nodes, middle (median) {int(np.median(g['n']))}"),
    ("Industries", ", ".join(INDUSTRY[s] for s in meta["scenarios"])),
    ("Simulated time per run", f"{meta['horizon']:,} days ({meta['horizon'] // 365} years), first {meta['warmup']} days are warm-up"),
    ("Train / validation / test", " / ".join(f"{(g['split'] == s).mean():.0%}" for s in ("train", "val", "test")) + " of networks"),
    ("Size on disk", f"{size_mb / 1e3:.2f} GB ({series_mb / 1e3:.2f} GB of it is the day-by-day history)"),
    ("Generator version", f"{meta['version']} (SupplyNetPy {meta['supplynetpy']})"),
]
if meta.get("wall_seconds"):
    rows.append(("Build time", f"{meta['wall_seconds'] / 3600:.1f} hours"))
(GEN / "key_numbers.md").write_text(md_table(["What", "Value"], rows))

# 2. How often each feature appears.
ech = g["topology"] == "echelon"
tiers = g["dc_tiers"][ech]
rows = [
    ("Layered network / random tree", f"{ech.mean():.0%} / {(~ech).mean():.0%} of networks"),
    ("Warehouse layers 0 / 1 / 2 / 3 (layered networks)", " / ".join(f"{(tiers == k).mean():.0%}" for k in range(4))),
    ("At least one node with two or more suppliers", f"{pct((g['multi_parent'][ech] > 0).mean())} of layered networks"),
    ("Large networks (over 150 nodes)", f"{pct((g['size_band'] == 'large').mean())} of networks"),
    ("Suppliers that can run out", f"{pct((c['finite_supply'] > 0).mean())} of setups"),
    ("Sites that can break down", f"{pct((c['outage_p'] > 0).mean())} of setups"),
    ("Seasonal demand", f"{pct((c['season_amp'] > 0).mean())} of setups"),
    ("Stress / control setups", f"{pct((c['regime'] == 'stress').mean())} / {pct((c['regime'] == 'control').mean())}"),
    ("Base setups with what-if copies", pct(np.isin(c["config_id"][base], c["base_config_id"][~base]).mean())),
]
(GEN / "feature_shares.md").write_text(md_table(["Feature", "Share"], rows))

# 3. Results per industry and regime.
rows = []
for sc in meta["scenarios"]:
    for reg in ("stress", "control"):
        m = ok & (r["scenario"] == sc) & (r["regime"] == reg)
        if m.any():
            f, p = r["main_fill"][m], r["main_profit_day"][m]
            rows.append((INDUSTRY[sc], reg, f"{m.sum():,}", f"{np.median(f):.2f}", pct((f < 0.7).mean()),
                         pct((p < 0).mean()), f"{np.nanmedian(r['main_margin'][m]):.2f}"))
(GEN / "results.md").write_text(md_table(
    ["Industry", "Group", "Runs", "Typical fill rate", "Fill rate below 0.7", "Losing money", "Typical margin"], rows))

# 4. Figures.
fig, ax = plt.subplots(figsize=(6, 3))
ax.hist(g["n"], bins=np.geomspace(max(g["n"].min(), 1), g["n"].max(), 30), color="#4393c3")
ax.set_xscale("log")
ax.set(xlabel="nodes in the network (log scale)", ylabel="networks", title="Network sizes")
fig.tight_layout()
fig.savefig(IMG / "sizes.png")

fig, ax = plt.subplots(figsize=(6, 3))
for sc in meta["scenarios"]:
    ax.hist(r["main_fill"][ok & (r["scenario"] == sc)], bins=np.linspace(0, 1, 41), histtype="step", lw=1.5,
            label=INDUSTRY[sc], density=True)
ax.set(xlabel="fill rate (share of customer demand served from the shelf)", ylabel="density",
       title="Fill rate by industry")
ax.legend(frameon=False, fontsize=8, loc="upper left")
fig.tight_layout()
fig.savefig(IMG / "fill.png")

fig, ax = plt.subplots(figsize=(6, 3))
ax.scatter(r["n"][ok], r["wall_seconds"][ok], s=3, alpha=0.3, color="#d6604d")
ax.set(xlabel="nodes in the network", ylabel="seconds", title="Computer time for one 5-year simulation")
fig.tight_layout()
fig.savefig(IMG / "run_time.png")


def draw(gid, path, title):
    """Draw one network in layers: suppliers on top, customers at the bottom."""
    t = ds.dataset(ROOT / "graphs").to_table(filter=pc.field("graph_id") == gid).to_pylist()[0]
    G = nx.DiGraph(list(zip(t["src"], t["dst"])))
    level = {}
    for v in nx.topological_sort(G):
        level[v] = max((level[u] + 1 for u in G.predecessors(v)), default=0)
    bottom = max(level[v] for v in G if t["roles"][v] != "D") + 1  # customers on their own bottom row
    for v, rl in enumerate(t["roles"]):
        G.nodes[v]["layer"] = bottom if rl == "D" else level[v]
    pos = nx.multipartite_layout(G, subset_key="layer", align="horizontal", scale=1)
    pos = {v: (x, -y) for v, (x, y) in pos.items()}
    fig, ax = plt.subplots(figsize=(7, 4))
    nx.draw_networkx_edges(G, pos, ax=ax, arrows=True, arrowsize=6, width=0.6, edge_color="#999999")
    for rl, name in ROLE_NAME.items():
        vs = [v for v in G if t["roles"][v] == rl]
        nx.draw_networkx_nodes(G, pos, nodelist=vs, node_color=COLOR[rl], node_size=40, ax=ax, label=name)
    ax.set_title(title)
    ax.legend(frameon=False, fontsize=8, loc="lower center", ncol=4, bbox_to_anchor=(0.5, -0.12))
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


rng = np.random.default_rng(0)
for name, topo, lo, hi in (("example_small", "echelon", 15, 25), ("example_medium", "echelon", 45, 70),
                           ("example_tree", "random_tree", 20, 35)):
    pick = np.flatnonzero((g["topology"] == topo) & (g["n"] >= lo) & (g["n"] <= hi) & ((g["dc_tiers"] >= 1) | (topo != "echelon")))
    if len(pick):
        k = pick[rng.integers(len(pick))]
        draw(int(g["graph_id"][k]), IMG / f"{name}.png",
             f"{'Layered network' if topo == 'echelon' else 'Random tree'}, {g['n'][k]} nodes (network {g['graph_id'][k]})")

# One run's daily history: pick a run with a visible dip in sales after the warm-up.
if (ROOT / "series").exists():
    s = load_series(str(ROOT), columns=["cust_demand", "cust_served"], run_ids=r["run_id"][ok][:400].tolist())
    gap = (s["cust_demand"] - s["cust_served"])[:, meta["warmup"]:].sum(1) / s["cust_demand"][:, meta["warmup"]:].sum(1)
    i = int(np.argsort(gap)[int(0.8 * len(gap))])  # a run with some, but not extreme, lost sales
    w = 7
    dem = s["cust_demand"][i][: len(s["day"]) // w * w].reshape(-1, w).sum(1)
    srv = s["cust_served"][i][: len(s["day"]) // w * w].reshape(-1, w).sum(1)
    weeks = np.arange(len(dem)) * w / 365
    fig, ax = plt.subplots(figsize=(7, 3))
    ax.plot(weeks, dem, lw=1, color="#999999", label="customer demand")
    ax.plot(weeks, srv, lw=1, color="#2166ac", label="served from the shelf")
    ax.axvspan(0, meta["warmup"] / 365, color="#eeeeee", label="warm-up year (not measured)")
    ax.set(xlabel="year", ylabel="units per week", title=f"Weekly sales of one simulation (run {s['run_id'][i]})")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(IMG / "series_example.png")

(GEN / "source.md").write_text(
    f"Numbers on this page come from the build `{ROOT.name}` (generator {meta['version']}, created {meta['created']}).\n")
print("wrote", sorted(p.name for p in GEN.iterdir()), sorted(p.name for p in IMG.iterdir()))
