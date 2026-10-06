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

# 4. Aplicar migraciones
docker compose exec api alembic upgrade head

# 5. Crear DB de tests (una sola vez)
docker compose exec db createdb -U postgres saas_test

# 6. Verificar que todo funciona
curl http://localhost:8000/health
# {"status":"ok","checks":{"api":"ok","database":"ok","redis":"ok"}}

# 7. Correr tests
docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@db:5432/saas_test" \
  api pytest -v
# 222 tests en ~23 archivos, ~100s
```

> **Nota clave**: Tests corren **dentro del contenedor `api`**. `TEST_DATABASE_URL` debe apuntar al servicio `db` (no `localhost`). El scheduler se deshabilita automáticamente con esta variable.

---

## 2. Arquitectura mental (3 minutos)

```
Tenant (negocio)
  ├── Service (qué se reserva: nombre, duración, precio, seña%)
  ├── Staff (quién atiende: nombre, activo)
  ├── BusinessHours (cuándo atiende: día, hora inicio/fin, por staff o negocio)
  └── Booking (el turno: cliente, servicio, staff, inicio/fin, precio, estado)

Booking.flow:
  pending → (pago MP approved) → confirmed → (24h antes) → reminder → (inicio) → completed/no_show
  pending → (cliente cancela) → cancelled
  pending → (seña no pagada) → expired → (pago tardío approved) → confirmed
```

**Patrones clave:**
- **Outbox**: cuando el webhook MP confirma el pago, crea `NotificationOutbox` en la misma transacción que la confirmación → job cada 60s envía WhatsApp. Al crear el booking NO hay outbox (solo `Booking` + `Payment`).
- **Anti-solapamiento**: `ExcludeConstraint` en Postgres (no en código) → race conditions imposibles.
- **MP OAuth por tenant**: cada negocio conecta su cuenta → tokens Fernet cifrados → dinero directo al dueño.
- **Scheduler in-process**: 4 jobs (outbox, reminders, expiración, refresh MP) + locks Redis/DB.

---

## 3. Convenciones de código (obligatorias)

| Regla | Ejemplo correcto | Ejemplo incorrecto |
|-------|------------------|-------------------|
| **Async everywhere** | `async def handler():` | `def handler():` |
| **SQLModel unificado** | `class User(SQLModel, table=True):` | `Base = declarative_base()` |
| **No commit en servicios** | `session.add(obj)` → caller `await session.commit()` | `await session.commit()` dentro del servicio |
| **Excepciones específicas** | `raise InvalidTransitionError(...)` | `raise Exception("...")` |
| **Logging** | `logger = logging.getLogger(__name__)` | `print(...)` o `logging.info(...)` |
| **Timing-safe** | `hmac.compare_digest(a, b)` | `a == b` |
| **Type hints** | `X \| None`, `list[X]`, `dict[K, V]` | `Optional[X]`, `List[X]`, `Dict[K, V]` |
| **Timezones** | `datetime.now(timezone.utc)` | `datetime.now()` o `datetime.utcnow()` |
| **Decimal dinero** | `Decimal("5000.00")` | `5000.0` (float) |
| **Queries con tenant_id** | `select(Booking).where(Booking.tenant_id == tenant.id)` | `select(Booking)` sin filtro |

---

## 4. Cómo agregar un endpoint

### 4.1 Elegir el router correcto

Los endpoints viven en `app/routers/`, agrupados por mecanismo de auth:

| Auth | Archivo |
|------|---------|
| Sin auth (público) | `app/routers/public.py` |
| Cookie `juturno_session` (panel HTML) | `app/routers/panel.py` o `app/routers/auth.py` |
| Header `X-Tenant-API-Key` | `app/routers/api.py` |

### 4.2 Definir schemas Pydantic

Si el schema es específico del endpoint, definilo en el mismo archivo de router. Si es compartido entre routers, usá `app/schemas.py`.

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

# Panel web (HTML, cookie)
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
    # 4. session.add(obj) — NO commit aquí
    return MiResponse(resultado="ok")
```

`app/main.py` ya registra todos los routers con `app.include_router(...)`. No hace falta tocarlo para agregar handlers dentro de un router existente.

### 4.5 Agregar tests
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
# Lint/typecheck (local, host)
ruff check app/ tests/
mypy app/

# Tests (dentro del contenedor, con TEST_DATABASE_URL)
docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@db:5432/saas_test" \
  api pytest tests/test_mi_feature.py -v
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
- ❌ **Nunca editar migración ya aplicada** en producción. Crear nueva.
- ✅ `btree_gist` ya existe (migración inicial).
- ✅ Server defaults: `server_default=sa.text("NOW()")`, `sa.text("'pending'")`, `sa.text("false")`, `sa.text("0")`, `sa.text("'received'")`.
- ✅ FKs con `ondelete="CASCADE"` (o `"SET NULL"` para staff_id en booking).

---

## 6. Cómo testear

### 6.1 Fixtures disponibles (`tests/conftest.py`)
```python
# setup_db (autouse): crea/borra tablas + btree_gist por test
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

### 6.4 Scheduler en tests
- **Se deshabilita automáticamente** si `TEST_DATABASE_URL` está seteada (ver lifespan en `app/main.py`).
- No hace falta mockear jobs; corren en tests de integración solo si los invocás manual.

---

## 7. Deploy a producción

```bash
# 1. Push a main
git push origin main

# 2. GitHub Actions corre CI (solo pytest; lint/typecheck van por pre-commit local)
#    Ver: https://github.com/JulianAltamirano192/Juturno/actions

# 3. Coolify detecta push → build imagen → deploy
#    - EntryPoint: alembic upgrade head + uvicorn
#    - Health check: /health (200 ok / 503 degraded)

# 4. Verificar en prod
curl https://api.juturno.com/health
# {"status":"ok","checks":{"api":"ok","database":"ok","redis":"ok"}}
```

**Rollback**: En Coolify → botón "Redeploy" en deployment anterior.

---

## 8. Links rápidos

| Doc | Para qué |
|-----|----------|
| [`README.md`](README.md) | Quickstart, env vars tabla completa, comandos |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Modelos, flujos, auth, slots, outbox, scheduler, MP, WhatsApp |
| [`DECISIONS.md`](DECISIONS.md) | 18 ADRs (D-001 a D-018) — por qué se hizo así |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Deploy, migraciones, rollback, backups, CI, secrets |
| [`API_REFERENCE.md`](API_REFERENCE.md) | 44 endpoints con schemas, auth, códigos de error |
| [`RUNBOOK.md`](RUNBOOK.md) | Incidentes: síntomas, diagnóstico, mitigación, fix |
| [`PLAN_MP_POR_TENANT.md`](PLAN_MP_POR_TENANT.md) | Implementación OAuth MP por tenant (detalle) |

---

## 9. Primeras tareas sugeridas

1. **Leer** `ARCHITECTURE.md` §2 (modelo de datos) y §5 (slots).
2. **Correr** tests y ver que pasan: `docker compose exec ... pytest -v`.
3. **Agregar** un endpoint trivial (ej. `GET /ping` que devuelve `{"pong": true}`) con test.
4. **Crear** migración dummy (add column nullable) y aplicarla.
5. **Revisar** `DECISIONS.md` D-003 (ExcludeConstraint), D-012 (MP OAuth), D-013 (session_version).
6. **Simular** incidente local: setear `ENVIRONMENT=production` y `SECRET_KEY=change-this-secret-key-in-production-juturno` (el default) en `.env` → el contenedor falla al arrancar con `ValidationError` → arreglar. (El validador solo dispara en prod — ver `app/config.py:40-45` y D-014).

---

## Ver también

- [`README.md`](README.md) — Quickstart completo
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Arquitectura detallada
- [`DECISIONS.md`](DECISIONS.md) — Contexto de decisiones
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Operación en prod
- [`API_REFERENCE.md`](API_REFERENCE.md) — Referencia endpoints
- [`RUNBOOK.md`](RUNBOOK.md) — Si algo se rompe
