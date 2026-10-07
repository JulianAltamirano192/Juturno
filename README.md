<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/juturno-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/brand/juturno-logo-light.png">
    <img alt="Juturno" src="docs/brand/juturno-logo-light.png" width="360">
  </picture>
</p>

<p align="center">
  SaaS multi-tenant de gestión de turnos con cobro de seña (Mercado Pago) y notificaciones WhatsApp.<br>
  En producción en <a href="https://api.juturno.com">api.juturno.com</a> desde septiembre 2026.
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
| Cache & locks | Redis 7 | TTL de API keys, mutex distribuido para scheduler |
| Scheduler | APScheduler (in-process) | Sin infra extra; un solo Dockerfile |
| Pagos | Mercado Pago Checkout Pro + **OAuth por tenant** | Cada negocio cobra en su propia cuenta (dinero directo) |
| Notificaciones | WhatsApp Business API (Meta) | Mayor tasa de apertura que email en Argentina |
| Cifrado | `cryptography` (Fernet) | Tokens OAuth de MP cifrados en reposo |
| Observabilidad | Sentry + health check profundo | Detección temprana de errores y dependencias caídas |
| Infra local | Docker Compose | `docker compose up` sin configuración adicional |
| Infra prod | Coolify + Traefik + Cloudflare DNS | Deploy desde Git, TLS automático, DNS gestionado |
| CI | GitHub Actions | Tests automáticos en cada push a `main` |
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

# 3. Aplicar migraciones
docker compose exec api alembic upgrade head

# 4. Crear DB de tests (una sola vez)
docker compose exec db createdb -U postgres saas_test

# 5. Verificar
curl http://localhost:8000/health
# {"status":"ok","checks":{"api":"ok","database":"ok","redis":"ok"}}
```

> **Nota**: Los tests corren **dentro del contenedor `api`** y usan `TEST_DATABASE_URL` apuntando al servicio `db` (no `localhost`). Ver sección [Tests](#8-tests).

---

## 3. Variables de entorno (fuente de verdad)

Copia `.env.example` a `.env` y completá **todas**. Las marcadas con ✅ son obligatorias en producción.

| Variable | Descripción | Oblig. prod | Ejemplo / Generación |
|----------|-------------|-------------|----------------------|
| `DATABASE_URL` | Conexión PostgreSQL async | ✅ | `postgresql+asyncpg://postgres:pass@db:5432/saas_db` |
| `POSTGRES_USER` | Usuario Postgres (compose) | ✅ | `postgres` |
| `POSTGRES_PASSWORD` | Password Postgres | ✅ | `openssl rand -hex 32` |
| `POSTGRES_DB` | Nombre de la DB | ✅ | `saas_db` |
| `REDIS_URL` | Conexión Redis | ✅ | `redis://redis:6379/0` |
| **`SECRET_KEY`** | **Firma cookies panel** | ✅ | `openssl rand -hex 32` |
| `CORS_ORIGINS` | Orígenes permitidos (JSON list) | ✅ | `["https://juturno.com"]` |
| `ENVIRONMENT` | `development` / `production` | ✅ | `production` |
| `PUBLIC_BASE_URL` | URL base pública (back_urls MP) | ✅ | `https://juturno.com` |
| `WHATSAPP_TOKEN` | Token permanente Meta (System User) | ✅ | `EAAxxxx...` |
| `WHATSAPP_PHONE_NUMBER_ID` | ID del número WhatsApp Business | ✅ | `123456789` |
| `META_VERIFY_TOKEN` | Token verificación webhook Meta | ✅ | Cadena aleatoria |
| `META_APP_SECRET` | Secret HMAC de la app Meta | ✅ | Cadena de Meta |
| `MP_ACCESS_TOKEN` | Access token plataforma (fallback legacy) | ⚠️ | `APP_USR-...` o `TEST-...` |
| `MP_SECRET_KEY` | Clave secreta para firmar webhooks MP | ✅ | Cadena de MP |
| `MP_MARKETPLACE_CLIENT_ID` | Client ID app MP (para OAuth) | ✅ | Número de MP |
| `MP_MARKETPLACE_CLIENT_SECRET` | Client Secret app MP | ✅ | Cadena de MP |
| `MP_MARKETPLACE_REDIRECT_URL` | URL callback OAuth | ✅ | `https://api.juturno.com/mp/connect/callback` |
| `MP_TOKEN_ENCRYPTION_KEY` | Clave Fernet para cifrar tokens OAuth | ✅ | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `MP_SANDBOX` | Usar credenciales sandbox de MP | ⚠️ | `true` (dev) / `false` (prod) |
| `SENTRY_DSN` | DSN de Sentry (opcional) | ❌ | `https://xxx@sentry.io/xxx` |
| `TEST_DATABASE_URL` | DB de tests (solo en CI/tests) | ❌ | `postgresql+asyncpg://postgres:pass@db:5432/saas_test` |

> **⚠️ Crítico**: `SECRET_KEY` **no puede quedar en el default** en producción. El arranque falla con `ValidationError` si `ENVIRONMENT=production` y `SECRET_KEY` es el valor por defecto (ver `app/config.py:40-45` y [DECISIONS.md](DECISIONS.md#d-014-secret_key-no-puede-usar-el-valor-default-en-producción)).

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
docker compose down -v                    # Bajar y borrar volúmenes

# Tests (dentro del contenedor api)
docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@db:5432/saas_test" \
  api pytest -v

# Lint / typecheck
ruff check app/ tests/
mypy app/
pre-commit run --all-files

# Migraciones
docker compose exec api alembic revision -m "descripción del cambio"
docker compose exec api alembic upgrade head
docker compose exec api alembic current
docker compose exec api alembic downgrade -1

# Backup manual (genera backups/saas_db_YYYYMMDD_HHMMSS.sql.gz; sube a S3 si S3_BACKUP_BUCKET está seteada)
./scripts/backup_db.sh

# Restaurar backup (⚠️ reemplaza datos actuales)
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
1. Se crea con un `NotificationOutbox` en la misma transacción (patrón Outbox).
2. El anti-solapamiento lo garantiza el `EXCLUDE USING gist` de Postgres (no el código).
3. Si hay pago, MP confirma vía webhook → el booking pasa a `confirmed`.
4. El outbox worker envía el WhatsApp de confirmación (y el recordatorio 24h antes).

**Multi-tenancy con dinero real**: cada tenant conecta su **propia** cuenta de Mercado Pago vía OAuth. El dinero de las señas va directo a la cuenta del negocio, no a la plataforma.

👉 Detalle completo en [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 6. Decisiones clave (resumen)

| ID | Decisión | Por qué |
|----|----------|---------|
| D-001 | Shared DB + `tenant_id` | Operación simple, un solo pool, migraciones únicas |
| D-002 | API Key (SHA-256) no JWT | Keys de alta entropía, lookup por índice único, cache Redis |
| D-003 | ExcludeConstraint (btree_gist) | Anti-solapamiento atómico bajo concurrencia extrema |
| D-004 | Patrón Outbox | Atomicidad reserva+notificación, resiliencia si Meta cae |
| D-005 | APScheduler in-process | Un solo proceso, sin broker extra, lock Redis para multi-réplica |
| D-012 | MP OAuth por tenant | Dinero directo al dueño, sin riesgo legal/fiscal agregado |
| D-013 | Cookie firmada + session_version | Invalidación instantánea de todas las sesiones al cambiar clave |
| D-014 | SECRET_KEY bloquea default en prod | Falla ruidosa, fuerza configuración explícita |

👉 Registro completo (20 ADRs) en [`DECISIONS.md`](DECISIONS.md).

---

## 7. Deploy (resumen)

- **Producción**: https://api.juturno.com (VPS Hetzner, Coolify + Traefik)
- **Panel del negocio**: https://juturno.com
- **Health check**: https://api.juturno.com/health
- **CI**: GitHub Actions corre tests (pytest) en cada push a `main`; lint y typecheck corren localmente vía pre-commit
- **Migraciones**: corren automáticamente en el entrypoint del contenedor (`alembic upgrade head`)
- **Backups**: script `scripts/backup_db.sh` con rotación 30 días (ver `DEPLOYMENT.md`)

👉 Procedimiento completo en [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## 8. Tests

```bash
# Requiere DB de tests creada (paso 4 del quickstart)
docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@db:5432/saas_test" \
  api pytest -v
```

- **211 tests** en 24 archivos (`tests/test_*.py`).
- Fixtures en `tests/conftest.py`: `setup_db` (crea/borra tablas + `btree_gist`), `db_session`, `client` (httpx.ASGITransport).
- El scheduler **se deshabilita automáticamente** cuando `TEST_DATABASE_URL` está seteada.
- CI corre todo en cada push a `main`; si falla, el merge se bloquea.

---

## 9. Licencia / Contacto

**Privado**. Todos los derechos reservados.

Proyecto personal de Julián Altamirano — julian@juturno.com

---

## Documentación relacionada

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Componentes, flujos, multi-tenancy, auth, slots, outbox, scheduler, webhooks
- [`DECISIONS.md`](DECISIONS.md) — 20 ADRs con contexto, alternativas, consecuencias
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Deploy, migraciones, rollback, backups, CI
- [`API_REFERENCE.md`](API_REFERENCE.md) — 44 endpoints con schemas, auth, códigos de error
- [`RUNBOOK.md`](RUNBOOK.md) — Incidentes: síntomas, diagnóstico, mitigación, fix
- [`ONBOARDING.md`](ONBOARDING.md) — Setup, arquitectura mental, convenciones, workflows
