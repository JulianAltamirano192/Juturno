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
| Rate limiting | slowapi (en memoria del proceso) |
| Tests | pytest + pytest-asyncio |
| Calidad | pre-commit (ruff, black, mypy) |
| Deploy | Coolify + Traefik en VPS Hetzner |

---

## Comandos clave

```bash
# Dev local
docker compose up -d --build
docker compose exec api alembic upgrade head

# Tests (dentro del contenedor api, usa la DB saas_test)
./scripts/test.sh [args de pytest]

# Lint / typecheck
ruff check app/ tests/
black --check app/ tests/
mypy app/
pre-commit run --all-files

# Migraciones
docker compose exec api alembic revision -m "descripción"
docker compose exec api alembic upgrade head

# Backup (sube a S3 si S3_BACKUP_BUCKET está seteada) / restore (⚠️ reemplaza los datos)
./scripts/backup_db.sh
./scripts/restore_db.sh <archivo.sql.gz | s3://bucket/key> [--yes]
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
- Race conditions imposibles por diseño. Los endpoints capturan `IntegrityError` y responden 409.

### Patrón Outbox
- `NotificationOutbox` se crea al confirmar el pago (webhook MP, **misma transacción** que la confirmación) o vía job `process_reminders`. Al crear el booking **NO** hay outbox.
- Job `process_outbox` cada 60s (APScheduler) envía WhatsApp, con commit por evento (D-022).
- Si Meta falla → el evento queda `failed` y se reintenta con backoff (1, 3, 7, 15, 31, 63 min; máx. 7 intentos, solo dentro de las 2 h desde su creación). No afecta la reserva.
- Al cancelar un booking se cancelan también sus eventos `failed` del outbox.

### Scheduler (4 jobs, in-process)
| Job | Frecuencia | Lock |
|-----|------------|------|
| `process_outbox` | 1 min | `FOR UPDATE SKIP LOCKED` (DB, sin Redis) |
| `process_reminders` | 5 min | Redis SET NX EX 30s |
| `process_deposit_expiration` | 1 min | Redis SET NX EX 300s + `SKIP LOCKED` por booking |
| `process_mp_token_refresh` | 24h | Redis SET NX EX 30s |

> El scheduler **NO arranca** si `TEST_DATABASE_URL` está seteada (evita colisiones en tests). No escalar a más de 1 réplica sin worker separado (D-005).

### Mercado Pago por tenant (OAuth)
- Cada tenant conecta su cuenta vía OAuth desde el panel (`/panel/settings`, `POST /panel/mp/connect/start`, callback `GET /mp/connect/callback`) → tokens cifrados (Fernet) en `tenant.mp_access_token_enc`.
- El `state` OAuth va atado al navegador por la cookie HttpOnly `mp_oauth_state`; `Tenant.mp_user_id` es único (D-021): una cuenta MP ya vinculada redirige a `?mp=account_in_use`.
- `resolve_mp_access_token(tenant)` descifra el token del tenant; el job `process_mp_token_refresh` renueva tokens próximos a vencer.
- **Producción (`MP_SANDBOX=false`, obligatorio)**: tenant sin MP conectado **no puede cobrar** → reserva pública rechaza 422 `ERR_PAGO_NO_CONFIGURADO`. El fallback al token de plataforma es solo sandbox.
- Webhook MP: valida HMAC (`x-signature`), replay protection (±5 min), idempotencia (`payment_events.event_id` PK), monto/moneda y tenant (D-019). `process_deposit_expiration` reconcilia con MP antes de expirar una seña (D-023).

### WhatsApp
- Webhook: `POST /webhooks/whatsapp` + verificación `GET` con `META_VERIFY_TOKEN`.
- Firma: `X-Hub-Signature-256` HMAC-SHA256 con `META_APP_SECRET`.
- Plantillas Utility: `booking_confirmation`, `booking_reminder` (aprobadas por Meta).
- `normalize_phone_for_meta()` remueve el `9` de `549...` para Meta (formato E.164 tradicional).

---

## Estructura de `app/`

```
main.py              # App FastAPI: middlewares, routers, lifespan y registro de los 4 jobs
routers/             # api.py (API key), auth.py (login/registro), panel.py (cookie), public.py (landing, /health, reserva pública)
schemas.py           # Schemas Pydantic de request/response
models.py            # SQLModel: Tenant, Service, Staff, BusinessHours, Booking, Payment, NotificationOutbox, ProcessedWebhookEvent (tabla payment_events), ApiKey
services.py          # Slots (compute_available_slots), ventanas horarias, effective_deposit
limiter.py           # slowapi (deshabilitado con TEST_DATABASE_URL)
templates.py         # Instancia Jinja2Templates
auth.py              # API key auth + cache Redis
session.py           # Cookie firmada HMAC + session_version
csrf.py              # CSRF double-submit para formularios panel
config.py            # Settings (BaseSettings), validadores de producción (model_post_init)
database.py          # Engine async + session maker
scheduler.py         # 4 jobs periódicos
outbox_worker.py     # Worker del patrón Outbox
booking_actions.py   # Máquina de estados Booking (confirm, cancel, no_show, complete)
cli.py               # CLI admin API keys: create/list/revoke (python -m app.cli)
mp_webhooks.py       # Webhook MP + create_mp_preference + reconciliación
mp_connect.py        # OAuth MP por tenant, resolve_mp_access_token, refresh
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

- **313 tests** en 31 archivos (`tests/test_*.py`), contados con `./scripts/test.sh --collect-only -q`.
- Fixtures en `tests/conftest.py`: `setup_db` (crea/borra tablas + `btree_gist`), `db_session`, `client` (httpx.ASGITransport).
- Scheduler deshabilitado automáticamente con `TEST_DATABASE_URL`.
- CI: GitHub Actions (`.github/workflows/ci.yml`) corre ruff, mypy y pytest en push y PR a `main`. Branch protection sin confirmar.

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
| `WHATSAPP_TOKEN` | Token permanente Meta (System User). Validada en prod |
| `WHATSAPP_PHONE_NUMBER_ID` | ID número WhatsApp Business |
| `META_VERIFY_TOKEN` | Verificación webhook Meta |
| `META_APP_SECRET` | HMAC secret Meta. Validada en prod |
| `MP_ACCESS_TOKEN` | Token plataforma (solo fallback sandbox) |
| `MP_SECRET_KEY` | Firma webhook MP. Validada en prod |
| `MP_MARKETPLACE_CLIENT_ID` | Client ID app MP (OAuth) |
| `MP_MARKETPLACE_CLIENT_SECRET` | Client Secret app MP |
| `MP_MARKETPLACE_REDIRECT_URL` | `https://api.juturno.com/mp/connect/callback` |
| `MP_NOTIFICATION_URL` | `https://api.juturno.com/webhooks/mercadopago` (validada en prod: `https://` y sufijo `/webhooks/mercadopago`) |
| `MP_TOKEN_ENCRYPTION_KEY` | Validada en prod; no rotar. Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `MP_SANDBOX` | `true` (dev) / `false` (prod, validada: `true` aborta el arranque) |
| `SENTRY_DSN` | Opcional |
| `TEST_DATABASE_URL` | Solo tests: `postgresql+asyncpg://...@db:5432/saas_test` (no setear en prod: apaga el scheduler) |

> En producción `app/config.py` aborta el arranque si `SECRET_KEY` es el default, si `MP_SANDBOX` es `true`, si falta alguna variable crítica (`META_APP_SECRET`, `MP_TOKEN_ENCRYPTION_KEY`, `MP_SECRET_KEY`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `MP_NOTIFICATION_URL`) o si `MP_NOTIFICATION_URL` no es `https://.../webhooks/mercadopago` (ver `DECISIONS.md` D-014 y D-018). Detalle y tabla completa en `README.md`.

---

## Referencias obligatorias

- `DECISIONS.md` — ADRs con contexto, alternativas, consecuencias (D-001 en adelante).
- `RUNBOOK.md` — Operación diaria, backups, incidentes, diagnóstico crashes.
- `ARCHITECTURE.md` — Detalle de componentes y flujos.
- `DEPLOYMENT.md` — Deploy, migraciones, rollback, backups, CI.
- `API_REFERENCE.md` — Endpoints con schemas, auth, códigos de error.
- `ONBOARDING.md` — Setup, arquitectura mental, convenciones, workflows.

---

## Flujo de trabajo

1. Cambios en commits chicos, mensajes **Conventional Commits** en inglés: `feat(booking): ...`, `fix(deploy): ...`
2. Cada feature nueva → tests.
3. Decisiones importantes → `DECISIONS.md` (formato ADR).
4. Incidentes → `RUNBOOK.md`.
5. Pre-commit hooks (ruff, black, mypy) corren en cada commit.
6. "Hecho" = tests verdes + ruff + mypy limpios. Sin push, deploy ni commit si Julián no lo pidió.
7. Nunca editar una migración ya commiteada (un hook lo bloquea): crear una nueva.

### Agentes y skills del proyecto (`.claude/`)

| Agente | Para qué |
|--------|----------|
| `code-reviewer` | Revisa el diff pendiente antes de commitear (solo lectura) |
| `security-auditor` | Auditoría de auth, CSRF, webhooks, pagos y endpoints públicos; obligatoria antes de cerrar cambios en esas áreas |
| `migration-reviewer` | Revisa migraciones nuevas antes de `alembic upgrade head` |
| `docs-keeper` | Sincroniza la documentación con el código al cerrar una tarea |

| Skill | Para qué |
|-------|----------|
| `new-endpoint` | Agregar un endpoint siguiendo las convenciones |
| `close-phase` | Cierre de fase: verificación, docs y decisiones |
| `prod-readiness` | Checklist previo al primer cliente real |

### Pendientes conocidos

- Ops: `--forwarded-allow-ips` en Coolify (rate limiting ve la IP de Traefik); branch protection de `main` sin confirmar.
- Alta autoservicio incompleta: sin cambio/recupero de contraseña ni verificación de email; `PATCH /tenants/me` y `/tenants/me/mp` siguen exigiendo API key.

---

## Contacto

Proyecto personal: Julián Altamirano (julian@juturno.com).
