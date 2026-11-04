#!/usr/bin/env bash
# Operator commands for the SQLTR campaign on one VM: evaluators run as host processes (the arena's
# "service without a model" mode), runners are Docker containers, engines come from stand/compose.yaml.
#
#   sudo bash case/deploy/arena.sh <command>      with /etc/sqltr-arena/operator.env filled
#
# Commands, in the order of the first deployment:
#   layout      directories, permissions, tokens, research network
#   image       build the runner image sqltr-runner:1 (ADR 0008)
#   freeze      freeze the metric package into $ROOT/metric (once per campaign)
#   data        train copy + public/private from operator seeds (/etc/sqltr-arena/seeds.json), references on Trino
#   accept      synthetic acceptance (case/acceptance/run.py) in a throwaway directory
#   journal     init the campaign journal with MAX_SOLUTIONS (once per campaign)
#   start|stop|status   train/public/private evaluators (systemd transient units)
#   readiness   GET /v1/search-readiness of private with the campaign conditions
#   researcher-env      print the env block for the researcher container (URLs, no secrets)
set -euo pipefail
ENV_FILE=${SQLTR_OPERATOR_ENV:-/etc/sqltr-arena/operator.env}
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy case/deploy/operator.env.example)"; exit 1; }
set -a; . "$ENV_FILE"; set +a
PY="$ARENA_DIR/.venv/bin/python"
SPLITS="train public private"
SERVICE_USER=${SERVICE_USER:-arena}

arena() { (cd "$ARENA_DIR" && "$PY" -m "$@"); }
port() { case $1 in train) echo "$TRAIN_PORT";; public) echo "$PUBLIC_PORT";; private) echo "$PRIVATE_PORT";; esac; }

cmd_layout() {
  id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  usermod -aG docker "$SERVICE_USER"
  install -d -m 0755 -o "$SERVICE_USER" "$ROOT" "$ROOT/assets" "$ROOT/logs"
  install -d -m 0700 -o root "$ROOT/env"
  install -d -m 0700 -o "$SERVICE_USER" "$ROOT/secrets" "$ROOT/search-control" "$ROOT/data"
  for s in $SPLITS; do
    install -d -m 0700 -o "$SERVICE_USER" "$ROOT/state-$s" "$ROOT/usage-$s" "$ROOT/data/$s"
    [ -s "$ROOT/secrets/$s-token" ] || { "$PY" -c "import secrets;print(secrets.token_urlsafe(32))" > "$ROOT/secrets/$s-token"; }
    chown "$SERVICE_USER" "$ROOT/secrets/$s-token"; chmod 0600 "$ROOT/secrets/$s-token"
  done
  # the journal directory is shared with the researcher container (uid 10001 writes, evaluator reads)
  install -d -m 0770 -o 10001 -g "$(id -g "$SERVICE_USER")" "$ROOT/search"
  # metric drivers inside the arena venv: the metric command runs with the evaluator's interpreter
  "$PY" -m pip install -q "psycopg[binary]==3.3.6" "trino==0.340.0" 2>/dev/null \
    || uv pip install --python "$PY" "psycopg[binary]==3.3.6" "trino==0.340.0"
  docker network inspect arena-research >/dev/null 2>&1 || \
    docker network create --subnet 172.30.0.0/24 --gateway "$LISTEN_HOST" arena-research
  echo "layout ready in $ROOT"
}

cmd_image() { (cd "$SQLTR_DIR" && docker build -f case/harness/runner.Dockerfile -t "$RUNNER_IMAGE" .); docker images --digests "${RUNNER_IMAGE%%:*}"; }

cmd_freeze() {
  [ -e "$ROOT/metric" ] && { echo "$ROOT/metric exists: the metric is frozen for this campaign"; exit 1; }
  local src; src=$(mktemp -d)
  cp -r "$SQLTR_DIR/case/metric/"{metric.json,score.py,metric_config.json,sqltr_metric} "$src/"
  find "$src" -name __pycache__ -prune -exec rm -rf {} +
  arena evaluator.prepare freeze --metric "$src" --output "$ROOT/metric"
  arena evaluator.prepare verify --metric "$ROOT/metric"
  chown -R "$SERVICE_USER" "$ROOT/metric"; chmod -R a+rX,go-w "$ROOT/metric"; rm -rf "$src"
  sha256sum "$ROOT/metric/metric.lock.json"
}

cmd_data() {
  local seeds=/etc/sqltr-arena/seeds.json sq=("$SQLTR_DIR/.venv/bin/python" -m sqltr --local)
  [ -f "$seeds" ] || { echo "missing $seeds (operator seeds, see README: never in git)"; exit 1; }
  cd "$SQLTR_DIR"
  [ -f data/train/dataset.jsonl ] || { echo "run stand/bootstrap.sh first (builds train)"; exit 1; }
  "${sq[@]}" build-dataset --split public --families "${PUBLIC_FAMILIES:-150}" --seed-file "$seeds" \
    --exclude data/train/instances.jsonl --out "$ROOT/data/public-build"
  "${sq[@]}" build-dataset --split private --families "${PRIVATE_FAMILIES:-150}" --seed-file "$seeds" \
    --exclude data/train/instances.jsonl "$ROOT/data/public-build/instances.jsonl" --out "$ROOT/data/private-build"
  for s in public private; do "${sq[@]}" build-references --split-dir "$ROOT/data/$s-build" --trino; done
  cp data/train/dataset.jsonl "$ROOT/data/train/dataset.jsonl"
  for s in public private; do cp "$ROOT/data/$s-build/dataset.jsonl" "$ROOT/data/$s/dataset.jsonl"; done
  chown -R "$SERVICE_USER" "$ROOT/data"; chmod -R go-rwx "$ROOT/data"
  sha256sum "$ROOT"/data/{train,public,private}/dataset.jsonl
}

cmd_accept() {
  local work="$ROOT-accept-$(date -u +%Y%m%dT%H%M%S)"
  (cd "$SQLTR_DIR" && "$SQLTR_DIR/.venv/bin/python" case/acceptance/run.py --arena "$ARENA_DIR" --work "$work" \
     --runner-image "$RUNNER_IMAGE" --local)
}

cmd_journal() {
  [ -e "$ROOT/search/search.sqlite3" ] && { echo "journal exists: resume with the same database"; exit 1; }
  arena evaluator.search --journal "$ROOT/search/search.sqlite3" init --max-solutions "$MAX_SOLUTIONS"
  chown 10001:"$(id -g "$SERVICE_USER")" "$ROOT"/search/search.sqlite3*; chmod 0660 "$ROOT"/search/search.sqlite3*
}

write_env() {  # split -> $ROOT/env/<split>.env (read by systemd, root-owned, 0600)
  local s=$1 f="$ROOT/env/$1.env"
  umask 077
  cat > "$f" <<EOF
ARENA_METRIC_PATH=$ROOT/metric
ARENA_DATASET_PATH=$ROOT/data/$s/dataset.jsonl
ARENA_STATE_DIR=$ROOT/state-$s
ARENA_USAGE_DIR=$ROOT/usage-$s
ARENA_TOKEN_FILE=$ROOT/secrets/$s-token
ARENA_ASSETS_DIR=$ROOT/assets
ARENA_RUNNER_IMAGE=$RUNNER_IMAGE
ARENA_HARNESSES='{"sqltr-program":{"image":"$RUNNER_IMAGE","uses_model":false,"network":"none"}}'
ARENA_USES_MODEL=false
ARENA_RUNNER_NETWORK=none
ARENA_MODEL=
ARENA_MODEL_BASE_URL=
ARENA_RUNNER_TIMEOUT=600
ARENA_EXECUTION_SLOTS=$EXECUTION_SLOTS
ARENA_RELEASE_ID=$RELEASE_ID-$s
SQLTR_PG_HOST=$SQLTR_PG_HOST
SQLTR_PG_PORT=$SQLTR_PG_PORT
SQLTR_PG_ADMIN_PASSWORD=$SQLTR_PG_ADMIN_PASSWORD
SQLTR_PG_RUNNER_PASSWORD=$SQLTR_PG_RUNNER_PASSWORD
SQLTR_TRINO_HOST=$SQLTR_TRINO_HOST
SQLTR_TRINO_PORT=$SQLTR_TRINO_PORT
EOF
  case $s in
    train) cat >> "$f" <<EOF
ARENA_SEARCH_MODE=standalone
ARENA_CAMPAIGN_ID=$CAMPAIGN_ID
ARENA_CASE_ID=sqltr
ARENA_DATASET_ID=$TRAIN_DATASET_ID
ARENA_CONDITIONS_ID=$CONDITIONS_ID
ARENA_TRAIN_TRACKING=false
EOF
    ;;
    public) printf 'ARENA_SPLIT=public\nARENA_SEARCH_MODE=standalone\n' >> "$f" ;;
    private) cat >> "$f" <<EOF
ARENA_SPLIT=private
ARENA_SEARCH_MODE=campaign
ARENA_SEARCH_JOURNAL=$ROOT/search/search.sqlite3
ARENA_SEARCH_MAX_SOLUTIONS=$MAX_SOLUTIONS
ARENA_SEARCH_STOP_PATH=$ROOT/search-control/stop.json
ARENA_FINALISTS=$FINALISTS
ARENA_OUTPUT_COUNT=$OUTPUT_COUNT
EOF
    ;;
  esac
}

cmd_start() {
  for s in $SPLITS; do
    write_env "$s"
    local app="from evaluator.service import Settings as S, create_app as c; app = c(S.from_env())"
    [ "$s" = train ] && app="from evaluator.train import TrainSettings as S, create_app as c; app = c(S.from_env())"
    local code="import uvicorn; $app; uvicorn.run(app, host='$LISTEN_HOST', port=$(port "$s"), access_log=False)"
    if [ -d /run/systemd/system ]; then
      systemctl is-active --quiet "sqltr-arena-$s" && { echo "$s already running"; continue; }
      systemd-run --unit "sqltr-arena-$s" --uid "$SERVICE_USER" --working-directory "$ARENA_DIR" \
        --property EnvironmentFile="$ROOT/env/$s.env" --property Restart=on-failure "$PY" -c "$code"
    else  # hosts without systemd: plain background process under the service user
      [ -f "$ROOT/logs/$s.pid" ] && kill -0 "$(cat "$ROOT/logs/$s.pid")" 2>/dev/null && { echo "$s already running"; continue; }
      ( set -a; . "$ROOT/env/$s.env"; set +a; cd "$ARENA_DIR"
        nohup runuser -u "$SERVICE_USER" -- "$PY" -c "$code" >> "$ROOT/logs/$s.log" 2>&1 &
        echo $! > "$ROOT/logs/$s.pid" )
    fi
  done
  sleep 5; cmd_status
}

cmd_stop() {
  for s in $SPLITS; do
    if [ -d /run/systemd/system ]; then systemctl stop "sqltr-arena-$s" 2>/dev/null || true
    elif [ -f "$ROOT/logs/$s.pid" ]; then pkill -P "$(cat "$ROOT/logs/$s.pid")" 2>/dev/null; kill "$(cat "$ROOT/logs/$s.pid")" 2>/dev/null; rm -f "$ROOT/logs/$s.pid"
    fi
  done; sleep 2; echo stopped
}

cmd_status() {
  for s in $SPLITS; do
    if curl -fsS "http://$LISTEN_HOST:$(port "$s")/healthz" >/dev/null 2>&1; then echo "$s: up :$(port "$s")"
    else echo "$s: DOWN (journalctl -u sqltr-arena-$s)"; fi
  done
}

cmd_readiness() {
  arena evaluator.client search-readiness --url "http://$LISTEN_HOST:$PRIVATE_PORT" \
    --token-file "$ROOT/secrets/private-token" --require-campaign --max-solutions "$MAX_SOLUTIONS" \
    --finalists "$FINALISTS" --output-count "$OUTPUT_COUNT" --release-id "$RELEASE_ID-private"
}

cmd_researcher_env() {
  cat <<EOF
# researcher container: network arena-research, journal $ROOT/search mounted at /search (rw)
SQLTR_TRAIN_URL=http://$LISTEN_HOST:$TRAIN_PORT
SQLTR_PUBLIC_URL=http://$LISTEN_HOST:$PUBLIC_PORT
SQLTR_PRIVATE_URL=http://$LISTEN_HOST:$PRIVATE_PORT
ARENA_SEARCH_JOURNAL=/search/search.sqlite3
# tokens: copy $ROOT/secrets/{train,public,private}-token into the researcher's secret mount, read-only
EOF
}

case "${1:-}" in
  layout|image|freeze|data|accept|journal|start|stop|status|readiness) "cmd_$1" ;;
  researcher-env) cmd_researcher_env ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
