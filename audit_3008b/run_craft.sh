#!/bin/bash
# Runs the craft-in-flight PoCs with the bot's venv; exit status = scripts with findings.
cd "$(dirname "$0")" || exit 1
fails=0
for t in test_craft_*.py; do
  echo "=== $t"
  ../.venv/bin/python "$t" 2>&1 | grep -v RuntimeWarning
  [ "${PIPESTATUS[0]}" != "0" ] && fails=$((fails+1))
done
exit $fails
