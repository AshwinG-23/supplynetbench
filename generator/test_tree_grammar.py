"""Checks tree_grammar.py against the oracle-verified TreeSearch and TreeCounter."""
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

from count_trees import TreeCounter
from tree_grammar import Enumerator, Grammar, Sampler, decode, load, save
from trees import TreeSearch, canonical_code, validate_tree


def check():
    compared = 0
    for n in range(4, 12):  # every positive tuple up to 11 nodes (the old search is oracle-checked to 9)
        for s in range(1, n):
            for m in range(1, n - s):
                for i in range(1, n - s - m):
                    counts = s, m, i, n - s - m - i
                    G = Grammar(counts, exact=True)
                    records = list(Enumerator(G))
                    assert len(set(records)) == len(records), ("duplicate record", counts)
                    keys = set()
                    for r in records:
                        roles, edges = decode(r)
                        validate_tree(roles, edges, counts)
                        keys.add(canonical_code(roles, edges))
                    assert len(keys) == len(records), ("isomorphic records", counts)
                    assert keys == {canonical_code(*g) for g in TreeSearch(*counts)}, counts
                    assert G.total() == TreeCounter(0).count(counts) == len(records), counts
                    compared += 1
    G = Grammar((4, 4, 8, 4))
    assert G.total() == 676788 == sum(1 for _ in Enumerator(G))
    assert Grammar((5, 10, 20, 10), exact=True).total() == 131944309480559

    # Sampler: canonical records, uniform (chi-square far below the 0.1% critical value 85.4 for df=49).
    G = Grammar((3, 3, 3, 3))
    population = set(Enumerator(G))
    sampler = Sampler(G, 1)
    draws = Counter(sampler.tree() for _ in range(10000))
    assert set(draws) == population
    expected = 10000 / len(population)
    assert sum((draws[r] - expected) ** 2 / expected for r in population) < 85.4
    S = Sampler(Grammar((5, 10, 20, 10)), seed=3)
    assert S.at(7) == S.at(7)
    for k in range(5):
        validate_tree(*decode(S.at(k)), (5, 10, 20, 10))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.npy"
        records = sorted(population)
        save(path, iter(records), (3, 3, 3, 3), 50, chunk=7)
        arr = load(path)
        assert arr.shape == (50, 24) and [bytes(row) for row in arr] == records
    print(f"tree_grammar checks passed ({compared} count tuples compared with TreeSearch)")


if __name__ == "__main__":
    check()
