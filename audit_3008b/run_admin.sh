#!/usr/bin/env bash
# Runs the admin/authorization PoCs (test_admin_*.py). Exit status = scripts with findings.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
failed=0
for t in test_admin_console test_admin_targets test_admin_suspension test_admin_slash; do
  echo "== $t"; ( cd "$HERE" && "$PY" "$t.py" 2>&1 | grep -v ' INFO\]\| WARNING\]\|^INFO:\|^WARNING:\|RuntimeWarning' )
  [ "${PIPESTATUS[0]}" -eq 0 ] || failed=$((failed+1)); echo
done
echo "$failed script(s) reproduced findings"; exit "$failed"
