#!/usr/bin/env bash
#
# Backup de la base de datos de Juturno.
# Uso: ./scripts/backup_db.sh [ruta_destino]
#
# Hace un dump de PostgreSQL comprimido y rota backups viejos (>30 días).

set -euo pipefail

BACKUP_DIR="${1:-./backups}"
RETENTION_DAYS=30
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB_NAME="${POSTGRES_DB:-saas_db}"
DB_USER="${POSTGRES_USER:-postgres}"
BACKUP_FILE="${BACKUP_DIR}/saas_db_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Iniciando backup de $DB_NAME..."

docker compose exec -T db pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" | gzip > "$BACKUP_FILE"

if [ ! -s "$BACKUP_FILE" ]; then
    echo "ERROR: El backup quedó vacío"
    rm -f "$BACKUP_FILE"
    exit 1
fi

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Backup OK: $BACKUP_FILE ($SIZE)"

echo "Rotando backups más viejos que $RETENTION_DAYS días..."
find "$BACKUP_DIR" -name "saas_db_*.sql.gz" -mtime +$RETENTION_DAYS -delete
REMAINING=$(find "$BACKUP_DIR" -name "saas_db_*.sql.gz" | wc -l)
echo "Backups activos: $REMAINING"

# Copia a S3 si S3_BACKUP_BUCKET está configurado
if [ -n "${S3_BACKUP_BUCKET:-}" ]; then
    S3_KEY="${S3_BACKUP_PREFIX:-juturno/backups}/$(basename "$BACKUP_FILE")"
    echo "Subiendo a s3://${S3_BACKUP_BUCKET}/${S3_KEY} ..."
    aws s3 cp "$BACKUP_FILE" "s3://${S3_BACKUP_BUCKET}/${S3_KEY}" \
        || { echo "[$(date +%Y-%m-%d\ %H:%M:%S)] WARNING: S3 upload failed; local backup preserved at $BACKUP_FILE"; exit 2; }
    echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Upload S3 OK: s3://${S3_BACKUP_BUCKET}/${S3_KEY}"
fi

echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Backup completado."
