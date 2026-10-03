# AGENTS.md — Contexto para agentes de IA en Juturno

## Qué es este proyecto

Juturno es un SaaS multi-tenant de gestión de turnos con cobro de señas y
notificaciones por WhatsApp. Cada negocio (tenant) gestiona sus turnos de
forma aislada desde un panel; los clientes reservan desde un link público,
pagan una seña con Mercado Pago, y reciben confirmación por WhatsApp.

Está en producción en https://api.juturno.com desde septiembre de 2026.
Es un proyecto personal de Julián Altamirano, desarrollado en paralelo a
sus estudios universitarios.

## Idioma

- **Código y comentarios**: español (rioplatense).
- **Docstrings**: español.
- **Mensajes de error al usuario**: español.
- **Nombres de variables, funciones, tablas**: inglés (estándar).
- **Documentación**: español (rioplatense).

## Stack técnico

| Categoría | Tecnología |
|---|---|
| Backend | FastAPI + SQLModel + Pydantic v2 |
| Base de datos | PostgreSQL 16 + btree_gist |
| Cache / locks | Redis 7 |
| Scheduler | APScheduler (in-process) |
| Pagos | Mercado Pago (Checkout Pro + OAuth por tenant) |
| Notificaciones | WhatsApp Business API (Meta) |
| Cifrado | cryptography (Fernet) |
| Observabilidad | Sentry + health check |
| Testing | pytest + pytest-asyncio |
| Calidad | pre-commit (ruff, black, mypy) |
| Deploy | Coolify + Traefik en VPS Hetzner |

## Estructura del repo

```
app/
  main.py              — App FastAPI, todos los endpoints
  models.py            — Modelos SQLModel (Tenant, Booking, etc.)
  services.py          — Lógica de slots y ventanas horarias
  auth.py              — API key + cookie de sesión
  session.py           — Firma y parseo de la cookie
  csrf.py              — CSRF del panel
  config.py            — Settings (Pydantic BaseSettings)
  database.py          — Engine async y session maker
  scheduler.py         — Jobs de APScheduler
  outbox_worker.py     — Worker del patrón Outbox
  booking_actions.py   — Máquina de estados de Booking (Tarea 8)
  mp_webhooks.py       — Webhook de Mercado Pago
  mp_connect.py        — OAuth de MP por tenant
  mp_crypto.py         — Cifrado Fernet de tokens MP
  webhooks.py          — Webhook de WhatsApp
  whatsapp_service.py  — Cliente de Meta
  password.py          — Hash PBKDF2
  slug.py              — Generación de slugs únicos
  phone.py             — Normalización de teléfonos argentinos
  templates/           — Jinja2 (panel + página pública)
alembic/versions/      — Migraciones (no editar retroactivamente)
tests/                 — pytest
docker-compose.yml     — Setup local
docker-compose.prod.yml — Setup producción
Dockerfile             — Build de la imagen
```

## Convenciones de código

- **Async everywhere**: los endpoints, servicios y jobs usan `async def`.
- **SQLModel**: los modelos son a la vez Pydantic y SQLAlchemy.
- **Nunca commit manual en servicios**: los callers deciden cuándo commitear.
- **Excepciones específicas**: `InvalidTransitionError`, `BookingNotStartedError`,
  `MPTokenCryptoError`, `InvalidPhoneError`. No usar `Exception` genérico salvo
  en boundaries (health checks, webhooks).
- **Logging estructurado**: `logger = logging.getLogger(__name__)`.
- **Timing-safe compares**: usar `hmac.compare_digest` para firmas.

## Anti-patrones a evitar

- No usar `Optional[X]`, usar `X | None` (Python 3.10+).
- No usar `List[X]`, usar `list[X]`. Idem `Dict`/`dict`, `Tuple`/`tuple`.
- No usar `datetime.now()` sin `tz`. Usar `datetime.now(timezone.utc)`.
- No hacer queries sin filtrar por `tenant_id` en endpoints autenticados.

## Comandos útiles

```bash
# Levantar el entorno local
docker compose up -d --build
docker compose exec api alembic upgrade head

# Correr tests
pytest tests/ -v

# Lint
ruff check app/ tests/

# Pre-commit
pre-commit run --all-files
```

## Cómo trabajamos

- Los cambios se hacen en commits chicos, con mensajes en inglés
  (`feat(booking): ...`, `fix(deploy): ...`).
- Cada feature nueva tiene tests.
- Las decisiones importantes se documentan en `DECISIONS.md` con formato ADR.
- Los incidentes se documentan en `RUNBOOK.md`.

## Cosas que NO hacer

- No commitear secretos (`.env`, keys, tokens). Si ves uno, avisá.
- No modificar migraciones ya aplicadas en producción. Crear una nueva.
- No romper el contrato de la API pública sin versionar.
- No inventar features que no están en el código.

## Contacto

Proyecto personal de Julián Altamirano (julian@juturno.com).
