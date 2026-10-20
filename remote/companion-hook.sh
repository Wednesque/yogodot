#!/usr/bin/env sh
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$DIR/companion-hook.py" "$@"
fi
exec python "$DIR/companion-hook.py" "$@"
