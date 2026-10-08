# AGENTS.md — Juturno SaaS

Juturno es un SaaS multi-tenant de gestión de turnos con cobro de señas (Mercado Pago) y notificaciones WhatsApp. En producción en https://api.juturno.com desde sept 2026.

---

## Stack

| Capa | Tech |
|------|------|
| Backend | FastAPI + SQLModel + Pydantic v2 |
| DB | PostgreSQL 16 + `btree_gist` (ExcludeConstraint) |
| Cache/locks | Redis 7 |
| Scheduler | APScheduler (in-process) |
| Pagos | Mercado Pago Checkout Pro + OAuth por tenant |
| Notificaciones | WhatsApp Business API (Meta) |
| Cifrado | cryptography (Fernet) |
| Observabilidad | Sentry + `/health` deep check |
| Tests | pytest + pytest-asyncio |
| Calidad | pre-commit (ruff, black, mypy) |
| Deploy | Coolify + Traefik en VPS Hetzner |

---

## Comandos clave

```bash
# Dev local
docker compose up -d --build
docker compose exec api alembic upgrade head

# Tests (dentro del contenedor api, usa saas_test DB)
docker compose exec -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@db:5432/saas_test" api pytest -v

# Lint / typecheck
ruff check app/ tests/
mypy app/
pre-commit run --all-files

# Migraciones
docker compose exec api alembic revision -m "descripción"
docker compose exec api alembic upgrade head

# Backup
./scripts/backup_db.sh
```

> **Importante**: `TEST_DATABASE_URL` debe apuntar al servicio `db` (no `localhost`). Los tests corren dentro del contenedor `api`.

---

## Arquitectura crítica

### Multi-tenancy
- **Shared DB** con `tenant_id` en todas las tablas. Aislamiento por lógica + constraints.
- **Nunca** hacer queries sin filtrar `tenant_id` en endpoints autenticados.
- API key auth: header `X-Tenant-API-Key` → SHA-256 lookup en `ApiKey` + cache Redis 60s.
- Panel web: cookie firmada HMAC-SHA256 (`juturno_session`) con `session_version` para invalidación instantánea.

### Anti-solapamiento de bookings
- **PostgreSQL ExcludeConstraint** con `btree_gist` + `tstzrange` — atómico a nivel motor, no en código.
- Constraint: `tenant_id =`, `COALESCE(staff_id, -1) =`, `tstzrange(start_time, end_time) &&` WHERE `status IN ('pending','confirmed')`.
- Race conditions imposibles por diseño.

### Patrón Outbox
- `NotificationOutbox` se crea al confirmar el pago (webhook MP, **misma transacción** que la confirmación) o vía job `process_reminders`. Al crear el booking **NO** hay outbox.
- Job `process_outbox` cada 60s (APScheduler) envía WhatsApp.
- Si Meta falla → reintento automático, no afecta la reserva.

### Scheduler (4 jobs, in-process)
| Job | Frecuencia | Lock |
|-----|------------|------|
| `process_outbox` | 60s | `FOR UPDATE SKIP LOCKED` (DB, sin Redis) |
| `process_reminders` | 5 min | Redis SET NX EX 30s |
| `process_deposit_expiration` | 1 min | Redis SET NX EX 30s |
| `process_mp_token_refresh` | 24h | Redis SET NX EX 30s |

> El scheduler **NO arranca** si `TEST_DATABASE_URL` está seteada (evita colisiones en tests).

### Mercado Pago por tenant (OAuth)
- Cada tenant conecta su cuenta vía OAuth → tokens cifrados (Fernet) en `tenant.mp_access_token_enc`.
- `resolve_mp_access_token(tenant)` descifra y renueva on-demand con `refresh_token`.
- **Producción (`MP_SANDBOX=false`)**: tenant sin MP conectado **no puede cobrar** → reserva pública rechaza 422.
- Webhook MP: valida HMAC (`x-signature`), replay protection (5 min), idempotencia (`payment_events.event_id` PK).

### WhatsApp
- Webhook: `POST /webhooks/whatsapp` + verificación `GET` con `META_VERIFY_TOKEN`.
- Firma: `X-Hub-Signature-256` HMAC-SHA256 con `META_APP_SECRET`.
- Plantillas Utility: `booking_confirmation`, `booking_reminder` (aprobadas por Meta).
- `normalize_phone_for_meta()` remueve el `9` de `549...` para Meta (formato E.164 tradicional).

---

## Estructura de `app/`

```
main.py              # App FastAPI, TODOS los endpoints, lifespan, scheduler
models.py            # SQLModel: Tenant, Service, Staff, Booking, Payment, BusinessHours, NotificationOutbox, ApiKey, ProcessedWebhookEvent
services.py          # Slots, ventanas horarias, effective_deposit
auth.py              # API key auth + cache Redis
session.py           # Cookie firmada HMAC + session_version
csrf.py              # CSRF double-submit para formularios panel
config.py            # Settings (BaseSettings), valida SECRET_KEY en prod
database.py          # Engine async + session maker
scheduler.py         # 4 jobs periódicos
outbox_worker.py     # Worker del patrón Outbox
booking_actions.py   # Máquina de estados Booking (confirm, cancel, no_show, complete)
cli.py               # CLI admin API keys: create/list/revoke (python -m app.cli)
mp_webhooks.py       # Webhook MP + create_mp_preference
mp_connect.py        # OAuth MP por tenant
mp_crypto.py         # Fernet encrypt/decrypt tokens MP
webhooks.py          # Webhook WhatsApp
whatsapp_service.py  # Cliente HTTP Meta
password.py          # PBKDF2 hash/verify
slug.py              # Slugs únicos para tenants
phone.py             # Normalización teléfonos argentinos
templates/           # Jinja2 (panel + página pública /t/{slug})
```

---

## Convenciones de código (no negociables)

- **Async everywhere**: endpoints, servicios, jobs = `async def`.
- **SQLModel** = Pydantic + SQLAlchemy unificado.
- **No commit manual en servicios**: callers deciden `await session.commit()`.
- **Excepciones específicas**: `InvalidTransitionError`, `BookingNotStartedError`, `MPTokenCryptoError`, `InvalidPhoneError`. No `Exception` genérico salvo boundaries (health, webhooks).
- **Logging**: `logger = logging.getLogger(__name__)`.
- **Timing-safe**: `hmac.compare_digest` para firmas.
- **Type hints**: `X | None` (no `Optional[X]`), `list[X]` (no `List[X]`), `dict[K, V]`.
- **Timezones**: `datetime.now(timezone.utc)` siempre; `zoneinfo` para tenant TZ.
- **Decimal** para dinero (nunca `float`).

---

## Anti-patrones a evitar

- ❌ `datetime.now()` sin tz → usar `datetime.now(timezone.utc)`
- ❌ Queries sin `tenant_id` en endpoints autenticados
- ❌ Editar migraciones ya aplicadas → crear nueva
- ❌ Commit secrets (`.env`, keys, tokens)
- ❌ Romper contrato API pública sin versionar
- ❌ Inventar features no implementadas

---

## Tests

- **189 tests** en 23 archivos (`tests/test_*.py`).
- Fixtures en `tests/conftest.py`: `setup_db` (crea/borra tablas + `btree_gist`), `db_session`, `client` (httpx.ASGITransport).
- Scheduler deshabilitado automáticamente con `TEST_DATABASE_URL`.
- CI: GitHub Actions (`.github/workflows/ci.yml`) corre todo en push a `main`.

---

## Variables de entorno críticas (ver `.env.example`)

| Variable | Descripción |
|----------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/db` |
| `REDIS_URL` | `redis://redis:6379/0` |
| **`SECRET_KEY`** | **Firma cookies panel — `openssl rand -hex 32`** |
| `CORS_ORIGINS` | JSON list: `["https://juturno.com"]` |
| `ENVIRONMENT` | `development` \| `production` |
| `PUBLIC_BASE_URL` | `https://juturno.com` |
| `WHATSAPP_TOKEN` | Token permanente Meta (System User) |
| `WHATSAPP_PHONE_NUMBER_ID` | ID número WhatsApp Business |
| `META_VERIFY_TOKEN` | Verificación webhook Meta |
| `META_APP_SECRET` | HMAC secret Meta |
| `MP_ACCESS_TOKEN` | Token plataforma (fallback legacy) |
| `MP_SECRET_KEY` | Firma webhook MP |
| `MP_MARKETPLACE_CLIENT_ID` | Client ID app MP (OAuth) |
| `MP_MARKETPLACE_CLIENT_SECRET` | Client Secret app MP |
| `MP_MARKETPLACE_REDIRECT_URL` | `https://api.juturno.com/mp/connect/callback` |
| `MP_NOTIFICATION_URL` | `https://api.juturno.com/webhooks/mercadopago` (obligatoria en prod) |
| `MP_TOKEN_ENCRYPTION_KEY` | Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `MP_SANDBOX` | `true` (dev) / `false` (prod) |
| `SENTRY_DSN` | Opcional |
| `TEST_DATABASE_URL` | Solo tests: `postgresql+asyncpg://...@db:5432/saas_test` |

> **`SECRET_KEY` default bloquea arranque en producción** (ver `config.py:40-45` y `DECISIONS.md` D-014).

---

## Referencias obligatorias

- `DECISIONS.md` — ADRs con contexto, alternativas, consecuencias (D-001 a D-018).
- `RUNBOOK.md` — Operación diaria, backups, incidentes, diagnóstico crashes.
- `ARCHITECTURE.md` — Detalle de componentes y flujos.
- `DEPLOYMENT.md` — Deploy, migraciones, rollback, backups, CI.
- `API_REFERENCE.md` — 44 endpoints con schemas, auth, códigos de error.
- `ONBOARDING.md` — Setup, arquitectura mental, convenciones, workflows.

---

## Flujo de trabajo

1. Cambios en commits chicos, mensajes **Conventional Commits** en inglés: `feat(booking): ...`, `fix(deploy): ...`
2. Cada feature nueva → tests.
3. Decisiones importantes → `DECISIONS.md` (formato ADR).
4. Incidentes → `RUNBOOK.md`.
5. Pre-commit hooks (ruff, black, mypy) corren en cada commit.

---

## Contacto

Proyecto personal: Julián Altamirano (julian@juturno.com).
