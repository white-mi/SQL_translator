#!/usr/bin/env bash
# Bring up the stand and produce the checkpoint-1 evidence in results/stand/.
#   bash stand/bootstrap.sh            # full run
#   STEP=baseline bash stand/bootstrap.sh   # rerun from one step: engines|load|refs|selfcheck|baseline
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
OUT=results/stand
mkdir -p "$OUT"
[ -f stand/.env ] || { echo "create stand/.env from stand/.env.example first"; exit 1; }
set -a; . stand/.env; set +a
export SQLTR_PG_HOST=127.0.0.1 SQLTR_TRINO_HOST=127.0.0.1

python3 -m venv .venv >/dev/null 2>&1 || true
. .venv/bin/activate
pip install -q -e . pytest

step=${STEP:-engines}
run() { echo "== $*"; "$@" 2>&1 | tee -a "$OUT/bootstrap.log"; }

if [ "$step" = engines ]; then
  run docker compose -f stand/compose.yaml --env-file stand/.env up -d
  echo "waiting for trino..."
  for i in $(seq 1 60); do
    docker compose -f stand/compose.yaml ps trino | grep -q healthy && break
    sleep 5
  done
  docker compose -f stand/compose.yaml ps | tee "$OUT/compose-ps.txt"
  step=load
fi

if [ "$step" = load ]; then
  run python -m sqltr --local setup-pg
  run python -m sqltr --local load-states --engine both
  step=refs
fi

if [ "$step" = refs ]; then
  run python -m sqltr build-dataset --split train --families 250 --seed 20261101 --out data/train
  run python -m sqltr --local build-references --split-dir data/train --trino
  cp data/train/references_report.json "$OUT/references_report.json"
  step=selfcheck
fi

if [ "$step" = selfcheck ]; then
  run python -m sqltr --local self-check --split-dir data/train
  cp data/train/self_check.json "$OUT/self_check.json"
  # out-of-search pairs (ADR 0009): references on Trino and pair equivalence
  run python -m sqltr build-holdout --out data/holdout
  run python -m sqltr --local build-references --split-dir data/holdout --trino
  run python -m sqltr --local self-check --split-dir data/holdout
  cp data/holdout/self_check.json "$OUT/holdout_self_check.json"
  step=baseline
fi

if [ "$step" = baseline ]; then
  run python -m pytest -q case/metric/tests
  run python -m sqltr --local run-local --split-dir data/train --workers 4 --no-cache --out results/baseline/train
  cp results/baseline/train/summary.json "$OUT/baseline_summary.json"
fi

echo "Evidence is in $OUT and results/baseline/train. Commit them: git add results && git commit -m 'stand: checkpoint-1 evidence'"
