#!/usr/bin/env bash
# Runs every audit_3008b PoC. Exit status = number of scripts that reproduced a finding.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
failed=0
for f in "$HERE"/test_*.py; do
  t="$(basename "$f" .py)"; echo "== $t"
  ( cd "$HERE" && "$PY" "$t.py" 2>&1 | grep -v ' INFO\]\| WARNING\]\|RuntimeWarning\|DeprecationWarning\|^INFO:\|^WARNING:' )
  [ "${PIPESTATUS[0]}" -eq 0 ] || failed=$((failed+1)); echo
done
echo "$failed script(s) reproduced findings"; exit "$failed"
