#!/bin/bash
# First install. MVGP_PYTHON may name Python 3.12 or an existing populated venv's Python.
set -euo pipefail
S=${MVGP_STUDIO:-/Users/Shared/mvgp-studio}
REPO=$(cd "$(dirname "$0")/.." && pwd)
PY=${MVGP_PYTHON:-python3.12}
umask 077
mkdir -p "$S"
S=$(cd "$S" && pwd -P)
export MVGP_STUDIO=$S
if [[ ! -e $S/venv ]]; then
  PREFIX=$("$PY" -c 'import sys; print(sys.prefix if sys.prefix != sys.base_prefix else "")')
  if [[ -n $PREFIX ]]; then
    ln -s "$PREFIX" "$S/venv"
  else
    "$PY" -m venv "$S/venv"
    "$S/venv/bin/python" -m pip install -r "$REPO/production/requirements.txt"
  fi
fi
cd "$REPO"
exec "$S/venv/bin/python" -m studio.init "$@"
