#!/usr/bin/env bash
# Laptop overnight build: keeps the laptop awake (lid closed too), stops cleanly at a deadline,
# then compacts, runs the loader self-check and regenerates the docs' numbers and figures.
# Usage: bash generator/run_night.sh [OUT] [HOURS]      (defaults: data/night_v071, 12)
#   TUPLES=n (default 150 ~ 62k runs, ~9.5 h on 13 workers), WORKERS=n (default 13)
# Progress: tail -f OUT/build.log      Stop early: kill $(cat OUT.night.pid)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=$(realpath -m "${1:-data/night_v071}"); HOURS=${2:-12}
export TUPLES=${TUPLES:-150} WORKERS=${WORKERS:-13}
if [ -z "${NIGHT_CHILD:-}" ]; then  # re-launch detached and holding a sleep / lid-switch inhibitor
    mkdir -p "$(dirname "$OUT")"
    NIGHT_CHILD=1 nohup systemd-inhibit --what=sleep:idle:handle-lid-switch --who=btp-dataset \
        --why="overnight dataset build" bash "$0" "$OUT" "$HOURS" >> "$OUT.night.log" 2>&1 &
    echo $! > "$OUT.night.pid"
    echo "night build started (pid $!): $OUT, $TUPLES tuples, $WORKERS workers, deadline $HOURS h"
    echo "log: $OUT.night.log   progress: tail -f $OUT/build.log"
    exit 0
fi

[ -e generator/.venv ] || ln -s ../.venv-gen generator/.venv  # run_build.sh expects generator/.venv
PY=generator/.venv/bin/python
echo "$(date '+%F %T') start: $TUPLES tuples, $WORKERS workers, deadline $HOURS h"
bash generator/run_build.sh "$OUT" --tuples "$TUPLES"
pid=$(cat "$OUT.pid")
end=$(( $(date +%s) + ${DEADLINE_MIN:-$(( HOURS * 60 ))} * 60 ))  # DEADLINE_MIN: for testing
while kill -0 "$pid" 2>/dev/null; do
    if [ "$(date +%s)" -ge "$end" ]; then  # finished shards are kept; same command + run_build.sh resumes
        echo "$(date '+%F %T') deadline reached: stopping the build"
        kill "$pid"
        while kill -0 "$pid" 2>/dev/null; do sleep 5; done
        echo "Stopped at the $HOURS h deadline on $(date '+%F %T'); uncompacted but loadable." > "$OUT/PARTIAL.txt"
        $PY generator/loader.py "$OUT" || true
        exit 0
    fi
    sleep 60
done

grep -q "done:" "$OUT/build.log" || { echo "$(date '+%F %T') build ended without 'done' (see $OUT.nohup.log)"; exit 1; }
echo "$(date '+%F %T') build done; compacting"
$PY generator/dataset.py compact "$OUT"
$PY generator/loader.py "$OUT"
MPLCONFIGDIR=${MPLCONFIGDIR:-/tmp/mpl-btp} $PY docs/make_figures.py "$OUT"
echo "$(date '+%F %T') all done (delete $OUT.raw once happy)"
