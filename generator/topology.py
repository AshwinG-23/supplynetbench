"""Network shapes for the dataset, as (roles, edges): roles 'S' 'M' 'I' 'D', edges (parent, child).

Two strata, recorded in the graphs table's `topology` column:
  echelon      (main, ~75%) layered DAG: suppliers -> plants -> 0-3 DC tiers -> retailers -> one customer
               stream per retailer (+ a few DCs that also sell to customers). Tier sizes grow
               geometrically (fan-out 2-20 per tier, skewed per parent), some sites have a second
               parent in the same tier (diamonds), a few links skip a tier (plant -> retailer, CDC ->
               retailer), some suppliers feed several plants.
  random_tree  (~25%) uniform unlabelled typed tree from tree_grammar (deep and thin; kept as a
               labelled "random topology" stratum / OOD test).
graph_hash = Weisfeiler-Lehman hash of the role-labelled digraph, the same function for both strata,
so isomorphic graphs always collide (and share a split). ponytail: WL can also merge two
non-isomorphic graphs (rare); that only drops a graph, never keeps a duplicate.
"""
import hashlib
import math
import random

import warnings

import networkx as nx

warnings.filterwarnings("ignore", "The hashes produced for directed graphs changed")

from tree_grammar import Enumerator, Grammar, Sampler, decode

DC_TIERS = (0.2, 0.4, 0.3, 0.1)  # P(k DC tiers), k = 0..3
FANOUT = (2, 20)                  # log-uniform growth per tier (plants -> ... -> retailers)
SUPPLIERS_PER_PLANT = (0.7, 2.5)
MULTI_SOURCE = (0.1, 0.3)         # P(site gets a 2nd parent in its parent tier), per graph
SKIP = (0.0, 0.08)                # P(site at tier >= 2 also gets a parent >= 2 tiers up), per graph
SHARED_SUPPLIER = (0.05, 0.3)     # P(plant gets an extra supplier that already feeds another plant)
DC_CUSTOMERS = (0.0, 0.15)        # P(a DC also has its own customer stream)


def wl_hash(roles, edges):
    g = nx.DiGraph()
    g.add_nodes_from((v, dict(r=r)) for v, r in enumerate(roles))
    g.add_edges_from(edges)
    return int(nx.weisfeiler_lehman_graph_hash(g, node_attr="r", iterations=6, digest_size=8), 16)


def features(roles, edges):
    """depth = longest path in edges (customer streams included); multi_parent = sites with >1 parent."""
    par = {}
    for u, v in edges:
        par.setdefault(v, []).append(u)
    depth = {}

    def visit(v):
        if v not in depth:
            depth[v] = 1 + max((visit(p) for p in par.get(v, [])), default=-1)
        return depth[v]
    for v in range(len(roles)):
        visit(v)
    return dict(depth=max(depth.values()), multi_parent=sum(len(p) > 1 for p in par.values()))


def _link(rng, parents, kids, a, edges, extra=0.0):
    """Every parent gets >= 1 child and every child >= 1 parent; leftover children pick a parent with
    weight (children so far + a) (small a = skewed fan-out); with prob `extra` a child adds a second parent."""
    parents, kids = parents[:], kids[:]
    rng.shuffle(parents)
    rng.shuffle(kids)
    deg = dict.fromkeys(parents, 0)
    got = {c: [] for c in kids}
    pairs = list(zip(parents, kids))
    for p in parents[len(kids):]:  # more parents than children (suppliers): extra parent of a random child
        pairs.append((p, rng.choice(kids)))
    for p, _ in pairs:
        deg[p] += 1
    for c in kids[len(parents):]:  # preferential: degrees updated after each pick
        p = rng.choices(parents, [deg[p] + a for p in parents])[0]
        deg[p] += 1
        pairs.append((p, c))
    for p, c in pairs:
        got[c].append(p)
    for c in kids:
        if len(parents) > len(got[c]) and rng.random() < extra:
            got[c].append(rng.choice([p for p in parents if p not in got[c]]))
    edges += [(p, c) for c in kids for p in got[c]]
    return got


def echelon(n, seed):
    """One layered DAG with exactly n nodes (n >= 6). Returns (roles, edges, info)."""
    rng = random.Random(seed)
    LU = lambda lo, hi: math.exp(rng.uniform(math.log(lo), math.log(hi)))
    for _ in range(10_000):
        k = rng.choices(range(4), DC_TIERS)[0]
        while True:  # fan-outs too wide for n: shrink them all by c; below 1.5 per tier, drop a DC tier
            f = [LU(*FANOUT) for _ in range(k + 1)]
            sig, pdc = rng.uniform(*SUPPLIERS_PER_PLANT), rng.uniform(*DC_CUSTOMERS)
            size = lambda c: [math.prod(c * y for y in f[:j + 1]) for j in range(k + 1)]  # per plant: DC1..DCk, retail
            nodes = lambda c: sig + 1 + sum(size(c)[:-1]) * (1 + pdc) + 2 * size(c)[-1]
            lo, hi = (0.0, 1.0) if nodes(1) > n else (1.0, 1.0)
            for _ in range(40 * (lo < hi)):  # bisection: largest c with nodes(c) <= n
                lo, hi = (lo, (lo + hi) / 2) if nodes((lo + hi) / 2) > n else ((lo + hi) / 2, hi)
            c = lo
            if c * min(f) >= 1.5 or not k:
                break
            k -= 1
        x = size(c)
        m = max(1, round(n / nodes(c)))
        s = max(1, round(sig * m))
        dc = [max(1, round(m * xj)) for xj in x[:-1]]
        e = sum(rng.random() < pdc for _ in range(sum(dc)))  # DCs with own customers
        if (n - s - m - sum(dc) - e) % 2:  # odd remainder: one more DC customer stream, else one more supplier
            e, s = (e + 1, s) if sum(dc) > e else (e, s + 1)
        r = (n - s - m - sum(dc) - e) // 2
        tiers = [m] + dc + [r]
        if s < 1 or any(b < a for a, b in zip(tiers, tiers[1:])) or s + m + sum(dc) + e + 2 * r != n:
            continue
        break
    else:
        raise ValueError(f"no echelon shape for n={n}")
    roles, ids = [], []
    for role, cnt in [("S", s)] + [("M", m)] + [("I", c) for c in dc + [r]]:
        ids.append(list(range(len(roles), len(roles) + cnt)))
        roles += role * cnt
    edges = []
    a = LU(0.3, 3)
    ms, skip = rng.uniform(*MULTI_SOURCE), rng.uniform(*SKIP)
    _link(rng, ids[0], ids[1], a, edges, extra=rng.uniform(*SHARED_SUPPLIER))
    for j in range(2, len(ids)):
        got = _link(rng, ids[j - 1], ids[j], a, edges, extra=ms)
        if j >= 3:  # tier skipping: extra parent two or more tiers up (never a supplier)
            for c in ids[j]:
                if rng.random() < skip:
                    p = rng.choice(ids[rng.randrange(1, j - 1)])
                    if p not in got[c]:
                        edges.append((p, c))
    sellers = ids[-1] + rng.sample([v for t in ids[2:-1] for v in t], e)
    for v in sellers:
        edges.append((v, len(roles)))
        roles.append("D")
    assert len(roles) == n
    return tuple(roles), tuple(edges), dict(dc_tiers=k, multi_source_p=round(ms, 4), skip_p=round(skip, 4))


def sample_tuple(rng, n):
    """(s, m, i, d) with n nodes in realistic proportions: suppliers 5-15%, plants 3-10%,
    customer-facing sites 30-90% of inventory sites. Feasible: d <= i, m <= s + d - 1."""
    while True:
        s = max(1, round(n * rng.uniform(0.05, 0.15)))
        m = max(1, round(n * rng.uniform(0.03, 0.10)))
        rest = n - s - m
        i = round(rest / (1 + rng.uniform(0.3, 0.9)))
        d = rest - i
        if min(s, m, i, d) >= 1 and d <= i and m <= s + d - 1:
            return s, m, i, d


def make_graphs(n, count, seed, stratum):
    """Worker: up to `count` distinct graphs with n nodes of one stratum, deterministic from seed.
    Repeated graphs are rejected (by hash); a small tuple with fewer trees than `count` is enumerated
    whole, and a small n may give fewer than `count` echelon graphs (after 30*count tries)."""
    rows, seen = [], set()

    def add(roles, edges, k, **info):
        h = wl_hash(roles, edges)
        if h in seen:
            return
        seen.add(h)
        c = {r: roles.count(r) for r in "SMID"}
        rows.append(dict(topology=stratum, roles="".join(roles), n=n, s=c["S"], m=c["M"], i=c["I"], d=c["D"], k=k, graph_hash=h,
                         tuple_seed=seed, src=[u for u, _ in edges], dst=[v for _, v in edges],
                         **features(roles, edges), **info))
    if stratum == "echelon":
        for k in range(30 * count):
            if len(rows) == count:
                break
            roles, edges, info = echelon(n, f"{seed}/{k}")
            add(roles, edges, k, **info)
        return rows
    t = sample_tuple(random.Random(seed), n)
    G = Grammar(t)
    recs = list(Enumerator(G)) if G.total() <= count else (bytes(Sampler(G, seed).at(k)) for k in range(30 * count))
    for k, rec in enumerate(recs):
        if len(rows) == count:
            break
        add(*decode(rec), k, record=rec)
    return rows


if __name__ == "__main__":
    import statistics
    import time
    for n in (6, 10, 13, 20, 50, 150, 300):
        t0 = time.perf_counter()
        g = make_graphs(n, 52, n, "echelon")
        for r in g:  # structure invariants
            roles = r["s"] * "S"
            par, kid = {}, {}
            for u, v in zip(r["src"], r["dst"]):
                par.setdefault(v, []).append(u)
                kid.setdefault(u, []).append(v)
            assert r["s"] + r["m"] + r["i"] + r["d"] == n and len(set(zip(r["src"], r["dst"]))) == len(r["src"])
            assert all(v in kid for v in range(n - r["d"])), "every site has a child"
            assert all(v in par for v in range(r["s"], n)), "every non-supplier has a parent"
        print(f"echelon n={n:3d}: {len(g)} distinct, depth median {statistics.median(r['depth'] for r in g)}, "
              f"dc_tiers {[sum(r['dc_tiers'] == k for r in g) for k in range(4)]}, multi-parent sites "
              f"{statistics.mean(r['multi_parent'] for r in g):.1f}, {time.perf_counter() - t0:.2f}s")
    assert len(make_graphs(7, 52, 1, "random_tree")) < 52  # small tuple: enumerated, fewer than asked
    g = make_graphs(40, 52, 3, "random_tree")
    assert len(g) == 52 and len({r["graph_hash"] for r in g}) == 52
    assert make_graphs(50, 5, 9, "echelon") == make_graphs(50, 5, 9, "echelon")  # deterministic
    print("topology checks passed")
