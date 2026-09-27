#!/bin/bash
# MVGP Mac operator entry point. One allow rule covers every live operation.
#   ops.sh status                          services, the code dir they run, local /review
#   ops.sh deploy <commit>                 studio/deploy.sh <commit> (idle, backup, switch, health, put back on failure)
#   ops.sh restart                         restart api and worker on the current code dir
#   ops.sh rollback                        previous code dir and config (never the database)
#   ops.sh reconcile-cost <input.json>     settle one unknown charge against the provider's own bill
#   ops.sh ledger                          pull fal's and apilio's own usage records for the 账本 now
set -euo pipefail

S=${MVGP_STUDIO:-/Users/Shared/mvgp-studio}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=$S/venv/bin/python
PORT=${MVGP_PORT:-8811}

host() {  # the Host the API answers: its configured public origin
  "$PY" -c 'import json,sys; from urllib.parse import urlsplit; c=json.load(open(sys.argv[1])); print(urlsplit(json.load(open(c["api_config"]))["public_origin"]).netloc)' "$S/current.json"
}
code_dir() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["code_dir"])' "$S/current.json"; }

tool_dir() {
  if [[ -f $HERE/deploy.sh ]]; then echo "$HERE"; else echo "$(code_dir)/studio"; fi
}

show() {
  # shellcheck disable=SC2009
  ps -axo pid=,etime=,args= | grep -E -e "--port $PORT( |$)" -e "production.worker --config" | grep -v grep | cut -c1-120 || true
  if [[ -f $S/current.json ]]; then echo "code dir: $(code_dir)"; else grep -n "^RUNTIME" "$S/launch.py" || true; fi
  curl -s -o /dev/null -w "local /review %{http_code}\n" -H "Host: $(host)" "http://127.0.0.1:$PORT/review" || true
}

case "${1:-status}" in
  status) show ;;
  deploy) "${MVGP_REPO:-$(dirname "$HERE")}/studio/deploy.sh" "${@:2}" ;;
  rollback) "$(tool_dir)/deploy.sh" --rollback ;;
  restart)
    # The worker first, with SIGTERM: the lean worker finishes the send in flight and records its receipt
    # A background-started worker ignores SIGINT.
    # shellcheck disable=SC2009
    worker() {
      config=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["worker_config"])' "$S/current.json")
      ps -axo pid=,args= | grep -F "production.worker --config $config" | grep -v grep | awk '{print $1}' || true
    }
    # shellcheck disable=SC2009
    api() { ps -axo pid=,args= | grep -E -e "--port $PORT( |$)" | grep -v grep | awk '{print $1}'; }
    read -r -a pids <<< "$(worker | tr '\n' ' ')"
    if (( ${#pids[@]} > 0 )); then kill -TERM "${pids[@]}"; fi
    n=0; while [[ -n $(worker) ]]; do (( n++ < 900 )) || { echo "the worker is still finishing a send" >&2; exit 4; }; sleep 1; done
    read -r -a pids <<< "$(api | tr '\n' ' ')"
    if (( ${#pids[@]} > 0 )); then kill "${pids[@]}"; fi
    n=0; while [[ -n $(api) ]]; do (( n++ < 150 )) || { echo "the api did not stop" >&2; exit 4; }; sleep 1; done
    (nohup "$PY" "$S/launch.py" api live >> "$S/logs/live-api.log" 2>&1 &)
    (nohup "$PY" "$S/launch.py" worker live >> "$S/logs/live-worker.log" 2>&1 &)
    sleep 8; show ;;
  reconcile-cost)
    cd "$(code_dir)"
    PYTHONPATH=$(code_dir) "$PY" -m production.operations --config "$S/config/live/operator.json" \
      reconcile-cost --input "${2:?input json}" ;;
  ledger)
    # the same pull the deploy runs; snapshots in state/live/ledger.
    "$PY" "$(tool_dir)/usage_pull.py" --studio "$S" --env-file "$S/env/live-worker.json" --env-file "$S/env/live-ledger.json" \
      --worker-config "$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["worker_config"])' "$S/current.json")" ;;
  *) sed -n 2,8p "$0"; exit 2 ;;
esac
