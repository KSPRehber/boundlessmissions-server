#!/bin/bash
# Runs every economy PoC; exit status is the number of scripts with findings.
cd "$(dirname "$0")"
fails=0
for f in test_econ_*.py; do
  echo "=== $f"
  ../.venv/bin/python "$f" 2>&1 | grep -v '^20[0-9][0-9]-\|RuntimeWarning' | grep '^  \(ok\|BUG\)\|^ *->\|finding(s)'
  [ "${PIPESTATUS:-0}" ] ; ../.venv/bin/python "$f" >/dev/null 2>&1 || fails=$((fails+1))
done
echo "scripts with findings: $fails"
exit $fails
