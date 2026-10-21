#!/usr/bin/env bash
# Full dataset build in the background. Resumable: after any stop or crash, run the same command again.
# Usage: bash generator/run_build.sh [OUT] [extra dataset.py flags]     (WORKERS=n overrides nproc-2)
# Progress: tail -f OUT/build.log      Stop: kill <pid> (pid in OUT.pid); finished shards are kept.
set -euo pipefail
OUT=$(realpath -m "${1:-dataset_full}"); shift || true
cd "$(dirname "$0")"
WORKERS=${WORKERS:-$(( $(nproc) > 3 ? $(nproc) - 2 : 1 ))}
if [ -f "$OUT.pid" ] && kill -0 "$(cat "$OUT.pid")" 2>/dev/null; then
    echo "build already running on $OUT (pid $(cat "$OUT.pid")); stop it first: kill $(cat "$OUT.pid")" >&2; exit 1
fi
mkdir -p "$(dirname "$OUT")"
# v2 sizing (pilot 16 Oct): 1-year warm-up + 4 measured years; 10% of base configs x5 reps, the rest x1; 25% of base
# configs + 2 what-if variants; 5% of sizes 150-300 (size_band=large). ~0.92M runs, ~21 h on 62 workers.
# 0.7.0 check build (17 Oct): per-run p50 3.9 s vs 3.0 s in 0.6.0, measured under heavy laptop load (CPU A/B of the new
# code <= ~5%); sizing kept. Re-time with the setup smoke build on the server; allow ~25% slack if it holds there.
# Disk (0.6.0 check build, 16 Oct): ~5.6 GB compacted + daily series (series/, 34 KB/run) ~32 GB = ~37 GB;
# peak ~75 GB during `compact` (OUT.raw + OUT.compact both exist). --no-series drops series/ (decided: keep it).
# Later flags win: 48 h on 64 cores -> add --tuples 4500 (~1.9M runs: ~77 GB, peak ~155 GB); 24 h on 32 cores -> --tuples 1100.
nohup .venv/bin/python dataset.py build "$OUT" --tuples 2200 --graphs 50 --reps 1 --rep-share 0.1 --rep-count 5 \
    --variants 2 --variant-share 0.25 --large-share 0.05 --large-max 300 --horizon 1825 --warmup 365 --series \
    --scenarios fmcg electronics pharma industrial --workers "$WORKERS" --resume "$@" >> "$OUT.nohup.log" 2>&1 &
echo $! > "$OUT.pid"
echo "started pid $! with $WORKERS workers -> $OUT (log: $OUT/build.log, stderr: $OUT.nohup.log)"
