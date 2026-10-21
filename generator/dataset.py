"""Build (or pilot) the supply-chain dataset: size grid -> networks (echelon DAGs ~75%, uniform trees ~25%)
-> configured chains (+ optional what-if variants) -> replicated SupplyNetPy runs -> Parquet bundle with one
filterable catalogue.

  python generator/dataset.py build  OUT --tuples 500 --graphs 50 --reps 5 --horizon H --warmup W --resume
  python generator/dataset.py build  OUT --tuples 4 --graphs 5 --reps 1   # smoke / timing (runs.wall_seconds)
  python generator/dataset.py build  OUT ... --variants 2   # + 2 what-if variants per config (same run seeds)
  python generator/dataset.py build  OUT ... --reps 1 --rep-share 0.1 --rep-count 5 --variants 2 --variant-share 0.2
         --large-share 0.03 --large-max 300   # 10% of base configs x5 reps, 20% with variants, 3% sizes 150-300
  python generator/dataset.py pilot  OUT --tuples 30  --graphs 1  --reps 5 --horizon 3650
  python generator/dataset.py warmup OUT          # MSER-5 / run-length analysis of a pilot
  python generator/dataset.py compact OUT         # after a build: sorted ~200 MB files + SHA256SUMS

Ids, seeds and splits depend only on --seed and the sizes, never on worker count or completion
order. Configs are written in shards as they finish (configure runs in the workers); --resume
skips shards whose runs part exists. A failed run is kept with status="error" and its message.

Bundle (every table is Parquet, joined on integer ids; types and column order in schemas()):
  graphs        graph_id, split, topology (echelon / random_tree), roles (one letter per node, S M I D),
                s m i d n, depth, multi_parent, dc_tiers / multi_source_p / skip_p (echelon), tuple_seed, k,
                graph_hash (uint64 Weisfeiler-Lehman hash, both strata), dup_of (first graph_id with that hash),
                record (tree_grammar record, trees only), src/dst (all edges, I->D included; node id = position
                in roles), size_band (base / large: --large-share sizes above --max-nodes)
  configs       config_id, graph_id, scenario, split, param_seed, base_config_id + variant columns (what-if:
                same network and run seeds as the base config, one intervention), config knobs (KNOBS),
                regime (stress / control), unit (label of one product unit)
  nodes / edges / demands   per-config parameters (one row per node / link / customer stream);
                edges of one target: main supplier first, share = its order weight (WeightedSelect)
  runs          catalogue: run_id (= config_id * meta rep_stride + replication), config_id, replication, seed,
                status, error, filter columns (scenario, regime, n, s m i d, depth, split, size_band) + network KPIs (NET) per window; filter here, then join
  node_results  run_id, window, node_id, role, per-node KPIs (NODE) over that window; sites, customer
                streams (role D, node_id = demands.demand_id) and suppliers (infinite ones: inventory
                columns null; they are left out of the network money KPIs).
  series        run_id + one FixedSizeList<float32>[horizon] column per chain.SERIES signal: element d = day d+1
                (warm-up included; drop it with loader.load_series(days="main")). Flows (ext_revenue, net_cost,
                profit, cust_demand, cust_served, book_profit) are that day's amount, LEVELS (inventory = product units
                at plants/DCs/retailers, no suppliers; site_backorder) the end-of-day level. --no-series skips it; 64-run row groups.
After `compact`, node_results / nodes / edges / demands / series also carry split + scenario, and every
table is sorted by (split, scenario, id) in files of ~--target-mb with 64k-row groups (series: 64 runs).
Checksums: SHA256SUMS for everything but series/, series/SHA256SUMS for it, so series/ can be skipped.
"""
import argparse
import fcntl
import hashlib
import json
import math
import multiprocessing as mp
import os
import random
import signal
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from importlib.metadata import version
from itertools import islice
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
from chain import SCENARIOS, SERIES, SUPPLYNETPY_COMMIT, configure, intervene, mser5, simulate  # noqa: E402
from topology import make_graphs  # noqa: E402

VERSION = "0.7.1"
KNOBS = ("lt_cv", "margin_factor", "gross_ratio", "plant_share", "retail_share", "service_mean", "bias_mean", "bias_sd",
         "rho_mean", "finite_supply", "season_amp", "season_phase", "season_spread", "trend", "outage_p", "mtbf_mean",
         "mttr_mean", "holding_rate_year", "freight_share")
D8, I16, I32, I64, F32 = pa.dictionary(pa.int8(), pa.string()), pa.int16(), pa.int32(), pa.int64(), pa.float32()
# Network KPIs per window (runs: {window}_{kpi}) and node KPIs per window (node_results).
NET = ("fill", "profit_day", "margin", "ext_revenue_day", "book_profit_day", "revenue_day", "cost_day", "purchase_day", "raw_day",
       "mfg_day", "holding_day", "transport_day", "demand_day", "avg_inventory", "inventory_end", "backorder_end",
       "stockout_share", "outage_days")
NODE = ("fill", "demand", "shortage", "received", "fulfilled", "placed", "got", "produced", "avg_inventory",
        "level_end", "backorder_end", "stockout_share", "revenue", "cost", "purchase", "mfg", "carry", "transport",
        "outage_days", "lead_time_obs", "lead_time_obs_p90")
LEVELS = ("inventory", "site_backorder")  # series signals stored as end-of-day levels; the rest are daily flows
SERIES_RG = 64  # series runs per row group (~1.7 MB): reading a few runs touches little; 256 would save only ~1%
COUNTERS = ("revenue", "cost", "purchase", "mfg", "carry", "transport", "received", "fulfilled", "placed", "got",
            "shortage", "produced", "zero_days", "outage_days")
UNITS = {  # stored as JSON in every part file's key-value metadata
    "money": "price units of the config (raw_cost scale), no currency",
    "*_day": "window total / window days",
    "fill": "share of units demanded that were shipped at once (a site order ships whole or waits; customers take "
            "what is on hand, the rest is lost); runs: customer units served / demanded",
    "retail_price": "price to customers (= sell_price except at a DC that also has customers: sell_price is its "
                    "price to sites; book revenue uses sell_price)",
    "profit_day": "ext_revenue_day - (raw_day + mfg_day + holding_day + transport_day): internal transfers excluded",
    "margin": "profit_day / ext_revenue_day: contribution after carrying cost, not GAAP gross margin (raw "
              "purchases booked at receipt; the change in stock value over the window is <= ~2% of revenue)",
    "book_profit_day revenue_day cost_day": "book values summed over all sites: internal sales count as revenue at "
                                            "the seller's price and cost at the buyer's buy_price, which differ at "
                                            "multi-parent sites, so book profit != profit_day there",
    "ext_revenue_day": "customer sales only", "raw_day": "manufacturers' purchases from suppliers",
    "purchase_day": "all purchases (raw + internal)", "holding_day": "inventory carrying cost",
    "revenue cost purchase mfg carry transport": "money, window total",
    "demand shortage received fulfilled placed got produced": "product units, window total",
    "avg_inventory": "exact time-average level (runs: product units summed over plants, DCs, retailers; finite suppliers' raw stock only in node rows)",
    "level_end inventory_end backorder_end": "product units at the window's last day",
    "stockout_share": "share of days ending with nothing on hand, an end-of-day snapshot (not time- or arrival-"
                      "weighted; runs: over customer-facing sites)",
    "mser_profit mser_fill mser_inventory": "days", "wall_seconds": "seconds",
    "lead_time lt_mean mfg_time cycle_days review first_review eoq_days mtbf mttr outage_days": "days",
    "lead_time_var": "days^2", "flow_day forecast_day units_day supply_rate": "units / day",
    "order_cost": "money per order (fixed part of freight; the EOQ-implied order cost at the cost basis, not measured)",
    "cost (edges)": "fixed freight per order on the link (order_cost x U(0.8, 1.2)); the variable freight, "
                    "freight_share x value received, is charged at receipt and is in transport / transport_day",
    "holding_cost": "money per unit-day = holding_rate_year / 365 x consolidated cost basis (plant unit cost "
                    "reaching the site), not the transfer price",
    "transport transport_day": "fixed freight per order placed + freight_share x purchase (value received)",
    "lead_time_obs lead_time_obs_p90": "days, mean and p90 order-to-receipt over orders received in the window "
                                       "(DC / retailer: product orders incl. waiting at the supplier; plant: raw "
                                       "orders, compare with the edges' lt_mean, not lead_time = + mfg_time); "
                                       "null for customers, suppliers and sites with no receipt",
    "peak_load": "plant label: rho_actual (flow x mfg_time / batch_size) x (1 + network seasonal amplitude at the "
                 "plant) x trend factor at the worse main-window edge; > 1 = cannot keep up at that peak",
    "unit": "what one product unit is in this scenario (label only)",
    "regime": "stress (failure-rich mixture: forecast bias, price wars, wide service targets) or control (share "
              "0.2: bias_mean 0, margin_factor 1, service_mean in {0.90, 0.95, 0.98}); own random stream", "service_z": "z of the site's (s,S) safety stock",
    "rho": "utilisation at base demand (plant: flow / (batch_size / mfg_time); finite supplier: flow / supply_rate)",
    "outage_days": "days the node accepted no orders (SupplyNetPy disruption) within the window; runs: sum over all "
                   "nodes, infinite suppliers included",
    "variant": "what-if intervention (outage: variant_start day, variant_value days; capacity: batch factor; "
               "service: new z) at variant_node; base_config_id = the unchanged config with the same run seeds"}


def schemas(windows):
    """One explicit schema per table: the same column order and types in every part file."""
    c = lambda t, *names: [pa.field(k, t) for k in names]
    size = c(I16, "s", "m", "i", "d", "n", "depth", "multi_parent")
    t = dict(
        graphs=c(I32, "graph_id") + c(D8, "split", "topology") + c(pa.string(), "roles") + size + c(I16, "dc_tiers") +
        c(F32, "multi_source_p", "skip_p") + c(I64, "tuple_seed") + c(I32, "k") + c(pa.uint64(), "graph_hash") +
        c(I32, "dup_of") + c(pa.binary(), "record") + c(pa.list_(I16), "src", "dst") + c(D8, "size_band"),
        configs=c(I32, "config_id", "graph_id") + c(D8, "scenario", "split") + c(I64, "param_seed") +
        c(I32, "base_config_id") + c(D8, "variant") + c(I16, "variant_node") + c(F32, "variant_start", "variant_value") +
        c(F32, "trend_pivot", *KNOBS) + c(D8, "regime", "unit"),
        nodes=c(I32, "config_id") + c(I16, "node_id") + c(D8, "role", "stage") +
        c(F32, "flow_day", "forecast_day", "demand_var_day", "lead_time", "lead_time_var") + c(I16, "review") +
        c(F32, "first_review", "cycle_days", "eoq_days", "order_cost", "service_z", "service_target", "pipeline",
          "safety_sd") + c(I32, "s_floor", "s", "S", "Q", "initial_level", "batch_size") +
        c(F32, "rho", "peak_load", "supply_rate") + c(I64, "supply_store") + c(F32, "mtbf", "mttr") +
        c(F32, "buy_price", "sell_price", "retail_price", "holding_cost", "mfg_time", "mfg_cost", "unit_cost", "raw_cost") +
        c(pa.string(), "recipe"),
        edges=c(I32, "config_id") + c(I16, "source", "target") + c(F32, "lt_mean", "lt_cv", "share", "cost"),
        demands=c(I32, "config_id") + c(I16, "demand_id", "node_id") +
        c(F32, "units_day", "arrivals_day", "qty_mean", "qty_var", "var_day", "season_amp", "season_phase", "trend"),
        runs=c(I32, "run_id", "config_id") + c(I16, "replication") + c(I64, "seed") + c(D8, "scenario", "split", "status", "regime") +
        c(pa.string(), "error") + c(I32, "graph_id") + size + c(D8, "topology", "variant", "size_band") + c(I32, "base_config_id") +
        c(F32, "margin_factor", "service_mean", "bias_mean", "rho_mean", "season_amp", "outage_p", "wall_seconds") +
        c(I32, "mser_profit", "mser_fill", "mser_inventory") + c(pa.bool_(), "warmup_ok") +
        c(F32, *(f"{w}_{k}" for w in windows for k in NET)),
        node_results=c(I32, "run_id") + c(D8, "window") + c(I16, "node_id") + c(D8, "role") + c(F32, *NODE),
        series=c(I32, "run_id") + c(pa.list_(F32, windows["main"][1]), *SERIES))
    meta = {"version": VERSION, "units": json.dumps(UNITS)}
    return {k: pa.schema(v, metadata=meta) for k, v in t.items()}


def sizes(rng, count, lo=10, hi=150):
    """Node counts spread evenly on a log scale, so small and large networks get equal weight."""
    return [round(math.exp(rng.uniform(math.log(lo), math.log(hi)))) for _ in range(count)]


def graph_job(args):
    """Worker: (n, count, seed, stratum) -> graph rows (topology.make_graphs; ids set by the caller)."""
    return make_graphs(*args)


def variant(cfg, rng, window):
    """One what-if intervention drawn from rng -> (changed cfg, configs columns): a forced outage of 14-90 days
    inside the window at a supplier, plant or DC, a plant capacity cut (batch x 0.3-0.9), or a service-level
    change (z +- 0.5-1.5) at one site."""
    kind = rng.choice(["outage", "capacity", "service"])
    if kind == "outage":
        r = rng.choice([r for r in cfg["nodes"] if r["stage"] in "SMI"])
        days = round(rng.uniform(14, 90), 1)
        start, val = round(rng.uniform(window[0], max(window[0], window[1] - days)), 1), days
        new = intervene(cfg, kind, r["node_id"], (start, days))
    elif kind == "capacity":
        r, start, val = rng.choice([r for r in cfg["nodes"] if r["role"] == "M"]), None, round(rng.uniform(0.3, 0.9), 3)
        new = intervene(cfg, kind, r["node_id"], val)
    else:
        r, start = rng.choice([r for r in cfg["nodes"] if r["role"] in "MI"]), None
        val = round(r["service_z"] + rng.choice((-1, 1)) * rng.uniform(0.5, 1.5), 3)
        new = intervene(cfg, kind, r["node_id"], val)
    return new, dict(variant=kind, variant_node=r["node_id"], variant_start=start, variant_value=val)


def split_of(h):
    """70/15/15 from the canonical-record hash: identical graphs always land in the same split."""
    x = h % 20
    return "train" if x < 14 else "val" if x < 17 else "test"


T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
        2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def hw(x):
    """95% CI half-width of the mean of x (Student t, df = len - 1); nan below 2 values."""
    x = np.asarray(x, float)
    return x.std(ddof=1) * (T975[len(x) - 2] if len(x) <= 31 else 1.96) / math.sqrt(len(x)) if len(x) > 1 else math.nan


def mser_cuts(y):
    """Warm-up check stored with every run: MSER-5 cut (days) of weekly (external) profit, fill and inventory.
    warmup_ok uses profit only: fill sits near 1.0 in many runs, so its cut is noise, and inventory follows the
    demand trend (v2 pilot: inventory cut > 1 year in 74% of runs with |trend| > 0.08, 12% with |trend| < 0.03)."""
    w = y[: len(y) // 7 * 7: 7]
    flow = np.diff(w, axis=0, prepend=0)
    fill = np.divide(flow[:, 4], flow[:, 3], out=np.ones(len(w)), where=flow[:, 3] > 0)
    return dict(mser_profit=7 * mser5(flow[:, 2]), mser_fill=7 * mser5(fill), mser_inventory=7 * mser5(w[:, 5]))


def run_job(job):
    """Worker: one (config, replication). Returns plain rows.
    Node fill = 1 - shortage / demand: the share of units asked of a node that it shipped at once.
    A site-to-site order adds its full size to the supplier's `received`; it ships whole or waits, so its
    shortage is the whole order if it waits (chain._track; SupplyNetPy's own shortage, order - level,
    credits the same stock to every queued order). A customer order adds only the served units to
    `received` and the rest is lost (consume_available). So demand =
    received + customer units lost, shortage = site-order shortage + customer units lost.
    fulfilled / (fulfilled + shortage) would count a late delivery twice. On a customer row (role D):
    demand = placed, shortage = placed - got. avg_inventory = exact time-average level (chain._track); network
    avg_inventory / inventory_end = product units at plants, DCs, retailers (finite suppliers only in node rows). Infinite suppliers get rows (orders, revenue, outage_days) with null
    inventory columns; they are left out of the network money KPIs (as in book_profit) but not outage_days."""
    cfg, run_id, seed, horizon, windows, keep_series, timeout = job
    t0 = time.perf_counter()
    y, snaps = simulate(cfg, seed, horizon, checkpoints=sorted({t for w in windows.values() for t in w if t > 0}),
                        timeout=timeout)
    role = {r["node_id"]: r["role"] for r in cfg["nodes"]}
    price = {r["node_id"]: r.get("retail_price") for r in cfg["nodes"]}
    inf = {r["node_id"] for r in cfg["nodes"] if r["role"] == "S" and not r.get("supply_rate")}  # infinite suppliers
    shop = {m["demand_id"]: m["node_id"] for m in cfg["demands"]}  # customer stream (D node) -> its site
    net_rows, node_rows = [], []
    for name, (a, b) in windows.items():
        A = y[a - 1] if a > 0 else np.zeros(len(SERIES))
        B, days = y[b - 1], b - a
        sa = {r["node"]: r for r in snaps.get(a, [])}
        rows = []
        for r in snaps[b]:
            p, v = sa.get(r["node"], {}), int(r["node"][1:])  # "n12" site / "d4" customer stream -> node id
            lt = [d for t, d in r.get("lead") or () if a < t <= b]
            rows.append(dict(node_id=v, role=role[v], level_end=r["level"], backorder_end=r["site_backorder"],
                             lead_time_obs=float(np.mean(lt)) if lt else None,
                             lead_time_obs_p90=float(np.percentile(lt, 90)) if lt else None,
                             avg_inventory=None if r["level_days"] is None else (r["level_days"] - p.get("level_days", 0)) / days,
                             **{k: r[k] - p.get(k, 0) for k in COUNTERS}))
        lost = {}  # customer units turned away, per customer-facing site
        for r in rows:
            if r["role"] == "D":
                r["shortage"] = r["placed"] - r["got"]
                lost[shop[r["node_id"]]] = lost.get(shop[r["node_id"]], 0) + r["shortage"]
        tot = lambda k, roles="SMID": sum(r[k] for r in rows if r["role"] in roles and r["node_id"] not in inf)
        ext = sum(r["got"] * price[shop[r["node_id"]]] for r in rows if r["role"] == "D")
        demand, served = B[3] - A[3], B[4] - A[4]
        profit = ext - tot("purchase", "M") - tot("mfg") - tot("carry") - tot("transport")
        net_rows.append({f"{name}_{k}": v for k, v in dict(
            fill=served / demand if demand else None, profit_day=profit / days, margin=profit / ext if ext else None,
            ext_revenue_day=ext / days, book_profit_day=(B[7] - A[7]) / days,
            revenue_day=tot("revenue", "SMI") / days, cost_day=tot("cost", "SMI") / days,
            purchase_day=tot("purchase") / days, raw_day=tot("purchase", "M") / days, mfg_day=tot("mfg") / days,
            holding_day=tot("carry") / days, transport_day=tot("transport") / days, demand_day=demand / days,
            avg_inventory=sum(r["avg_inventory"] or 0 for r in rows if r["role"] != "S"), inventory_end=B[5], backorder_end=B[6],  # backorder: units owed between sites
            stockout_share=sum(r["zero_days"] for r in rows if r["node_id"] in lost) / (len(lost) * days),
            outage_days=sum(r["outage_days"] for r in rows)).items()})  # infinite suppliers included
        for r in rows:
            site = r["role"] != "D"
            zd = r.pop("zero_days")
            if site:  # site-order units not available when ordered (chain._track) + customer units lost
                r["shortage"] += lost.get(r["node_id"], 0)
            d = r["received"] + lost.get(r["node_id"], 0) if site else r["placed"]
            node_rows.append(dict(run_id=run_id, window=name, demand=d, fill=1 - r["shortage"] / d if d else None,
                                  stockout_share=zd / days if site and r["node_id"] not in inf else None, **r))
    cuts = mser_cuts(y)
    row = dict(run_id=run_id, wall_seconds=round(time.perf_counter() - t0, 3), **cuts,
               warmup_ok=cuts["mser_profit"] <= windows["main"][0])
    for r in net_rows:
        row.update(r)
    if keep_series:  # cumulative -> daily flows (in float64: float32 cumulative sums lose the daily digits)
        flow = [k for k, c in enumerate(SERIES) if c not in LEVELS]
        y = y.copy()
        y[1:, flow] = np.diff(y[:, flow], axis=0)
    return row, node_rows, (y.astype(np.float32) if keep_series else None)


def table(rows, schema=None):
    """Rows -> Table under `schema` (missing key -> null, unknown key or out-of-range value -> error);
    without one, over the union of keys (from_pylist keeps only the first row's)."""
    if schema is None:
        return pa.table({k: [r.get(k) for r in rows] for k in dict.fromkeys(k for r in rows for k in r)})
    extra = {k for r in rows for k in r} - set(schema.names)
    assert not extra, f"columns missing from the schema: {extra}"
    fl = lambda f, v: float(v) if v is not None and pa.types.is_floating(f.type) else v  # int > 2^24 -> float32 refuses
    return pa.table([pa.array([fl(f, r.get(f.name)) for r in rows], f.type) for f in schema], schema=schema)


def save_json(path, obj):
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


@contextmanager
def pool(workers):
    """Spawned workers; a dead worker raises BrokenProcessPool (mp.Pool would hang). On any error or
    kill the workers are terminated, not awaited (a running simulation can take up to --run-timeout)."""
    ex = ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"))
    try:
        yield ex
    except BaseException:
        for p in mp.active_children():
            p.terminate()
        raise
    finally:
        ex.shutdown(cancel_futures=True)


def pq_options(schema):
    """zstd; floats and float lists BYTE_STREAM_SPLIT (no dictionary), everything else dictionary-encoded."""
    fl = [f.name for f in schema if pa.types.is_floating(f.type)]
    ls = [f.name for f in schema if pa.types.is_fixed_size_list(f.type) and pa.types.is_floating(f.type.value_type)]
    return dict(compression="zstd", use_dictionary=[f.name for f in schema if f.name not in fl + ls],
                column_encoding={**dict.fromkeys(fl, "BYTE_STREAM_SPLIT"),
                                 **dict.fromkeys((f"{c}.list.element" for c in ls), "BYTE_STREAM_SPLIT")})


def write(out, name, t, part):
    if t is not None and len(t):
        (out / name).mkdir(parents=True, exist_ok=True)
        tmp = out / name / f".part-{part:05d}.tmp"  # dot prefix: ignored by readers until renamed
        pq.write_table(t, tmp, row_group_size=SERIES_RG if name == "series" else 1 << 16, **pq_options(t.schema))
        os.replace(tmp, out / name / f"part-{part:05d}.parquet")


def run_config(job):
    """Worker: configure one graph x scenario and run its replications -> (config_id, {table: pa.Table}).
    Any failure is recorded in runs.status / runs.error instead of stopping the build."""
    cid, g, sc, pseed, J, var, reps = job  # var: None (base config) or (base config_id, variant number >= 1)
    base = var[0] if var else cid
    tabs = {k: [] for k in ("configs", "nodes", "edges", "demands", "runs", "node_results")}
    series, cfg, err, vcols = [], {}, None, {}
    try:
        cfg = configure(g["roles"], list(zip(g["src"], g["dst"])), sc, pseed,
                        edge_years=(J["windows"]["main"][1] - J["windows"]["main"][0]) / 730)
        cfg["trend_pivot"] = J["pivot"]
        if var:
            cfg, vcols = variant(cfg, random.Random(f"{pseed}/{var[1]}"), J["windows"]["main"])
    except Exception as e:
        err = f"configure: {type(e).__name__}: {e}"
    tabs["configs"].append(dict(config_id=cid, graph_id=g["graph_id"], scenario=sc, param_seed=pseed, split=g["split"],
                                base_config_id=base, **vcols, trend_pivot=cfg.get("trend_pivot"),
                                regime=cfg.get("regime"), unit=cfg.get("unit"), **{k: cfg.get(k) for k in KNOBS}))
    tabs["nodes"] = [dict(config_id=cid, **{k: (json.dumps(v) if isinstance(v, dict) else v)
                                            for k, v in r.items() if k not in ("scenario", "forced_outages")})
                     for r in cfg.get("nodes", [])]
    tabs["edges"] = [dict(config_id=cid, **r) for r in cfg.get("edges", [])]
    tabs["demands"] = [dict(config_id=cid, **r) for r in cfg.get("demands", [])]
    for rep in range(reps):
        rid = cid * J["stride"] + rep
        seed = J["seed"] * 1_000_003 + base * J["stride"] + rep  # a variant reuses its base config's run seeds
        info = dict(run_id=rid, config_id=cid, replication=rep, seed=seed, scenario=sc, split=g["split"],
                    graph_id=g["graph_id"], topology=g["topology"], size_band=g["size_band"], variant=vcols.get("variant"), base_config_id=base,
                    **{k: cfg.get(k) for k in ("regime", "margin_factor", "service_mean", "bias_mean", "rho_mean", "season_amp",
                                               "outage_p")},
                    **{k: g[k] for k in ("s", "m", "i", "d", "n", "depth", "multi_parent")}, status="ok", error=None)
        try:
            if err:
                raise RuntimeError(err)
            row, nrows, y = run_job((cfg, rid, seed, J["horizon"], J["windows"], J["series"], J["timeout"]))
            tabs["runs"].append({**info, **row})
            tabs["node_results"] += nrows
            if y is not None:
                series.append((rid, y))
        except Exception as e:
            tabs["runs"].append(dict(info, status="error", error=f"{type(e).__name__}: {e}"[:2000]))
    sch = schemas(J["windows"])
    tabs = {k: table(v, sch[k]) for k, v in tabs.items() if v}
    if series:
        H = len(series[0][1])
        tabs["series"] = pa.table([pa.array([r for r, _ in series], I32)] + [pa.FixedSizeListArray.from_arrays(
            pa.array(np.concatenate([y[:, k] for _, y in series])), H) for k in range(len(SERIES))], schema=sch["series"])
    return cid, tabs


def build(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=a.resume)
    lock = open(out / ".lock", "w")  # one build per OUT: a second one would clobber the same part files
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"{out}: another build is running on this OUT")
    logf = open(out / "build.log", "a")

    def log(msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()
    rng = random.Random(a.seed)
    ns = sizes(rng, a.tuples, a.min_nodes, a.max_nodes)
    strata = ["echelon" if rng.random() < a.echelon_share else "random_tree" for _ in ns]
    lr, band = random.Random(f"{a.seed}/large"), ["base"] * len(ns)  # own stream: the base sizes stay as without it
    for k in range(len(ns)):  # large-size stratum (size extrapolation): echelon only, the tree sampler is slow there
        if lr.random() < a.large_share:
            ns[k], strata[k], band[k] = sizes(lr, 1, a.max_nodes, a.large_max)[0], "echelon", "large"
    windows = {"main": (a.warmup, a.horizon)}
    for c in a.checkpoints:  # extra post-warm-up horizons, e.g. 1 / 2 / 5 years after warm-up
        if a.warmup + c < a.horizon:
            windows[f"h{c}"] = (a.warmup, a.warmup + c)
    meta = dict(version=VERSION, supplynetpy=version("supplynetpy"), supplynetpy_commit=SUPPLYNETPY_COMMIT,
                seed=a.seed, tuples=a.tuples, graphs_per_tuple=a.graphs, min_nodes=a.min_nodes, max_nodes=a.max_nodes,
                echelon_share=a.echelon_share, variants=a.variants, variant_share=a.variant_share,
                rep_share=a.rep_share, rep_count=a.rep_count, rep_stride=max(a.reps, a.rep_count * (a.rep_share > 0)),
                large_share=a.large_share, large_max=a.large_max,
                horizon=a.horizon, warmup=a.warmup, windows=windows, reps=a.reps, scenarios=a.scenarios,
                series=a.series, shard=a.shard)
    meta = json.loads(json.dumps(meta))
    if (out / "meta.json").exists():  # resume: refuse to mix shards built with other settings
        old = json.loads((out / "meta.json").read_text())
        bad = [k for k in meta if old.get(k) != meta[k]]
        if bad:
            sys.exit(f"{out}: existing bundle differs in {bad}; use a new OUT")
        meta = old
    if meta.get("compacted"):
        sys.exit(f"{out}: compacted bundle, cannot resume into it")
    meta.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))
    save_json(out / "meta.json", meta)
    t0 = time.time()
    try:
        graph_rows, first = [], {}
        with pool(a.workers) as ex:  # own pool, so the graph tables die with its workers
            for rows, b in zip(ex.map(graph_job, [(n, a.graphs, rng.randrange(2**31), st) for n, st in zip(ns, strata)]), band):
                for g in rows:
                    g = dict(graph_id=len(graph_rows), size_band=b, **g)
                    g.update(dup_of=first.setdefault(g["graph_hash"], g["graph_id"]), split=split_of(g["graph_hash"]))
                    graph_rows.append(g)
        write(out, "graphs", table(graph_rows, schemas(windows)["graphs"]), 0)
        # index = config_id; each base config is followed by its what-if variants (--variants, for a
        # --variant-share of base configs); a --rep-share of base configs (and their variants) get --rep-count
        # replications, the rest --reps. Both picks hash (seed, base number): independent of workers and order.
        pick = lambda kind, j, p: p > 0 and random.Random(f"{a.seed}/{kind}/{j}").random() < p
        base = [(g["graph_id"], sc, rng.randrange(2**31)) for g in graph_rows for sc in a.scenarios]
        jobs = []
        for j, (gid, sc, ps) in enumerate(base):
            nv, b = a.variants * pick("variant", j, a.variant_share), len(jobs)
            reps = a.rep_count if pick("rep", j, a.rep_share) else a.reps
            jobs += [(gid, sc, ps, (b, k) if k else None, reps) for k in range(1 + nv)]
        per = max(1, round(a.shard * len(jobs) / sum(j[4] for j in jobs)))  # configs per shard (~--shard runs)
        size = lambda k: min(per, len(jobs) - k * per)
        done = {k for k in range(-(-len(jobs) // per)) if (out / "runs" / f"part-{k:05d}.parquet").exists()}
        total, skip = sum(j[4] for j in jobs), sum(j[4] for k in done for j in jobs[k * per: k * per + per])
        for f in out.glob("*/.part-*.tmp"):  # leftovers of an interrupted shard: it is recomputed whole
            f.unlink()
        for f in out.glob("*/part-*.parquet"):
            if f.parent.name != "graphs" and int(f.stem[5:]) not in done:
                f.unlink()
        log(f"{len(graph_rows)} graphs ({len(graph_rows) - len(first)} duplicates) from {len(ns)} sizes, "
            f"{len(jobs)} configs, {total} runs; {skip} already written; {a.workers} workers")
        keep = ("graph_id", "roles", "src", "dst", "split", "topology", "size_band", "s", "m", "i", "d", "n", "depth", "multi_parent")
        J = dict(stride=meta["rep_stride"], seed=a.seed, horizon=a.horizon, windows=windows, series=a.series, timeout=a.run_timeout,
                 pivot=sum(windows["main"]) / 2)  # demand trend factor = 1 mid main window
        todo = ((cid, {k: graph_rows[gid][k] for k in keep}, sc, pseed, J, var, reps)
                for cid, (gid, sc, pseed, var, reps) in enumerate(jobs) if cid // per not in done)
        buf, ran, failed, t1, last, pend = {}, 0, 0, time.time(), time.time(), set()
        with pool(a.workers) as ex:
            while True:  # at most 2 x workers configs in flight; shard content is ordered by config_id
                pend |= {ex.submit(run_config, j) for j in islice(todo, 2 * a.workers - len(pend))}
                if not pend:
                    break
                fin, pend = wait(pend, return_when=FIRST_COMPLETED)
                for cid, tabs in (f.result() for f in fin):
                    k = cid // per
                    buf.setdefault(k, []).append((cid, tabs))
                    ran += len(tabs["runs"])
                    failed += tabs["runs"]["status"].to_pylist().count("error")
                    if len(buf[k]) == size(k):  # shard complete: runs part written last, it marks the shard done
                        parts = [t for _, t in sorted(buf.pop(k), key=lambda x: x[0])]
                        for name in ("configs", "nodes", "edges", "demands", "node_results", "series", "runs"):
                            ts = [t[name] for t in parts if name in t]
                            if ts:
                                write(out, name, pa.concat_tables(ts, promote_options="permissive"), k)
                if time.time() - last >= a.log_every or skip + ran == total:
                    el, last = time.time() - t1, time.time()
                    log(f"{skip + ran}/{total} runs ({ran} this session, {failed} failed), {ran / el:.2f} runs/s, "
                        f"elapsed {el / 3600:.2f} h, ETA {(total - skip - ran) / max(ran, 1) * el / 3600:.2f} h")
    except BrokenProcessPool:
        log("aborted: a worker process died (out of memory? killed?); finished shards are kept, rerun with --resume")
        sys.exit(1)
    st = query(out, columns=["status"])["status"].to_pylist()
    meta.update(graphs=len(graph_rows), duplicate_graphs=len(graph_rows) - len(first), configs=len(jobs), runs=len(st),
                failed_runs=st.count("error"), wall_seconds=round(time.time() - t0), finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    save_json(out / "meta.json", meta)
    log(f"done: {len(st)} runs in bundle, {st.count('error')} failed")


def query(root, filter=None, table="runs", columns=None):
    """Load one table, filtered with predicate pushdown, e.g.
    query(root, (pc.field('scenario') == 'pharma') & (pc.field('n') > 100)).
    Part files may differ in schema (an all-null column, int vs float): read them under the union."""
    import pyarrow.dataset as ds
    files = sorted(str(f) for f in (Path(root) / table).glob("part-*.parquet"))
    schema = pa.unify_schemas([pq.read_schema(f) for f in files], promote_options="permissive")
    return ds.dataset(files, schema=schema, format="parquet").to_table(filter=filter, columns=columns)


def compact(a):
    """Rewrite a finished bundle into files of ~target_mb, each table sorted by (split, scenario, id) with
    64k-row groups, so filters on split / scenario / id ranges skip whole row groups. node_results, nodes,
    edges, demands and series gain split + scenario. Written to OUT.compact/ (one table at a time, a
    finished table is kept on rerun), then swapped in: OUT -> OUT.raw, OUT.compact -> OUT. Rerun after
    any crash. One pass per table: parts are already in id order, so each (split, scenario) group is
    appended to its own files in order (named part-GG-JJJJ, so name order = sort order)."""
    import shutil
    root = Path(a.out).absolute()
    new, raw = root.with_name(root.name + ".compact"), root.with_name(root.name + ".raw")
    if not root.exists() and new.exists() and raw.exists():  # crashed between the two renames
        os.rename(new, root)
        return print(f"{root}: swap finished; the uncompacted bundle is in {raw}")
    lock = open(root / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"{root}: a build is running on this OUT")
    meta = json.loads((root / "meta.json").read_text())
    if meta.get("compacted") or "finished" not in meta or meta["version"] != VERSION:
        sys.exit(f"{root}: needs a finished, uncompacted version {VERSION} bundle")
    if raw.exists():
        sys.exit(f"{raw} exists; move it away first")
    new.mkdir(exist_ok=True)
    cf = query(root, table="configs", columns=["config_id", "split", "scenario"])
    groups = sorted(set(zip(cf["split"].to_pylist(), cf["scenario"].to_pylist())))
    gcode = np.zeros(len(cf), np.int8)  # config_id -> index of its (split, scenario) group
    gcode[cf["config_id"].to_numpy()] = [groups.index(g) for g in zip(cf["split"].to_pylist(), cf["scenario"].to_pylist())]
    target = a.target_mb * 1e6
    for name in sorted(d.name for d in root.iterdir() if d.is_dir() and any(d.glob("part-*.parquet"))):
        if (new / name).exists():
            continue
        tmp = new / f".{name}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        files = sorted((root / name).glob("part-*.parquet"))
        sch = pq.read_schema(files[0])
        key = sch.names[0]  # graph_id / config_id / run_id
        if name == "graphs":
            sp = sorted(set(pq.read_table(files[0], columns=["split"])["split"].to_pylist()))
            gs = [(x,) for x in sp]
        else:
            gs = groups
            for i, c in enumerate(("split", "scenario")):
                if c not in sch.names:
                    sch = sch.insert(1 + i, pa.field(c, D8))
        sch = sch.with_metadata({"version": VERSION, "units": json.dumps(UNITS)})
        state = [dict(buf=[], rows=0, w=None, j=0, last=-1) for _ in gs]
        rg = SERIES_RG if name == "series" else 1 << 16  # also bounds the buffer: <= 12 groups x 64 runs x 58 KB

        def flush(g, final=False):
            st = state[g]
            if st["buf"] and (final or st["rows"] >= rg):
                t = pa.concat_tables(st["buf"])
                if st["w"] is None:
                    st["path"] = tmp / f"part-{g:02d}-{st['j']:04d}.parquet"
                    st["w"] = pq.ParquetWriter(st["path"], sch, **pq_options(sch))
                st["w"].write_table(t, row_group_size=rg)
                st["buf"], st["rows"] = [], 0
                if os.path.getsize(st["path"]) >= target:
                    st["w"].close()
                    st["w"], st["j"] = None, st["j"] + 1
            if final and st["w"]:
                st["w"].close()
        for f in files:
            t = pq.read_table(f)
            if name == "graphs":
                g = np.array([sp.index(x) for x in t["split"].to_pylist()])
            else:
                ids = t[key].to_numpy()
                g = gcode[ids // meta.get("rep_stride", meta["reps"]) if key == "run_id" else ids]
                for i, c in enumerate(("split", "scenario")):
                    if c not in t.column_names:
                        t = t.add_column(1 + i, c, pa.array([groups[x][i] for x in g], D8))
            t = t.cast(sch)
            for k in np.unique(g):
                part = t.filter(pa.array(g == k))
                ids = part[key].to_numpy()
                assert ids[0] >= state[k]["last"] and (np.diff(ids) >= 0).all(), f"{f}: {key} out of order"
                state[k]["last"] = ids[-1]
                state[k]["buf"].append(part)
                state[k]["rows"] += len(part)
                flush(k)
        for g in range(len(gs)):
            flush(g, final=True)
        os.rename(tmp, new / name)
    for f in root.iterdir():  # meta, logs, warmup.parquet ...
        if f.is_file() and f.name != ".lock" and not (new / f.name).exists():
            shutil.copy2(f, new / f.name)
    meta.update(compacted=dict(time=time.strftime("%Y-%m-%d %H:%M:%S"), target_mb=a.target_mb, groups=groups))
    save_json(new / "meta.json", meta)
    nsum = 0
    for d in (new, new / "series"):  # series/ has its own SHA256SUMS: a bundle without series/ still checks clean
        sums = []
        for f in sorted(p for p in d.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
            if d == new and f.relative_to(new).parts[0] == "series":
                continue
            h = hashlib.sha256()
            with open(f, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            sums.append(f"{h.hexdigest()}  {f.relative_to(d)}\n")
        if d.is_dir():
            (d / "SHA256SUMS").write_text("".join(sums))
            nsum += len(sums)
    os.rename(root, raw)
    os.rename(new, root)
    print(f"{root}: compacted ({nsum} files, check: cd {root} && sha256sum -c SHA256SUMS, and in series/); "
          f"the uncompacted bundle is in {raw} (delete it when satisfied)")


def warmup(a):
    """MSER-5 on weekly flows of each pilot run, then the 95% CI half-width over replications (profit as a
    share of external revenue, fill absolute) for whole-year measured windows. generator/pilot_report.py
    has the full analysis (transient by year, seasonal bias, trend drift, label distributions)."""
    root = Path(a.out)
    meta = json.loads((root / "meta.json").read_text())
    H = meta["horizon"]
    runs = [r for r in query(root).to_pylist() if r.get("status", "ok") == "ok"]
    from loader import load_series
    s = load_series(root)  # daily -> cumulative (levels stay levels), the layout chain.simulate returns
    Y = dict(zip(s["run_id"].tolist(), np.stack([s[c] if c in LEVELS else s[c].cumsum(1, dtype=np.float64)
                                                 for c in SERIES], 2).astype(np.float64)))
    wk = H // 7
    out = []
    by_cfg = {}
    for r in runs:
        y = Y[r["run_id"]][: wk * 7: 7]  # weekly cumulative values
        flow = np.diff(y, axis=0, prepend=0)
        fill = np.divide(flow[:, 4], flow[:, 3], out=np.full(wk, np.nan), where=flow[:, 3] > 0)
        cut = {m: 7 * mser5(np.nan_to_num(x, nan=np.nanmean(x)))
               for m, x in (("profit", flow[:, 2]), ("fill", fill), ("inventory", y[:, 5]))}
        by_cfg.setdefault(r["config_id"], []).append((r, cut))
    for cid, reps in by_cfg.items():
        r0 = reps[0][0]
        # median over replications (one stockout dip in one replication is not warm-up); profit only (see mser_cuts)
        W = int(np.median([c["profit"] for _, c in reps]))
        row = dict(config_id=cid, scenario=r0["scenario"], n=r0["n"], depth=r0["depth"], warmup_days=W,
                   **{f"warmup_{m}": int(np.median([c[m] for _, c in reps])) for m in ("profit", "fill", "inventory")})
        for T in (365, 730, 1095, 1460, 1825, 2555):
            if W + T > H:
                break
            ys = [Y[r["run_id"]] for r, _ in reps]
            pday = np.array([(y[W + T - 1, 2] - y[W - 1, 2]) / T for y in ys]) if W else \
                np.array([y[T - 1, 2] / T for y in ys])
            fill = np.array([(y[W + T - 1, 4] - y[max(W, 1) - 1, 4]) / max(y[W + T - 1, 3] - y[max(W, 1) - 1, 3], 1e-9)
                             for y in ys])
            rev = np.array([(y[W + T - 1, 0] - (y[W - 1, 0] if W else 0)) / T for y in ys])
            row[f"profit_relhw_{T}"] = float(hw(pday) / max(rev.mean(), 1e-9))  # share of external revenue
            row[f"fill_hw_{T}"] = float(hw(fill))
        # drift check: profit/day in the two halves after warm-up (median over replications)
        ys = [Y[r["run_id"]] for r, _ in reps]
        mid = (W + H) // 2
        p1 = np.median([(y[mid - 1, 2] - y[max(W, 1) - 1, 2]) / (mid - W) for y in ys])
        p2 = np.median([(y[H - 1, 2] - y[mid - 1, 2]) / (H - mid) for y in ys])
        r1 = np.median([(y[H - 1, 0] - y[max(W, 1) - 1, 0]) / (H - W) for y in ys])
        row["drift_profit"] = float((p2 - p1) / max(r1, 1e-9))  # share of external revenue
        f1 = np.median([(y[mid - 1, 4] - y[max(W, 1) - 1, 4]) / max(y[mid - 1, 3] - y[max(W, 1) - 1, 3], 1e-9) for y in ys])
        f2 = np.median([(y[H - 1, 4] - y[mid - 1, 4]) / max(y[H - 1, 3] - y[mid - 1, 3], 1e-9) for y in ys])
        row["drift_fill"] = float(f2 - f1)
        out.append(row)
    pq.write_table(table(out), root / "warmup.parquet")
    W = np.array([r["warmup_days"] for r in out])
    print(json.dumps(dict(configs=len(out), warmup_days=dict(zip(["p50", "p90", "p95", "p99", "max"],
                                                                   np.percentile(W, [50, 90, 95, 99, 100]).round().tolist()))), indent=1))
    for T in (365, 730, 1095, 1460, 1825, 2555):
        k = f"profit_relhw_{T}"
        vals = [r[k] for r in out if k in r]
        if vals:
            fh = [r[f"fill_hw_{T}"] for r in out if k in r]
            print(f"T={T:5d}d  configs={len(vals):4d}  profit half-width / ext. revenue p50 {np.nanmedian(vals):.3f} p90 "
                  f"{np.nanpercentile(vals, 90):.3f} | share <=1%: {np.mean(np.array(vals) <= 0.01):.2f} | "
                  f"fill half-width p50 {np.nanmedian(fh):.4f} p90 {np.nanpercentile(fh, 90):.4f}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["build", "pilot", "warmup", "compact"])
    p.add_argument("out")
    p.add_argument("--tuples", type=int, default=30, help="network sizes n (log-uniform), one stratum each")
    p.add_argument("--graphs", type=int, default=1, help="distinct graphs per size (fewer if n is too small)")
    p.add_argument("--echelon-share", type=float, default=0.75, help="share of sizes drawn as echelon DAGs, rest trees")
    p.add_argument("--variants", type=int, default=0, help="what-if variants per config (same run seeds as the base)")
    p.add_argument("--scenarios", nargs="+", default=list(SCENARIOS))
    p.add_argument("--reps", type=int, default=5, help="replications per config (outside the --rep-share subset)")
    p.add_argument("--rep-share", type=float, default=0, help="share of base configs (+ their variants) with --rep-count reps")
    p.add_argument("--rep-count", type=int, default=5)
    p.add_argument("--variant-share", type=float, default=1, help="share of base configs that get the --variants")
    p.add_argument("--large-share", type=float, default=0, help="share of sizes drawn from (--max-nodes, --large-max]")
    p.add_argument("--large-max", type=int, default=300)
    # Defaults from the v2 pilot (16 Oct, 200 configs x 5 reps x 10 y): the start-up transient (initial stock
    # S, free) is year 1 only; 4 measured whole years (seasonality) give profit 95% CI <= 1% of external revenue
    # for the median config (p90 2.6%), fill CI p50 1.0 pp. The trend pivot is the middle of the main window.
    p.add_argument("--horizon", type=int, default=1825)
    p.add_argument("--warmup", type=int, default=365)
    p.add_argument("--checkpoints", type=int, nargs="*", default=[], help="extra windows: warm-up + C days (whole years)")
    p.add_argument("--min-nodes", type=int, default=10)
    p.add_argument("--max-nodes", type=int, default=150)
    p.add_argument("--series", action=argparse.BooleanOptionalAction, default=True,
                   help="store the daily network series (table series/; on by default)")
    p.add_argument("--shard", type=int, default=1000, help="runs per Parquet part file (rounded to whole configs)")
    p.add_argument("--resume", action="store_true", help="continue into an existing OUT, skipping written shards")
    p.add_argument("--run-timeout", type=int, default=3600, help="wall seconds before a run is recorded as failed")
    p.add_argument("--log-every", type=int, default=60, help="seconds between progress lines (stdout + OUT/build.log)")
    # ponytail: small default so a laptop run cannot exhaust RAM (~0.1-0.25 GB per worker);
    # pass --workers explicitly on the server (e.g. cores - 2).
    p.add_argument("--workers", type=int, default=min(4, max(1, mp.cpu_count() - 2)))
    p.add_argument("--seed", type=int, default=20261005)
    p.add_argument("--target-mb", type=float, default=200, help="compact: file size per table and group")
    a = p.parse_args()
    if a.mode == "pilot":
        a.series, a.warmup, a.horizon = True, 0, max(a.horizon, 3650)
    if a.mode in ("warmup", "compact"):
        return globals()[a.mode](a)
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # `kill PID` stops cleanly, pool workers included
    build(a)


if __name__ == "__main__":
    main()
