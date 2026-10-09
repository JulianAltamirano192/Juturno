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

- ~~D-017: slots duplicados~~ — **Resuelto**: lógica extraída a `compute_available_slots` en `app/services.py`; ambos endpoints la usan.
- ~~D-018: env vars críticas sin validador en startup~~ — **Resuelto**: `model_post_init` valida `META_APP_SECRET`, `MP_TOKEN_ENCRYPTION_KEY`, `MP_SECRET_KEY`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` en producción.
- ~~`PublicServiceRead.price/deposit_amount: float`~~ — **Resuelto**: ahora `Decimal` con `field_serializer` que serializa como número.
- Alta autoservicio incompleta: conectar/desconectar MP ya se puede desde el panel con la cookie
  (`/panel/settings`, `POST /panel/mp/connect/start`, `POST /panel/mp/disconnect`), pero
  `PATCH /tenants/me` (y `/tenants/me/mp` de la API) siguen exigiendo
  API key. No hay cambio/recupero de contraseña ni verificación de email.
  - ~~Pendiente (seguridad): el `state` OAuth no está atado al navegador (account-linking).~~ — **Resuelto**: cookie HttpOnly `mp_oauth_state` debe coincidir con el state en el callback; se eliminó `GET /mp/connect/start` (API key), solo se conecta desde el panel.
  - ~~`Tenant.mp_user_id` no es único (`MultipleResultsFound` → webhook 500).~~ — **Resuelto en
    `92a24d9`** (migración `c7d8e9f0a1b2`): índice único parcial; el callback redirige a
    `?mp=account_in_use` sin guardar tokens. El `user_id` del body del webhook sigue sin firma (lo
    acota el guard de `collector_id`); un 404 por token equivocado deja el evento `failed`, así la
    entrega legítima con la misma clave se procesa igual.
- ~~CSRF en `/panel/*`: `validate_csrf` solo exige que exista la cookie, no la compara con el form.~~ — **Resuelto en `7be0fcd`**: ahora lee el form body y compara con `hmac.compare_digest` (double-submit).
- ~~`idempotency_key` es UNIQUE global~~ — **Resuelto en `8cfa0d6`** (migración `b0e5b8028ae7`): constraint ahora es `(tenant_id, idempotency_key)`.
- ~~`BookingCreate` acepta `price_at_booking` del cliente~~ — **Resuelto en `8cfa0d6`**: campo eliminado del schema. Los schemas públicos ya usan `Decimal`.
- ~~Webhook MP: falta validar monto >= seña y `booking.tenant_id == tenant resuelto`.~~
  **Resuelto en `4e3af49`** (guard de collector_id para todos los estados, currency ARS, amount
  is_finite). ~~Pendiente: `deposit_at_booking`, race condition Payment, CHECK deposit >= 0~~
  **Resuelto en `f128344`**: `deposit_at_booking` snapshotted en creación, webhook lee el valor
  fijo, migration backfill + CHECK >= 0, SELECT FOR UPDATE serializa webhooks concurrentes.
  ~~MEDIA #2: booking cargado sin SELECT FOR UPDATE; race con job de expiración.~~
  **Resuelto**: `app/mp_webhooks.py` usa `select(Booking).with_for_update()`; scheduler usa
  `.with_for_update(skip_locked=True, of=Booking)`. También: CHECK en `__table_args__` para
  `deposit_at_booking >= 0` (BAJA #2); doble cálculo `effective_deposit` eliminado (BAJA #4).
- ~~Outbox (D-016): commit por lote → riesgo de reenvíos y mensajes "veneno"~~ — **Resuelto (D-022)**: commit por evento y reintentos de `failed` con backoff (máx. 7 intentos, ventana 2 h).
- ~~Sin rate limiting en endpoints públicos~~; uvicorn sin `--forwarded-allow-ips` detrás de Traefik. — **Resuelto parcialmente en `6ab9cac`**: slowapi activo (10/min login, 5/min register, 20/min public bookings). **Pendiente ops**: configurar `--forwarded-allow-ips=<IP_Traefik>` en Coolify para que `get_remote_address` reciba la IP real del cliente y no la de Traefik.
- ~~CI solo corre pytest~~; sin branch protection confirmada. — **Resuelto parcialmente en `614e278`**: ruff y mypy agregados al workflow. Pendiente: confirmar branch protection en GitHub.
- ~~Backups sin copia externa ni restore probado~~ — **Resuelto en `746778c`**: `backup_db.sh` sube a S3 (condicional a `S3_BACKUP_BUCKET`); nuevo `restore_db.sh` con soporte local y S3.
- Docs: reescritos completos el 2026-10-08 contra el código. PLAN_MP_POR_TENANT.md sigue siendo histórico.

## Hallazgos abiertos (code-review de `app/`, 2026-10-08) — sin corregir

Verificados a mano: 1, 6, 10 y 12. El resto viene del review y hay que confirmarlo antes de arreglar.

1. Registro (`app/routers/auth.py`) no setea `timezone`: el tenant queda en `UTC` (default del
   modelo) y el panel no permite cambiarlo. Slots, agenda y texto de WhatsApp quedan desfasados
   (`outbox_worker.format_booking_datetime` trata `UTC` como Buenos Aires).
2. `POST /public/bookings` (y `api.py`) no valida `start_time` contra ahora, horario de atención,
   grilla de slots ni si servicio/staff están activos; solo el EXCLUDE evita solapamientos.
3. Acciones de agenda en el panel cargan el booking sin `FOR UPDATE`: un cancelar concurrente con
   el webhook MP puede pisar `confirmed` y dejar viva la outbox de confirmación.
4. Confirmar desde el panel permite `expired → confirmed` sin capturar `IntegrityError` (500 en
   vez de 409) y sin encolar la confirmación.
5. `DELETE /tenants/me/mp` no tiene el guard de señas pendientes que sí tiene
   `/panel/mp/disconnect`, y duplica su lógica.
6. Recordatorios: ventana fija `[now+24h, now+24h+5m]`; un run salteado o un turno confirmado con
   menos de 24h de anticipación nunca recibe recordatorio.
7. ~~`process_mp_token_refresh` sin try/except por tenant: una excepción corta el refresh del resto.~~ — **Resuelto** (rama `fix/mp-token-refresh-per-tenant`).
8. Guard de desconexión MP del panel: con `deposit_expiration_minutes` null un pending abandonado
   bloquea la desconexión para siempre; con deadline vencido pero no expirado aún, la permite.
9. CSRF: cada GET del panel rota la cookie `csrf_token`; formularios de otras pestañas dan 403.
10. `app/webhooks.py`: el verify token de Meta se compara con `==`, no con `hmac.compare_digest`.
11. Menores: reembolso/contracargo no cambia el booking; booking inexistente en webhook se marca
    procesado; `Payment.amount` y `create_mp_preference` usan `float`; el form de servicios
    acepta `Infinity`/montos fuera de `Numeric(10,2)` (500).
12. Nada en `app/` incrementa `Tenant.session_version`: `/logout` solo borra la cookie, no hay
    forma de invalidar sesiones activas (verificado; ver D-013).

## Flujo de trabajo

Planificar → test primero → implementar → `./scripts/test.sh` → agente `code-reviewer` →
commit (cuando Julián lo pida) → `docs-keeper` si cambió comportamiento, endpoints o env vars.
Agentes del proyecto: `code-reviewer`, `security-auditor`, `migration-reviewer`, `docs-keeper`.
Skills del proyecto: `new-endpoint`, `close-phase`, `prod-readiness`.
