# Juturno — contexto para Claude Code

SaaS multi-tenant de gestión de turnos para negocios de servicios simples (peluquerías,
estudios de tatuaje, lavaderos, etc.). La terminología del producto y del panel debe ser
**genérica**, nunca específica de peluquería. Cobra señas con Mercado Pago (OAuth por tenant:
el dinero va directo a la cuenta del negocio, la plataforma no toca la seña) y notifica por
WhatsApp (Meta). Stack: FastAPI + SQLModel + PostgreSQL 16 + Redis 7 + APScheduler in-process.
Prod: VPS Hetzner con Coolify + Traefik (api.juturno.com, panel en juturno.com).
Desarrollador: Julián (estudiante, trabaja de a una tarea por vez).

## Documentación (leer bajo demanda, no todo junto)

| Doc | Leer cuando |
| --- | --- |
| ARCHITECTURE.md | Vas a tocar modelos, auth, slots, outbox, scheduler, MP, WhatsApp o la máquina de estados |
| DECISIONS.md | Querés saber por qué algo es así (ADRs D-001...) o vas a tomar una decisión nueva |
| API_REFERENCE.md | Agregás o cambiás endpoints |
| ONBOARDING.md | Convenciones, cómo agregar endpoint / migración / test |
| RUNBOOK.md, DEPLOYMENT.md | Incidentes, deploy, rollback, backups, rotación de secrets |
| PLAN_MP_POR_TENANT.md | Solo histórico (desactualizado: nombres de variables viejos) |

Si un doc contradice el código, manda el código: avisá y proponé corregir el doc.

## Comandos

- Tests: `./scripts/test.sh [args de pytest]` (corre dentro del contenedor `api`; con
  `TEST_DATABASE_URL` seteada el scheduler NO arranca)
- Calidad (host): `ruff check app/ tests/` · `black --check app/ tests/` · `mypy app/`
- Migración: `docker compose exec api alembic revision -m "msg"` → editar → `... upgrade head`
- Levantar local: `docker compose up -d --build`
- "Hecho" = tests verdes + ruff + mypy limpios.

## Arquitectura: lo que no es obvio

- Una sola DB para todos los tenants. **Toda query autenticada filtra `tenant_id`.**
- Dos autenticaciones: header `X-Tenant-API-Key` (SHA-256, cache Redis 60s) y cookie firmada
  `juturno_session` para el panel (HMAC + `session_version`). CSRF en formularios del panel.
- Los servicios **no hacen commit**: lo hace quien los llama.
- Anti-solapamiento: `EXCLUDE USING gist` en `booking` (solo bloquea `pending`/`confirmed`).
  Los endpoints capturan `IntegrityError` → 409.
- Estados de booking: `app/booking_actions.py` (`transition_booking_status`); no cambiar
  `booking.status` a mano.
- Outbox: la confirmación se encola en la misma transacción que confirma el pago (webhook MP);
  el recordatorio 24h lo encola `process_reminders`. `process_outbox` envía por WhatsApp.
- 4 jobs APScheduler en el proceso de la API (outbox, reminders, expiración de señas, refresh
  de tokens MP) con lock Redis / `SKIP LOCKED`. No escalar a >1 réplica sin worker separado.
- MP: `resolve_mp_access_token(tenant)`. En producción un tenant sin MP conectado NO puede
  cobrar (422 `ERR_PAGO_NO_CONFIGURADO`); el fallback a la cuenta de plataforma es solo sandbox.
- Teléfonos: en DB `549XXXXXXXXXX`; a Meta se envía sin el `9` (`normalize_phone_for_meta`).
- Dinero siempre `Decimal`; fechas `datetime.now(timezone.utc)` y `zoneinfo` por tenant.
- Firmas y tokens se comparan con `hmac.compare_digest`.

## Reglas duras

- Nunca leas, imprimas ni commitees `.env` ni secrets. No toques producción.
- Nunca edites una migración ya commiteada: creá una nueva (un hook lo bloquea).
- No rotes `MP_TOKEN_ENCRYPTION_KEY` (deja ilegibles los tokens de los tenants).
- No debilites ni borres tests para que pasen; si un test falla, diagnosticá la causa.
- Cambios en auth, webhooks, pagos o endpoints públicos: test primero y revisión con el
  agente `security-auditor` antes de dar la tarea por cerrada.
- Una tarea por vez. No hagas push, deploy ni commit sin que Julián lo pida.
- Commits: Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).

## Estado conocido (auditoría 2026-10-04) — mencionalo si tu tarea lo toca; no lo "arregles de pasada"

- Alta autoservicio incompleta: conectar MP (`/mp/connect/start`, `/tenants/me/mp`) y
  `PATCH /tenants/me` exigen API key; el panel con cookie no tiene botón. No hay cambio/recupero
  de contraseña ni verificación de email.
- CSRF en `/panel/*`: `validate_csrf` solo exige que exista la cookie, no la compara con el form.
- `idempotency_key` es UNIQUE global (debería ser `(tenant_id, idempotency_key)`).
- `BookingCreate` acepta `price_at_booking` del cliente y montos `float`: el endpoint público
  debe usar siempre `service.price`; dinero en `Decimal`.
- ~~Webhook MP: falta validar monto >= seña y `booking.tenant_id == tenant resuelto`.~~
  **Resuelto en `4e3af49`** (guard de collector_id para todos los estados, currency ARS, amount
  is_finite). Pendiente: `deposit_at_booking`, race condition Payment, CHECK deposit >= 0
  — ver D-019 y roadmap Fase 0.
- Outbox (D-016): commit por lote → riesgo de reenvíos y mensajes "veneno"; pasar a commit por evento.
- Sin rate limiting en endpoints públicos; uvicorn sin `--forwarded-allow-ips` detrás de Traefik.
- CI solo corre pytest (Python 3.12 en CI vs 3.11 en Dockerfile); sin branch protection confirmada.
- Backups sin copia externa ni restore probado.
- Docs desactualizados: README (conteo de tests), PLAN_MP.

## Flujo de trabajo

Planificar → test primero → implementar → `./scripts/test.sh` → agente `code-reviewer` →
commit (cuando Julián lo pida) → `docs-keeper` si cambió comportamiento, endpoints o env vars.
Agentes del proyecto: `code-reviewer`, `security-auditor`, `migration-reviewer`, `docs-keeper`.
Skills del proyecto: `new-endpoint`, `close-phase`, `prod-readiness`.
