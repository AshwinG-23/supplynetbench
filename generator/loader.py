"""Per-config graph arrays from a dataset bundle (version >= 0.6.0, compacted or not), read shard-wise.

  from loader import load_graphs, to_pyg
  for g in load_graphs("data/demo_v2", split="train", scenario="pharma"):
      g["x"], g["edge_index"], g["y_node"][0] ...

Join rules (every id is an integer):
  * configs.graph_id -> graphs.graph_id;  runs.config_id -> configs.config_id;
    run_id = config_id * rep_stride + replication (meta.json; rep_stride = reps unless --rep-share).
  * nodes / edges / demands are keyed by config_id; node_results by (run_id, window, node_id).
  * Node ids are positions in graphs.roles, 0..n-1 within a config, the same in nodes.node_id,
    edges.source/target, demands.demand_id/node_id, node_results.node_id and graphs.src/dst (a DAG: a site
    can have several parents). nodes holds one row per node in node_id order, S M I D alike.
  * A customer stream is the D node whose id is demands.demand_id; its site is demands.node_id. I->D
    edges live only in demands (and graphs.src/dst), not in edges.
  * Infinite suppliers have node_results rows with null inventory columns (avg_inventory NaN in y_node).
  * edges of one target: main supplier first; share = the weight each order is sent to it with.
  * configs.base_config_id != config_id marks a what-if variant (configs.variant*): same graph and run seeds;
    the intervention is in x (VARIANT_COLS at variant_node) and in meta.
  * A config whose configure failed (no nodes rows) is skipped; a missing table (all runs failed) reads empty.
  * graphs.dup_of = first graph_id with the same structure; duplicates share a split (split from graph_hash).
  * A failed run (runs.status == "error") has no node_results and null KPIs: its y rows are NaN.
  * 0.7.0 added nodes.peak_load (a column of x; 0 in older bundles), node_results.lead_time_obs(_p90) (pass
    them in targets=; NaN off plants / DCs) and configs.regime / unit.
Units of every column: Parquet key-value metadata "units" of any part file (dataset.UNITS).

Daily network series (table series/, never read by load_graphs; skip downloading series/ if not needed):
  from loader import load_series, aggregate
  s = load_series("data/demo_v2", split="test", columns=["profit", "inventory"], days="main")  # warm-up dropped
  s["run_id"] [R], s["day"] [D] day indices (index d = simulated day d + 1), s["profit"] [R, D] float32
  w = aggregate(s, 7)   # weekly: flows summed, levels averaged

Each yielded dict (one per config, replications stacked on the first axis of y):
  x [n, len(X_COLS)] float32 node parameters (NaN -> 0) + demand-stream parameters on D rows + stage one-hot
  edge_index [2, E] int64 (links, then I->D edges);  edge_attr [E, len(E_COLS)] float32
  y_node [replications of the config, n, len(targets)] float32 from node_results[window];  y_graph [reps, len(graph_targets)]
  meta: config_id, graph_id, split, scenario, regime (stress / control; None before 0.7.0), base_config_id, variant, variant_node, variant_start (outage start
        day of the run), variant_value (outage days / batch factor / new z), run_ids
"""
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

NODE_COLS = ["flow_day", "forecast_day", "demand_var_day", "lead_time", "lead_time_var", "review", "first_review",
             "cycle_days", "eoq_days", "order_cost", "service_z", "service_target", "s", "S", "Q", "initial_level",
             "batch_size", "rho", "peak_load", "supply_rate", "supply_store", "mtbf", "mttr", "buy_price", "sell_price",
             "retail_price", "holding_cost", "mfg_time", "mfg_cost", "unit_cost", "raw_cost"]
DEMAND_COLS = ["units_day", "arrivals_day", "qty_mean", "qty_var", "var_day", "season_amp", "season_phase", "trend"]
STAGES = "SMIRD"  # I = DC, R = inventory site with customers
# What-if intervention at its node (0 elsewhere and in base configs): kind one-hot, and for an outage its
# start (days after the window start) and length. Capacity / service changes also show in batch_size / s, S, z.
VARIANT_COLS = ["var_outage", "var_capacity", "var_service", "var_outage_start", "var_outage_days"]
X_COLS = NODE_COLS + DEMAND_COLS + VARIANT_COLS + [f"stage_{s}" for s in STAGES]
E_COLS = ["lt_mean", "lt_cv", "share", "cost", "is_customer"]
TARGETS = ("fill", "avg_inventory", "shortage", "revenue", "cost")
GRAPH_TARGETS = ("fill", "profit_day", "margin", "ext_revenue_day", "avg_inventory")
LEVELS = ("inventory", "site_backorder")  # series signals that are end-of-day levels (= dataset.LEVELS); the rest are flows


def _dataset(root, table):
    files = sorted(str(f) for f in (Path(root) / table).glob("part-*.parquet"))
    if not files:  # e.g. every run failed (no node_results) or every configure failed (no nodes)
        return None
    return ds.dataset(files, schema=pa.unify_schemas([pq.read_schema(f) for f in files], promote_options="permissive"),
                      format="parquet")


def _cols(t):  # dictionary columns decoded first: their to_numpy maps a null to the first dictionary value
    dec = lambda a: a.cast(a.type.value_type) if pa.types.is_dictionary(a.type) else a
    return {c: dec(t[c]).to_numpy(zero_copy_only=False) for c in t.column_names}


def load_graphs(root, split=None, scenario=None, window="main", targets=TARGETS, graph_targets=GRAPH_TARGETS,
                chunk=512):
    """Yield one dict per config (see module doc), `chunk` configs per read of each table."""
    root = Path(root)
    meta = json.loads((root / "meta.json").read_text())
    reps = meta.get("rep_stride", meta["reps"])  # run_id stride; a config's own replication count may be lower
    D = {t: _dataset(root, t) for t in ("configs", "nodes", "edges", "demands", "runs", "node_results")}
    compacted = bool(meta.get("compacted"))
    t0 = {w: a for w, (a, _) in meta["windows"].items()}[window]
    f = pc.scalar(True)
    if split:
        f &= pc.field("split") == split
    if scenario:
        f &= pc.field("scenario") == scenario
    cf = _cols(D["configs"].to_table(filter=f, columns=[c for c in (
        "config_id", "graph_id", "split", "scenario", "regime", "base_config_id", "variant", "variant_node",
        "variant_start", "variant_value") if c in D["configs"].schema.names]))
    # Compacted: each (split, scenario) group is sorted by id in its own files, so read group by group
    # and add the group to every filter (row-group pruning). Uncompacted: parts are in id order.
    keys = list(zip(cf["split"], cf["scenario"])) if compacted else [None] * len(cf["config_id"])
    groups = {}
    for i, k in enumerate(keys):
        groups.setdefault(k, []).append(i)
    gcols = [f"{window}_{k}" for k in graph_targets]
    for grp, idx in groups.items():
        idx = np.array(idx)[np.argsort(cf["config_id"][idx], kind="stable")]
        for s in range(0, len(idx), chunk):
            sel = idx[s: s + chunk]
            ids = cf["config_id"][sel]

            def read(table, key, extra=None, scale=1):
                if D[table] is None:  # missing table: every column empty
                    return defaultdict(lambda: np.zeros(0, np.int64))
                e = (pc.field(key) >= int(ids[0]) * scale) & (pc.field(key) < (int(ids[-1]) + 1) * scale)
                if grp:
                    e &= (pc.field("split") == grp[0]) & (pc.field("scenario") == grp[1])
                if extra is not None:
                    e &= extra
                c = _cols(D[table].to_table(filter=e))
                o = np.argsort(c[key], kind="stable")
                c = {k: v[o] for k, v in c.items()}
                c["_cid"] = c[key] // scale
                return c
            no, ed, de = read("nodes", "config_id"), read("edges", "config_id"), read("demands", "config_id")
            ru = read("runs", "run_id", scale=reps)
            nr = read("node_results", "run_id", pc.field("window") == window, scale=reps)
            span = lambda c, i: slice(*np.searchsorted(c["_cid"], [i, i + 1]))
            for j, cid in zip(sel, ids):
                a, b, m, r, y = (span(c, cid) for c in (no, ed, de, ru, nr))
                n = a.stop - a.start
                if not n:  # configure failed: no network to yield
                    continue
                assert (no["node_id"][a] == np.arange(n)).all(), cid
                x = np.zeros((n, len(X_COLS)), np.float32)
                x[:, :len(NODE_COLS)] = np.column_stack([no[c][a] if c in no else np.zeros(n) for c in NODE_COLS])
                x[de["demand_id"][m], len(NODE_COLS):len(NODE_COLS) + len(DEMAND_COLS)] = \
                    np.column_stack([de[c][m] for c in DEMAND_COLS])
                kind = cf["variant"][j]
                vn = None if kind is None else int(cf["variant_node"][j])
                if kind:
                    c0 = len(NODE_COLS) + len(DEMAND_COLS)
                    x[vn, c0 + ("outage", "capacity", "service").index(kind)] = 1
                    if kind == "outage":
                        x[vn, c0 + 3: c0 + 5] = cf["variant_start"][j] - t0, cf["variant_value"][j]
                x[np.arange(n), len(X_COLS) - len(STAGES) + np.array([STAGES.index(s) for s in no["stage"][a]])] = 1
                np.nan_to_num(x, copy=False)
                ei = np.concatenate([np.stack([ed["source"][b], ed["target"][b]]),
                                     np.stack([de["node_id"][m], de["demand_id"][m]])], 1).astype(np.int64)
                ea = np.zeros((ei.shape[1], len(E_COLS)), np.float32)
                ea[:b.stop - b.start, :4] = np.column_stack([ed[c][b] for c in E_COLS[:4]])
                ea[b.stop - b.start:, 4] = 1
                k = max(1, r.stop - r.start)  # this config's replications
                yn = np.full((k, n, len(targets)), np.nan, np.float32)
                yn[nr["run_id"][y] % reps, nr["node_id"][y]] = np.column_stack([nr[c][y] for c in targets])
                yg = np.full((k, len(gcols)), np.nan, np.float32)
                yg[ru["run_id"][r] % reps] = np.column_stack([ru[c][r] for c in gcols]).astype(np.float32)
                yield dict(x=x, edge_index=ei, edge_attr=ea, y_node=yn, y_graph=yg,
                           meta=dict(config_id=int(cid), graph_id=int(cf["graph_id"][j]), split=cf["split"][j],
                                     scenario=cf["scenario"][j], regime=cf["regime"][j] if "regime" in cf else None,
                                     base_config_id=int(cf["base_config_id"][j]),
                                     variant=kind, variant_node=vn,
                                     variant_start=None if kind != "outage" else float(cf["variant_start"][j]),
                                     variant_value=None if kind is None else float(cf["variant_value"][j]),
                                     run_ids=ru["run_id"][r].tolist()))


def load_series(root, run_ids=None, columns=None, days=None, split=None, scenario=None):
    """Daily network series -> dict(run_id [R] int32 in run_id order, day [D] day indices, one [R, D] float32 array
    per column). Day index d = the simulated day d + 1; warm-up is indices < meta["warmup"].
    days: (start, end) half-open day indices, or a window name of meta["windows"] ("main" = after warm-up).
    columns: subset of chain.SERIES (default all). run_ids / split / scenario: filters (and-ed). Only the
    requested columns are read, row groups (64 runs) outside the run_id range are skipped, and each batch
    is cut to the day range at once, so memory ~ R x D x columns x 4 B. Failed runs have no series."""
    root = Path(root)
    meta = json.loads((root / "meta.json").read_text())
    D = _dataset(root, "series")
    if D is None:
        raise FileNotFoundError(f"{root}/series: no series table (built with --no-series, or not downloaded)")
    cols = columns or [f.name for f in D.schema if pa.types.is_fixed_size_list(f.type)]
    a, b = meta["windows"][days] if isinstance(days, str) else days or (0, meta["horizon"])
    f = pc.scalar(True)
    if split or scenario:
        rf = pc.scalar(True)
        for c, v in (("split", split), ("scenario", scenario)):
            if v:
                rf &= pc.field(c) == v
                if c in D.schema.names:  # compacted: prunes whole files
                    f &= pc.field(c) == v
        ids = _dataset(root, "runs").to_table(filter=rf, columns=["run_id"])["run_id"].to_numpy()
        run_ids = ids if run_ids is None else np.intersect1d(ids, run_ids)
    if run_ids is not None:
        ids = np.unique(np.asarray(run_ids, np.int64))
        if not len(ids):
            ids = np.array([-1])  # matches nothing
        f &= (pc.field("run_id") >= int(ids[0])) & (pc.field("run_id") <= int(ids[-1])) & pc.field("run_id").isin(ids)
    rid, out = [], {c: [] for c in cols}
    for bt in D.to_batches(columns=["run_id"] + cols, filter=f):
        if not len(bt):
            continue
        rid.append(bt["run_id"].to_numpy())
        for c in cols:
            v = bt[c].flatten().to_numpy().reshape(len(bt), -1)
            out[c].append(v[:, a:b].copy() if (a, b) != (0, v.shape[1]) else v)
    o = np.argsort(np.concatenate(rid) if rid else np.zeros(0, np.int32), kind="stable")
    res = dict(run_id=(np.concatenate(rid) if rid else np.zeros(0, np.int32))[o], day=np.arange(a, b))
    for c in cols:
        res[c] = np.concatenate(out[c])[o] if rid else np.zeros((0, b - a), np.float32)
    return res


def aggregate(s, period=7):
    """load_series dict -> blocks of `period` days (7 weekly, 30 ~monthly, 365 yearly): flows summed (float64
    accumulate), LEVELS averaged; day = first day index of each block; a last partial block is dropped."""
    k = len(s["day"]) // period * period
    res = dict(run_id=s["run_id"], day=s["day"][:k:period])
    for c, v in s.items():
        if c not in res:
            w = v[:, :k].reshape(len(v), -1, period)
            res[c] = (w.mean(2) if c in LEVELS else w.sum(2, dtype=np.float64)).astype(np.float32)
    return res


def to_pyg(g, rep=0):
    """One replication of a load_graphs dict -> torch_geometric.data.Data (needs torch_geometric)."""
    import torch
    from torch_geometric.data import Data
    t = torch.from_numpy
    return Data(x=t(g["x"]), edge_index=t(g["edge_index"]), edge_attr=t(g["edge_attr"]), y=t(g["y_node"][rep]),
                y_graph=t(g["y_graph"][rep:rep + 1]), **g["meta"])


if __name__ == "__main__":  # self-check on a bundle: python loader.py OUT
    import sys
    import time
    sys.path.insert(0, str(Path(__file__).parent))
    from dataset import query
    root = sys.argv[1]
    t0 = time.perf_counter()
    gs = list(load_graphs(root))
    dt = time.perf_counter() - t0
    graphs = query(root, table="graphs", columns=["graph_id", "roles", "src", "dst"]).to_pydict()
    rec = {g: (r, list(zip(a, b))) for g, r, a, b in zip(*graphs.values())}
    nr = query(root, pc.field("window") == "main", "node_results").to_pydict()
    look = {(r, v): i for i, (r, v) in enumerate(zip(nr["run_id"], nr["node_id"]))}
    for g in gs[:: max(1, len(gs) // 7)]:
        roles, edges = rec[g["meta"]["graph_id"]]
        assert g["x"].shape == (len(roles), len(X_COLS)) and g["edge_index"].shape == (2, len(edges))
        assert sorted(map(tuple, g["edge_index"].T.tolist())) == sorted(edges)
        assert all(STAGES[k] in (r, "R" if r == "I" else r) for k, r in zip(g["x"][:, -5:].argmax(1), roles))
        for rid in g["meta"]["run_ids"]:
            for v in range(len(roles)):
                i = look.get((rid, v))
                want = [np.nan] * len(TARGETS) if i is None else [nr[c][i] for c in TARGETS]
                np.testing.assert_allclose(g["y_node"][g["meta"]["run_ids"].index(rid), v], np.array(want, float), rtol=1e-6)
    msg = f"{len(gs)} configs in {dt:.2f} s = {dt / len(gs) * 1e3:.2f} ms/config"
    if (Path(root) / "series").is_dir():  # series vs the runs KPIs, filters, day cut, aggregate
        meta = json.loads((Path(root) / "meta.json").read_text())
        a, b = meta["windows"]["main"]
        ru = query(root, pc.field("status") == "ok", columns=["run_id", "split", "main_demand_day", "main_ext_revenue_day",
                                                             "main_inventory_end"]).to_pydict()
        t0 = time.perf_counter()
        s = load_series(root, run_ids=ru["run_id"][:1000])
        rd = time.perf_counter() - t0
        assert s["run_id"].tolist() == sorted(ru["run_id"][:1000]) and s["profit"].shape == (len(s["run_id"]), meta["horizon"])
        m = load_series(root, run_ids=ru["run_id"], days="main", columns=["cust_demand", "ext_revenue", "inventory"])
        i = {r: k for k, r in enumerate(ru["run_id"])}
        k = [i[r] for r in m["run_id"]]
        np.testing.assert_allclose(m["cust_demand"].sum(1, dtype=float) / (b - a), np.array(ru["main_demand_day"])[k], rtol=1e-5)
        np.testing.assert_allclose(m["ext_revenue"].sum(1, dtype=float) / (b - a), np.array(ru["main_ext_revenue_day"])[k], rtol=1e-4)
        np.testing.assert_allclose(m["inventory"][:, -1], np.array(ru["main_inventory_end"])[k], rtol=1e-6)
        sp = ru["split"][0]
        t = load_series(root, split=sp, columns=["profit"], days=(10, 20))
        assert set(t["run_id"].tolist()) == {r for r, x in zip(ru["run_id"], ru["split"]) if x == sp}
        assert t["profit"].shape == (len(t["run_id"]), 10) and list(t) == ["run_id", "day", "profit"]
        full = load_series(root, run_ids=t["run_id"][:3], columns=["profit"])
        np.testing.assert_array_equal(t["profit"][:3], full["profit"][:, 10:20])
        w = aggregate(m, 7)
        np.testing.assert_allclose(w["cust_demand"][:, 0], m["cust_demand"][:, :7].sum(1), rtol=1e-6)
        assert len(load_series(root, run_ids=[])["run_id"]) == 0
        msg += f"; series of {len(s['run_id'])} runs (all days, 8 columns) read in {rd:.2f} s"
    print(msg + "; checks passed")
