# Finding your way around

This page shows how to open the dataset, pick the part you need, and turn it into arrays for a model.
All examples are in Python.

## Setup

```bash
git clone <this repository>
cd <repository>
python -m venv .venv && .venv/bin/pip install -r generator/requirements.txt pandas
```

Download the dataset into, say, `data/v2`. Skip the `series/` folder if you do not need the day-by-day
history. After downloading, check that nothing is missing:

```bash
cd data/v2 && sha256sum -c SHA256SUMS
```

In Python, make the helper code importable:

```python
import sys; sys.path.insert(0, "generator")
import pyarrow.compute as pc
from dataset import query
from loader import load_graphs, to_pyg, load_series, aggregate
```

## Filter and sort the catalogue

`query(root, filter, table, columns)` reads one table and keeps only matching rows. The filter is
applied while reading, so the rest of the file is never loaded.

```python
# All finished medicine runs, as a pandas table
runs = query("data/v2", (pc.field("scenario") == "pharma") & (pc.field("status") == "ok")).to_pandas()

# Badly served networks: fill rate below 0.7
bad = runs[runs.main_fill < 0.7]

# Sort by profit per day, best first
runs.sort_values("main_profit_day", ascending=False).head()
```

Useful columns to filter on (all in the `runs` table):

| Filter by | Column | Values |
|---|---|---|
| Industry | `scenario` | `fmcg`, `electronics`, `pharma`, `industrial` |
| Machine-learning group | `split` | `train`, `val`, `test` |
| Well or badly run | `regime` | `control`, `stress` |
| Network size | `n` | 10 … ~300 |
| Network shape | `topology`, `depth`, `multi_parent` | `echelon` / `random_tree`, numbers |
| Big networks only | `size_band` | `base`, `large` |
| Base setups only | `variant` | null for base setups |
| What-if copies | `variant`, `base_config_id` | `outage`, `capacity`, `service` |
| Runs that passed the warm-up check | `warmup_ok` | `True` / `False` |
| Breakdowns | `main_outage_days` | 0 = no breakdown |

More examples:

```python
# Large layered networks in the test group
big = query("data/v2", (pc.field("size_band") == "large") & (pc.field("split") == "test"))

# Every what-if pair: join each copy to its base
r = query("data/v2", columns=["run_id", "config_id", "base_config_id", "replication", "variant", "main_fill"]).to_pandas()
base = r[r.variant.isna()]
pairs = r[r.variant.notna()].merge(base, left_on=["base_config_id", "replication"],
                                   right_on=["config_id", "replication"], suffixes=("", "_base"))
pairs["fill_change"] = pairs.main_fill - pairs.main_fill_base

# The numbers of one setup's nodes
nodes = query("data/v2", pc.field("config_id") == 42, table="nodes").to_pandas()
```

## Graph arrays for machine learning

`load_graphs` gives one Python dict per setup, with NumPy arrays ready for a graph neural network:

```python
for g in load_graphs("data/v2", split="train", scenario="pharma"):
    g["x"]            # [nodes, features]  node inputs (stock rule, prices, delivery times, node kind, ...)
    g["edge_index"]   # [2, links]         who supplies whom
    g["edge_attr"]    # [links, features]  delivery time, its spread, order share, cost
    g["y_node"]       # [runs, nodes, 5]   node results: fill, avg_inventory, shortage, revenue, cost
    g["y_graph"]      # [runs, 5]          network results: fill, profit_day, margin, ext_revenue_day, avg_inventory
    g["meta"]         # ids, industry, regime, what-if change
```

With PyTorch Geometric [^pyg] installed, `to_pyg(g)` turns it into a `Data` object.

## The day-by-day history

```python
s = load_series("data/v2", split="test", scenario="pharma",
                columns=["cust_demand", "cust_served", "profit"], days="main")   # warm-up dropped
s["run_id"]        # [runs]
s["profit"]        # [runs, days]  float32
w = aggregate(s, 7)   # weekly totals (use 30 for roughly monthly)
```

Only the asked-for columns, runs and days are read from disk.

## Check the files yourself

```bash
generator/.venv/bin/python generator/loader.py data/v2
```

This checks that the tables join correctly and that the daily history adds up to the summary results.

[^pyg]: M. Fey and J. E. Lenssen, "Fast Graph Representation Learning with PyTorch Geometric", ICLR Workshop, 2019. <https://pyg.org/>
