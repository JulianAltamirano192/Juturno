# Deployment

> Cómo desplegar, migrar, hacer rollback y operar en producción. Para DevOps y devs que deployan.

---

## 1. Entorno local (`docker-compose.yml`)

```yaml
# Servicios:
# - db: postgres:16-alpine (puerto 5432, healthcheck pg_isready)
# - redis: redis:7-alpine (puerto 6379, healthcheck redis-cli ping)
# - api: build local (puerto 8000, volumes para hot-reload, depends_on healthy)
```

**Levantar:**
```bash
docker compose up -d --build
docker compose exec api alembic upgrade head
```

**Variables** (`.env` en raíz, copia de `.env.example`):
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- `DATABASE_URL` (se construye en compose con los 3 anteriores)
- `REDIS_URL=redis://redis:6379/0`
- `MP_SANDBOX=true` (dev usa sandbox)
- `ENVIRONMENT=development`
- `SECRET_KEY` (cualquier valor, no se valida en dev)

**Tests:**
```bash
# Crear DB tests (una vez)
docker compose exec db createdb -U postgres saas_test

# Correr tests (dentro del contenedor api)
docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@db:5432/saas_test" \
  api pytest -v
```

> `TEST_DATABASE_URL` apunta al servicio `db` (no `localhost`). El scheduler **NO arranca** si esta variable está seteada (`main.py:116-153`).

---

## 2. Entorno producción (`docker-compose.prod.yml` + Coolify)

```yaml
# Servicios:
# - db: postgres:16-alpine (sin puertos expuestos, volume persistente)
# - redis: redis:7-alpine (sin puertos expuestos)
# - api: build . (command: alembic upgrade head + uvicorn --proxy-headers)
```

**Entrypoint del contenedor API** (`docker-compose.prod.yml:29-31`):
```bash
sh -c "python -m alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"
```
- Migraciones corren **automáticamente** al iniciar el contenedor.
- `--proxy-headers` necesario detrás de Traefik (X-Forwarded-For, X-Forwarded-Proto).

**Variables de entorno en Coolify** (todas obligatorias salvo `SENTRY_DSN`):
| Variable | Origen |
|----------|--------|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Generadas por Coolify (DB gestionada) o secrets |
| `DATABASE_URL` | `postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}` |
| `REDIS_URL` | `redis://redis:6379/0` |
| `SECRET_KEY` | `openssl rand -hex 32` (⚠️ validador bloquea default en prod) |
| `CORS_ORIGINS` | `["https://juturno.com"]` (JSON list) |
| `ENVIRONMENT` | `production` |
| `PUBLIC_BASE_URL` | `https://juturno.com` |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | Meta Developer Console (System User) |
| `META_VERIFY_TOKEN`, `META_APP_SECRET` | Meta Webhook config |
| `MP_ACCESS_TOKEN` | Access token plataforma (fallback legacy) |
| `MP_SECRET_KEY` | MP Developers > Webhooks > Secret |
| `MP_MARKETPLACE_CLIENT_ID/SECRET` | MP Developers > App > Credenciales |
| `MP_MARKETPLACE_REDIRECT_URL` | `https://api.juturno.com/mp/connect/callback` |
| `MP_NOTIFICATION_URL` | `https://api.juturno.com/webhooks/mercadopago` (⚠️ validador bloquea vacía en prod) |
| `MP_TOKEN_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `MP_SANDBOX` | `false` (producción real) |
| `SENTRY_DSN` | Opcional (Sentry project settings) |

> **Checklist pre-deploy**: verificar que **todas** las variables críticas están seteadas en Coolify antes del primer deploy. Ver D-014 y D-018 en `DECISIONS.md`.

> ⚠️ **Pendiente de confirmar**: `docker-compose.prod.yml` solo lista explícitamente un subset de variables en la sección `environment` del servicio `api` (DATABASE_URL, REDIS_URL, WHATSAPP_*, META_*, MP_ACCESS_TOKEN, MP_SECRET_KEY, SENTRY_DSN, ENVIRONMENT, CORS_ORIGINS). Las demás (`SECRET_KEY`, `PUBLIC_BASE_URL`, `MP_MARKETPLACE_*`, `MP_TOKEN_ENCRYPTION_KEY`, `MP_SANDBOX`) deben llegar al contenedor vía el mecanismo de env vars de Coolify; verificar que la configuración actual del proyecto en Coolify las inyecta efectivamente al runtime del contenedor.

---

## 3. Dockerfile

```dockerfile
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY . /app/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
```

- `requirements.txt` = solo deps de producción (14 paquetes).
- `requirements-dev.txt` = `requirements.txt` + pytest, ruff, black, mypy, pre-commit (para CI/local).
- `.dockerignore` deja afuera `.env*` (salvo `.env.example`), `backups/`, `.git`, caches, config de tooling y `frontend/`: `COPY . /app/` no mete secrets en la imagen; las variables llegan en runtime (Coolify).
- `app/static/` sí va en la imagen y se sirve público en `/static`. La imagen slim no trae `/etc/mime.types`, por eso `app/main.py` registra `.webp` y `.woff2` a mano.
- `curl` instalado para healthcheck (`docker-compose*.yml` usa `curl -f http://localhost:8000/health`).
- `gcc libpq-dev` para compilar `asyncpg`.

---

## 4. Migraciones (Alembic)

**Crear migración:**
```bash
docker compose exec api alembic revision -m "descripción del cambio"
# Editar archivo generado en alembic/versions/
```

**Aplicar:**
```bash
docker compose exec api alembic upgrade head
```

**Ver estado:**
```bash
docker compose exec api alembic current
docker compose exec api alembic history
```

**Revertir última:**
```bash
docker compose exec api alembic downgrade -1
```

**Reglas de oro:**
- ❌ **Nunca editar migración ya aplicada** en producción. Crear una nueva que corrija.
- ✅ Migraciones versionadas en Git (`alembic/versions/`).
- ✅ `btree_gist` se crea en migración inicial (`1a2b3c4d5e6f`: `CREATE EXTENSION IF NOT EXISTS btree_gist`).
- ✅ Server defaults en migración `4d5e6f7a8b9c` (`NOW()`, `'pending'`, `false`, `0`, `'received'`).

---

## 5. Rollback

**Rollback de código (imagen Docker):**
```bash
# En Coolify: botón "Redeploy" sobre el deployment anterior (recomendado)

# O en CLI (VPS o local): checkout del commit anterior + rebuild
git checkout <commit_anterior>
docker compose -f docker-compose.prod.yml up -d --build
# (volver a main con: git checkout main)
```

**Rollback de migraciones:**
```bash
# Solo si la migración NO tiene cambios destructivos (DROP COLUMN, DROP TABLE)
docker compose exec api alembic downgrade -1

# Si es destructivo: restaurar backup DB (ver sección 7) + redeploy código anterior
```

**Rollback completo (código + DB):**
```bash
# 1. Restaurar backup DB anterior al deploy
gunzip -c backups/saas_db_YYYYMMDD_HHMMSS.sql.gz | docker compose exec -T db psql -U postgres -d saas_db

# 2. Redeploy tag de imagen anterior
# En Coolify: botón "Redeploy" en deployment anterior
```

---

## 6. Health Check

**Endpoint:** `GET /health` (`app/main.py:199-234`)

**Response OK (200):**
```json
{
  "status": "ok",
  "checks": {
    "api": "ok",
    "database": "ok",
    "redis": "ok"
  }
}
```

**Response Degraded (503):**
```json
{
  "status": "degraded",
  "checks": {
    "api": "ok",
    "database": "error: OperationalError",
    "redis": "ok"
  }
}
```

**Verifica:** API viva + `SELECT 1` en Postgres + `PING` en Redis.
- Traefik/Coolify usa este endpoint para healthcheck del contenedor (interval 10s, timeout 5s, retries 3).
- Si `degraded` → Traefik saca el contenedor del pool → 503 en `/health` público.

---

## 7. Backups

**Scripts:** `scripts/backup_db.sh` y `scripts/restore_db.sh` (ejecutables)

### Backup manual

```bash
# Solo local (backups/saas_db_YYYYMMDD_HHMMSS.sql.gz):
./scripts/backup_db.sh

# Con copia a S3:
S3_BACKUP_BUCKET=mi-bucket S3_BACKUP_PREFIX=juturno/backups ./scripts/backup_db.sh
```

Rotación automática local: borra archivos >30 días. La rotación en S3 se configura
con una lifecycle policy en el bucket (recomendado: `Expiration: 90 days`).

### Restore

```bash
# Desde archivo local:
./scripts/restore_db.sh backups/saas_db_YYYYMMDD_HHMMSS.sql.gz

# Desde S3:
./scripts/restore_db.sh s3://mi-bucket/juturno/backups/saas_db_YYYYMMDD_HHMMSS.sql.gz

# Sin confirmación interactiva (scripts, CI):
./scripts/restore_db.sh <archivo> --yes
```

> ⚠️ **Backup incluye tokens OAuth MP cifrados**. Para restaurar en otro entorno,
> **necesitás la misma `MP_TOKEN_ENCRYPTION_KEY`** (ver D-012, D-018).

### Automatizar en prod (cron en VPS)

Requiere `awscli` instalado en el VPS (`apt install awscli` o `pip install awscli`)
y credenciales AWS configuradas en el entorno de root (`~/.aws/credentials` o vars de entorno).

```bash
# /etc/cron.d/juturno-backup
# Runs daily at 03:00 UTC — adjust TZ if needed
0 3 * * * root \
  cd /opt/juturno && \
  S3_BACKUP_BUCKET=mi-bucket \
  S3_BACKUP_PREFIX=juturno/backups \
  AWS_DEFAULT_REGION=us-east-1 \
  ./scripts/backup_db.sh >> /var/log/juturno-backup.log 2>&1
```

Para que el cron pueda ver las credenciales AWS sin exponer secretos en cron.d,
la alternativa recomendada es usar un IAM Role en la instancia (si es EC2/Hetzner Cloud)
o guardar las credenciales en `/root/.aws/credentials`.

### Test de restore

Después de cada deploy a producción, verificar que el backup más reciente es restaurable:

```bash
# 1. Hacer backup manual
./scripts/backup_db.sh

# 2. Levantar un contenedor DB temporal y restaurar ahí (sin tocar prod)
docker run --rm -d \
  --name juturno-restore-test \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=saas_db \
  postgres:16-alpine

# Esperar que arranque (~2s), luego:
BACKUP_FILE=$(ls -t backups/saas_db_*.sql.gz | head -1)
gunzip -c "$BACKUP_FILE" | docker exec -i juturno-restore-test \
  psql -U postgres -d saas_db

# 3. Verificar que hay datos
docker exec juturno-restore-test \
  psql -U postgres -d saas_db -c "SELECT count(*) FROM tenant;"

# 4. Limpiar
docker stop juturno-restore-test
```

Un restore exitoso confirma que el backup no está corrupto.

---

## 8. CI (GitHub Actions)

**Workflow:** `.github/workflows/ci.yml`

**Triggers:** push/PR a `main`

**Jobs:**
1. `test` (ubuntu-latest):
   - Services: postgres:16-alpine, redis:7-alpine
   - Python 3.12, cache pip
   - Instala `requirements-dev.txt`
   - Crea extensión `btree_gist` en `saas_test`
   - `pytest -v` con env vars de test (MP_SECRET_KEY=test-secret, etc.)

**Checks:**
- CI corre **solo `pytest -v`** (no hay steps de lint/typecheck en el workflow).
- `ruff check app/ tests/`, `black` y `mypy app/` corren localmente vía pre-commit hooks (`.pre-commit-config.yaml`).

---

## 9. Secrets Management

- **Nunca** commitear `.env`, keys, tokens al repo.
- **Coolify**: env vars en UI del proyecto (encriptadas en reposo).
- **Rotación**:
  - `SECRET_KEY`: invalida todas las cookies → avisar usuarios (ver D-013, D-014).
  - `MP_TOKEN_ENCRYPTION_KEY`: **no rotar** sin migrar tokens cifrados (perderían acceso). Ver D-012.
  - `MP_SECRET_KEY` / `META_APP_SECRET`: rotar en MP/Meta Developer Console + actualizar Coolify simultáneamente.
  - `WHATSAPP_TOKEN`: rotar en Meta (System User) + actualizar Coolify.

---

## 10. Escalabilidad conocida (límites actuales)

| Componente | Límite | Mitigación futura |
|------------|--------|-------------------|
| Scheduler in-process | Duplicados entre réplicas mitigados con lock Redis `SET NX EX 30s` (outbox usa `FOR UPDATE SKIP LOCKED`); si el proceso API cae, los jobs paran | Worker separado Celery/ARQ (D-005 deuda) |
| API keys cache | TTL 60s → key revocada aceptada 60s | Reducir TTL o invalidar proactivamente |
| Outbox worker | Batch commit → fallo retrasa exitosos | Commit por evento / worker separado (D-016) |
| ExcludeConstraint | Funciona hasta millones de filas/tenant | Sharding por tenant si crece mucho (D-001 deuda) |
| VPS único | SPOF, sin HA | Kubernetes / Fly.io (D-010 deuda) |

---

## Ver también

- [`README.md`](README.md) — Quickstart, env vars tabla completa
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Componentes, flujos
- [`RUNBOOK.md`](RUNBOOK.md) — Incidentes, diagnóstico, procedimientos
- [`DECISIONS.md`](DECISIONS.md) — D-005, D-010, D-011, D-014, D-018
