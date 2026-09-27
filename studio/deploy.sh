#!/bin/bash
# Deploy a lean MVGP commit on this Mac, or roll back. Never touches the database.
#   studio/deploy.sh <commit>      code dir, config, stop the worker, idle check, backup, stop the api, switch, start,
#                                  health; puts back on failure
#   studio/deploy.sh <commit> --local-desk   the same, and the desk moves to http://localhost:8811 without a login
# (the public tunnel is stopped separately)
#   studio/deploy.sh --rollback    put back the previous code dir and config (current.previous.json) and restart
# What runs is named in $S/current.json; studio/launch.py reads it. Evidence: $S/evidence/deploy-<ts>.json.
set -euo pipefail

S=${MVGP_STUDIO:-/Users/Shared/mvgp-studio}
REPO=$(cd "$(dirname "$0")/.." && pwd)
PY=$S/venv/bin/python
PORT=${MVGP_PORT:-8811}
TS=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE=$S/evidence/deploy-$TS.json
CURRENT=$S/current.json
PREVIOUS=$S/current.previous.json
STEPS=()
FIRST_INSTALL=0
umask 077

step() { STEPS+=("$1"); echo "deploy: $1"; }

write_evidence() {  # write_evidence <outcome>
  "$PY" - "$EVIDENCE" "$1" "${STEPS[@]}" <<'EOF'
import json, os, sys
path, outcome, steps = sys.argv[1], sys.argv[2], sys.argv[3:]
current = json.load(open(os.environ['MVGP_CURRENT'])) if os.path.exists(os.environ['MVGP_CURRENT']) else None
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump({'outcome': outcome, 'steps': steps, 'current': current}, out, indent=1)
EOF
}

write_current() {  # write_current <code_dir> <api_config> <worker_config>
  "$PY" - "$CURRENT" "$1" "$2" "$3" <<'EOF'
import json, os, sys
path, code, api, worker = sys.argv[1:]
tmp = path + '.tmp'
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump({'code_dir': code, 'api_config': api, 'worker_config': worker}, out, indent=1)
os.replace(tmp, path)
EOF
}

current_field() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

site_host() {  # the Host the API answers: its configured public origin
  "$PY" -c 'import json,sys; from urllib.parse import urlsplit; print(urlsplit(json.load(open(sys.argv[1]))["public_origin"]).netloc)' \
    "$(current_field "$CURRENT" api_config)"
}

ensure_current() {
  # Before the first lean deploy the running tree is the release named in the old launcher.
  if [[ ! -f $CURRENT ]]; then
    if [[ -f $S/init.json && -f $S/config/live/api.json && -f $S/config/live/worker.json && -f $S/state/live/metadata.sqlite ]]; then
      FIRST_INSTALL=1
      step "first install: using initialized configs"
      return
    fi
    local release
    release=$(grep -oE "runtime/release-[0-9a-z-]+" "$S/launch.py" | head -1 || true)
    [[ -n $release ]] || { echo "deploy: no current.json and no release in launch.py" >&2; exit 2; }
    write_current "$S/$release" "$S/config/live/api.json" "$S/config/live/worker.json"
    step "recorded the running tree $release as current"
  fi
}

# ps, not pgrep -f: pgrep can match a waiting shell's own command line.
worker_pids() {  # the live worker, by the config path in current.json; never the rehearsal
  # shellcheck disable=SC2009
  ps -axo pid=,args= | grep -E "production.worker --config $(current_field "$CURRENT" worker_config)( |$)" \
    | grep -v grep | awk '{print $1}' || true
}
api_pids() {  # the live api, by its port
  # shellcheck disable=SC2009
  ps -axo pid=,args= | grep -E -e "--port $PORT( |$)" | grep -v grep | awk '{print $1}' || true
}

stop_worker() {
  # SIGTERM. A worker started in the background by a script ignores SIGINT (checked 2026-09-26: SIG_IGN), so SIGTERM
  # is the only signal it gets. The lean worker turns SIGTERM into "finish the send in flight, start nothing new"
  # The release-97 worker cannot stop gracefully, so it is stopped only when the idle
  # check, run right now, finds nothing queued or in flight. Never SIGKILL.
  local pids=() waited=0
  if [[ $(basename "$(current_field "$CURRENT" code_dir)") == release-* ]] \
      && ! "$PY" "$REPO/studio/idle_check.py" --db "$S/state/live/metadata.sqlite" > "$S/tmp/deploy-idle-old-$TS.json"; then
    echo "deploy: the release worker has work in flight and cannot stop gracefully; try again when idle" >&2
    return 1
  fi
  read -r -a pids <<< "$(worker_pids | tr '\n' ' ')"
  if (( ${#pids[@]} > 0 )); then kill -TERM "${pids[@]}"; fi
  while [[ -n $(worker_pids) ]]; do
    (( waited++ < 900 )) || { echo "deploy: the worker is still finishing a send after 15 minutes" >&2; return 1; }
    sleep 1
  done
  step "stopped the worker after its work in flight"
}

stop_api() {
  local pids=() waited=0
  read -r -a pids <<< "$(api_pids | tr '\n' ' ')"
  if (( ${#pids[@]} > 0 )); then kill "${pids[@]}"; fi
  while [[ -n $(api_pids) ]] || lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
    (( waited++ < 150 )) || { echo "deploy: the api did not stop" >&2; return 1; }
    sleep 1
  done
  step "stopped the api"
}

stop_services() { stop_worker && stop_api; }


start_worker() {
  (nohup "$PY" "$S/launch.py" worker live >> "$S/logs/live-worker.log" 2>&1 &)
}

start_services() {
  mkdir -p "$S/logs"
  (nohup "$PY" "$S/launch.py" api live >> "$S/logs/live-api.log" 2>&1 &)
  start_worker
  step "started api and worker from $(current_field "$CURRENT" code_dir)"
}

healthy() {
  local tries=0 body review worker host
  host=$(site_host)
  while (( tries++ < 60 )); do
    body=$(curl -s -H "Host: $host" "http://127.0.0.1:$PORT/health" || true)
    review=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:$PORT/review" || true)
    if [[ $body == '{"status":"ok"}' && $review == 200 ]]; then
      sleep 5
      # shellcheck disable=SC2009
      worker=$(ps -axo pid=,args= | grep -E "production.worker --config $(current_field "$CURRENT" worker_config)( |$)" | grep -v grep || true)
      if [[ -n $worker ]]; then step "healthy: /health ok, /review 200, worker running"; return 0; fi
    fi
    sleep 1
  done
  step "unhealthy: /health '$body', /review $review"
  return 1
}

put_back() {
  step "putting back the previous code dir and config"
  stop_services || true
  cp -p "$PREVIOUS" "$CURRENT"
  start_services
  if healthy; then write_evidence rolled-back; else write_evidence rollback-unhealthy; fi
  exit 1
}

export MVGP_CURRENT=$CURRENT

if [[ ${1:-} == --rollback ]]; then
  [[ -f $PREVIOUS ]] || { echo "deploy: no previous code dir recorded" >&2; exit 2; }
  step "rollback to $(current_field "$PREVIOUS" code_dir)"
  stop_services
  swap=$(mktemp "$S/tmp/current.XXXXXX")
  cp -p "$CURRENT" "$swap"
  cp -p "$PREVIOUS" "$CURRENT"
  mv "$swap" "$PREVIOUS"
  start_services
  if healthy; then write_evidence rolled-back; exit 0; fi
  write_evidence rollback-unhealthy
  exit 1
fi

COMMIT=${1:?usage: deploy.sh <commit> [--local-desk] | --rollback}
LOCAL_DESK=${2:-}
[[ -z $LOCAL_DESK || $LOCAL_DESK == --local-desk ]] || { echo "deploy: unknown option $LOCAL_DESK" >&2; exit 2; }
SHA=$(git -C "$REPO" rev-parse --short=12 "$COMMIT^{commit}")
CODE=$S/runtime/lean-$SHA
CONFIG=$S/runtime/lean-$SHA.config${LOCAL_DESK:+-local}
ensure_current

# 1. Code dir: the commit's production and studio trees, read-only once written.
#    Fonts and manuals ship under production/.
if [[ ! -d $CODE ]]; then
  mkdir -m 700 "$CODE"
  git -C "$REPO" archive "$SHA" production studio | tar -x -C "$CODE"
  chmod -R a-w "$CODE"
fi
step "code dir $CODE"
if (( FIRST_INSTALL )); then
  write_current "$CODE" "$S/config/live/api.json" "$S/config/live/worker.json"
fi

# 2. Config beside it: migrated from the release configs the first time, else the current lean configs,
#    validated by the new code's own models.
if [[ ! -d $CONFIG ]]; then
  if [[ $(basename "$(current_field "$CURRENT" code_dir)") == release-* ]]; then
    "$PY" "$CODE/studio/migrate_config.py" --api "$S/config/live/api.json" --worker "$S/config/live/worker.json" \
      --db "$S/state/live/metadata.sqlite" --out-dir "$CONFIG" > "$S/tmp/deploy-migrate-$TS.json"
  else
    mkdir -m 700 "$CONFIG"
    cp -p "$(current_field "$CURRENT" api_config)" "$CONFIG/api.json"
    cp -p "$(current_field "$CURRENT" worker_config)" "$CONFIG/worker.json"
    if [[ -n $LOCAL_DESK ]]; then
      "$PY" - "$CONFIG" "$PORT" <<'EOF'
import json, os, sys
folder, port = sys.argv[1], sys.argv[2]
origin = f'http://localhost:{port}'
for name, change in (('api.json', {'public_origin': origin, 'local_owner': True}), ('worker.json', {'public_origin': origin})):
    path = os.path.join(folder, name)
    value = {**json.load(open(path)), **change}
    if name == 'api.json':
        value.pop('owner', None)
    fd = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=1)
    os.replace(path + '.tmp', path)
EOF
    fi
  fi
fi
# Every config dir, also one prepared ahead, is checked by the new code: its models and the worker's lease rule
# (release 3's first attempt passed the models and its worker still refused to start).
if ! PYTHONPATH=$CODE "$PY" "$CODE/studio/check_config.py" "$CONFIG/api.json" "$CONFIG/worker.json"; then
  echo "deploy: $CONFIG does not suit $CODE; nothing was stopped" >&2
  exit 1
fi
step "config $CONFIG"

# 3. Stop the worker first: it finishes the send in flight and starts nothing new. Only then is "idle" still true
# when the services stop. Busy: the worker is started again and nothing changes.
if ! stop_worker; then
  write_evidence worker-still-finishing; echo "deploy: nothing switched; restart the worker with ops.sh restart once it exits" >&2; exit 4
fi
if ! "$PY" "$REPO/studio/idle_check.py" --db "$S/state/live/metadata.sqlite" > "$S/tmp/deploy-idle-$TS.json"; then
  start_worker
  step "busy: see tmp/deploy-idle-$TS.json; the worker was started again"; write_evidence refused-busy; exit 3
fi
step "idle"

# 4. Backup: online SQLite copy (checked), media and payload clones, configs, current.json and launcher.
BACKUP=$S/backup/deploy-$TS
mkdir -m 700 "$BACKUP"
"$PY" - "$S/state/live/metadata.sqlite" "$BACKUP/metadata.sqlite" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
dst.close(); src.close()
EOF
mkdir -m 700 "$BACKUP/state-live"
for part in media review-payloads releases; do
  [[ -e $S/state/live/$part ]] && cp -cRp "$S/state/live/$part" "$BACKUP/state-live/$part"
done
cp -Rp "$S/config/live" "$BACKUP/config-live"
cp -p "$CURRENT" "$S/launch.py" "$BACKUP/"
[[ -d $(dirname "$(current_field "$CURRENT" api_config)") ]] && cp -Rp "$(dirname "$(current_field "$CURRENT" api_config)")" "$BACKUP/config-current"
step "backup $BACKUP (quick_check ok)"

# 5. Stop the api, switch, start, check. The old launcher is kept once so a release tree can still be started.
if [[ ! -f $S/launch-release.py ]] && grep -q "runtime/release-" "$S/launch.py"; then
  cp -p "$S/launch.py" "$S/launch-release.py"
fi
stop_api
# with both services stopped, only a 再拍一批 answered after this event re-fires by itself.
# A failure inside $(...) in an argument does not stop a set -e script (bug hunt 2026-09-27), so check it here. Nothing is
# switched yet: the running release starts again unchanged.
if ! cutoff=$("$PY" "$REPO/studio/reshoot_cutoff.py" --db "$S/state/live/metadata.sqlite" --worker-config "$CONFIG/worker.json"); then
  step "reshoot cutoff could not be written; starting the current release again"
  start_services
  exit 1
fi
step "reshoot cutoff: event $cutoff"
cp -p "$CURRENT" "$PREVIOUS"
write_current "$CODE" "$CONFIG/api.json" "$CONFIG/worker.json"
install -m 600 "$CODE/studio/launch.py" "$S/launch.py"
step "switched current.json to $CODE"
start_services
healthy || put_back
# the providers' own usage records for the 账本, once per deploy; a failure never undoes it.
if "$PY" "$REPO/studio/usage_pull.py" --studio "$S" --env-file "$S/env/live-worker.json" --env-file "$S/env/live-ledger.json" \
    --worker-config "$CONFIG/worker.json" > "$S/tmp/deploy-usage-$TS.txt" 2>&1; then
  step "usage records pulled (tmp/deploy-usage-$TS.txt)"
else
  step "usage pull failed, see tmp/deploy-usage-$TS.txt; the deploy stands"
fi
write_evidence deployed
