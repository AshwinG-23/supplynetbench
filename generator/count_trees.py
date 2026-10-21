"""Exact unlabelled typed-tree counts using multiset series and dissymmetry.

Default rules of trees.py: at most one demand per inventory. No graphs are built.
See TREES.md for the equations. Pure Python; time-bounded, no count on timeout.
"""
import argparse
import itertools
import json
import math
import time
from functools import lru_cache

try:
    from .trees import feasible, check_counts
except ImportError:
    from trees import feasible, check_counts

ZERO = (0, 0, 0, 0)


class TreeCounter:
    def __init__(self, seconds=10, distinct_demands=True):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("seconds must be non-negative and finite")
        self.seconds = seconds
        self.distinct_demands = distinct_demands
        self.started = time.perf_counter()
        # Per-instance caches; discarded with this counter rather than retained globally.
        self.branch = lru_cache(None)(self.branch)
        self.multiset = lru_cache(None)(self.multiset)
        self.log_derivative = lru_cache(None)(self.log_derivative)

    def tick(self):
        if self.seconds and time.perf_counter() - self.started >= self.seconds:
            raise TimeoutError("counting time budget exhausted; exact count unknown")

    def subcounts(self, n):
        for a in itertools.product(*(range(v + 1) for v in n)):
            self.tick()
            yield a, tuple(v - u for v, u in zip(n, a))

    def branch(self, kind, n):
        self.tick()
        role = {"a": 0, "b": 1, "e": 1, "c": 2, "f": 2, "h": 2, "g": 3}[kind]
        if n[role] == 0:
            return 0
        rest = tuple(v - (k == role) for k, v in enumerate(n))
        if kind == "g":
            return int(rest == ZERO)
        if kind == "a":
            return self.multiset("b", rest)
        left, right, need_left, need_right = {
            "b": ("a", "c", False, True),
            "e": ("a", "c", True, False),
            "c": ("ef", "cg", False, True),
            "f": ("ef", "cg", True, False),
            "h": ("ef", "c", True, False),
        }[kind]
        return self.product(left, right, rest, need_left, need_right)

    def product(self, left, right, n, need_left=False, need_right=False, branches=False):
        total = 0
        for a, b in self.subcounts(n):
            if (need_left and a == ZERO) or (need_right and b == ZERO):
                continue
            x = self.branch(left, a) if branches else self.multiset(left, a)
            if x:
                y = self.branch(right, b) if branches else self.multiset(right, b)
                total += x * y
        return total

    def log_derivative(self, kinds, a, axis):
        divisor = math.gcd(*a)
        total = 0
        for k in range(1, divisor + 1):
            if divisor % k == 0:
                reduced = tuple(v // k for v in a)
                total += (a[axis] // k) * sum(self.branch(kind, reduced) for kind in kinds)
        return total

    def multiset(self, kinds, n):
        self.tick()
        if n == ZERO:
            return 1
        if kinds == "cg" and self.distinct_demands:
            # An inventory can have a multiset of I children and zero or one D.
            result = self.multiset("c", n)
            if n[3]:
                result += self.multiset("c", (*n[:3], n[3] - 1))
            return result
        axis = min((j for j in range(4) if n[j]), key=lambda j: n[j])
        total = 0
        for a, rest in self.subcounts(n):
            if a[axis]:
                weight = self.log_derivative(kinds, a, axis)
                if weight:
                    total += weight * self.multiset(kinds, rest)
        quotient, remainder = divmod(total, n[axis])
        assert remainder == 0, "multiset coefficient must be an integer"
        return quotient

    def count(self, counts):
        check_counts(counts)
        if not feasible(counts):
            return 0
        rooted = 0
        for role in range(4):
            rest = tuple(v - (j == role) for j, v in enumerate(counts))
            if role == 0:
                rooted += self.multiset("b", rest) - int(rest == ZERO)
            elif role == 1:
                rooted += self.product("a", "c", rest, True, True)
            elif role == 2:
                rooted += self.product("ef", "cg", rest, True, True)
            else:
                rooted += self.branch("h" if self.distinct_demands else "f", rest)
        edge_rooted = sum(self.product(left, right, counts, branches=True)
                          for left, right in (("a", "b"), ("e", "c"), ("f", "c"), ("h" if self.distinct_demands else "f", "g")))
        answer = rooted - edge_rooted
        assert answer >= 0
        return answer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("counts", type=int, nargs=4, metavar="N", help="counts in S M I D order")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--allow-shared-demand-parent", action="store_true", help="legacy variant")
    args = parser.parse_args()
    try:
        check_counts(args.counts)
        counter = TreeCounter(args.seconds, distinct_demands=not args.allow_shared_demand_parent)
    except ValueError as exc:
        parser.error(str(exc))
    result = {"counts": args.counts, "method": "multiset_dissymmetry", "graph_class": "typed_directed_tree",
              "distinct_demand_parents": counter.distinct_demands}
    try:
        result.update(count=counter.count(tuple(args.counts)), complete=True)
    except TimeoutError as exc:
        result.update(count=None, complete=False, reason=str(exc))
    except RecursionError:
        result.update(count=None, complete=False, reason="Python recursion limit; exact count unknown")
    result["elapsed_seconds"] = round(time.perf_counter() - counter.started, 6)
    result["cached_coefficients"] = sum(f.cache_info().currsize for f in
                                         (counter.branch, counter.multiset, counter.log_derivative))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
