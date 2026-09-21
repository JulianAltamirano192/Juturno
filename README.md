# Juturno

![CI](https://github.com/JulianAltamirano192/Juturno/actions/workflows/ci.yml/badge.svg)

SaaS multi-tenant de gestión de turnos con cobro de señas y notificaciones
por WhatsApp.

## Stack

- **Backend**: FastAPI + SQLModel + Pydantic v2
- **Base de datos**: PostgreSQL 16 (con `btree_gist` para anti-solapamiento)
- **Cache & locks**: Redis 7
- **Scheduler**: APScheduler (dentro del proceso de la API)
- **Pagos**: Mercado Pago (Checkout Pro)
- **Notificaciones**: WhatsApp Business API (Meta)
- **Observabilidad**: Sentry
- **Infra local**: Docker Compose

## Arquitectura (resumen)

```
Cliente (WhatsApp) ──┐
                     ├──► Meta Webhooks ──► FastAPI ──► PostgreSQL
Cliente (Web) ───────┤                          │
                     │                          ├──► Redis (locks + cache)
Mercado Pago ────────┴──► MP Webhooks ────────┤
                                               ├──► WhatsApp API (envío)
                                               └──► Mercado Pago API
```

Para el detalle de componentes, flujos y decisiones de diseño, ver
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Requisitos

- Docker + Docker Compose
- Python 3.11+ (para correr tests localmente, opcional)
- Cuenta de Meta con WhatsApp Business API configurada
- Cuenta de Mercado Pago con aplicación creada

## Setup local

### 1. Clonar y configurar env

```bash
git clone git@github.com:JulianAltamirano192/Juturno.git
cd Juturno
cp .env.example .env
```

Editar `.env` y completar todas las variables (ver sección siguiente).

### 2. Levantar servicios

```bash
docker compose up -d --build
```

Esto levanta 3 contenedores:
- `saas_db` — PostgreSQL 16
- `saas_redis` — Redis 7
- `saas_api` — FastAPI + scheduler

### 3. Aplicar migraciones

```bash
docker compose exec api alembic upgrade head
```

### 4. Crear DB de tests

```bash
docker compose exec db createdb -U postgres saas_test
```

### 5. Verificar

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

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
| `ENVIRONMENT` | Entorno (`development`/`production`) | `development` |

## Tests

```bash
docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:<pass>@db:5432/saas_test" \
  api pytest -v
```

**Cobertura actual**: 20 tests
- `test_auth.py` — autenticación multi-tenant (6)
- `test_booking_constraints.py` — ExcludeConstraint cross-tenant (2)
- `test_integration.py` — flujo de reserva + webhooks (4)
- `test_mp_webhooks.py` — idempotencia, replay, timestamp (4)
- `test_server_defaults.py` — defaults a nivel DB (3)
- `test_slots.py` — cálculo de slots (1)

El CI corre estos tests automáticamente en cada push a `main`.

## Migraciones

**Crear una migración nueva**:

```bash
docker compose exec api alembic revision -m "descripción del cambio"
# Editar el archivo generado en alembic/versions/
docker compose exec api alembic upgrade head
```

**Aplicar migraciones en producción**:

```bash
docker compose exec api alembic upgrade head
```

**Revertir la última**:

```bash
docker compose exec api alembic downgrade -1
```

## Scheduler

`APScheduler` corre dos jobs dentro del proceso de la API:

1. **`process_outbox`** — cada 60s, procesa notificaciones pendientes.
2. **`process_reminders`** — cada 5 min, envía recordatorios 24h antes.

Ambos usan un **lock distribuido en Redis** para evitar doble ejecución si
corren varias instancias.

## Webhooks

### WhatsApp (Meta)

- **Ruta**: `POST /webhooks/whatsapp`
- **Verificación**: `hub.verify_token` en `GET /webhooks/whatsapp`
- **Firma**: valida `X-Hub-Signature-256` con `META_APP_SECRET`

### Mercado Pago

- **Ruta**: `POST /webhooks/mercadopago`
- **Firma**: valida `x-signature` (HMAC-SHA256) con `MP_SECRET_KEY`
- **Replay protection**: rechaza timestamps > 5 min
- **Idempotencia**: usa tabla `payment_events` para no procesar duplicados

## Desarrollo

### Pre-commit hooks

El repo tiene hooks configurados (ruff, black, mypy). Para activarlos:

```bash
pip install pre-commit
pre-commit install
```

Los hooks corren automáticamente en cada commit y arreglan formato.

### Estructura del proyecto

```
app/
├── auth.py              # Autenticación por API key
├── cli.py               # CLI de gestión (crear/listar/revocar keys)
├── config.py            # Settings (pydantic-settings)
├── database.py          # Engine y session factory
├── main.py              # App FastAPI + endpoints + lifespan
├── models.py            # Modelos SQLModel
├── mp_webhooks.py       # Webhooks de Mercado Pago
├── outbox_worker.py     # Procesa notification_outbox
├── scheduler.py         # Recordatorios 24h antes
├── services.py          # Lógica de cálculo de slots
├── webhooks.py          # Webhooks de WhatsApp
└── whatsapp_service.py  # Cliente HTTP de WhatsApp API

alembic/versions/        # Migraciones
tests/                   # Tests pytest
```

## Deploy

Ver [`RUNBOOK.md`](RUNBOOK.md) para instrucciones de deploy y operación.

## Licencia

Privado. Todos los derechos reservados.
