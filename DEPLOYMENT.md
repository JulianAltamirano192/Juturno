# Deployment

> Cómo desplegar, migrar, hacer rollback y operar en producción. Para DevOps y devs que deployan.

---

## 1. Entorno local (`docker-compose.yml`)

```yaml
# Servicios:
# - db: postgres:16-alpine (container saas_db, puerto 5432, healthcheck pg_isready)
# - redis: redis:7-alpine (container saas_redis, puerto 6379, healthcheck redis-cli ping)
# - api: build local con requirements-dev.txt (container saas_api, puerto 8000,
#        env_file .env, volúmenes de app/, alembic/, tests/ y scripts/, depends_on healthy)
```

**Levantar:**
```bash
docker compose up -d --build
docker compose exec api alembic upgrade head
```

**Variables** (`.env` en raíz, copia de `.env.example`):
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (el compose tiene defaults `postgres` / `postgres` / `saas_db`)
- `DATABASE_URL` y `REDIS_URL` los pisa el compose (`environment:`) apuntando a los servicios `db` y `redis`
- `MP_SANDBOX=true` (dev usa sandbox; es el default de `Settings`)
- `ENVIRONMENT=development`
- `SECRET_KEY` (cualquier valor, no se valida fuera de producción)

**Tests:**
```bash
# Crear DB tests (una vez)
docker compose exec db createdb -U postgres saas_test

# Correr tests (dentro del contenedor api; el script lee POSTGRES_PASSWORD de .env)
./scripts/test.sh
./scripts/test.sh tests/test_slots.py -x   # argumentos de pytest
```

> `TEST_DATABASE_URL` apunta al servicio `db` (no `localhost`). El scheduler **NO arranca** si esta variable está seteada (`lifespan` en `app/main.py`).

---

## 2. Entorno producción (`docker-compose.prod.yml` + Coolify)

```yaml
# Servicios:
# - db: postgres:16-alpine (sin puertos expuestos, volume persistente postgres_data)
# - redis: redis:7-alpine (sin puertos expuestos)
# - api: build . (command: alembic upgrade head + uvicorn --proxy-headers)
```

**Entrypoint del contenedor API** (`command` de `docker-compose.prod.yml`):
```bash
sh -c "python -m alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"
```
- **Antes** de migrar, el servicio `backup` del mismo compose hace un dump de la base (ver "Backup automático en cada deploy" en la sección 7); la API depende de que termine bien (`service_completed_successfully`), así que sin backup no hay migración.
- Migraciones corren **automáticamente** al iniciar el contenedor. El `CMD` del Dockerfile (sin migraciones) solo aplica si se usa la imagen sin ese compose.
- `--proxy-headers` hace que uvicorn lea `X-Forwarded-For` / `X-Forwarded-Proto` detrás de Traefik, pero solo de IPs confiables: por defecto `--forwarded-allow-ips` es `127.0.0.1`.
- **Rate limiting (D-033)**: la app calcula la IP real con `client_ip` (`app/limiter.py`) a partir de `X-Forwarded-For` / `CF-Connecting-IP`; no depende de que uvicorn confíe en el proxy. Requisitos de ops:
  - **No** setear `--forwarded-allow-ips` ni `FORWARDED_ALLOW_IPS`: el cliente pasaría a ser el edge de Cloudflare (buckets compartidos) y con `*` se confiaría en el extremo izquierdo de `X-Forwarded-For`, que controla el cliente.
  - El puerto 8000 **no** debe publicarse (en Coolify, "Ports Mappings" vacío): `client_ip` confía en los headers de cualquier peer privado.
  - Si Cloudflare agrega rangos de IP, actualizar `CLOUDFLARE_NETWORKS` en `app/limiter.py` (los clientes afectados caen en un bucket compartido por IP del edge: más estricto, no se saltea).
  - Chequeo post-deploy: más de 10 logins fallidos desde un celular con datos móviles deben dar 429 ahí, mientras que desde otra red el login sigue andando normal.
- En Coolify el auto-deploy está deshabilitado: mergear a `main` no despliega, hay que hacer "Redeploy" a mano (dato operativo, no verificable desde el repo).

**Variables de entorno en Coolify.** `app/config.py` valida en el arranque (con `ENVIRONMENT=production`) las marcadas "Validada"; sin ellas el contenedor entra en crash loop (el `alembic upgrade head` del entrypoint también importa `Settings` y muere antes de uvicorn).

| Variable | Origen | Validación al arranque |
|----------|--------|------------------------|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Generadas por Coolify (DB gestionada) o secrets | No |
| `DATABASE_URL` | `postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}` | No |
| `REDIS_URL` | `redis://redis:6379/0` | No |
| `SECRET_KEY` | `openssl rand -hex 32` | Validada: no puede ser el default (⚠️ crash loop si lo es) |
| `CORS_ORIGINS` | `["https://juturno.com"]` (lista JSON) | No |
| `ENVIRONMENT` | `production` (el compose prod lo fija) | n/a |
| `PUBLIC_BASE_URL` | `https://juturno.com` | No |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | Meta Developer Console (System User) | Validada |
| `META_VERIFY_TOKEN` | Meta Webhook config | Validada (vacía = el handshake de Meta falla siempre) |
| `META_APP_SECRET` | Meta Webhook config | Validada |
| `MP_ACCESS_TOKEN` | Access token plataforma (solo fallback sandbox) | No |
| `MP_SECRET_KEY` | MP Developers > Webhooks > Secret | Validada |
| `MP_MARKETPLACE_CLIENT_ID`, `MP_MARKETPLACE_CLIENT_SECRET` | MP Developers > App > Credenciales | No |
| `MP_MARKETPLACE_REDIRECT_URL` | Debe coincidir exacto con la Redirect URL registrada en MP Developers (default: `https://api.juturno.com/mp/connect/callback`) | No |
| `MP_NOTIFICATION_URL` | `https://api.juturno.com/webhooks/mercadopago` | Validada: exige `https://` y sufijo `/webhooks/mercadopago` |
| `MP_TOKEN_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | Validada |
| `MP_SANDBOX` | `false` (producción real) | Validada: `true` aborta el arranque |
| `SENTRY_DSN` | Opcional (Sentry project settings) | No |

> **Checklist pre-deploy**: verificar que **todas** las variables están seteadas en Coolify antes del primer deploy y después de agregar cualquier variable nueva a `Settings`. Ver D-014 y D-018 en `DECISIONS.md`. No setear `TEST_DATABASE_URL` en prod: apaga el scheduler.

> **Plantilla de Meta antes de desplegar**: la plantilla `owner_new_booking` (categoría Utility, idioma es, 4 parámetros: negocio, cliente, servicio, fecha) tiene que estar **aprobada** en Meta antes del deploy que trae el aviso al dueño (D-036). Si no lo está, cada aviso falla, se reintenta 7 veces en 2 h y queda `failed`. Es el mismo número de la plataforma (`WHATSAPP_PHONE_NUMBER_ID`); no hay variable nueva. Antes de conectar el número real de producción falta además la verificación OTP del número del dueño (D-036).

> ⚠️ **Pendiente de confirmar**: `docker-compose.prod.yml` solo pasa explícitamente al servicio `api` este subset: `DATABASE_URL`, `REDIS_URL`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `META_VERIFY_TOKEN`, `META_APP_SECRET`, `MP_ACCESS_TOKEN`, `MP_SECRET_KEY`, `SENTRY_DSN`, `ENVIRONMENT` y `CORS_ORIGINS`. Las demás (`SECRET_KEY`, `PUBLIC_BASE_URL`, `MP_MARKETPLACE_*`, `MP_NOTIFICATION_URL`, `MP_TOKEN_ENCRYPTION_KEY`, `MP_SANDBOX`) tienen que llegar al contenedor por el mecanismo de variables de Coolify. Ya hubo un crash loop en producción (2026-10-07) porque `MP_SANDBOX` no estaba definida en Coolify y el default es `true`. Desde el repo no se puede verificar qué inyecta Coolify en runtime: confirmarlo en los Runtime Logs del contenedor.

---

## 3. Dockerfile

```dockerfile
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
ARG REQUIREMENTS=requirements.txt
COPY requirements.txt requirements-dev.txt /app/
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r ${REQUIREMENTS}
COPY . /app/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
```

- `requirements.txt` = solo deps de producción (15 paquetes, incluye `slowapi`).
- `requirements-dev.txt` = `requirements.txt` + pytest, pytest-asyncio, pre-commit, ruff, black (fijado en `26.10.0`, igual que `.pre-commit-config.yaml`), mypy y `types-redis`. El `Dockerfile` instala `requirements.txt` por defecto (prod); `docker-compose.yml` (local) pasa `REQUIREMENTS=requirements-dev.txt`, así recrear el contenedor `api` no pierde pytest.
- La imagen es Python 3.11; CI corre en Python 3.12 (ver sección 8).
- `.dockerignore` deja afuera `.env*` (salvo `.env.example`), `backups/`, `.git`, caches, config de tooling y `frontend/`: `COPY . /app/` no mete secrets en la imagen; las variables llegan en runtime (Coolify).
- `app/static/` sí va en la imagen y se sirve público en `/static`. La imagen slim no trae `/etc/mime.types`, por eso `app/main.py` registra `.webp` y `.woff2` a mano.
- `curl` instalado para el healthcheck (`docker-compose*.yml` usa `curl -f http://localhost:8000/health`).
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
- ❌ **Nunca editar una migración ya commiteada** (un hook del repo lo bloquea). Crear una nueva que corrija.
- Migraciones versionadas en Git (`alembic/versions/`); revisarlas con el agente `migration-reviewer` antes de aplicar.
- `btree_gist` se crea en la migración inicial (`1a2b3c4d5e6f`: `CREATE EXTENSION IF NOT EXISTS btree_gist`).
- Server defaults en migración `4d5e6f7a8b9c` (`NOW()`, `'pending'`, `false`, `0`, `'received'`).
- Deploy de `4948a27a05b1` (`booking.internal_notes`, notas internas por turno): solo agrega una columna `VARCHAR(500)` nullable, sin default ni backfill (cambio de catálogo, lock instantáneo). Se aplica sola con el entrypoint; el downgrade la elimina (y con ella las notas).
- Deploy de `82b61531a397` (`tenant.public_notice`, aviso destacado de la página pública): solo agrega una columna `VARCHAR(280)` nullable, sin default ni backfill (cambio de catálogo, lock instantáneo). Se aplica sola con el entrypoint; el downgrade la elimina (y con ella los avisos).
- Deploy de `dcb3cce62e10` (`tenant.business_category` y `tenant.business_category_other`, rubro del negocio): solo agrega dos columnas nullable, `VARCHAR(20)` y `VARCHAR(60)`, sin default ni backfill (cambio de catálogo, lock instantáneo); los negocios existentes quedan con rubro `NULL` hasta que lo elijan en Ajustes. Se aplica sola con el entrypoint; el downgrade las elimina (y con ellas los rubros).
- Deploy de `a12e6a5a2a92` (`tenant.locality`, `tenant.instagram`, `tenant.facebook` y tabla `tenant_logo`, perfil del negocio): agrega tres columnas nullable (`VARCHAR(60)`, `VARCHAR(30)`, `VARCHAR(100)`, sin default ni backfill) y una tabla nueva y vacía (`tenant_id` PK/FK con `ON DELETE CASCADE`, `data` `bytea`, `updated_at`); no bloquea nada. Se aplica sola con el entrypoint; el downgrade elimina la tabla (y con ella los logos) y las columnas. La imagen necesita `pillow>=12.2.0` (en `requirements.txt`): alcanza un Redeploy normal, porque el `Dockerfile` copia `requirements.txt` antes del `pip install` y el cambio invalida esa capa de la caché (no hace falta build sin caché). Los logos viven en Postgres, así que ya entran en `backup_db.sh` y en la copia a S3 (D-037); la base crece unos KB por negocio con logo.
- Deploy de `2cf8d49114fa` (`tenant.brand_color`, color de marca de la página pública): agrega una columna `VARCHAR(7)` nullable, sin default ni backfill, y el CHECK `ck_tenant_brand_color`. Crear el CHECK escanea `tenant` con un lock breve, pero todas las filas tienen `NULL` y valida al instante (la tabla es chica). Se aplica sola con el entrypoint; el downgrade elimina el CHECK y la columna (y con ella los colores elegidos).
- Deploy de `58769fb62c2a` (`tenant.logo_updated_at`, logo en el header del panel): agrega una columna `timestamptz` nullable y la rellena desde `tenant_logo.updated_at` (un `UPDATE` sobre las pocas filas con logo, lock breve). Se aplica sola con el entrypoint; el downgrade la elimina (es un dato derivado: `tenant_logo` sigue siendo la fuente). Mientras el contenedor nuevo migra, el viejo sigue atendiendo: un logo subido o quitado justo en esos segundos deja la columna desincronizada. Si pasa (logo que no aparece en el panel, o imagen rota), corré una vez:
  `UPDATE tenant t SET logo_updated_at = l.updated_at FROM tenant_logo l WHERE l.tenant_id = t.id AND t.logo_updated_at IS DISTINCT FROM l.updated_at;`
  `UPDATE tenant SET logo_updated_at = NULL WHERE logo_updated_at IS NOT NULL AND id NOT IN (SELECT tenant_id FROM tenant_logo);`
- Deploy de `9a9d04b9ff5e` (horarios por profesional, borra el UNIQUE `uq_business_hours_tenant_staff_day` de `business_hours`): solo borra un constraint (cambio de catálogo, lock breve); no toca datos. Se aplica sola con el entrypoint. El downgrade recrea el UNIQUE y **falla** si ya hay dos franjas el mismo día para un profesional: borrá las sobrantes antes de bajar la migración.
- Deploy de `f4e2a9d38afb` (días bloqueados, tabla nueva `blocked_period`): crea una tabla vacía con FK a `tenant` y `staff` (`ON DELETE CASCADE`), el CHECK `ck_blocked_period_date_order` y dos índices; no toca datos ni bloquea nada. Se aplica sola con el entrypoint; el downgrade elimina la tabla (y con ella los bloqueos cargados).
- Deploy de `e7a8de2da19a` (tiempo libre por servicio, `service.buffer_minutes`): agrega una columna `INTEGER NOT NULL DEFAULT 0` y el CHECK `ck_service_buffer_minutes`; los servicios existentes quedan en 0, o sea sin cambios de comportamiento. La tabla `service` es chica, así que el lock es breve. Se aplica sola con el entrypoint; el downgrade elimina el CHECK y la columna (y con ella los tiempos libres configurados): hay backup en cada deploy. Los turnos ya tomados no se tocan, porque el tiempo libre vive en su `end_time`.
- Como el entrypoint de prod corre `alembic upgrade head`, una migración que falle deja el contenedor sin arrancar. Ejemplo: `c7d8e9f0a1b2` (`uq_tenant_mp_user_id`) falla si hay `mp_user_id` duplicados; ver el chequeo previo en `RUNBOOK.md` 3.3.

**Cloudflare (defensa extra para el export CSV):** `GET /panel/agenda/export` devuelve datos personales con `Cache-Control: no-store, private` y sin extensión `.csv` (Cloudflare cachea esa extensión por defecto y la URL del mes es igual para todos los negocios). Como segunda barrera, se recomienda una Cache Rule que haga bypass de caché para `/panel*`.

---

## 5. Rollback

**Rollback de código:**
```bash
# En Coolify: botón "Redeploy" sobre el deployment anterior (recomendado)

# O en CLI (VPS o local): checkout del commit anterior + rebuild
git checkout <commit_anterior>
# exclude_from_hc es de Coolify y `docker compose` lo rechaza: se filtra.
sed '/exclude_from_hc/d' docker-compose.prod.yml | docker compose -f - up -d --build
# (volver a main con: git checkout main)
```

> El repo no publica imágenes a un registry: prod buildea con `build: .` desde el commit desplegado. El rollback es siempre por commit, no por tag de imagen.

**Rollback de migraciones:**
```bash
# Solo si la migración NO tiene cambios destructivos (DROP COLUMN, DROP TABLE)
docker compose exec api alembic downgrade -1

# Si es destructivo: restaurar backup DB (ver sección 7) + redeploy del commit anterior
```

**Rollback completo (código + DB):**
```bash
# 1. ⚠️ Restaurar backup DB anterior al deploy (reemplaza los datos actuales)
./scripts/restore_db.sh backups/saas_db_YYYYMMDD_HHMMSS.sql.gz

# 2. Redeploy del commit anterior
# En Coolify: botón "Redeploy" en el deployment anterior
```

---

## 6. Health Check

**Endpoint:** `GET /health` (`app/routers/public.py`)

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

**Verifica:** API viva + `SELECT 1` en Postgres + `PING` en Redis. Cada check que falla se reporta como `error: <NombreDeLaExcepción>`.
- El `healthcheck` de los compose (`curl -f http://localhost:8000/health`, interval 10s, timeout 5s, retries 3) marca el contenedor como unhealthy si responde 503; Coolify/Traefik se apoyan en ese estado para no enrutar a un contenedor roto.
- No verifica el scheduler ni WhatsApp/MP: un scheduler caído o un token vencido no se ven en `/health`.

---

## 7. Backups

**Scripts:** `scripts/backup_db.sh` y `scripts/restore_db.sh` (ejecutables). Ambos usan `docker compose exec -T db ...` del compose del directorio actual y leen `POSTGRES_DB` / `POSTGRES_USER` del entorno de la shell (defaults `saas_db` / `postgres`), no del `.env`. En el VPS con Coolify los contenedores tienen nombres propios: verificar que `docker compose exec db` resuelva al servicio correcto desde el directorio donde se corre (no pudo confirmarse desde el repo).

### Backup automático en cada deploy (D-042)

`docker-compose.prod.yml` tiene un servicio `backup` de un solo uso (`postgres:16-alpine`, la misma imagen que `db`, así `pg_dump` coincide en versión mayor). En cada deploy espera a que `db` esté sano, hace `pg_dump --clean --if-exists` comprimido, verifica el gzip (`gzip -t`), lo deja en el volumen `db_backups` como `saas_db_YYYYMMDD_HHMMSS.sql.gz` (hora UTC) y conserva las 20 copias más recientes. Si algo falla, termina con error, no deja el `.tmp` y **la API no arranca ni migra**. Lleva `exclude_from_hc: true` para que Coolify no cuente su contenedor terminado en la salud de la app.

- **Dónde quedan:** en el VPS, en el volumen `db_backups` (Coolify le antepone el id del recurso: `docker volume ls | grep db_backups`). Están en el mismo servidor: protegen de una migración o un deploy malo, **no** de perder el VPS. Bajá copias a otro lado (`scp`) o activá S3 (ver abajo).
- **Listar:** `docker run --rm -v <volumen>:/backups alpine ls -lh /backups`
- **Sacar una copia del volumen:** `docker run --rm -v <volumen>:/backups -v /root/juturno-backups:/out alpine cp /backups/<archivo> /out/` y después `chmod 600 /root/juturno-backups/*` (tienen nombres y teléfonos de clientes; los dumps se crean con `umask 077`).
- **Restaurar** (probalo primero con una copia en local, ver "Test de restore"):
  1. Frenar la API para que nadie escriba: `docker stop <contenedor api>` (tiene `restart: unless-stopped` y Coolify puede volver a levantarla en un Redeploy: no despliegues mientras restaurás).
  2. Si el backup es de **antes** de una migración, `--clean` solo borra lo que estaba en el dump: tablas o columnas nuevas quedan y `alembic_version` vuelve a la revisión vieja, así que el próximo deploy choca con "already exists". Recreá el esquema antes: `docker exec <contenedor db> sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"'`. Ojo: la extensión `btree_gist` vive en `public` y el dump la vuelve a crear.
  3. Restaurar en una sola transacción, cortando al primer error: `zcat /root/juturno-backups/<archivo> | docker exec -i <contenedor db> sh -c 'psql -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`.
  4. Volver a levantar la API (`docker start <contenedor api>` o Redeploy del commit que corresponde a esa versión de la base).
- **Si el backup falla en un deploy** (la API queda sin arrancar): mirá los logs del contenedor `backup` en Coolify. Causas típicas: contraseña de `POSTGRES_PASSWORD` cambiada o **disco lleno**. Con el disco lleno la rotación no llega a correr (solo corre después de un backup exitoso): liberá espacio a mano borrando las copias más viejas (`docker run --rm -v <volumen>:/backups alpine sh -c 'ls -1r /backups/saas_db_*.sql.gz | tail -n +11 | xargs -r rm -f'` deja las 10 más recientes) y hacé Redeploy.

### Backup manual

```bash
# Solo local (backups/saas_db_YYYYMMDD_HHMMSS.sql.gz):
./scripts/backup_db.sh

# Con copia a S3:
S3_BACKUP_BUCKET=mi-bucket S3_BACKUP_PREFIX=juturno/backups ./scripts/backup_db.sh
```

- El dump es `pg_dump --clean --if-exists` comprimido con gzip; si queda vacío el script falla y lo borra.
- Rotación automática local: borra archivos `saas_db_*.sql.gz` de más de 30 días.
- Si `S3_BACKUP_BUCKET` está seteada, sube con `aws s3 cp` a `s3://$S3_BACKUP_BUCKET/${S3_BACKUP_PREFIX:-juturno/backups}/<archivo>`. Si la subida falla, el script conserva el backup local y sale con código 2.
- La rotación en S3 se configura con una lifecycle policy en el bucket (recomendado: `Expiration: 90 days`).

### Restore

```bash
# Desde archivo local:
./scripts/restore_db.sh backups/saas_db_YYYYMMDD_HHMMSS.sql.gz

# Desde S3 (descarga con aws s3 cp a un tmp):
./scripts/restore_db.sh s3://mi-bucket/juturno/backups/saas_db_YYYYMMDD_HHMMSS.sql.gz

# Sin confirmación interactiva (scripts, CI):
./scripts/restore_db.sh <archivo> --yes
```

> ⚠️ El restore **sobreescribe todos los datos** de la base (el dump trae `DROP ... IF EXISTS`). Sin `--yes` pide escribir `YES`. Nunca correrlo contra producción sin haber hecho un backup fresco antes.

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
la alternativa recomendada es guardarlas en `/root/.aws/credentials` (o un IAM Role si la
máquina vive en AWS; en un VPS Hetzner no aplica).

Este cron es una recomendación: no hay evidencia en el repo de que esté instalado en el VPS. Confirmarlo.

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

**Triggers:** push y pull request a `main`

**Jobs:**
1. `test` (ubuntu-latest):
   - Services: postgres:16-alpine (DB `saas_test`), redis:7-alpine
   - Python 3.12, cache pip
   - Instala `requirements-dev.txt`
   - Crea extensión `btree_gist` en `saas_test`
   - `ruff check app/ tests/`
   - `mypy app/`
   - `pytest -v` con env vars de test (`TEST_DATABASE_URL`, `MP_SECRET_KEY=test-secret`, `META_APP_SECRET=test-app-secret`, etc.)

**Checks:**
- CI corre ruff, mypy y pytest. `black --check` no está en el workflow: corre localmente vía pre-commit (`.pre-commit-config.yaml`, junto con ruff y mypy).
- El workflow no despliega: el deploy es manual desde Coolify.
- `main` tiene branch protection (desde 2026-10-09): PR obligatorio (0 aprobaciones), check `test` obligatorio, aplica también a admins, sin force-push ni borrado. Ningún cambio entra a `main` sin pasar el CI.

---

## 9. Secrets Management

- **Nunca** commitear `.env`, keys, tokens al repo.
- **Coolify**: env vars en UI del proyecto (encriptadas en reposo).
- **Rotación**:
  - `SECRET_KEY`: invalida todas las cookies → avisar usuarios (ver D-013, D-014).
  - ❌ `MP_TOKEN_ENCRYPTION_KEY`: **no rotar** sin migrar tokens cifrados (los tenants perderían el cobro). Ver D-012.
  - `MP_SECRET_KEY` / `META_APP_SECRET`: rotar en MP/Meta Developer Console + actualizar Coolify simultáneamente.
  - `WHATSAPP_TOKEN`: rotar en Meta (System User) + actualizar Coolify.

---

## 10. Escalabilidad conocida (límites actuales)

| Componente | Límite | Mitigación futura |
|------------|--------|-------------------|
| Scheduler in-process | 4 jobs dentro del proceso de la API. Con más de 1 réplica, los duplicados se mitigan con lock Redis (`SET NX EX`: 30s en reminders y token refresh, 300s en expiración de señas) y `FOR UPDATE SKIP LOCKED` (outbox, expiración); aun así no se escala a >1 réplica sin worker separado. Si el proceso API cae, los jobs paran | Worker separado Celery/ARQ (D-005 deuda) |
| API keys cache | TTL 60s → key revocada aceptada 60s | Reducir TTL o invalidar proactivamente |
| Outbox worker | Commit por evento con reintentos (backoff 1, 3, 7, 15, 31, 63 min; máx. 7 intentos, ventana de 2 h). Un evento agotado queda `failed` hasta intervención manual | `next_attempt_at` propio si hace falta otro calendario (D-022) |
| Rate limiting | slowapi con almacenamiento en memoria del proceso (`app/limiter.py` no configura Redis): los contadores se reinician con cada deploy y no se comparten entre réplicas; la IP del cliente sale de `client_ip` (D-033); usuarios detrás de un NAT de operadora comparten IP, y el login se limita solo por IP (sin límite por cuenta) | Ver requisitos en la sección 2; no setear `--forwarded-allow-ips` |
| ExcludeConstraint | Funciona hasta millones de filas/tenant | Sharding por tenant si crece mucho (D-001 deuda) |
| VPS único | SPOF, sin HA | Kubernetes / Fly.io (D-010 deuda) |

---

## Ver también

- [`README.md`](README.md) — Quickstart, env vars tabla completa
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Componentes, flujos
- [`RUNBOOK.md`](RUNBOOK.md) — Incidentes, diagnóstico, procedimientos
- [`DECISIONS.md`](DECISIONS.md) — D-005, D-010, D-011, D-014, D-018, D-022
