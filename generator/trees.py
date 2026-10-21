"""Enumerate typed, directed supply-chain trees before assigning parameters.

See TREES.md for the precise graph class, completeness argument and limits.
The search uses only the standard library; NetworkX is an independent test oracle.
"""

import argparse
import csv
import hashlib
import itertools
import json
import math
import time
from datetime import datetime
from collections import Counter, defaultdict
from pathlib import Path


ROLES = ("S", "M", "I", "D")
NAMES = dict(zip(ROLES, ("supplier", "manufacturer", "inventory", "demand")))
ALLOWED = {("S", "M"), ("M", "I"), ("I", "I"), ("I", "D")}
TABLES = {
    "graphs": ["graph_id", "s", "m", "i", "d", "node_count", "edge_count"],
    "nodes": ["graph_id", "node_id", "node_type"],
    "edges": ["graph_id", "source", "target"],
}


def check_counts(counts):
    if len(counts) != 4 or any(type(n) is not int or n < 0 for n in counts):
        raise ValueError("s, m, i, d must be four non-negative integers")


def feasible(counts):
    """Necessary and sufficient for THIS tree class (proof in TREES.md)."""
    check_counts(counts)
    s, m, i, d = counts
    return min(counts) > 0 and d <= i and m <= s + d - 1


def canonical_code(roles, edges):
    """Exact type- and direction-preserving tree key, independent of node IDs."""
    n = len(roles)
    adj = [[] for _ in roles]
    for u, v in edges:
        adj[u].append((v, ">"))
        adj[v].append((u, "<"))
    degree = [len(a) for a in adj]
    leaves = [v for v in range(n) if degree[v] <= 1]
    remaining = n
    while remaining > 2:
        if not leaves:
            raise ValueError("canonical_code requires a tree")
        remaining -= len(leaves)
        following = []
        for v in leaves:
            for u, _ in adj[v]:
                degree[u] -= 1
                if degree[u] == 1:
                    following.append(u)
        leaves = following
    codes = []
    for root in leaves:
        parent = {root: -1}
        order = [root]
        for v in order:
            for u, _ in adj[v]:
                if u != parent[v]:
                    parent[u] = v
                    order.append(u)
        code = {}
        for v in reversed(order):
            children = sorted(sign + code[u] for u, sign in adj[v] if parent.get(u) == v)
            code[v] = roles[v] + "(" + "".join(children) + ")"
        codes.append(code[root])
    return min(codes)


def validate_tree(roles, edges, counts, distinct_demands=True):
    """Independent linear graph checks; invalid inputs raise ValueError."""
    check_counts(counts)
    n = sum(counts)
    if n == 0 or Counter(roles) != Counter(dict(zip(ROLES, counts))):
        raise ValueError("wrong node types or counts")
    if counts[3] > counts[2]:
        raise ValueError("d must not exceed i")
    if len(edges) != n - 1 or len(set(edges)) != len(edges):
        raise ValueError("a tree needs n-1 distinct edges")
    incoming = [0] * n
    outgoing = [0] * n
    demands = [0] * n
    adj = [[] for _ in range(n)]
    for u, v in edges:
        if type(u) is not int or type(v) is not int or not (0 <= u < n and 0 <= v < n):
            raise ValueError("invalid endpoint")
        if u == v or (roles[u], roles[v]) not in ALLOWED:
            raise ValueError("forbidden edge")
        incoming[v] += 1
        outgoing[u] += 1
        demands[u] += roles[v] == "D"
        adj[u].append(v)
        adj[v].append(u)
    seen = {0}
    pending = [0]
    for v in pending:
        for u in adj[v]:
            if u not in seen:
                seen.add(u)
                pending.append(u)
    if len(seen) != n:
        raise ValueError("disconnected")
    for v, role in enumerate(roles):
        if role == "S" and (incoming[v] or not outgoing[v]):
            raise ValueError("supplier must supply a manufacturer")
        if role in ("M", "I") and (not incoming[v] or not outgoing[v]):
            raise ValueError("every facility needs an input and output")
        if role == "D" and (incoming[v] != 1 or outgoing[v]):
            raise ValueError("demand must be a leaf with one parent")
        if distinct_demands and demands[v] > 1:
            raise ValueError("at most one demand per inventory")


class SearchStopped(Exception):
    pass


class TreeSearch:
    """Single-use streaming exhaustive search, with optional explicit limits."""

    def __init__(self, s, m, i, d, *, limit=0, seconds=0, distinct_demands=True, symmetry=True, orbit_pruning=True):
        self.counts = (s, m, i, d)
        check_counts(self.counts)
        if type(limit) is not int or limit < 0 or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("limits must be non-negative and finite; zero means unlimited")
        self.limit, self.seconds = limit, seconds
        self.distinct_demands = distinct_demands
        self.symmetry = symmetry
        self.orbit_pruning = orbit_pruning
        # Equal typed forests have equal possible continuations at this depth.
        # ponytail: partial keys grow with search; a faster canonical deletion rule would remove this cache.
        self.partial_seen = defaultdict(set)
        self.roles = tuple(role for role, count in zip(ROLES, self.counts) for _ in range(count))
        self.S = range(s)
        self.M = range(s, s + m)
        self.I = range(s + m, s + m + i)
        self.end = s + m + i
        self.parent = list(range(self.end))
        self.size = [1] * self.end
        self.outdegree = [0] * self.end
        self.inputs = {}
        self.edges = []
        self.history = []
        # Only the baseline without partial symmetry needs global output keys.
        self.seen = set()
        self.stats = dict(unique=0, duplicates=0, states=0, completions=0,
                          complete=False, stop_reason="not_started", elapsed_seconds=0, partial_duplicates=0)

    def tick(self):
        if self.seconds and time.perf_counter() - self.started >= self.seconds:
            raise SearchStopped("time_limit")

    def find(self, v):
        while self.parent[v] != v:
            v = self.parent[v]
        return v

    def union(self, u, v):
        u, v = self.find(u), self.find(v)
        if u == v:
            raise AssertionError("parent selection introduced an undirected cycle")
        if self.size[u] < self.size[v]:
            u, v = v, u
        self.history.append((v, u, self.size[u]))
        self.parent[v] = u
        self.size[u] += self.size[v]

    def parent_sets(self, candidates, mandatory=(), require_all=False, max_parents=None, orbits=None):
        """At most one parent from each CURRENT undirected component."""
        groups = defaultdict(list)
        for u in candidates:
            groups[self.find(u)].append(u)
        required = {}
        for u in mandatory:
            root = self.find(u)
            if root in required or u not in groups[root]:
                return
            required[root] = u
        if max_parents is None:
            max_parents = len(groups)
        if len(required) > max_parents:
            return
        if orbits is not None:
            # Equal components offer equal rooted choices. Select multisets of
            # choices directly, instead of all labelled subsets/permutations.
            kinds = defaultdict(list)
            for root, nodes in groups.items():
                if root not in required:
                    representatives = {orbits[u]: u for u in nodes}
                    kinds[tuple(sorted(representatives))].append(representatives)
            kinds = list(kinds.items())

            def choose(pos, chosen):
                self.tick()
                if pos == len(kinds):
                    if chosen:
                        yield tuple(sorted(chosen))
                    return
                signature, copies = kinds[pos]
                maximum = min(len(copies), max_parents - len(chosen))
                minimum = len(copies) if require_all else 0
                for size in range(minimum, maximum + 1):
                    for types in itertools.combinations_with_replacement(signature, size):
                        vertices = tuple(copies[k][kind] for k, kind in enumerate(types))
                        yield from choose(pos + 1, chosen + vertices)

            yield from choose(0, tuple(mandatory))
            return
        if require_all:
            if len(groups) > max_parents:
                return
            choices = [(required[root],) if root in required else nodes for root, nodes in groups.items()]
            for parents in itertools.product(*choices):
                self.tick()
                yield tuple(sorted(parents))
            return
        optional = [u for u in candidates if self.find(u) not in required]

        def visit(start, chosen, roots):
            self.tick()
            if chosen:
                yield tuple(sorted(chosen))
            if len(chosen) == max_parents:
                return
            for pos in range(start, len(optional)):
                u = optional[pos]
                root = self.find(u)
                if root not in roots:
                    yield from visit(pos + 1, chosen + (u,), roots | {root})

        yield from visit(0, tuple(mandatory), set(required))

    def parent_orbits(self, end):
        """Exact rooted-component IDs, by two tree passes; IDs are local to this state."""
        adj = [[] for _ in range(end)]
        for u, v in self.edges:
            adj[u].append((v, ">"))
            adj[v].append((u, "<"))
        intern = {}
        def encode(v, neighbours):
            key = (self.roles[v], tuple(sorted(neighbours)))
            if key not in intern:
                intern[key] = len(intern)
            return intern[key]
        parent = [-2] * end
        order = []
        for root in range(end):
            if parent[root] != -2:
                continue
            parent[root] = -1
            pending = [root]
            for v in pending:
                order.append(v)
                for u, sign in adj[v]:
                    if u != parent[v]:
                        parent[u] = v
                        pending.append(u)
        down, up, rooted = [0]*end, [0]*end, [0]*end
        for v in reversed(order):
            down[v] = encode(v, [(sign, down[u]) for u, sign in adj[v] if u != parent[v]])
        for v in order:
            neighbours = [(sign, up[v] if u == parent[v] else down[u]) for u, sign in adj[v]]
            rooted[v] = encode(v, neighbours)
            for pos, (u, _) in enumerate(adj[v]):
                if u != parent[v]:
                    up[u] = encode(v, neighbours[:pos] + neighbours[pos+1:])
        return rooted

    def possible(self, next_v, components):
        s, m, i, d = self.counts
        remaining_m = max(0, s + m - next_v)
        remaining_i = self.end - max(s + m, next_v)
        frontier = Counter()
        unserved_m = Counter()
        for v in range(s, next_v):
            if self.outdegree[v] == 0:
                frontier[self.find(v)] += 1
                if v < s + m:
                    unserved_m[self.find(v)] += 1
        if max(frontier.values(), default=0) > d:
            return False
        if next_v < s + m:
            live = len({self.find(v) for v in range(s, next_v)})
            unused_s = sum(self.outdegree[v] == 0 for v in self.S)
            # Each remaining manufacturer can activate at most one new component.
            if live + min(unused_s, remaining_m) < m - d + 1:
                return False
        else:
            if any(self.outdegree[v] == 0 for v in self.S):
                return False
            # Future joins can eliminate at most components-1 frontier endpoints.
            if sum(frontier.values()) - components + 1 > d:
                return False
            if max(unserved_m.values(), default=0) > remaining_i:
                return False
            if remaining_i == 0 and components != 1:
                return False
        return True

    def forest_code(self, end):
        groups = defaultdict(list)
        for v in range(end):
            groups[self.find(v)].append(v)
        component_edges = defaultdict(list)
        for u, v in self.edges:
            component_edges[self.find(u)].append((u, v))
        codes = []
        for root, nodes in groups.items():
            mapping = {v: k for k, v in enumerate(nodes)}
            roles = tuple(self.roles[v] for v in nodes)
            edges = tuple((mapping[u], mapping[v]) for u, v in component_edges[root])
            codes.append(canonical_code(roles, edges))
        return tuple(sorted(codes))

    def finish(self):
        self.stats["completions"] += 1
        sinks = tuple(v for v in self.I if self.outdegree[v] == 0)
        extra = self.counts[3] - len(sinks)
        if extra < 0:
            return
        if self.distinct_demands:
            placements = itertools.combinations([v for v in self.I if v not in sinks], extra)
        else:
            placements = itertools.combinations_with_replacement(self.I, extra)
        # Deleting D leaves recovers this unique S/M/I core. Different cores
        # cannot produce isomorphic outputs; only its own placements can collide.
        seen = set() if self.symmetry else self.seen
        for extra_parents in placements:
            self.tick()
            attachment = sorted(sinks + extra_parents)
            edges = tuple(self.edges) + tuple((u, self.end + k) for k, u in enumerate(attachment))
            if extra or not self.symmetry:
                key = canonical_code(self.roles, edges)
                if key in seen:
                    self.stats["duplicates"] += 1
                    continue
                seen.add(key)
            self.stats["unique"] += 1
            yield self.roles, edges
            if self.limit and self.stats["unique"] >= self.limit:
                raise SearchStopped("output_limit")

    def visit(self, v, components):
        self.tick()
        self.stats["states"] += 1
        if v == self.end:
            yield from self.finish()
            return
        s, m, i, d = self.counts
        candidates = list(self.S if v < s + m else range(s, v))
        mandatory = ()
        if v == s + m - 1:
            mandatory = tuple(u for u in self.S if self.outdegree[u] == 0)
        if v == self.end - 1:
            mandatory = tuple(u for u in self.M if self.outdegree[u] == 0)
        if v >= s + m:
            frontier = sum(self.outdegree[u] == 0 for u in range(s, v))
            if frontier - components + 1 == d:
                candidates = [u for u in candidates if self.outdegree[u] == 0]
        max_parents = components
        if v < s + m:
            max_parents = components + 1 - max(1, m - d + 1)
        orbits = self.parent_orbits(v) if self.symmetry and self.orbit_pruning else None
        for parents in self.parent_sets(candidates, mandatory, require_all=v == self.end - 1,
                                        max_parents=max_parents, orbits=orbits):
            self.tick()
            # Manufacturers are interchangeable before parameter assignment.
            if not self.symmetry and s < v < s + m and parents < self.inputs[v - 1]:
                continue
            if any(self.outdegree[u] >= d for u in parents):
                continue
            checkpoint = len(self.history)
            self.inputs[v] = parents
            for u in parents:
                self.union(u, v)
                self.outdegree[u] += 1
                self.edges.append((u, v))
            following_components = components + 1 - len(parents)
            try:
                if self.possible(v + 1, following_components):
                    if self.symmetry:
                        key = self.forest_code(v + 1)
                        if key in self.partial_seen[v + 1]:
                            self.stats["partial_duplicates"] += 1
                            continue
                        self.partial_seen[v + 1].add(key)
                    yield from self.visit(v + 1, following_components)
            finally:
                for u in parents:
                    self.outdegree[u] -= 1
                    self.edges.pop()
                while len(self.history) > checkpoint:
                    child, root, old_size = self.history.pop()
                    self.parent[child] = child
                    self.size[root] = old_size
                del self.inputs[v]

    def __iter__(self):
        if self.stats["stop_reason"] != "not_started":
            raise RuntimeError("TreeSearch is single-use; construct another search")
        self.started = time.perf_counter()
        self.stats["stop_reason"] = "consumer_stopped"
        try:
            if feasible(self.counts):
                yield from self.visit(self.counts[0], self.counts[0])
            self.stats.update(complete=True, stop_reason="exhausted")
        except SearchStopped as exc:
            self.stats["stop_reason"] = str(exc)
        finally:
            self.stats["elapsed_seconds"] = round(time.perf_counter() - self.started, 6)


def main():
    run_started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("s", type=int)
    parser.add_argument("m", type=int)
    parser.add_argument("i", type=int)
    parser.add_argument("d", type=int)
    parser.add_argument("--limit", type=int, default=0, help="maximum unique trees; 0 = unlimited (default)")
    parser.add_argument("--seconds", type=float, default=0, help="optional search time budget; 0 = unlimited (default)")
    demand_mode = parser.add_mutually_exclusive_group()
    demand_mode.add_argument("--distinct-demand-parents", dest="distinct_demand_parents",
                             action="store_true", default=True, help="at most one demand per inventory (default)")
    demand_mode.add_argument("--allow-shared-demand-parent", dest="distinct_demand_parents",
                             action="store_false", help="legacy variant: allow several demands per inventory")
    parser.add_argument("--out", type=Path, help="new output directory; default: timestamped directory under data/trees")
    parser.add_argument("--open", action="store_true", help="open the saved gallery in your browser")
    parser.add_argument("--baseline", action="store_true", help="disable partial symmetry pruning for comparison")
    args = parser.parse_args()
    try:
        search = TreeSearch(args.s, args.m, args.i, args.d, limit=args.limit, seconds=args.seconds,
                            distinct_demands=args.distinct_demand_parents, symmetry=not args.baseline)
    except ValueError as exc:
        parser.error(str(exc))
    if args.out is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        args.out = Path("data/trees") / f"s{args.s}_m{args.m}_i{args.i}_d{args.d}_{stamp}"
    handles = []
    writers = {}
    timing = {"generation_seconds": 0.0}

    def timed_graphs():
        iterator = iter(search)
        while True:
            started = time.perf_counter()
            try:
                graph = next(iterator)
            except StopIteration:
                timing["generation_seconds"] += time.perf_counter() - started
                return
            timing["generation_seconds"] += time.perf_counter() - started
            yield graph
    if args.out:
        args.out.mkdir(parents=True, exist_ok=False)
        for name, columns in TABLES.items():
            handle = (args.out / (name + ".csv")).open("w", newline="")
            handles.append(handle)
            writers[name] = csv.writer(handle)
            writers[name].writerow(columns)
    try:
        for roles, edges in timed_graphs():
            validate_tree(roles, edges, search.counts, search.distinct_demands)
            if args.out:
                graph_id = hashlib.sha256(canonical_code(roles, edges).encode()).hexdigest()
                writers["graphs"].writerow([graph_id, *search.counts, len(roles), len(edges)])
                writers["nodes"].writerows((graph_id, v, NAMES[role]) for v, role in enumerate(roles))
                writers["edges"].writerows((graph_id, u, v) for u, v in edges)
    finally:
        for handle in handles:
            handle.close()
    result = dict(schema_version=1, graph_class="typed_directed_tree", counts=dict(zip(ROLES, search.counts)),
                  distinct_demand_parents=search.distinct_demands,
                  method="partial_forest_symmetry" if search.symmetry else "baseline",
                  limit=args.limit, seconds=args.seconds,
                  **search.stats)
    result["count_kind"] = "exact" if search.stats["complete"] else "lower_bound"
    result["generation_seconds"] = round(timing["generation_seconds"], 6)
    result["output_directory"] = str(args.out.resolve())
    result["gallery"] = str((args.out / "index.html").resolve())
    if args.out:
        schema = {"tables": {
            name: {"path": name + ".csv", "columns": {
                col: "string" if col in ("graph_id", "node_type") else "integer" for col in columns}}
            for name, columns in TABLES.items()},
            "primary_keys": {"graphs": ["graph_id"], "nodes": ["graph_id", "node_id"],
                             "edges": ["graph_id", "source", "target"]},
            "references": {"nodes.graph_id": "graphs.graph_id", "edges.graph_id": "graphs.graph_id",
                           "edges.(graph_id,source)": "nodes.(graph_id,node_id)",
                           "edges.(graph_id,target)": "nodes.(graph_id,node_id)"},
            "node_types": list(NAMES.values())}
        (args.out / "schema.json").write_text(json.dumps(schema, indent=2) + "\n")
        (args.out / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
        try:
            from .tree_gallery import build_gallery
        except ImportError:
            from tree_gallery import build_gallery
        gallery_started = time.perf_counter()
        build_gallery(args.out)
        result["gallery_seconds"] = round(time.perf_counter() - gallery_started, 6)
        result["total_seconds"] = round(time.perf_counter() - run_started, 6)
        (args.out / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
        if args.open:
            import webbrowser
            webbrowser.open((args.out / "index.html").resolve().as_uri())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
