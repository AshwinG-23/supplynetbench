#!/usr/bin/env bash
# One-time server setup: venv (python 3.12+, for numpy 2.5 / networkx 3.7) + pinned requirements
# + self-checks + a timing smoke build. With uv, an older system python is fine: uv fetches 3.12.
# Usage: bash generator/setup_server.sh            (PYTHON=python3.12 to choose the interpreter)
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if command -v uv >/dev/null; then
    "$PY" -c 'import sys; assert sys.version_info >= (3, 12)' 2>/dev/null || PY=3.12  # uv downloads it
    uv venv -p "$PY" .venv && uv pip install -p .venv/bin/python -r requirements.txt
elif ! "$PY" -c 'import sys; assert sys.version_info >= (3, 12)' 2>/dev/null; then
    echo "need python 3.12+ (numpy 2.5.4 / networkx 3.7), got: $("$PY" -V 2>&1)." >&2
    echo "Install python3.12 and rerun with PYTHON=python3.12, or install uv (it fetches 3.12 itself)." >&2
    exit 1
else
    "$PY" -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
.venv/bin/python -c 'import importlib.metadata as m; v = m.version("supplynetpy"); assert v == "0.2.2", v'
.venv/bin/python chain.py
# Timing smoke: 10 tuples x 2 graphs x 4 scenarios x 1 rep = 80 runs at the full-build horizon (few sizes: rough).
W=${WORKERS:-$(( $(nproc) > 3 ? $(nproc) - 2 : 1 ))}
SMOKE=$(mktemp -d)/smoke
.venv/bin/python dataset.py build "$SMOKE" --tuples 10 --graphs 2 --reps 1 --horizon 1825 --warmup 365 --series --workers "$W"
.venv/bin/python - "$SMOKE" "$W" <<'PY'
import sys; from pathlib import Path; from dataset import query
r = query(sys.argv[1], columns=["wall_seconds", "status"]).to_pydict()
w, runs, workers = [x for x in r["wall_seconds"] if x is not None], 2200 * 50 * 4 * 2.1, int(sys.argv[2])  # run_build.sh mix
print(f"smoke: {r['status'].count('ok')}/{len(r['status'])} runs ok, mean {sum(w) / len(w):.2f} s/run (laptop P-core: ~3.5);"
      f" full build ({runs:.0f} runs, {workers} workers; large sizes +17%) ~ {1.17 * sum(w) / len(w) * runs / workers / 3600:.1f} h")
ser = sum(f.stat().st_size for f in Path(sys.argv[1], "series").glob("*.parquet")) / len(r["status"])
print(f"series: {ser / 1e3:.1f} KB/run (check build: 34) -> {ser * runs / 1e9:.0f} GB for the full build, ~2x that peak at compact")
PY
