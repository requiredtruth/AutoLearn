#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "Run ./install.sh first." >&2; exit 1; }
cd "$ROOT"
if [ "$#" -eq 0 ]; then set -- report; fi
exec "$PY" -m autolearn "$@"
