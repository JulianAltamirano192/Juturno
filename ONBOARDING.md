# Onboarding — Guía para dev nuevo

> Setup, arquitectura mental, convenciones y workflows. Para ser productivo en < 1 hora.

---

## 1. Setup local (paso a paso)

```bash
# 1. Clonar
git clone git@github.com:JulianAltamirano192/Juturno.git
cd Juturno

# 2. Configurar env (copiar ejemplo y editar)
cp .env.example .env
# Completar MÍNIMO: POSTGRES_PASSWORD, SECRET_KEY, MP_SANDBOX=true
# El resto puede quedar vacío para dev local (algunos features no funcionarán)

# 3. Levantar servicios
docker compose up -d --build
# Levanta: saas_db (PostgreSQL 16), saas_redis (Redis 7), saas_api (FastAPI + APScheduler)
# El compose local instala requirements-dev.txt en la imagen (pytest, ruff, black, mypy).

# 4. Aplicar migraciones
docker compose exec api alembic upgrade head

# 5. Crear DB de tests (una sola vez)
docker compose exec db createdb -U postgres saas_test

# 6. Verificar que todo funciona
curl http://localhost:8000/health
# {"status":"ok","checks":{"api":"ok","database":"ok","redis":"ok"}}

# 7. Correr tests
./scripts/test.sh
# 313 tests en 31 archivos (./scripts/test.sh --collect-only -q)
```

> **Nota clave**: Los tests corren **dentro del contenedor `api`**. `./scripts/test.sh` arma `TEST_DATABASE_URL` leyendo `POSTGRES_PASSWORD` del `.env` y ejecuta `docker compose exec ... api pytest <args>` (sin args corre `-v`). La URL apunta al servicio `db` (no `localhost`). Con `TEST_DATABASE_URL` seteada el scheduler NO arranca y el rate limiter queda deshabilitado.

Ejemplos:

```bash
./scripts/test.sh                                  # toda la suite, -v
./scripts/test.sh tests/test_slots.py -v           # un archivo
./scripts/test.sh -k "idempotency" -x              # por nombre, corta al primer fallo
./scripts/test.sh --collect-only -q                # contar tests sin correrlos
```

---

## 2. Arquitectura mental (3 minutos)

```
Tenant (negocio)
  ├── Service (qué se reserva: nombre, duración, precio, seña)
  ├── Staff (quién atiende: nombre, activo)
  ├── BusinessHours (cuándo atiende: día, hora inicio/fin, por staff o negocio)
  └── Booking (el turno: cliente, servicio, staff, inicio/fin, precio, seña, estado)

Booking.status: pending | confirmed | expired | cancelled | no_show | completed
(transiciones en app/booking_actions.py; nunca se cambia booking.status a mano)

Flujo típico:
  pending → (pago MP approved) → confirmed → (24h antes: recordatorio por WhatsApp) → completed / no_show
  pending → (dueño/cliente cancela) → cancelled
  pending → (seña no pagada a tiempo) → expired → (pago tardío approved y slot libre) → confirmed
```

**Patrones clave:**
- **Outbox**: cuando el webhook MP confirma el pago, crea `NotificationOutbox` en la misma transacción que la confirmación; el recordatorio de 24h lo encola `process_reminders`. `process_outbox` envía por WhatsApp (commit por evento, reintentos de `failed` con backoff; ver D-022). Al crear el booking NO hay outbox (solo `Booking` + `Payment`).
- **Anti-solapamiento**: `ExcludeConstraint` en Postgres (no en código; solo bloquea `pending`/`confirmed`) → race conditions imposibles. Los endpoints capturan `IntegrityError` → 409.
- **MP OAuth por tenant**: cada negocio conecta su cuenta desde el panel → tokens Fernet cifrados → dinero directo al dueño. En producción un tenant sin MP conectado no puede cobrar (422 `ERR_PAGO_NO_CONFIGURADO`).
- **Scheduler in-process**: 4 jobs APScheduler en el proceso de la API (`app/main.py`), con locks Redis / `SKIP LOCKED`. No escalar a más de 1 réplica sin worker separado.

| Job | Frecuencia | Qué hace |
|-----|------------|----------|
| `process_outbox` | cada 1 min | Envía mensajes pendientes por WhatsApp |
| `process_reminders` | cada 5 min | Encola recordatorios 24h antes del turno |
| `process_deposit_expiration` | cada 1 min | Expira reservas `pending` con la seña vencida |
| `process_mp_token_refresh` | cada 1440 min (24 h) | Renueva tokens OAuth de MP por vencer |

---

## 3. Convenciones de código (obligatorias)

| Regla | Ejemplo correcto | Ejemplo incorrecto |
|-------|------------------|-------------------|
| **Async everywhere** | `async def handler():` | `def handler():` |
| **SQLModel unificado** | `class User(SQLModel, table=True):` | `Base = declarative_base()` |
| **No commit en servicios** | `session.add(obj)` → caller `await session.commit()` | `await session.commit()` dentro del servicio |
| **Excepciones específicas** | `raise InvalidTransitionError(...)` | `raise Exception("...")` |
| **Logging** | `logger = logging.getLogger(__name__)` | `print(...)` o `logging.info(...)` |
| **Timing-safe** | `hmac.compare_digest(a, b)` | `a == b` (firmas, tokens, CSRF) |
| **Type hints** | `X \| None`, `list[X]`, `dict[K, V]` | `Optional[X]`, `List[X]`, `Dict[K, V]` |
| **Timezones** | `datetime.now(timezone.utc)`, `zoneinfo` por tenant | `datetime.now()` o `datetime.utcnow()` |
| **Decimal dinero** | `Decimal("5000.00")` | `5000.0` (float) en schemas o modelos |
| **Queries con tenant_id** | `select(Booking).where(Booking.tenant_id == tenant.id)` | `select(Booking)` sin filtro |
| **Estado de booking** | `await transition_booking_status(session, booking, "cancelled", actor="owner")` | `booking.status = "cancelled"` |
| **Estilo** | `black` (88 cols) + `ruff` | formatear a mano |

⚠️ Cambios en auth, webhooks, pagos o endpoints públicos: test primero y revisión de seguridad antes de cerrar la tarea.

---

## 4. Cómo agregar un endpoint

### 4.1 Elegir el router correcto

Los endpoints viven en `app/routers/` (más `app/mp_connect.py`, `app/mp_webhooks.py` y `app/webhooks.py` para OAuth y webhooks), agrupados por mecanismo de auth:

| Auth | Archivo |
|------|---------|
| Sin auth (público) | `app/routers/public.py` |
| Cookie `juturno_session` (panel HTML) | `app/routers/panel.py` o `app/routers/auth.py` (login, registro, logout) |
| Header `X-Tenant-API-Key` | `app/routers/api.py` (y `/tenants/me/mp` en `app/mp_connect.py`) |
| Firma de webhook | `app/mp_webhooks.py` (Mercado Pago), `app/webhooks.py` (Meta/WhatsApp) |

### 4.2 Definir schemas Pydantic

Si el schema es específico del endpoint, definilo en el mismo archivo de router. Si es compartido entre routers, usá `app/schemas.py`. Dinero siempre `Decimal` (si el JSON público debe salir como número, usá un `field_serializer`, como `PublicServiceRead`).

```python
class MiRequest(BaseModel):
    campo: str
    valor: int = Field(gt=0)

class MiResponse(BaseModel):
    resultado: str
```

### 4.3 Elegir dependencia de autenticación
```python
# API Key (endpoints máquina-a-máquina)
current_tenant: Tenant = Depends(get_current_tenant)

# Panel web (HTML, cookie): redirige a /login si no hay sesión
tenant: Tenant = Depends(get_current_tenant_from_session)
```

### 4.4 Escribir handler en el router correspondiente
```python
# En app/routers/api.py (ejemplo con API Key)
router = APIRouter()

@router.post("/mi-endpoint", response_model=MiResponse)
async def mi_endpoint(
    payload: MiRequest,
    current_tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db),
):
    # 1. Validaciones de negocio
    # 2. Queries SIEMPRE filtrando tenant_id
    stmt = select(Modelo).where(Modelo.tenant_id == current_tenant.id, ...)
    # 3. Lógica
    # 4. session.add(obj); el endpoint (quien llama) hace commit, no el servicio
    return MiResponse(resultado="ok")
```

Para el **panel** (formularios POST): llamá `await validate_csrf(request)` al principio (403 si falla), obtené el token con `generate_csrf_token(request)` (reusa el de la cookie) + `set_csrf_cookie(response, token)` en los GET/re-renders, y respondé con `RedirectResponse(..., status_code=303)`. Filtrá por `tenant.id` y devolvé 404 si el recurso no es del tenant.

Para **rate limiting**: `@limiter.limit("N/minute")` (de `app.limiter`) debajo de `@router.post(...)`, y el handler tiene que recibir `request: Request`. Hoy solo lo usan `/login`, `/register` y `/public/bookings`.

`app/main.py` ya registra todos los routers con `app.include_router(...)`. No hace falta tocarlo para agregar handlers dentro de un router existente. Si creás un router nuevo, registralo ahí.

Después de agregar el endpoint, actualizá `API_REFERENCE.md` (ruta, auth, schemas, errores, rate limit).

### 4.5 Agregar tests (test primero)
```python
# tests/test_mi_feature.py
@pytest.mark.asyncio
async def test_mi_endpoint(client, db_session):
    raw_key = await _create_api_key(db_session, tenant_id)
    res = await client.post("/mi-endpoint", json={"campo": "x", "valor": 1}, headers=_auth_headers(raw_key))
    assert res.status_code == 200
    assert res.json()["resultado"] == "ok"
```

> Si el test necesita mockear algo importado en un router (ej. `create_mp_preference`), el patch target es el módulo del router donde se importa, no `app.main`. Ejemplo: `monkeypatch.setattr("app.routers.public.create_mp_preference", mock_fn)`.

### 4.6 Verificar calidad
```bash
# Lint / formato / tipos (en el host, con el venv de dev)
ruff check app/ tests/
black --check app/ tests/
mypy app/

# Tests (dentro del contenedor, vía script)
./scripts/test.sh tests/test_mi_feature.py -v
./scripts/test.sh            # suite completa antes de dar la tarea por hecha
```

"Hecho" = tests verdes + ruff + mypy limpios.

**pre-commit** (`.pre-commit-config.yaml`): corre higiene de archivos (trailing whitespace, EOF, yaml, archivos grandes, merge markers, claves privadas), `ruff --fix`, `black` (pinneado en 26.10.0, igual que `requirements-dev.txt`) y `mypy` (excluye `tests/` y `alembic/`).

```bash
pip install -r requirements-dev.txt   # en tu venv local
pre-commit install                    # una vez: se corre en cada commit
pre-commit run --all-files            # a demanda
```

---

## 5. Cómo crear migración

```bash
# 1. Generar archivo vacío
docker compose exec api alembic revision -m "add campo x a tabla y"

# 2. Editar archivo en alembic/versions/XXXX_add_campo_x.py
#    - upgrade(): op.add_column(...), op.create_index(...), etc.
#    - downgrade(): op.drop_column(...), op.drop_index(...)

# 3. Aplicar
docker compose exec api alembic upgrade head

# 4. Verificar
docker compose exec api alembic current
```

**Reglas de migración:**
- ❌ **Nunca editar una migración ya commiteada**: crear una nueva. Un hook de Claude Code (`.claude/hooks/protect-migrations.sh`) lo bloquea para archivos versionados en git.
- `btree_gist` ya existe (migración inicial).
- Server defaults: `server_default=sa.text("NOW()")`, `sa.text("'pending'")`, `sa.text("false")`, `sa.text("0")`, `sa.text("1")`, `sa.text("'received'")`.
- FKs con `ondelete="CASCADE"` (o `"SET NULL"` para staff_id en booking).
- Columnas NOT NULL nuevas sobre tablas con datos: agregá `server_default` (o backfill) para no romper filas existentes.
- Si el modelo cambia, actualizá también `app/models.py`: los tests crean las tablas con `SQLModel.metadata.create_all`, no con Alembic, así que una migración rota no la detecta la suite.
- En producción, el contenedor ejecuta `python -m alembic upgrade head` antes de levantar uvicorn (`docker-compose.prod.yml`).

---

## 6. Cómo testear

### 6.1 Fixtures disponibles (`tests/conftest.py`)
```python
# setup_db (autouse): crea btree_gist + tablas (metadata.create_all) antes de cada test y las borra al terminar
# db_session: AsyncSession visible para requests de integración
# client: httpx.AsyncClient con ASGITransport(app) + override get_db
```

### 6.2 Helpers de auth (`tests/test_integration.py`)
```python
async def _create_api_key(db_session, tenant_id: int) -> str:
    raw_key = f"test-key-tenant-{tenant_id}"
    db_session.add(ApiKey(tenant_id=tenant_id, key_hash=hash_api_key(raw_key)))
    await db_session.commit()
    return raw_key

def _auth_headers(raw_key: str) -> dict:
    return {"X-Tenant-API-Key": raw_key}
```

### 6.3 Mocks comunes
```python
# MP webhook
monkeypatch.setattr(mp_webhooks.settings, "MP_SECRET_KEY", "test-secret")
async def mock_get_payment_details(data_id, access_token=None):
    return {"status": "approved", "external_reference": f"booking-{booking_id}", ...}
monkeypatch.setattr(mp_webhooks, "get_payment_details", mock_get_payment_details)

# Tiempo: no se usa freezegun. Los tests controlan el tiempo con offsets
# reales (ej: timedelta(hours=-1) para "ya empezó", +1 para "futuro").
# Ver tests/test_booking_actions.py::_make_booking.
```

### 6.4 Scheduler y rate limiter en tests
- **El scheduler se deshabilita automáticamente** si `TEST_DATABASE_URL` está seteada (ver lifespan en `app/main.py`). No hace falta mockear jobs; corren en tests solo si los invocás manual.
- **El rate limiter también** (`app/limiter.py`: `enabled=not TEST_DATABASE_URL`). Para testear los límites, `tests/test_rate_limiting.py` usa un fixture que pone `limiter.enabled = True` y resetea el storage.

### 6.5 Reglas
- ❌ No debilites ni borres tests para que pasen: si un test falla, diagnosticá la causa.
- Los tests de seguridad (CSRF, firmas, multi-tenant) cubren casos negativos; agregá el caso negativo cuando toques esas áreas.

---

## 7. Deploy a producción

```bash
# 1. Push a main
git push origin main

# 2. GitHub Actions corre CI (.github/workflows/ci.yml): ruff, mypy y pytest
#    (black corre solo vía pre-commit local).
#    Ver: https://github.com/JulianAltamirano192/Juturno/actions

# 3. Coolify detecta push → build imagen → deploy
#    - Comando: python -m alembic upgrade head && uvicorn app.main:app --proxy-headers
#    - Health check: /health (200 ok / 503 degraded)

# 4. Verificar en prod
curl https://api.juturno.com/health
# {"status":"ok","checks":{"api":"ok","database":"ok","redis":"ok"}}
```

**Rollback**: En Coolify → botón "Redeploy" en deployment anterior.

⚠️ No hay push, deploy ni commit sin que Julián lo pida. Detalles de operación en `DEPLOYMENT.md` y `RUNBOOK.md`.

---

## 8. Links rápidos

| Doc | Para qué |
|-----|----------|
| [`README.md`](README.md) | Quickstart, env vars tabla completa, comandos |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Modelos, flujos, auth, slots, outbox, scheduler, MP, WhatsApp |
| [`DECISIONS.md`](DECISIONS.md) | 30 ADRs (D-001 a D-030) — por qué se hizo así |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Deploy, migraciones, rollback, backups, CI, secrets |
| [`API_REFERENCE.md`](API_REFERENCE.md) | 47 rutas con schemas, auth, rate limits, códigos de error |
| [`RUNBOOK.md`](RUNBOOK.md) | Incidentes: síntomas, diagnóstico, mitigación, fix |
| [`PLAN_MP_POR_TENANT.md`](PLAN_MP_POR_TENANT.md) | Solo histórico (desactualizado: nombres de variables viejos) |

---

## 9. Primeras tareas sugeridas

1. **Leer** `ARCHITECTURE.md` §2 (modelo de datos) y §5 (slots).
2. **Correr** tests y ver que pasan: `./scripts/test.sh`.
3. **Agregar** un endpoint trivial (ej. `GET /ping` que devuelve `{"pong": true}`) con test.
4. **Crear** migración dummy (add column nullable) y aplicarla.
5. **Revisar** `DECISIONS.md` D-003 (ExcludeConstraint), D-012 (MP OAuth), D-013 (session_version).
6. **Simular** incidente local: setear `ENVIRONMENT=production` y `SECRET_KEY=change-this-secret-key-in-production-juturno` (el default) en `.env` → el contenedor falla al arrancar con `ValueError` → arreglar. (`Settings.model_post_init` en `app/config.py` solo valida en producción: además del `SECRET_KEY`, exige `MP_SANDBOX=false`, `META_APP_SECRET`, `MP_TOKEN_ENCRYPTION_KEY`, `MP_SECRET_KEY`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` y `MP_NOTIFICATION_URL` con forma `https://.../webhooks/mercadopago`; ver D-014 y D-018.)

---

## Ver también

- [`README.md`](README.md) — Quickstart completo
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Arquitectura detallada
- [`DECISIONS.md`](DECISIONS.md) — Contexto de decisiones
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Operación en prod
- [`API_REFERENCE.md`](API_REFERENCE.md) — Referencia endpoints
- [`RUNBOOK.md`](RUNBOOK.md) — Si algo se rompe
