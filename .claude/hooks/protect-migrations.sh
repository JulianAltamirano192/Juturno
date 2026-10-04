#!/usr/bin/env bash
# PreToolUse: bloquea editar migraciones de Alembic que ya están versionadas en git.
in=$(cat)
f=$(printf '%s' "$in" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null \
  || printf '%s' "$in" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
case "$f" in
  */alembic/versions/*.py)
    if git -C "${CLAUDE_PROJECT_DIR:-.}" ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      echo "Bloqueado: '$f' ya está commiteada. Nunca se edita una migración aplicada: creá una nueva con 'alembic revision'." >&2
      exit 2
    fi
    ;;
esac
exit 0
