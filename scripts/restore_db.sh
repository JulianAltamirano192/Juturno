#!/usr/bin/env bash
#
# Restaura la base de datos de Juturno desde un backup .sql.gz.
# Uso: ./scripts/restore_db.sh <ruta_o_s3_uri> [--yes]
#
#   <ruta>   Ruta local al archivo .sql.gz,
#            o URI s3://bucket/key para descargarlo primero.
#   --yes    Omite la confirmación interactiva (para scripts).
#
# ADVERTENCIA: sobreescribe la base de datos existente.

set -euo pipefail

BACKUP_SRC="${1:-}"
SKIP_CONFIRM=false
[ "${2:-}" = "--yes" ] && SKIP_CONFIRM=true

if [ -z "$BACKUP_SRC" ]; then
    echo "Uso: $0 <ruta_local.sql.gz | s3://bucket/key> [--yes]"
    exit 1
fi

DB_NAME="${POSTGRES_DB:-saas_db}"
DB_USER="${POSTGRES_USER:-postgres}"
TMPDIR_LOCAL=""

# Descarga desde S3 si es necesario
if [[ "$BACKUP_SRC" == s3://* ]]; then
    TMPDIR_LOCAL=$(mktemp -d)
    LOCAL_FILE="${TMPDIR_LOCAL}/$(basename "$BACKUP_SRC")"
    echo "Descargando $BACKUP_SRC ..."
    aws s3 cp "$BACKUP_SRC" "$LOCAL_FILE"
    BACKUP_SRC="$LOCAL_FILE"
fi

if [ ! -f "$BACKUP_SRC" ]; then
    echo "ERROR: No se encontró el archivo: $BACKUP_SRC"
    exit 1
fi

SIZE=$(du -h "$BACKUP_SRC" | cut -f1)
echo ""
echo "  Archivo : $BACKUP_SRC ($SIZE)"
echo "  Base    : $DB_NAME"
echo ""
echo "  ADVERTENCIA: esto sobreescribe TODOS los datos en $DB_NAME."
echo ""

if [ "$SKIP_CONFIRM" = false ]; then
    read -r -p "¿Confirmar restore? (escribí YES para continuar): " CONFIRM
    if [ "$CONFIRM" != "YES" ]; then
        echo "Cancelado."
        [ -n "$TMPDIR_LOCAL" ] && rm -rf "$TMPDIR_LOCAL"
        exit 0
    fi
fi

echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Iniciando restore de $DB_NAME ..."
gunzip -c "$BACKUP_SRC" | docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME"

echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Restore completado."

[ -n "$TMPDIR_LOCAL" ] && rm -rf "$TMPDIR_LOCAL"
