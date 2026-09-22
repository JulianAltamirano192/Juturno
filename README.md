# Juturno

![CI](https://github.com/JulianAltamirano192/Juturno/actions/workflows/ci.yml/badge.svg)

> *Proyecto personal desarrollado en paralelo a los estudios universitarios. El objetivo es construir
> un producto con un propósito real, listo para operar en producción.*

SaaS multi-tenant de gestión de turnos con cobro de señas y notificaciones por WhatsApp.
Los clientes reservan, pagan y reciben la confirmación sin intervención del negocio.

---

## Stack

| Categoría | Tecnología | Por qué |
|---|---|---|
| **Backend** | FastAPI + SQLModel + Pydantic v2 | Async nativo, validación robusta, DX excelente |
| **Base de datos** | PostgreSQL 16 + `btree_gist` | Anti-solapamiento atómico a nivel motor (no en código) |
| **Cache & locks** | Redis 7 | TTL de API keys y mutex distribuido para el scheduler |
| **Scheduler** | APScheduler (in-process) | Sin infraestructura extra; un solo Dockerfile |
| **Pagos** | Mercado Pago Checkout Pro | Amplia adopción en LATAM |
| **Notificaciones** | WhatsApp Business API (Meta) | Mayor tasa de apertura que el email en Argentina |
| **Observabilidad** | Sentry | Detección temprana de errores en producción |
| **Infra local** | Docker Compose | `docker compose up` sin configuración adicional |
| **CI** | GitHub Actions | Tests automáticos en cada push a `main` |
| **Calidad** | pre-commit (ruff + black + mypy) | Consistencia de estilo y verificación de tipos |

---

## Arquitectura (resumen)

```
Cliente (Web) ──────────────────────────────────► FastAPI (uvicorn)
                                                       │
WhatsApp (Meta) ──► Webhook /webhooks/whatsapp ───────►│──► PostgreSQL 16
                                                       │         + btree_gist
Mercado Pago ────► Webhook /webhooks/mercadopago ─────►│
                                                       │──► Redis 7
APScheduler ─────► outbox (60s) + reminders (5m) ─────┘    locks + cache
```

Cada booking:
1. Se crea con un `NotificationOutbox` en la misma transacción (patrón Outbox).
2. El anti-solapamiento lo garantiza el `EXCLUDE USING gist` de Postgres (no el código).
3. Si hay pago, MP confirma vía webhook → el booking pasa a `confirmed`.
4. El outbox worker envía el WhatsApp de confirmación (y el recordatorio 24h antes).

Para el detalle de componentes y flujos ver [`ARCHITECTURE.md`](ARCHITECTURE.md).
Para las decisiones de diseño ver [`DECISIONS.md`](DECISIONS.md).

---

## Requisitos

- Docker + Docker Compose
- Python 3.11+ (solo para correr tests localmente, opcional)
- Cuenta de Meta con WhatsApp Business API configurada
- Cuenta de Mercado Pago con aplicación creada

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
# {"status":"ok"}
```

Si la respuesta es `{"status":"ok"}`, el servicio está operativo.

---

## Variables de entorno

| Variable | Descripción | Ejemplo |
|---|---|---|
| `DATABASE_URL` | Conexión a PostgreSQL | `postgresql+asyncpg://postgres:pass@db:5432/saas_db` |
| `REDIS_URL` | Conexión a Redis | `redis://redis:6379/0` |
| `CORS_ORIGINS` | Orígenes permitidos (JSON list) | `["https://tudominio.com"]` |
| `WHATSAPP_TOKEN` | Token permanente de Meta (System User) | `EAAxxxx...` |
| `WHATSAPP_PHONE_NUMBER_ID` | ID del número de WhatsApp Business | `123456789` |
| `META_VERIFY_TOKEN` | Token de verificación del webhook | Cadena aleatoria |
| `META_APP_SECRET` | Secret HMAC de la app de Meta | Cadena de Meta |
| `MP_ACCESS_TOKEN` | Access token de Mercado Pago | `APP_USR-...` o `TEST-...` |
| `MP_SECRET_KEY` | Clave secreta para firmar webhooks MP | Cadena de MP |
| `SENTRY_DSN` | DSN de Sentry (opcional) | `https://xxx@sentry.io/xxx` |
| `ENVIRONMENT` | Entorno (`development` / `production`) | `development` |

Usar `.env.example` como base; contiene todos los campos con descripción.

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

**Cobertura actual**: 8 archivos, +41 tests

| Archivo | Qué cubre |
|---|---|
| `test_auth.py` | Autenticación multi-tenant, cross-tenant isolation, `last_used_at` (6 tests) |
| `test_booking_constraints.py` | ExcludeConstraint cross-tenant, race conditions (2 tests) |
| `test_integration.py` | Flujo de reserva end-to-end + webhooks (4 tests) |
| `test_mp_webhooks.py` | Idempotencia, replay protection, timestamp (4 tests) |
| `test_public_endpoints.py` | Endpoints públicos + integración MP completa (7 tests) |
| `test_server_defaults.py` | Defaults a nivel DB + TIMESTAMPTZ (3 tests) |
| `test_slots.py` | Cálculo de slots disponibles (1 test) |
| `test_whatsapp_webhooks.py` | Verificación de firma HMAC y eventos WA (varios tests) |

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

`APScheduler` corre dos jobs dentro del proceso de la API:

1. **`process_outbox`** — cada 60s, toma los `NotificationOutbox` pendientes y los envía por WhatsApp.
2. **`process_reminders`** — cada 5 min, busca bookings con inicio en ~24h y encola recordatorios.

Ambos usan un **lock distribuido en Redis** (`SET NX EX`) para evitar doble ejecución si hay varias réplicas.

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
- **Idempotencia**: tabla `payment_events` con `event_id` como PK — no se procesa dos veces el mismo evento
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

### Estructura del proyecto

```
app/
├── auth.py              # Autenticación por API key (X-Tenant-API-Key)
├── cli.py               # CLI para crear/listar/revocar API keys
├── config.py            # Settings con pydantic-settings
├── database.py          # Engine async y session factory
├── main.py              # App FastAPI: endpoints + lifespan + scheduler
├── models.py            # Modelos SQLModel (Tenant, Booking, Payment, ...)
├── mp_webhooks.py       # Webhooks MP + helper create_mp_preference
├── outbox_worker.py     # Procesa notification_outbox cada 60s
├── scheduler.py         # Recordatorios 24h antes (cada 5 min)
├── services.py          # Cálculo de slots disponibles
├── webhooks.py          # Webhooks de WhatsApp
└── whatsapp_service.py  # Cliente HTTP de WhatsApp Business API

alembic/versions/        # Migraciones (versionadas en Git)
scripts/
└── backup_db.sh         # Backup con rotación automática (30 días)
tests/                   # 8 archivos de tests pytest-asyncio
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

---

## Operación

Ver [`RUNBOOK.md`](RUNBOOK.md) para operación diaria, backups e incidentes.

El despliegue a producción aún no está documentado: está pendiente para Q1 2027
(ver [`DECISIONS.md`](DECISIONS.md) → Roadmap).

---

## Licencia

Privado. Todos los derechos reservados.
