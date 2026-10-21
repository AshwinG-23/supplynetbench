# SupplyNetBench

A large dataset of **simulated single-product supply chains**: suppliers, factories, warehouses, shops
and customers, each network run day by day for five years with
[SupplyNetPy](https://github.com/SupplyChainSimulation/SupplyNetPy). For every network it stores the full
description (who supplies whom and every number that controls it) and what happened (sales, stock,
costs, profit, per node and per day).

It is meant for training and testing models, for example graph neural networks, that predict how a
supply chain will perform without running the simulation.

**Documentation:** see the `docs/` folder, published with GitHub Pages (link added after the first deploy).
**Data:** on Hugging Face (link added after the first upload).

## Contents

| Path | What |
|---|---|
| `generator/` | The code that builds the dataset (`topology.py` networks, `chain.py` numbers and simulation, `dataset.py` build and tables, `loader.py` reading) |
| `docs/` | The documentation site (MkDocs), and `make_figures.py`, which fills its numbers from a dataset |
| `check.md` | Every numeric assumption, with its status and sources |

## Quick start

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r generator/requirements.txt
.venv/bin/python generator/chain.py          # self-checks
.venv/bin/python generator/dataset.py build data/small --tuples 2 --graphs 5 --reps 1 --workers 4
.venv/bin/python generator/loader.py data/small
```

A full build uses `generator/run_build.sh` (server, `generator/setup_server.sh` first) or
`generator/run_night.sh` (laptop, overnight).

## Credits

B.Tech project at IIT Goa by Ashwin Gaikwad, guided by Tushar Lone and Prof. Neha Karanjkar.
Built on SupplyNetPy (T. Lone and N. Karanjkar, [arXiv:2607.09745](https://arxiv.org/abs/2607.09745)).

## Licence

Code: MIT (`LICENSE`). Dataset: CC BY 4.0 (`LICENSE-DATA`).
