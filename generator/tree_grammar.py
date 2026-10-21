"""Duplicate-free generation, uniform sampling and compact storage of typed trees.

Same graph class as trees.py (default: at most one demand per inventory), but
built directly from the counting grammar instead of search + deduplication:

* every tree is rooted at its centroid (or, when n is even and two centroids
  exist, at the source end of the central edge, whose direction makes the two
  halves distinguishable), and children are emitted in one canonical order.
  So each unlabelled tree is produced exactly once: no canonical keys, no
  partial-state cache, no rejection.
* counts come from the same multiset (Polya) series as count_trees.py, computed
  bottom-up with numpy over the 4-D coefficient box.
* `sample` draws uniformly at random (Nijenhuis-Wilf RANRUT, generalised to
  4 counts), for sizes whose full set is far too large to enumerate.

Record format: one tree = 2n bytes, preorder from the root, (symbol, children)
per node. symbol = type (S0 M1 I2 D3) + 4 if the edge goes node -> parent.
Records of one run have equal length, so a run is an (N, 2n) uint8 array.
"""
import argparse
import itertools
import json
import math
import sys
import time
from bisect import bisect_left
from functools import lru_cache
from pathlib import Path

import numpy as np

KIND_ORDER = "abcefgh"
# Branch kinds (root type, edge to the external parent):
# a S->parent M, b M<-parent S, e M->parent I, c I<-parent, f I->parent I,
# h I->parent D, g D<-parent I.
ROOT = {"a": (0, 1), "b": (1, 0), "e": (1, 1), "c": (2, 0), "f": (2, 1), "h": (2, 1), "g": (3, 0)}
UNIT = {t: tuple(int(k == t) for k in range(4)) for t in range(4)}
# Multiset series: name -> branch kinds it draws from.
MSETS = {"Hb": "b", "Ha": "a", "Hc": "c", "Hef": "ef"}
# Children of each branch kind: (input multiset, output multiset); "-1" means nonempty.
CHILDREN = {"a": (None, "Hb"), "b": ("Ha", "Hc-1"), "e": ("Ha-1", "Hc"),
            "c": ("Hef", "J-1"), "f": ("Hef-1", "J"), "h": ("Hef-1", "Hc")}
ROOTS = {0: (None, "Hb-1"), 1: ("Ha-1", "Hc-1"), 2: ("Hef-1", "J-1")}  # centroid vertex
BICENTROID = (("a", "b"), ("e", "c"), ("f", "c"), ("h", "g"))  # (source half, target half)


def sub(v, u):
    return tuple(x - y for x, y in zip(v, u))


def fits(u, v):
    return all(x <= y for x, y in zip(u, v))


def rev(v):
    """Index of the flipped sub-box: X[rev(v)][u] == X[v - u]."""
    return tuple(slice(x, None, -1) for x in v)


def box(v):
    return tuple(slice(0, x + 1) for x in v)


class Grammar:
    """Coefficient tables for one target (s, m, i, d)."""

    def __init__(self, counts, exact=False):
        if len(counts) != 4 or any(type(x) is not int or x < 0 for x in counts):
            raise ValueError("s, m, i, d must be four non-negative integers")
        self.counts = t = tuple(counts)
        self.n = sum(t)
        self.cap = (self.n - 1) // 2  # centroid: every branch has at most this many nodes
        shape = tuple(x + 1 for x in t)
        dtype = object if exact else np.float64
        T = self.T = {k: np.zeros(shape, dtype) for k in
                      ["a", "b", "e", "c", "f", "h", "g", "Ha", "Hb", "Hc", "Hef", "J", "Pac", "PefJ", "Pefc"]}
        self.tot = np.indices(shape).sum(0)
        for k in ("Ha", "Hb", "Hc", "Hef", "J", "Pac", "PefJ", "Pefc"):
            T[k][0, 0, 0, 0] = 1
        order = sorted(itertools.product(*map(range, shape)), key=sum)[1:]
        L = {k: np.zeros((4,) + shape, dtype) for k in MSETS}

        def at(name, v, unit):
            u = sub(v, unit)
            return T[name][u] if min(u) >= 0 else 0

        for v in order:
            T["a"][v] = at("Hb", v, UNIT[0])
            if v[1]:
                u = sub(v, UNIT[1])
                T["b"][v] = T["Pac"][u] - T["Ha"][u]
                T["e"][v] = T["Pac"][u] - T["Hc"][u]
            if v[2]:
                u = sub(v, UNIT[2])
                T["c"][v] = T["PefJ"][u] - T["Hef"][u]
                T["f"][v] = T["PefJ"][u] - T["J"][u]
                T["h"][v] = T["Pefc"][u] - T["Hc"][u]
            T["g"][v] = int(v == UNIT[3])
            for name, kinds in MSETS.items():
                self._log_derivative(L[name], kinds, v)
                T[name][v] = self._exp_step(L[name], T[name], v)
            T["J"][v] = T["Hc"][v] + at("Hc", v, UNIT[3])
            for p, x, y in (("Pac", "Ha", "Hc"), ("PefJ", "Hef", "J"), ("Pefc", "Hef", "Hc")):
                T[p][v] = (T[x][box(v)] * T[y][rev(v)]).sum()
        # Centroid-restricted multisets: identical up to `cap` nodes, recomputed above it.
        self.small = self.tot <= self.cap
        for name, kinds in MSETS.items():
            Lr = np.zeros((4,) + shape, dtype)
            T[name + "_L"] = np.where(self.small, T[name], 0).astype(dtype)
            for v in order:
                self._log_derivative(Lr, kinds, v, restricted=True)
                if sum(v) > self.cap:
                    T[name + "_L"][v] = self._exp_step(Lr, T[name + "_L"], v)
        T["J_L"] = T["Hc_L"].copy()
        T["J_L"][..., 1:] += T["Hc_L"][..., :-1]

    def _log_derivative(self, L, kinds, v, restricted=False):
        # L_j[v] = sum over k | gcd(v) of (v_j/k) F[v/k]  (F = branch kinds summed)
        g = math.gcd(*v)
        for k in range(1, g + 1):
            if g % k == 0:
                w = tuple(x // k for x in v)
                if restricted and sum(w) > self.cap:
                    continue
                F = sum(self.T[kind][w] for kind in kinds)
                for j in range(4):
                    L[(j,) + v] += w[j] * F

    def _exp_step(self, L, H, v):
        # log-derivative recurrence: v_j H[v] = sum_{0<u<=v} L_j[u] H[v-u]
        j = max(range(4), key=lambda k: v[k])
        total = (L[j][box(v)] * H[rev(v)]).sum()
        if self.T["a"].dtype == object:
            q, r = divmod(total, v[j])
            assert r == 0, "multiset coefficient must be an integer"
            return q
        return total / v[j]

    def series(self, name, v, restricted=False):
        """Coefficient of a (possibly '-1', i.e. nonempty) multiset series."""
        base = name[:-2] if name.endswith("-1") else name
        if min(v) < 0:
            return 0
        if name.endswith("-1") and not any(v):
            return 0
        return self.T[base + ("_L" if restricted else "")][v]

    def total(self):
        """Number of unlabelled trees with exactly these counts."""
        t = self.counts
        if min(t) == 0 or t[3] > t[2]:
            return 0  # every role is required; see trees.feasible
        result = 0
        for root, (inp, out) in ROOTS.items():
            v = sub(t, UNIT[root])
            if min(v) < 0:
                continue
            X = self._split_array(inp, out, v, restricted=True)
            result += X.sum()
        if self.n % 2 == 0:
            result += self._bicentroid().sum()
        return result

    def _split_array(self, inp, out, v, restricted):
        """Weights of each input-size u for (inputs of size u) x (outputs of size v-u)."""
        suffix = "_L" if restricted else ""
        if inp is None:
            X = np.zeros([x + 1 for x in v], self.T["a"].dtype)
            X[(0, 0, 0, 0)] = self.series(out, v, restricted)
            return X
        A = self.T[inp.rstrip("-1") + suffix][box(v)].copy()
        B = self.T[out.rstrip("-1") + suffix][rev(v)].copy()
        if inp.endswith("-1"):
            A[0, 0, 0, 0] = 0
        if out.endswith("-1"):
            B[tuple(v)] = 0  # B is flipped: B[v] holds the empty output multiset
        return A * B

    def _bicentroid(self):
        """Per (pair, source-size) weight of trees whose central edge splits n/2 | n/2."""
        t, half = self.counts, self.n // 2
        on_half = self.tot[box(t)] == half
        return np.stack([np.where(on_half, self.T[s][box(t)] * self.T[d][rev(t)], 0)
                         for s, d in BICENTROID])


# ----------------------------------------------------------------- enumeration

class Enumerator:
    """Exhaustive, duplicate-free generation of canonical records."""

    def __init__(self, grammar, memo_limit=5_000_000):
        self.G = grammar
        self.memo_limit = memo_limit
        self.elements = lru_cache(None)(self.elements)
        self._classes = {}
        self._opts = {}
        self._need = {}

    def classes(self, kinds, cap):
        key = (kinds, cap)
        if key not in self._classes:
            T = self.G.T
            found = [(KIND_ORDER.index(k), w, k) for k in kinds
                     for w in zip(*np.nonzero(T[k] > 0)) if sum(w) <= cap]
            found = sorted((r, tuple(map(int, w)), k) for r, w, k in found)
            once = "g" in kinds  # a J multiset: at most one demand child
            self._classes[key] = [(k, w, once and k == "g") for _, w, k in found]
        return self._classes[key]

    def opts(self, kinds, cap, v):
        key = (kinds, cap, v)
        if key not in self._opts:
            cls = self.classes(kinds, cap)
            opts = [(pos, sub(v, w)) for pos in range(len(cls) - 1, -1, -1)
                    for w in [cls[pos][1]] if fits(w, v)]
            self._opts[key] = opts, [-p for p, _ in opts]
        return self._opts[key]

    def need(self, kinds, cap, v):
        """Smallest class bound that can still complete a multiset of size v."""
        if not any(v):
            return -1
        key = (kinds, cap, v)
        if key not in self._need:
            cls = self.classes(kinds, cap)
            best = math.inf
            for pos, rem in self.opts(kinds, cap, v)[0]:
                if self.need(kinds, cap, rem) <= pos - cls[pos][2]:
                    best = pos
            self._need[key] = best
        return self._need[key]

    def elements(self, kind, w):
        """Sorted canonical records of every branch of this kind and size (memoised)."""
        if kind == "g":
            return (bytes((3, 0)),)
        if self.G.T[kind][w] > self.memo_limit:
            # ponytail: branch lists are held in memory; use sample() for sizes this large.
            raise MemoryError(f"{self.G.T[kind][w]:.3g} branches of kind {kind}, size {w}; "
                              "the full set is too large to enumerate - use sampling")
        typ, up = ROOT[kind]
        inp, out = CHILDREN[kind]
        return tuple(sorted(self._node(typ + 4 * up, inp, out, sub(w, UNIT[typ]), math.inf)))

    def _node(self, sym, inp, out, v, cap):
        """Every record of a node whose children are (inputs multiset, outputs multiset)."""
        restricted = cap != math.inf
        G = self.G
        for u in itertools.product(*map(range, [x + 1 for x in v])):
            rest = sub(v, u)
            if (inp is None and any(u)) or (inp and not G.series(inp, u, restricted)):
                continue
            if not G.series(out, rest, restricted):
                continue
            # ponytail: output multisets up to 1e5 are cached per split; larger ones are regenerated.
            ys = list(self.mset(out, cap, rest)) if G.series(out, rest, restricted) <= 1e5 else None
            for x in (self.mset(inp, cap, u) if inp else [()]):
                head = b"".join(x)
                for y in (ys if ys is not None else self.mset(out, cap, rest)):
                    k = len(x) + len(y)
                    if k > 255:
                        raise ValueError("more than 255 children at one node")
                    yield bytes((sym, k)) + head + b"".join(y)

    def mset(self, name, cap, v):
        kinds = {"Ha": "a", "Hb": "b", "Hc": "c", "Hef": "ef", "J": "cg"}[name.rstrip("-1")]
        return self._mset(kinds, cap, v, len(self.classes(kinds, cap)) - 1, None)

    def _mset(self, kinds, cap, v, bound, index_bound):
        """Non-increasing sequences of (class, element) of total size v."""
        if not any(v):
            yield ()
            return
        cls = self.classes(kinds, cap)
        opts, negpos = self.opts(kinds, cap, v)
        start = bisect_left(negpos, -bound)
        for pos, rem in opts[start:]:
            kind, w, once = cls[pos]
            nxt = pos - once
            if self.need(kinds, cap, rem) > nxt:
                continue
            elems = self.elements(kind, w)
            top = len(elems) - 1 if (pos != bound or index_bound is None) else index_bound
            for idx in range(top, -1, -1):
                for rest in self._mset(kinds, cap, rem, nxt, None if once else idx):
                    yield (elems[idx],) + rest

    def __iter__(self):
        G = self.G
        t = G.counts
        if not G.total():
            return
        for root, (inp, out) in ROOTS.items():
            v = sub(t, UNIT[root])
            if min(v) >= 0:
                yield from self._node(root, inp, out, v, G.cap)
        if G.n % 2 == 0:
            half = G.n // 2
            for (src, dst), w in itertools.product(BICENTROID, itertools.product(*map(range, [x + 1 for x in t]))):
                if sum(w) != half or not G.T[src][w] or not G.T[dst][sub(t, w)]:
                    continue
                typ = ROOT[src][0]
                targets = self.elements(dst, sub(t, w))
                for s in self.elements(src, w):
                    for d in targets:
                        yield bytes((typ, s[1] + 1)) + d + s[2:]


# -------------------------------------------------------------------- sampling

class Sampler:
    """Uniform random trees (floating-point weights; bias ~1e-15 per choice)."""

    def __init__(self, grammar, seed=0):
        self.G = grammar
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.axis = np.indices(grammar.T["a"].shape)
        self.F = {}  # (kinds, restricted) -> summed branch counts

    def _pick(self, weights):
        flat = np.cumsum(weights.ravel().astype(np.float64))
        r = self.rng.random() * flat[-1]
        return np.unravel_index(int(np.searchsorted(flat, r, side="right")), weights.shape)

    def branch(self, kind, w):
        if kind == "g":
            return bytes((3, 0))
        typ, up = ROOT[kind]
        inp, out = CHILDREN[kind]
        return self._node(typ + 4 * up, inp, out, sub(w, UNIT[typ]), False)

    def _node(self, sym, inp, out, v, restricted):
        u = self.G._split_array(inp, out, v, restricted)
        u = tuple(map(int, self._pick(u))) if inp else (0, 0, 0, 0)
        kids = self.mset(inp, u, restricted) if inp else []
        kids += self.mset(out, sub(v, u), restricted)
        return bytes((sym, len(kids))) + b"".join(k for _, k in kids)

    def mset(self, name, v, restricted):
        """Uniform multiset of the named series; returns [(sort key, record)] sorted descending."""
        base = name.rstrip("-1")
        out = []
        if base == "J":  # at most one demand child
            with_d = self.G.series("Hc", sub(v, UNIT[3]), restricted)
            if with_d and self.rng.random() * self.G.series("J", v, restricted) < with_d:
                out.append(((KIND_ORDER.index("g"), UNIT[3], b"\3\0"), b"\3\0"))
                v = sub(v, UNIT[3])
            base = "Hc"
        kinds = MSETS[base]
        T, suffix = self.G.T, "_L" if restricted else ""
        while any(v):
            # RANRUT step: choose (size w, copies k) with weight w_j F[w] H[v - k w]
            j = max(range(4), key=lambda k: v[k])
            if (kinds, restricted) not in self.F:
                F = sum(T[k] for k in kinds)
                self.F[kinds, restricted] = np.where(self.G.small, F, 0) if restricted else F
            F = self.F[kinds, restricted]
            terms = []
            for k in range(1, max(v) + 1):
                vk = tuple(x // k for x in v)
                if not any(vk):
                    break
                terms.append((k, self.axis[(j,) + box(vk)] * F[box(vk)] *
                              T[base + suffix][tuple(slice(x, None, -k) for x in v)]))
            sizes = np.array([x.sum() for _, x in terms], dtype=np.float64)
            k, weights = terms[int(np.searchsorted(np.cumsum(sizes), self.rng.random() * sizes.sum(), side="right"))]
            w = tuple(map(int, self._pick(weights)))
            counts = np.array([float(T[kind][w]) for kind in kinds])
            kind = kinds[int(np.searchsorted(np.cumsum(counts), self.rng.random() * counts.sum(), side="right"))]
            rec = self.branch(kind, w)
            out += [((KIND_ORDER.index(kind), w, rec), rec)] * k
            v = sub(v, tuple(k * x for x in w))
        return sorted(out, reverse=True)

    def at(self, index):
        """Sample number `index` of this seed: reproducible, and parallel workers need no coordination."""
        self.rng = np.random.default_rng([self.seed, index])
        return self.tree()

    def tree(self):
        G = self.G
        t = G.counts
        weights = [G._split_array(inp, out, sub(t, UNIT[r]), True).sum() if min(sub(t, UNIT[r])) >= 0 else 0
                   for r, (inp, out) in ROOTS.items()]
        bi = G._bicentroid() if G.n % 2 == 0 else None
        weights.append(bi.sum() if bi is not None else 0)
        weights = np.array(weights, dtype=np.float64)
        choice = int(np.searchsorted(np.cumsum(weights), self.rng.random() * weights.sum(), side="right"))
        if choice < 3:
            inp, out = ROOTS[choice]
            return self._node(choice, inp, out, sub(t, UNIT[choice]), True)
        pair, *w = map(int, self._pick(bi))
        src, dst = BICENTROID[pair]
        s, d = self.branch(src, tuple(w)), self.branch(dst, sub(t, tuple(w)))
        return bytes((ROOT[src][0], s[1] + 1)) + d + s[2:]


# ---------------------------------------------------------------- decode / I/O

def decode(record):
    """Record -> (roles, edges); node ids are preorder positions (0 = root)."""
    record = bytes(record)
    roles, edges, stack = [], [], []  # stack of [node, unfilled child slots]
    for pos in range(0, len(record), 2):
        sym, kids = record[pos], record[pos + 1]
        v = len(roles)
        roles.append("SMID"[sym & 3])
        if stack:
            top = stack[-1]
            edges.append((v, top[0]) if sym & 4 else (top[0], v))
            top[1] -= 1
            if not top[1]:
                stack.pop()
        if kids:
            stack.append([v, kids])
    return tuple(roles), tuple(edges)


def save(path, records, counts, graphs, chunk=100_000, **meta):
    """Stream `graphs` records into one .npy (graphs, 2n) uint8 array plus a JSON sidecar."""
    path = Path(path)
    width = 2 * sum(counts)
    arr = np.lib.format.open_memmap(path, mode="w+", dtype=np.uint8, shape=(graphs, width))
    k = 0
    it = iter(records)
    while batch := list(itertools.islice(it, chunk)):
        arr[k:k + len(batch)] = np.frombuffer(b"".join(batch), np.uint8).reshape(-1, width)
        k += len(batch)
    if k != graphs:
        raise ValueError(f"expected {graphs} records, got {k}")
    arr.flush()
    path.with_suffix(".json").write_text(json.dumps(dict(format="tree_grammar/1", counts=list(counts),
                                                         graphs=graphs, **meta), indent=1))
    return arr


def load(path):
    """Memory-mapped (N, 2n) uint8 array; decode(arr[k]) gives graph k."""
    return np.load(path, mmap_mode="r")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["count", "enumerate", "sample"])
    p.add_argument("counts", type=int, nargs=4, metavar="N", help="s m i d")
    p.add_argument("--exact", action="store_true", help="count with exact integers (slower)")
    p.add_argument("-k", type=int, default=10, help="number of samples")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, help=".npy output for enumerate/sample")
    a = p.parse_args()
    t0 = time.perf_counter()
    G = Grammar(tuple(a.counts), exact=a.exact or a.mode == "count")
    total = G.total()
    # float tables: exact below 2**53, otherwise an approximation (use `count`/--exact)
    info = dict(counts=a.counts, total=int(total) if G.T["a"].dtype == object or total < 2**53 else float(total),
                table_seconds=round(time.perf_counter() - t0, 3))
    if a.mode != "count":
        t1 = time.perf_counter()
        if a.mode == "enumerate":
            graphs, records = int(total), iter(Enumerator(G))
        else:
            S = Sampler(G, a.seed)
            graphs, records = a.k, (S.at(k) for k in range(a.k))
        if a.out:
            save(a.out, records, a.counts, graphs, mode=a.mode, seed=a.seed if a.mode == "sample" else None)
            info["bytes"] = a.out.stat().st_size
        else:
            graphs = sum(1 for _ in records)
        info.update(graphs=graphs, seconds=round(time.perf_counter() - t1, 3))
        info["graphs_per_second"] = round(graphs / max(info["seconds"], 1e-9))
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    sys.exit(main())
