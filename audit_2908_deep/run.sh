#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
failed=0
for t in test_gemini_review_surface test_client_attested_rewards test_link_code_purge test_upload_quota test_token_surface; do
  echo "== $t"; ( cd "$HERE" && "$PY" "$t.py" 2>&1 | grep -v ' INFO\]\| WARNING\]\|^INFO:\|^WARNING:\|^ERROR:' )
  [ "${PIPESTATUS[0]}" -eq 0 ] || failed=$((failed+1)); echo
done
echo "$failed script(s) reproduced findings"; exit "$failed"
