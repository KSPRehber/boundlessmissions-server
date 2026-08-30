#!/usr/bin/env bash
# Runs every account/auth audit PoC. Exit non-zero if any finding reproduced.
cd "$(dirname "$0")/.."
PY=.venv/bin/python
rc=0
for t in audit_3008b/test_account_*.py; do
  echo "=== $t ==="
  $PY "$t" 2>&1 | grep -v "^20[0-9][0-9]-\|Deprecation\|from starlette"
  [ ${PIPESTATUS[0]} -ne 0 ] && rc=1
done
exit $rc
