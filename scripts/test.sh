#!/usr/bin/env bash
# Corre pytest dentro del contenedor api contra la DB de tests. Uso: ./scripts/test.sh [args pytest]
set -euo pipefail
cd "$(dirname "$0")/.."
PW="$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)"
exec docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:${PW}@db:5432/saas_test" \
  api pytest "${@:--v}"
