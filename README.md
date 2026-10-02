# Juturno

![CI](https://github.com/JulianAltamirano192/Juturno/actions/workflows/ci.yml/badge.svg)

> *Proyecto personal desarrollado en paralelo a los estudios universitarios. El objetivo es construir
> un producto con un propósito real, listo para operar en producción.*

SaaS multi-tenant de gestión de turnos con cobro de señas y notificaciones por WhatsApp.
Cada negocio gestiona sus turnos desde su panel; los clientes reservan, pagan y reciben
la confirmación sin intervención del negocio.

**En producción**: https://api.juturno.com

---

## Stack

| Categoría | Tecnología | Por qué |
|---|---|---|
| **Backend** | FastAPI + SQLModel + Pydantic v2 | Async nativo, validación robusta, DX excelente |
| **Base de datos** | PostgreSQL 16 + `btree_gist` | Anti-solapamiento atómico a nivel motor (no en código) |
| **Cache & locks** | Redis 7 | TTL de API keys, mutex distribuido para el scheduler |
| **Scheduler** | APScheduler (in-process) | Sin infraestructura extra; un solo Dockerfile |
| **Pagos** | Mercado Pago Checkout Pro + **OAuth por tenant** | Cada negocio cobra en su propia cuenta (dinero directo) |
| **Notificaciones** | WhatsApp Business API (Meta) | Mayor tasa de apertura que el email en Argentina |
| **Cifrado** | `cryptography` (Fernet) | Tokens OAuth de MP cifrados en DB |
| **Observabilidad** | Sentry + health check profundo | Detección temprana de errores y dependencias caídas |
| **Infra local** | Docker Compose | `docker compose up` sin configuración adicional |
| **Infra prod** | Coolify + Traefik + Cloudflare DNS | Deploy desde Git, TLS automático, DNS gestionado |
| **CI** | GitHub Actions | Tests automáticos en cada push a `main` |
| **Calidad** | pre-commit (ruff + black + mypy) | Consistencia de estilo y verificación de tipos |

---

## Arquitectura (resumen)

```
Cliente (Web) ─────────────────────────────────────► FastAPI (uvicorn)
                                                        │
WhatsApp (Meta) ──► Webhook /webhooks/whatsapp ────────►│──► PostgreSQL 16
                                                        │       + btree_gist
Mercado Pago ────► Webhook /webhooks/mercadopago ──────►│
                                                        │──► Redis 7
APScheduler ─────► 4 jobs (outbox, reminders, ─────────┘    locks + cache
                   deposit_expiration, mp_token_refresh)
```

**Cada booking**:
1. Se crea con un `NotificationOutbox` en la misma transacción (patrón Outbox).
2. El anti-solapamiento lo garantiza el `EXCLUDE USING gist` de Postgres (no el código).
3. Si hay pago, MP confirma vía webhook → el booking pasa a `confirmed`.
4. El outbox worker envía el WhatsApp de confirmación (y el recordatorio 24h antes).

**Multi-tenancy con dinero real**: cada tenant conecta su **propia** cuenta de Mercado Pago
vía OAuth. El dinero de las señas va directo a la cuenta del negocio, no a la plataforma.

Para el detalle de componentes y flujos ver [`ARCHITECTURE.md`](ARCHITECTURE.md).
Para las decisiones de diseño ver [`DECISIONS.md`](DECISIONS.md).
Para incidentes y operación ver [`RUNBOOK.md`](RUNBOOK.md).

---

## Requisitos

- Docker + Docker Compose
- Python 3.11+ (solo para correr tests localmente fuera del contenedor, opcional)
- Cuenta de Meta con WhatsApp Business API configurada
- Cuenta de Mercado Pago con aplicación creada (para OAuth por tenant)
- (Producción) VPS con Coolify instalado + dominio con DNS en Cloudflare

---

## Setup local

### 1. Clonar y configurar env

```bash
git clone git@github.com:JulianAltamirano192/Juturno.git
cd Juturno
cp .env.example .env
```

Editar `.env` y completar todas las variables (ver tabla más abajo).

### 2. Levantar servicios

```bash
docker compose up -d --build
```

Esto levanta 3 contenedores:
- `saas_db` — PostgreSQL 16
- `saas_redis` — Redis 7
- `saas_api` — FastAPI + APScheduler

### 3. Aplicar migraciones

```bash
docker compose exec api alembic upgrade head
```

### 4. Crear DB de tests (una sola vez)

```bash
docker compose exec db createdb -U postgres saas_test
```

### 5. Verificar que todo funciona

```bash
curl http://localhost:8000/health
```

**Esperado**:
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

Si algún check falla, retorna `503` con `"status": "degraded"`.

---

## Variables de entorno

| Variable | Descripción | Obligatoria en prod | Ejemplo |
|---|---|---|---|
| `DATABASE_URL` | Conexión a PostgreSQL | ✅ | `postgresql+asyncpg://postgres:pass@db:5432/saas_db` |
| `POSTGRES_USER` | Usuario de Postgres (para compose) | ✅ | `postgres` |
| `POSTGRES_PASSWORD` | Password de Postgres | ✅ | (generar con `openssl rand -hex 32`) |
| `POSTGRES_DB` | Nombre de la DB | ✅ | `saas_db` |
| `REDIS_URL` | Conexión a Redis | ✅ | `redis://redis:6379/0` |
| **`SECRET_KEY`** | **Firma de cookies del panel** | ✅ | `openssl rand -hex 32` |
| `CORS_ORIGINS` | Orígenes permitidos (JSON list) | ✅ | `["https://juturno.com"]` |
| `ENVIRONMENT` | `development` / `production` | ✅ | `production` |
| `PUBLIC_BASE_URL` | URL pública base del proyecto | ✅ | `https://juturno.com` |
| `WHATSAPP_TOKEN` | Token permanente de Meta (System User) | ✅ | `EAAxxxx...` |
| `WHATSAPP_PHONE_NUMBER_ID` | ID del número de WhatsApp Business | ✅ | `123456789` |
| `META_VERIFY_TOKEN` | Token de verificación del webhook | ✅ | Cadena aleatoria |
| `META_APP_SECRET` | Secret HMAC de la app de Meta | ✅ | Cadena de Meta |
| `MP_ACCESS_TOKEN` | Access token de la **plataforma** (fallback legacy) | ⚠️ | `APP_USR-...` o `TEST-...` |
| `MP_SECRET_KEY` | Clave secreta para firmar webhooks MP | ✅ | Cadena de MP |
| `MP_MARKETPLACE_CLIENT_ID` | Client ID de la app MP (para OAuth) | ✅ | Número de MP |
| `MP_MARKETPLACE_CLIENT_SECRET` | Client Secret de la app MP | ✅ | Cadena de MP |
| `MP_MARKETPLACE_REDIRECT_URL` | URL de callback OAuth | ✅ | `https://api.juturno.com/mp/connect/callback` |
| `MP_TOKEN_ENCRYPTION_KEY` | Clave Fernet para cifrar tokens OAuth | ✅ | `Fernet.generate_key().decode()` |
| `MP_SANDBOX` | Usar credenciales sandbox de MP | ⚠️ | `true` (dev) / `false` (prod) |
| `SENTRY_DSN` | DSN de Sentry (opcional) | ❌ | `https://xxx@sentry.io/xxx` |
| `TEST_DATABASE_URL` | DB de tests | ❌ | `postgresql+asyncpg://...@db:5432/saas_test` |

⚠️ **Importante**: `SECRET_KEY` **debe** cambiarse en producción. Si queda en el default
`change-this-secret-key-in-production-juturno`, el arranque falla con `ValidationError`
(ver D-014 en `DECISIONS.md`).

Usar `.env.example` como base; contiene todos los campos con descripción.

**Generar valores seguros**:
```bash
# SECRET_KEY, POSTGRES_PASSWORD, META_VERIFY_TOKEN
openssl rand -hex 32

# MP_TOKEN_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Tests

```bash
# Asegurarse de tener la DB de tests creada (ver Setup local, paso 4)
docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@db:5432/saas_test" \
  api pytest -v
```

`TEST_DATABASE_URL` debe apuntar al servicio `db` (no a `localhost`): los tests corren dentro del
contenedor de la API. El valor por defecto de `tests/conftest.py` asume ejecución local en el host.

**Nota**: el scheduler se deshabilita automáticamente cuando `TEST_DATABASE_URL` está seteada,
para que los jobs en background no interfieran con los tests.

**Cobertura actual**: 20 archivos, **169 tests**

| Archivo | Qué cubre |
|---|---|
| `test_auth.py` | API key auth, cross-tenant isolation, `last_used_at` |
| `test_booking_constraints.py` | ExcludeConstraint cross-tenant, race conditions |
| `test_integration.py` | Flujo de reserva end-to-end + webhooks |
| `test_slots.py` | Cálculo de slots disponibles |
| `test_server_defaults.py` | Defaults a nivel DB + TIMESTAMPTZ |
| `test_phone.py` | Normalización de números de teléfono |
| `test_deposit_expiration.py` | Job de expiración de señas |
| `test_mp_crypto.py` | Cifrado/descifrado Fernet de tokens MP |
| `test_mp_connect.py` | Flujo OAuth de conexión con MP |
| `test_mp_token_refresh.py` | Job de renovación de tokens OAuth |
| `test_mp_webhooks.py` | Firma HMAC, replay protection, idempotencia |
| `test_mp_webhook_tenant.py` | Resolución de tenant por `user_id` del webhook |
| `test_mp_tenant_payment.py` | Cobro con token del tenant (no de la plataforma) |
| `test_public_endpoints.py` | Endpoints públicos + integración MP completa |
| `test_whatsapp_webhooks.py` | Firma HMAC y manejo de eventos de Meta |
| `test_login.py` | Login del panel, sesiones firmadas |
| `test_register.py` | Registro de tenant + onboarding |
| `test_services_panel.py` | CRUD de servicios del panel |
| `test_staff_panel.py` | CRUD de staff + horarios |
| `test_agenda_panel.py` | Vista de agenda por día |
| `test_business_hours_panel.py` | Configuración de horarios de atención |

El CI corre todos estos en cada push a `main`. Si alguno falla, el merge se bloquea.

---

## Migraciones

```bash
# Crear una migración nueva
docker compose exec api alembic revision -m "descripción del cambio"
# Editar el archivo generado en alembic/versions/
docker compose exec api alembic upgrade head

# Aplicar en producción
docker compose exec api alembic upgrade head

# Ver migración actual
docker compose exec api alembic current

# Revertir la última
docker compose exec api alembic downgrade -1
```

> **Nota**: nunca editar una migración ya aplicada. Si un cambio produjo un error, crear una nueva migración que lo corrija.

---

## Scheduler

`APScheduler` corre **4 jobs** dentro del proceso de la API:

| Job | Frecuencia | Qué hace |
|---|---|---|
| `process_outbox` | 60s | Toma los `NotificationOutbox` pendientes y los envía por WhatsApp |
| `process_reminders` | 5 min | Busca bookings confirmados que empiezan en ~24h y encola recordatorios |
| `process_deposit_expiration` | 5 min | Expira bookings `pending` cuyo deadline de seña venció |
| `process_mp_token_refresh` | 24h | Renueva tokens OAuth de MP que vencen en <30 días |

Todos usan un **lock distribuido en Redis** (`SET NX EX`) para evitar doble ejecución si hay varias réplicas.

---

## Panel del negocio

El dueño del negocio accede al panel con email + password. La sesión es una **cookie firmada
HMAC-SHA256** (payload `{tenant_id}.{session_version}.{expires_at}`).

- **Registro**: `/register`
- **Login**: `/login`
- **Dashboard**: `/dashboard`
- **Servicios**: `/panel/services/*`
- **Staff**: `/panel/staff/*`
- **Horarios**: `/panel/horarios/*`
- **Agenda**: `/panel/agenda`
- **Conexión MP**: `/mp/connect/start` (OAuth a cuenta propia)

**Invalidación de sesiones**: al cambiar la contraseña, se incrementa `tenant.session_version`.
Todas las cookies activas quedan invalidadas instantáneamente (ver D-013).

**CSRF**: los endpoints del panel que modifican estado usan un token CSRF en formulario.

---

## Webhooks

### WhatsApp (Meta)

- **Ruta**: `POST /webhooks/whatsapp`
- **Verificación**: `GET /webhooks/whatsapp` — responde al challenge de Meta con `hub.verify_token`
- **Firma**: valida `X-Hub-Signature-256` con HMAC-SHA256 sobre el body crudo usando `META_APP_SECRET`

### Mercado Pago

- **Ruta**: `POST /webhooks/mercadopago`
- **Firma**: valida `x-signature` (HMAC-SHA256) con `MP_SECRET_KEY`
- **Replay protection**: rechaza timestamps > 5 min
- **Idempotencia**: tabla `payment_events` con `event_id` como PK
- **Resolución de tenant**: identifica el tenant por el `user_id` del payload (= `collector_id` de MP),
  y usa `tenant.mp_access_token_enc` para consultar el pago
- **Auto-creación**: si llega un pago aprobado sin `Payment` en la DB, lo crea on-the-fly

---

## Desarrollo

### Pre-commit hooks

```bash
pip install pre-commit
pre-commit install
```

Los hooks (ruff, black, mypy) corren automáticamente en cada commit.
Si los hooks modifican archivos, el commit se aborta: agregar los cambios con `git add -A`
y volver a commitear.

### Dependencias de desarrollo

```bash
# Instalar todo (prod + dev)
pip install -r requirements-dev.txt

# Solo prod
pip install -r requirements.txt
```

Los tests, linters y type checkers viven en `requirements-dev.txt`, no en `requirements.txt`.
El Dockerfile instala solo las de producción.

### Estructura del proyecto

```
app/
├── auth.py              # API key + sesiones firmadas (get_current_tenant, get_current_tenant_from_session)
├── cli.py               # CLI para crear/listar/revocar API keys
├── config.py            # Settings con pydantic-settings
├── csrf.py              # Validación CSRF para formularios del panel
├── database.py          # Engine async y session factory
├── main.py              # App FastAPI: endpoints + lifespan + scheduler
├── models.py            # Modelos SQLModel (Tenant, Booking, Payment, BusinessHours, ...)
├── mp_connect.py        # Flujo OAuth con Mercado Pago (connect, callback, refresh)
├── mp_crypto.py         # Cifrado/descifrado Fernet de tokens MP
├── mp_webhooks.py       # Webhooks MP + helper create_mp_preference
├── outbox_worker.py     # Procesa notification_outbox cada 60s
├── password.py          # Hash de contraseñas (PBKDF2)
├── phone.py             # Normalización de números argentinos
├── scheduler.py         # 4 jobs periódicos
├── services.py          # Cálculo de slots disponibles
├── session.py           # Creación/parseo de cookies de sesión firmadas
├── slug.py              # Generación de slugs para tenants
├── webhooks.py          # Webhooks de WhatsApp
└── whatsapp_service.py  # Cliente HTTP de WhatsApp Business API

app/templates/           # Templates Jinja2 del panel
alembic/versions/        # Migraciones (versionadas en Git)
scripts/
└── backup_db.sh         # Backup con rotación automática (30 días)
tests/                   # 20 archivos de tests pytest-asyncio
```

### Convenciones de commits

Se utiliza [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: agregar endpoint público de slots
fix: corregir validación de firma en webhooks MP
docs: actualizar ARCHITECTURE.md con sección de backups
chore: bump dependencias menores
```

---

## Backups

```bash
# Backup manual (genera backups/saas_db_YYYYMMDD_HHMMSS.sql.gz)
./scripts/backup_db.sh

# Restaurar (⚠️ reemplaza los datos actuales; verificar que exista un backup previo)
gunzip -c backups/saas_db_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose exec -T db psql -U postgres -d saas_db
```

El script rota automáticamente los backups con más de 30 días. Ver [`RUNBOOK.md`](RUNBOOK.md)
para el procedimiento completo y [`ARCHITECTURE.md`](ARCHITECTURE.md) para la política de retención.

**Nota**: el backup incluye los tokens OAuth de MP cifrados. Para restaurar en otro entorno
necesitás la misma `MP_TOKEN_ENCRYPTION_KEY`.

---

## Operación y deploy

- **Producción**: https://api.juturno.com (VPS en Hetzner, deployado con Coolify + Traefik)
- **Panel del negocio**: https://juturno.com
- **Health check**: https://api.juturno.com/health

Ver [`RUNBOOK.md`](RUNBOOK.md) para operación diaria, backups e incidentes (incluye
diagnóstico de crashes por env var faltante, tokens OAuth vencidos, y más).

---

## Licencia

Privado. Todos los derechos reservados.
