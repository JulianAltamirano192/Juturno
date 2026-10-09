<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/juturno-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/brand/juturno-logo-light.png">
    <img alt="Juturno" src="docs/brand/juturno-logo-light.png" width="360">
  </picture>
</p>

<p align="center">
  SaaS multi-tenant de gestión de turnos con cobro de seña (Mercado Pago) y notificaciones WhatsApp.<br>
  En producción en <a href="https://juturno.com">juturno.com</a> desde septiembre 2026.
</p>

<p align="center">
  <a href="https://github.com/JulianAltamirano192/Juturno/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/JulianAltamirano192/Juturno/actions/workflows/ci.yml/badge.svg"></a>
</p>

---

## 1. Stack

| Capa | Tecnología | Justificación |
|------|------------|---------------|
| Backend | FastAPI + SQLModel + Pydantic v2 | Async nativo, validación robusta, DX excelente |
| Base de datos | PostgreSQL 16 + `btree_gist` | Anti-solapamiento atómico a nivel motor (ExcludeConstraint) |
| Cache & locks | Redis 7 | TTL de API keys, mutex distribuido para scheduler, rate limiting |
| Scheduler | APScheduler (in-process) | Sin infra extra; un solo Dockerfile |
| Pagos | Mercado Pago Checkout Pro + **OAuth por tenant** | Cada negocio cobra en su propia cuenta (dinero directo) |
| Notificaciones | WhatsApp Business API (Meta) | Mayor tasa de apertura que email en Argentina |
| Cifrado | `cryptography` (Fernet) | Tokens OAuth de MP cifrados en reposo |
| Rate limiting | slowapi | Límites en login, registro y reservas públicas |
| Observabilidad | Sentry + health check profundo | Detección temprana de errores y dependencias caídas |
| Infra local | Docker Compose | `docker compose up` sin configuración adicional |
| Infra prod | Coolify + Traefik + Cloudflare DNS | Deploy desde Git, TLS automático, DNS gestionado |
| CI | GitHub Actions | ruff, mypy y pytest en cada push y PR a `main` |
| Calidad | pre-commit (ruff + black + mypy) | Consistencia de estilo y verificación de tipos |

---

## 2. Quickstart local (5 pasos)

```bash
# 1. Clonar y configurar env
git clone git@github.com:JulianAltamirano192/Juturno.git
cd Juturno
cp .env.example .env
# Editar .env con tus valores (ver tabla abajo)

# 2. Levantar servicios
docker compose up -d --build
# Levanta: saas_db (PostgreSQL 16), saas_redis (Redis 7), saas_api (FastAPI + APScheduler)
# La imagen local instala requirements-dev.txt (pytest, ruff, black, mypy)

# 3. Aplicar migraciones
docker compose exec api alembic upgrade head

# 4. Crear DB de tests (una sola vez)
docker compose exec db createdb -U postgres saas_test

# 5. Verificar
curl http://localhost:8000/health
# {"status":"ok","checks":{"api":"ok","database":"ok","redis":"ok"}}
```

> **Nota**: Los tests corren **dentro del contenedor `api`** y usan `TEST_DATABASE_URL` apuntando al servicio `db` (no `localhost`). `./scripts/test.sh` lo arma solo. Ver sección [Tests](#8-tests).

---

## 3. Variables de entorno (fuente de verdad)

La fuente de verdad es `app/config.py` (clase `Settings`); `.env.example` es la plantilla. Copiá `.env.example` a `.env` y completá todas.

La columna "Oblig. prod" distingue dos casos:
- **Validada**: si falta, el arranque falla con `ValueError` (`model_post_init` en `app/config.py`).
- **Sí**: hace falta para que el producto ande, pero el arranque no la valida.

| Variable | Descripción | Oblig. prod | Ejemplo / Generación |
|----------|-------------|-------------|----------------------|
| `DATABASE_URL` | Conexión PostgreSQL async | Sí | `postgresql+asyncpg://postgres:pass@db:5432/saas_db` |
| `POSTGRES_USER` | Usuario Postgres (solo compose) | Sí | `postgres` |
| `POSTGRES_PASSWORD` | Password Postgres (solo compose) | Sí | `openssl rand -hex 32` |
| `POSTGRES_DB` | Nombre de la DB (solo compose) | Sí | `saas_db` |
| `REDIS_URL` | Conexión Redis | Sí | `redis://redis:6379/0` |
| `SECRET_KEY` | Firma cookies del panel | Validada (no puede ser el default) | `openssl rand -hex 32` |
| `CORS_ORIGINS` | Orígenes permitidos (lista JSON) | Sí | `["https://juturno.com"]` |
| `ENVIRONMENT` | `development` / `production` | Sí | `production` |
| `PUBLIC_BASE_URL` | URL base pública (back_urls MP, dominio de la cookie `mp_oauth_state`) | Sí | `https://juturno.com` |
| `WHATSAPP_TOKEN` | Token permanente Meta (System User) | Validada | `EAAxxxx...` |
| `WHATSAPP_PHONE_NUMBER_ID` | ID del número WhatsApp Business | Validada | `123456789` |
| `META_VERIFY_TOKEN` | Token verificación webhook Meta (`GET /webhooks/whatsapp`) | Sí | Cadena aleatoria |
| `META_APP_SECRET` | Secret HMAC de la app Meta | Validada | Cadena de Meta |
| `MP_ACCESS_TOKEN` | Access token de la plataforma: solo fallback en sandbox | No (ver nota) | `APP_USR-...` o `TEST-...` |
| `MP_SECRET_KEY` | Clave secreta para validar la firma de los webhooks MP | Validada | Cadena de MP |
| `MP_MARKETPLACE_CLIENT_ID` | Client ID app MP (OAuth) | Sí | Número de MP |
| `MP_MARKETPLACE_CLIENT_SECRET` | Client Secret app MP (OAuth) | Sí | Cadena de MP |
| `MP_MARKETPLACE_REDIRECT_URL` | URL callback OAuth (debe coincidir exacto con la registrada en MP) | Sí | `https://api.juturno.com/mp/connect/callback` |
| `MP_NOTIFICATION_URL` | Webhook MP de este entorno (`notification_url`); vacía = no se manda | Validada (`https://` y sufijo `/webhooks/mercadopago`) | `https://api.juturno.com/webhooks/mercadopago` |
| `MP_TOKEN_ENCRYPTION_KEY` | Clave Fernet para cifrar tokens OAuth | Validada | Ver comando abajo |
| `MP_SANDBOX` | `true` = credenciales de prueba (checkout sandbox) | Validada (debe ser `false`) | `true` (dev) / `false` (prod) |
| `SENTRY_DSN` | DSN de Sentry (opcional) | No | `https://xxx@sentry.io/xxx` |
| `TEST_DATABASE_URL` | DB de tests (solo tests/CI). Si está seteada el scheduler no arranca | ❌ no setear en prod | `postgresql+asyncpg://postgres:pass@db:5432/saas_test` |

Para los backups, `scripts/backup_db.sh` lee además `POSTGRES_USER`, `POSTGRES_DB`, `S3_BACKUP_BUCKET` y `S3_BACKUP_PREFIX` del entorno de la shell (no son parte de `Settings`). Ver [`DEPLOYMENT.md`](DEPLOYMENT.md).

> ⚠️ **Crítico**: en producción (`ENVIRONMENT=production`) `app/config.py` aborta el arranque con `ValueError` si `SECRET_KEY` es el valor por defecto, si `MP_SANDBOX` es `true`, si falta alguna de las variables "Validada" de la tabla o si `MP_NOTIFICATION_URL` no es `https://.../webhooks/mercadopago`. Un contenedor en crash loop con "Restarting" suele ser una de estas. Ver [DECISIONS.md](DECISIONS.md#d-014-secret_key-no-puede-usar-el-valor-default-en-producción) y D-018.

> Nota sobre `MP_ACCESS_TOKEN`: en producción un tenant sin MP conectado no puede cobrar (422 `ERR_PAGO_NO_CONFIGURADO`); el fallback al token de la plataforma solo aplica con `MP_SANDBOX=true`.

**Generar valores seguros:**
```bash
# SECRET_KEY, POSTGRES_PASSWORD, META_VERIFY_TOKEN
openssl rand -hex 32

# MP_TOKEN_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 4. Comandos útiles

```bash
# Desarrollo local
docker compose up -d --build              # Levantar todo
docker compose exec api alembic upgrade head  # Migraciones
docker compose logs -f api                # Ver logs API
docker compose down -v                    # Bajar y BORRAR volúmenes (incluye la DB local)

# Tests (dentro del contenedor api; arma TEST_DATABASE_URL desde .env)
./scripts/test.sh                         # equivale a pytest -v
./scripts/test.sh tests/test_slots.py -x  # argumentos de pytest
./scripts/test.sh --collect-only -q       # contar tests

# Lint / typecheck (en el host o dentro del contenedor)
ruff check app/ tests/
black --check app/ tests/
mypy app/
pre-commit run --all-files

# Migraciones
docker compose exec api alembic revision -m "descripción del cambio"
docker compose exec api alembic upgrade head
docker compose exec api alembic current
docker compose exec api alembic downgrade -1

# Administrar API keys (CLI)
docker compose exec api python -m app.cli create-api-key --help
docker compose exec api python -m app.cli list-api-keys --help
docker compose exec api python -m app.cli revoke-api-key --help

# Backup manual (genera backups/saas_db_YYYYMMDD_HHMMSS.sql.gz; sube a S3 si S3_BACKUP_BUCKET está seteada)
./scripts/backup_db.sh

# ⚠️ Restaurar backup (reemplaza los datos actuales)
./scripts/restore_db.sh backups/saas_db_YYYYMMDD_HHMMSS.sql.gz --yes
# O desde S3: ./scripts/restore_db.sh s3://bucket/path/saas_db_YYYYMMDD_HHMMSS.sql.gz --yes
```

---

## 5. Arquitectura (resumen)

```
Cliente (Web) ─────────────────────────────────────► FastAPI (uvicorn)
                                                      │
WhatsApp (Meta) ──► Webhook /webhooks/whatsapp ──────►│──► PostgreSQL 16
                                                      │       + btree_gist
Mercado Pago ────► Webhook /webhooks/mercadopago ────►│
                                                      │──► Redis 7
APScheduler ─────► 4 jobs (outbox, reminders, ────────┘    locks + cache
                    deposit_expiration, mp_token_refresh)
```

**Cada booking:**
1. Se crea en `pending` (con seña) sin outbox: la notificación no se encola hasta que hay pago.
2. El anti-solapamiento lo garantiza el `EXCLUDE USING gist` de Postgres (no el código); los endpoints devuelven 409 ante `IntegrityError`.
3. Si hay pago, MP confirma vía webhook: el booking pasa a `confirmed` y el `NotificationOutbox` de confirmación se encola en la **misma transacción** (patrón Outbox).
4. El job `process_reminders` encola el recordatorio 24h antes; el job `process_outbox` envía los WhatsApp (commit por evento, reintentos con backoff, D-022).
5. Si la seña no se paga a tiempo, `process_deposit_expiration` pasa el booking a `expired` (antes reconcilia con MP, D-023).

**Multi-tenancy con dinero real**: cada tenant conecta su **propia** cuenta de Mercado Pago vía OAuth (desde el panel, `/panel/settings`). El dinero de las señas va directo a la cuenta del negocio, no a la plataforma.

Detalle completo en [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 6. Decisiones clave (resumen)

| ID | Decisión | Por qué |
|----|----------|---------|
| D-001 | Shared DB + `tenant_id` | Operación simple, un solo pool, migraciones únicas |
| D-002 | API Key (SHA-256) no JWT | Keys de alta entropía, lookup por índice único, cache Redis |
| D-003 | ExcludeConstraint (btree_gist) | Anti-solapamiento atómico bajo concurrencia extrema |
| D-004 | Patrón Outbox | Atomicidad pago+notificación, resiliencia si Meta cae |
| D-005 | APScheduler in-process | Un solo proceso, sin broker extra; locks Redis / `SKIP LOCKED` (no escalar a más de 1 réplica sin worker separado) |
| D-012 | MP OAuth por tenant | Dinero directo al dueño, sin riesgo legal/fiscal agregado |
| D-013 | Cookie firmada + session_version | Invalidación instantánea de todas las sesiones al cambiar clave |
| D-014 | SECRET_KEY bloquea default en prod | Falla ruidosa, fuerza configuración explícita |
| D-018 | Validadores de env vars críticas | El arranque falla si falta una variable de producción |
| D-019 | Webhook MP: guards y aislamiento por tenant | Fail-closed en producción, monto y tenant validados |
| D-021 | `Tenant.mp_user_id` único | Una cuenta MP no puede estar vinculada a dos negocios |
| D-022 | Outbox con commit por evento y backoff | Un evento "veneno" no frena ni duplica al resto |
| D-023 | Reconciliar con MP antes de expirar una seña | Un webhook perdido no deja un pago sin reserva |

Registro completo en [`DECISIONS.md`](DECISIONS.md).

---

## 7. Deploy (resumen)

- **Producción**: https://api.juturno.com (VPS Hetzner, Coolify + Traefik)
- **Panel del negocio**: https://juturno.com
- **Health check**: https://api.juturno.com/health
- **CI**: GitHub Actions corre ruff, mypy y pytest en cada push y PR a `main`; black corre solo vía pre-commit
- **Migraciones**: corren automáticamente en el `command` de `docker-compose.prod.yml` (`alembic upgrade head` antes de uvicorn)
- **Backups**: `scripts/backup_db.sh` con rotación local de 30 días y copia opcional a S3 (ver `DEPLOYMENT.md`)
- **Pendiente de ops**: configurar `--forwarded-allow-ips` en uvicorn/Coolify para que el rate limiting vea la IP real del cliente (ver `DEPLOYMENT.md`)

Procedimiento completo en [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## 8. Tests

```bash
# Requiere DB de tests creada (paso 4 del quickstart)
./scripts/test.sh
```

- **356 tests** en 34 archivos (`tests/test_*.py`), contados con `./scripts/test.sh --collect-only -q`.
- Fixtures en `tests/conftest.py`: `setup_db` (crea/borra tablas + `btree_gist`), `db_session`, `client` (httpx.ASGITransport).
- El scheduler **se deshabilita automáticamente** cuando `TEST_DATABASE_URL` está seteada.
- CI corre ruff, mypy y pytest en cada push y PR a `main`. Que el merge quede bloqueado si falla depende de la branch protection de GitHub, que **no está confirmada**.

---

## 9. Licencia / Contacto

**Privado**. Todos los derechos reservados.

Proyecto personal de Julián Altamirano — julian@juturno.com

---

## Documentación relacionada

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Componentes, flujos, multi-tenancy, auth, slots, outbox, scheduler, webhooks
- [`DECISIONS.md`](DECISIONS.md) — ADRs con contexto, alternativas, consecuencias
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Deploy, migraciones, rollback, backups, CI
- [`API_REFERENCE.md`](API_REFERENCE.md) — Endpoints con schemas, auth, códigos de error
- [`RUNBOOK.md`](RUNBOOK.md) — Incidentes: síntomas, diagnóstico, mitigación, fix
- [`ONBOARDING.md`](ONBOARDING.md) — Setup, arquitectura mental, convenciones, workflows
