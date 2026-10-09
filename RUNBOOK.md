# Runbook — Respuesta a incidentes

> Procedimientos operativos para diagnosticar y mitigar incidentes en producción. Para on-call y devs.

---

## 1. Principios

1. **Logs primero**: `docker compose logs -f api --tail 100` antes de tocar nada.
2. **Health check**: `curl https://api.juturno.com/health` → ¿`ok` o `degraded`?
3. **No adivinar**: usar queries diagnósticas, no suposiciones.
4. **Mitigar → Diagnosticar → Fix permanente**: en ese orden.
5. **Documentar**: todo incidente → entrada en este runbook + ADR si genera deuda.

> **Nombres de contenedores**: los comandos de este runbook usan **nombres de servicio** (`api`, `db`, `redis`) vía `docker compose`, válidos en local y prod. Los `container_name` (`saas_api`, `saas_db`, `saas_redis`) solo existen en `docker-compose.yml` (local); en prod, Coolify asigna nombres propios a los contenedores. En el VPS, correr los comandos desde el directorio del proyecto.

---

## 2. Tabla de incidentes

| Síntoma | Logs a revisar | Query diagnóstica | Mitigación inmediata | Fix permanente |
|---------|----------------|-------------------|----------------------|----------------|
| **API crash loop** (`SECRET_KEY cannot be the default value in production`, `MP_SANDBOX must be False in production`, `Missing required env vars in production: ...` o `MP_NOTIFICATION_URL must be https://.../webhooks/mercadopago`) | `docker compose logs api` (en Coolify: Runtime Logs del contenedor `api`). El log nombra la variable | En Coolify revisar la variable que nombra el error (no imprimir secrets) | Setear/corregir la variable en Coolify (`SECRET_KEY` ≠ default, `MP_SANDBOX=false`, etc.) + redeploy | Checklist deploy en DEPLOYMENT.md (D-014, D-018) |
| **Health check "degraded: database"** | `docker compose logs api` | `docker compose exec db pg_isready -U postgres -d saas_db` | Verificar DB viva, conexiones (max_connections) | Revisar pool asyncpg, connection leaks |
| **Health check "degraded: redis"** | `docker compose logs api` | `docker compose exec redis redis-cli ping` | Verificar Redis vivo, memoria (INFO memory) | Revisar red/credenciales, maxmemory policy |
| **Migración pendiente tras deploy** (500s masivos, `UndefinedColumn`/`relation does not exist` en logs) | `docker compose logs api \| grep -i "alembic\|undefined\|does not exist"` | `docker compose exec api alembic current` vs `alembic heads` (deben coincidir) | `docker compose exec api alembic upgrade head` o Redeploy en Coolify (el `command` de `docker-compose.prod.yml` corre migraciones antes de uvicorn) | PR con migración debe mergearse junto al código; verificar entrypoint intacto |
| **Webhook MP 401 "Firma de Mercado Pago inválida"** | `docker compose logs api \| grep -i mercadopago` | `SELECT * FROM payment_events WHERE status='failed' ORDER BY received_at DESC LIMIT 10` | Verificar `MP_SECRET_KEY` en Coolify = MP Developers | Rotar secret si comprometido; actualizar ambas partes |
| **Webhook MP 403 "Timestamp del webhook fuera de ventana de tolerancia"** (tolerancia ±5 min) | `docker compose logs api \| grep -i timestamp` | `docker compose exec api date` vs `date` en host | Sincronizar reloj (NTP) en VPS / contenedor | `chrony`/`systemd-timesyncd` activo en VPS |
| **Outbox atascado (pending > 10 min)** (el job corre cada 60 s) | `docker compose logs api \| grep -i outbox` | `SELECT id, booking_id, notification_type, status, retry_count, error_message, created_at FROM notification_outbox WHERE status='pending' ORDER BY created_at;` | Reintentar manual: `docker compose exec api python -c "from app.outbox_worker import process_outbox; from app.database import async_session_maker; import asyncio; asyncio.run(process_outbox(async_session_maker))"` | Verificar `WHATSAPP_TOKEN` vigente, rate limit Meta, phone_number_id correcto |
| **ExcludeConstraint violation 409** | `docker compose logs api \| grep -i integrity` | `SELECT * FROM booking WHERE status IN ('pending','confirmed') AND tenant_id=? AND COALESCE(staff_id,-1)=? AND tstzrange(start_time,end_time) && tstzrange('...','...');` | Verificar race condition legítima (reintento cliente) | Confirmar constraint correcto; idempotency_key en cliente |
| **Scheduler no corre (jobs no ejecutan)** (jobs: `outbox_job` 1 min, `reminder_job` 5 min, `deposit_expiration_job` 1 min, `mp_token_refresh_job` 24 h) | `docker compose logs api \| head -50` | Buscar `"Scheduler distribuido iniciado correctamente."` al arranque; `SELECT * FROM pg_stat_activity WHERE state='active';` | Verificar `TEST_DATABASE_URL` **no** seteada en prod (si lo está, lifespan imprime `"Entorno de test detectado: scheduler NO iniciado."` y skipea los jobs) | Fix env var en Coolify; D-005 |
| **Token MP vencido (cobro falla 502/422)** | `docker compose logs api \| grep -i mercadopago` | `SELECT id, name, mp_user_id, mp_token_expires_at FROM tenant WHERE mp_token_expires_at < now() AND mp_refresh_token_enc IS NOT NULL;` | Reconexión manual OAuth por el dueño: Panel → Configuración → Conectar Mercado Pago | Job refresh proactivo (D-015); alertar si refresh falla |
| **Pago aprobado pero booking no confirma** | `docker compose logs api \| grep -i "webhook_mp\|confirmed"` | `SELECT * FROM payment_events WHERE event_id='...' AND status='processed';` + `SELECT * FROM booking WHERE id=?;` | Reprocesar webhook manual (re-enviar desde MP o curl con mismo payload) | Verificar idempotencia `payment_events`; `_resolve_token_for_payment` correcto. Si la seña venció antes de que llegue el webhook, el job `process_deposit_expiration` primero consulta a MP (D-023) y el webhook reconfirma un `expired` si el horario sigue libre |
| **WhatsApp no llega (confirmación/recordatorio)** | `docker compose logs api \| grep -i whatsapp` | `SELECT * FROM notification_outbox WHERE status='failed' ORDER BY created_at DESC LIMIT 20;` | Verificar `WHATSAPP_TOKEN` no vencido, `WHATSAPP_PHONE_NUMBER_ID` correcto. Los `failed` se reintentan solos (D-022) con backoff 1, 3, 7, 15, 31 y 63 min, hasta 7 intentos y solo dentro de las 2 h desde su creación; para reenviar uno agotado o viejo: `UPDATE notification_outbox SET status='pending' WHERE id=...` (⚠️ avisará de un turno con atraso: confirmar que el turno sigue vigente) | Monitorear rate limits Meta; plantillas aprobadas |
| **Booking creado pero sin payment_url (502)** | `docker compose logs api \| grep -i "preference\|checkout"` | `SELECT * FROM booking WHERE id=?;` + `SELECT * FROM payment WHERE booking_id=?;` | Verificar `resolve_mp_access_token` → token válido; `MP_SANDBOX` coherente | D-012 regla prod vs sandbox; validar OAuth conectado |
| **502 "Error al procesar el cobro del negocio"** (`MPTokenCryptoError`) | `docker compose logs api \| grep -i "descifrar\|MPTokenCryptoError"` | `docker compose exec api printenv \| grep MP_TOKEN_ENCRYPTION_KEY` | Verificar que `MP_TOKEN_ENCRYPTION_KEY` en Coolify es la misma con la que se cifraron los tokens (rotación sin migración = tokens ilegibles) | Recuperar la clave original; si se perdió, cada tenant debe reconectar MP vía OAuth (regenera tokens con la clave nueva) |
| **Conexión MP termina en `?mp=account_in_use`** | `docker compose logs api \| grep -i "mp/connect/callback"` | `SELECT id, name, mp_user_id FROM tenant WHERE mp_user_id = '<mp_user_id>';` | La cuenta MP ya está vinculada a otro tenant: el dueño debe conectar una cuenta distinta, o desconectar la cuenta del otro negocio si fue un error | Índice único `uq_tenant_mp_user_id` (D-021); no hay que tocar nada en DB si el rechazo es correcto |
| **Conexión MP termina en `?mp=other_browser`** | `docker compose logs api \| grep -i "mp/connect/callback"` | `docker compose exec api printenv \| grep -E "PUBLIC_BASE_URL\|MP_MARKETPLACE_REDIRECT_URL"` | Si el dueño abrió el link en otro navegador/app, repetir desde el panel en el mismo navegador. Si pasa siempre: la cookie `mp_oauth_state` usa `Domain` = host de `PUBLIC_BASE_URL` solo si el callback es subdominio de ese host; con `PUBLIC_BASE_URL=https://www.juturno.com` y callback `api.juturno.com` la cookie no llega | Corregir `PUBLIC_BASE_URL` (p. ej. `https://juturno.com`) en Coolify y redeploy |
| **OAuth MP: `{"error":"Invalid request parameters"}` al conectar** | `docker compose logs api \| grep -i "mp/connect"` | `docker compose exec api printenv MP_MARKETPLACE_REDIRECT_URL` (no hay secret en esa variable) | La Redirect URL no coincide exacto con la registrada en MP Developers > app. Registrar la misma URL o corregir `MP_MARKETPLACE_REDIRECT_URL` en Coolify y redeploy | Mantener ambas en sync; no es un bug de código |
| **OAuth MP: "la aplicación no puede conectarse a tu cuenta" tras el login** | n/a (falla del lado de MP) | n/a | Activar las credenciales de producción de la app en MP Developers (rubro + URL del sitio) | Checklist de alta de MP antes de abrir a clientes |
| **429 Too Many Requests / clientes distintos bloqueados entre sí** | `docker compose logs api \| grep -i "rate limit"` | Comparar la IP que ve la app con la real del cliente (los límites son 10/min login, 5/min registro, 20/min reservas públicas) | Esperar 1 min; si bloquea a todos, es la IP de Traefik: falta `--forwarded-allow-ips` | Configurar `--forwarded-allow-ips=<IP de Traefik>` en el comando de uvicorn en Coolify (pendiente de ops). Los contadores son en memoria del proceso y se reinician con cada deploy |

---

## 3. Procedimientos comunes

### 3.1 Ver logs de la API
```bash
# Últimas 100 líneas + follow
docker compose logs -f api --tail 100

# Filtrar por componente
docker compose logs api --tail 200 | grep -i "outbox\|mercadopago\|whatsapp\|scheduler"

# Último arranque (lifespan + scheduler)
docker compose logs api --tail 50 | head -30
```

### 3.2 Conectar a DB / Redis
```bash
# PostgreSQL (psql)
docker compose exec db psql -U postgres -d saas_db

# Consultas útiles
# - Booking status: SELECT id, tenant_id, status, start_time FROM booking WHERE id=?;
# - Outbox pendientes: SELECT * FROM notification_outbox WHERE status='pending';
# - Webhook events: SELECT * FROM payment_events WHERE status='failed' ORDER BY received_at DESC LIMIT 10;
# - Tokens MP: SELECT id, name, mp_user_id, mp_token_expires_at FROM tenant WHERE mp_access_token_enc IS NOT NULL;

# Redis (redis-cli)
docker compose exec redis redis-cli

# Keys útiles
# - API key cache: KEYS auth:apikey:*   (TTL 60 s)
# - OAuth state: KEYS mp_connect_state:*
# - Scheduler locks: KEYS *-job-lock   (reminder-job-lock, deposit-expiration-job-lock, mp-token-refresh-job-lock)
# - TTL: TTL auth:apikey:<hash>
```

### 3.3 Ejecutar migración en prod

> ⚠️ **Antes de desplegar `c7d8e9f0a1b2` (`uq_tenant_mp_user_id`)**: si hay `mp_user_id` duplicados, `CREATE UNIQUE INDEX` falla, la migración no se aplica y el contenedor **no arranca** (el entrypoint corre `alembic upgrade head`). Verificá en la DB de prod:
> ```sql
> SELECT mp_user_id, count(*) FROM tenant WHERE mp_user_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1;
> SELECT id, name, mp_user_id FROM tenant WHERE mp_user_id IN ('', 'None');
> ```
> Si la primera devuelve filas, decidí a mano qué tenant conserva la cuenta y desconectá/limpiá la del otro (`mp_user_id = NULL` y tokens). Las filas con `''` o `'None'` (valores basura de versiones viejas) se pisan con `NULL`. Recién ahí desplegá. (Si la migración ya está aplicada en prod, este chequeo es solo histórico.)
```bash
# En Coolify: botón "Redeploy" (el command de prod corre alembic upgrade head). El auto-deploy está deshabilitado: mergear a main no despliega
# O manual:
docker compose exec api alembic upgrade head

# Ver migración actual
docker compose exec api alembic current

# Ver historial
docker compose exec api alembic history --verbose
```

### 3.4 Rollback deploy
```bash
# En Coolify: botón "Redeploy" en deployment anterior (recomendado)

# Manual (CLI): checkout del commit anterior + rebuild con compose de prod
git checkout <commit_anterior>
docker compose -f docker-compose.prod.yml up -d --build
# Volver a main: git checkout main
```

> El repo no publica imágenes a un registry — prod buildea con `build: .` desde el commit desplegado. El rollback es siempre por commit, no por tag de imagen.

### 3.5 Restaurar backup
```bash
# 1. Identificar backup correcto (fecha anterior al incidente)
ls -la backups/

# 2. ⚠️ Restaurar (REEMPLAZA los datos actuales; hacer un backup fresco antes)
# Desde archivo local:
./scripts/restore_db.sh backups/saas_db_YYYYMMDD_HHMMSS.sql.gz --yes
# Desde S3 (requiere awscli y credenciales AWS; el bucket es el de S3_BACKUP_BUCKET):
./scripts/restore_db.sh s3://bucket/path/saas_db_YYYYMMDD_HHMMSS.sql.gz --yes

# 3. Verificar
docker compose exec api alembic current
curl https://api.juturno.com/health
```

### 3.6 Rotar secrets críticos

| Secret | Procedimiento | Impacto |
|--------|---------------|---------|
| `SECRET_KEY` | 1. Generar nueva: `openssl rand -hex 32` 2. Actualizar Coolify 3. Redeploy | **Invalida TODAS las cookies activas** → usuarios deben reloguear. Avisar antes. |
| `MP_TOKEN_ENCRYPTION_KEY` | **NO ROTAR sin migración**. Requiere descifrar todos los tokens con clave vieja y recifrar con nueva. | Si rotás sin migrar → tokens MP ilegibles → cobros fallan (502). |
| `MP_SECRET_KEY` | 1. Rotar en MP Developers > Webhooks 2. Actualizar Coolify 3. Redeploy | Webhooks MP fallan 401 hasta sincronizar ambas partes. |
| `META_APP_SECRET` | 1. Rotar en Meta Developer Console 2. Actualizar Coolify 3. Redeploy | Webhooks WhatsApp fallan 401 hasta sincronizar. |
| `WHATSAPP_TOKEN` | 1. Rotar en Meta (System User) 2. Actualizar Coolify 3. Redeploy | Envío WhatsApp falla hasta actualizar. |

---

## 4. Contactos y escalación

| Incidente | Contacto | SLA |
|-----------|----------|-----|
| API caída (health check degraded >5min) | Julián (owner) | Inmediato |
| Webhook MP/WhatsApp fallando | Julián | <30 min |
| DB/Redis caídos | Julián + Hetzner support | <15 min |
| Backup corrupto / restore falla | Julián | <1 hora |
| Security breach (secret expuesto) | Julián | Inmediato (rotar secret) |

---

## Ver también

- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Deploy, migraciones, backups, health check
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Componentes para diagnóstico
- [`DECISIONS.md`](DECISIONS.md) — D-014 (SECRET_KEY), D-015 (MP refresh), D-022 (outbox con commit por evento y reintentos), D-023 (reconciliación de señas)
- [`API_REFERENCE.md`](API_REFERENCE.md) — Códigos de error por endpoint
- [`ONBOARDING.md`](ONBOARDING.md) — Setup local para reproducir
