#!/usr/bin/env bash
# PostToolUse: ruff --fix + black sobre los .py que edita Claude (no bloquea si falla).
in=$(cat)
f=$(printf '%s' "$in" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null \
  || printf '%s' "$in" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
case "$f" in
  *.py)
    if command -v ruff >/dev/null; then ruff check --fix --quiet "$f" >&2 || true; fi
    if command -v black >/dev/null; then black --quiet "$f" >&2 || true; fi
    ;;
esac
exit 0
