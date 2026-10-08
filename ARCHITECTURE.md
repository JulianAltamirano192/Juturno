# Arquitectura de Juturno

> Descripción técnica de componentes, flujos de datos y decisiones de diseño. Para devs que trabajan en el código.

---

## 1. Visión general (diagrama ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENTE (Web / Móvil)                          │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │
                    ┌─────────────▼─────────────┐
                    │     FastAPI (uvicorn)     │
                    │  app/main.py (lifespan,   │
                    │  middleware, routers)      │
                    └─────────────┬─────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
        ▼                         ▼                         ▼
┌───────────────┐       ┌─────────────────┐       ┌───────────────┐
│  PostgreSQL   │       │      Redis      │       │  APScheduler  │
│    16         │       │       7         │       │  (in-process) │
│  + btree_gist │       │                 │       │               │
└───────────────┘       └─────────────────┘       └───────┬───────┘
                                                          │
                    ┌─────────────────────────────────────┼─────────────────────────────────────┐
                    │                                     │                                     │
                    ▼                                     ▼                                     ▼
            ┌───────────────┐                   ┌─────────────────┐                 ┌─────────────────┐
            │   Webhooks    │                   │  Mercado Pago   │                 │  WhatsApp       │
            │               │                   │  (OAuth + MP)   │                 │  (Meta)         │
            │ /webhooks/    │                   │                 │                 │                 │
            │ whatsapp      │                   │ /mp/connect/*   │                 │ /webhooks/      │
            │ /webhooks/    │                   │ /webhooks/      │                 │ whatsapp        │
            │ mercadopago   │                   │ mercadopago     │                 │                 │
            └───────────────┘                   └─────────────────┘                 └─────────────────┘
```

**Flujo de un booking típico:**
1. Cliente accede a `/t/{slug}` → ve servicios → elige slot → `POST /public/bookings`
2. Se crea `Booking` (status `pending`) + `Payment` (status `pending`, con `mp_checkout_url`) en **misma transacción**
3. Se crea preferencia MP con token del tenant (OAuth) → devuelve `payment_url`
4. Cliente paga en MP → MP envía webhook a `/webhooks/mercadopago`
5. Webhook valida HMAC, replay protection, idempotencia → consulta MP con token del tenant
6. Si `approved` → `transition_booking_status(booking, "confirmed")` + crea `NotificationOutbox` (tipo `confirmation`) en **misma transacción**
7. Job `process_outbox` (cada 60s) envía WhatsApp via Meta Graph API
8. Job `process_reminders` (cada 5min) encola recordatorio 24h antes → outbox reminder
9. Job `process_deposit_expiration` (cada 1min) expira `pending` sin pago → libera slot
10. Job `process_mp_token_refresh` (diario) renueva tokens OAuth que vencen en <30 días

---

## 2. Modelo de datos (tablas + relaciones)

```
Tenant (1) ──────< Service
     │                │
     │                ├── duration_minutes, price, deposit_amount (nullable, default 30%)
     │                └── is_active
     │
     ├──< Staff
     │       └── is_active (default true)
     │
     ├──< BusinessHours
     │       ├── staff_id NULL = horario del negocio
     │       ├── staff_id = ID = horario del profesional (prioridad)
     │       ├── day_of_week (0=Lun..6=Dom)
     │       ├── start_time / end_time (Time, sin TZ)
     │       └── unique (tenant_id, staff_id, day_of_week)
     │
     ├──< Booking
     │       ├── service_id, staff_id (nullable)
     │       ├── client_name, client_phone (formato 549XXXXXXXXXX)
     │       ├── start_time / end_time (TIMESTAMPTZ)
     │       ├── price_at_booking (Decimal)
     │       ├── deposit_at_booking (Decimal, nullable) — snapshot de effective_deposit al crear
     │       ├── status: pending | confirmed | cancelled | expired | no_show | completed
     │       ├── idempotency_key (unique compuesto con tenant_id — `uq_booking_idempotency_key`)
     │       ├── reminder_sent (bool)
     │       └── Auditoría (Tarea 8):
     │           status_changed_at, status_changed_by,
     │           cancellation_reason, no_show_at, completed_at
     │
     ├──< Payment (1:N por Booking)
     │       ├── amount, method (mercado_pago), status
     │       ├── mp_payment_id, mp_preference_id, mp_checkout_url
     │       └── paid_at
     │
     ├──< ApiKey
     │       ├── key_hash (SHA-256, unique)
     │       ├── last_used_at (throttle 5min), revoked_at
     │
     ├──< NotificationOutbox
     │       ├── notification_type: confirmation | reminder
     │       ├── status: pending | sent | failed | cancelled
     │       └── retry_count, error_message
     │
     └──< ProcessedWebhookEvent (idempotencia MP)
             ├── event_id (PK), event_type, payload (JSON)
             ├── status: received | processing | processed | failed
             └── received_at, processed_at
```

**Constraints críticas:**
- `ExcludeConstraint excl_overlapping_bookings` en `booking`:
  ```sql
  EXCLUDE USING gist (
      tenant_id WITH =,
      COALESCE(staff_id, -1) WITH =,
      tstzrange(start_time, end_time) WITH &&
  ) WHERE (status IN ('pending', 'confirmed'));
  ```
  - Garantiza anti-solapamiento atómico a nivel motor (no en código)
  - `tenant_id` como primera dimensión = aislamiento cross-tenant automático
  - `COALESCE(staff_id, -1)` agrupa bookings sin staff en bucket ficticio
  - Solo `pending` y `confirmed` bloquean; `cancelled`/`expired`/`no_show`/`completed` liberan

---

## 3. Multi-tenancy (shared DB + tenant_id + constraints)

- **Una sola DB** (`saas_db`) para todos los tenants.
- **Tablas principales** (`tenant`, `service`, `staff`, `business_hours`, `booking`, `api_key`) tienen `tenant_id` con FK `ON DELETE CASCADE`. Las tablas auxiliares (`payment`, `notification_outbox`, `payment_events`) referencian el tenant indirectamente vía `booking_id`.
- **Aislamiento**: lógica de aplicación + constraints de DB.
- **API Key auth**: `X-Tenant-API-Key` → SHA-256 → lookup en `ApiKey` (índice único) → cache Redis 60s.
- **Panel web**: cookie `juturno_session` firmada HMAC-SHA256 con `tenant_id.session_version.expires_at.signature`.
- **Nunca** hacer queries sin filtrar `tenant_id` en endpoints autenticados.

---

## 4. Autenticación

### 4.1 API Key (`X-Tenant-API-Key`)
- Header `X-Tenant-API-Key` con key generada por CLI (256 bits, alta entropía).
- Hash: `SHA-256` determinístico (no bcrypt/argon2) → permite índice único en `ApiKey.key_hash`.
- Cache Redis: `auth:apikey:{hash}` → `tenant_id` (TTL 60s). Trade-off: key revocada puede seguir aceptándose hasta 60s.
- `last_used_at` actualizado con throttle (máx 1 vez cada 5 min).

### 4.2 Panel web (cookie firmada)
- Cookie `juturno_session` = `{tenant_id}.{session_version}.{expires_at}.{signature}`
- Firma: HMAC-SHA256 con `SECRET_KEY`.
- `session_version` en `Tenant` (default 1, `server_default`). Al cambiar password → `session_version += 1` → invalida **todas** las cookies activas instantáneamente (D-013).
- Validación: `hmac.compare_digest` (timing-safe), expiración, `tenant_id` existe, `session_version` coincide.
- Cookie: `HttpOnly`, `SameSite=Lax`, `Secure` en prod, `max_age=14d`.

### 4.3 CSRF (double-submit cookie)
- Token generado con `secrets.token_hex(32)` en GET que renderiza formulario.
- Cookie `csrf_token` (no HttpOnly, `SameSite=Lax`, `Secure` en prod, 2h).
- Campo oculto en formulario con mismo token.
- POST valida `hmac.compare_digest(form_token, cookie_token)`.

---

## 5. Slots y ventanas horarias

### 5.1 BusinessHours (horarios de atención)
- **Negocio**: `staff_id IS NULL` → aplica a todos los días sin horario de staff específico.
- **Profesional**: `staff_id = ID` → prioridad sobre horario del negocio.
- **Sin filas en tabla**: fallback estático `09:00-18:00` (no rompe página pública).
- **Con filas pero sin día**: día **cerrado** (lista vacía).
- Unique constraint: `(tenant_id, staff_id, day_of_week)`.

### 5.2 `resolve_day_windows(session, tenant_id, day_of_week, day_date, tenant_timezone, staff_id=None)`
Lógica de prioridad:
1. Si `staff_id` → busca `BusinessHours(tenant_id, staff_id, day_of_week)`.
   - Si hay → usa esas ventanas.
   - Si no hay → ¿tiene el staff **alguna** fila en cualquier día?
     - Sí → día cerrado (`[]`).
     - No → cae a horario general del negocio.
2. Horario general: `BusinessHours(tenant_id, staff_id=NULL, day_of_week)`.
   - Si hay → usa esas ventanas.
   - Si no hay → ¿tiene el negocio **alguna** fila general en cualquier día?
     - Sí → día cerrado (`[]`).
     - No → fallback `09:00-18:00`.

### 5.3 `calculate_available_slots(windows, bookings, duration_min, granularity_min=30)`
- `windows`: lista de `(window_start, window_end)` del día (puede ser mañana + tarde).
- `bookings`: lista de `(start, end)` de reservas `pending`/`confirmed` del día.
- **Mergea solapamientos** en bookings para simplificar búsqueda de gaps.
- **Alineación**: ancla la grilla al `window_start` de cada ventana (no a medianoche).
  - `offset = (gap_start - window_start).total_seconds()`
  - `remainder = offset % granularity_seconds`
  - Primer slot = `gap_start` si `remainder==0`, si no `gap_start + (granularity - remainder)`.
- Retorna `list[str]` formato `"HH:MM"` en orden cronológico.
- **Nota**: diseñado para un día de un tenant/staff (pocas ventanas, pocas reservas). No llamar con cientos de ventanas.

### 5.4 Filtro de slots pasados (hoy)
- En endpoints de slots: si `day == today_local` (en TZ del tenant), filtra slots `< now_local`.

---

## 6. Anti-solapamiento (ExcludeConstraint)

- **PostgreSQL `EXCLUDE USING gist`** + extensión `btree_gist` + `tstzrange`.
- Constraint vive en migración `9a1b2c3d4e5f` (filtro por status `pending/confirmed`; tenant_id agregado en `3c4d5e6f7a8b`):
  ```sql
  ALTER TABLE booking ADD CONSTRAINT excl_overlapping_bookings
  EXCLUDE USING gist (
      tenant_id WITH =,
      (COALESCE(staff_id, -1)) WITH =,
      tstzrange(start_time, end_time) WITH &&
  ) WHERE (status IN ('pending', 'confirmed'));
  ```
- **Ventajas**: atómico bajo concurrencia extrema, declarativo, versionado, cross-tenant safe.
- **Race conditions imposibles**: la DB rechaza el INSERT con `IntegrityError` (exclusion violation).
- Endpoints capturan `IntegrityError` → 409 "Slot ya reservado o superpuesto" (o recuperan por idempotency_key).

---

## 7. Máquina de estados Booking (`app/booking_actions.py`)

```python
VALID_TRANSITIONS = {
    "pending":   {"confirmed", "cancelled", "expired"},
    "confirmed": {"cancelled", "no_show", "completed"},
    "expired":   {"confirmed"},          # webhook MP puede confirmar pago tardío
    "cancelled": set(),
    "no_show":   set(),
    "completed": set(),
}

REQUIRE_STARTED = {"no_show", "completed"}  # solo si start_time <= now
```

**Auditoría (Tarea 8):**
- `status_changed_at` (TIMESTAMPTZ), `status_changed_by` (actor: `"owner"|"system"|"webhook_mp"`)
- `cancellation_reason` (solo si `cancelled`)
- `no_show_at`, `completed_at` (TIMESTAMPTZ)
- Al cancelar: marca `NotificationOutbox` pendientes del booking como `cancelled` (`error_message="booking_cancelled"`).

**NO hace commit**: el caller decide cuándo `await session.commit()` (permite agrupar con otras operaciones).

---

## 8. Patrón Outbox (`NotificationOutbox` + `process_outbox`)

**Tabla `notification_outbox`:**
- `booking_id`, `notification_type` (`confirmation`|`reminder`), `status` (`pending`|`sent`|`failed`|`cancelled`)
- `retry_count`, `error_message`, `created_at`

**Flujo:**
1. Al crear booking (`POST /bookings` o `/public/bookings`) → inserta `Booking` (y en flujo público también `Payment`). **No** se crea `NotificationOutbox` acá.
2. Al confirmar por webhook MP (`approved`) → marca `confirmed` + crea `NotificationOutbox(type="confirmation")` si no existe — **misma transacción** que la confirmación.
3. Job `process_reminders` (cada 5min) → busca bookings `confirmed` con `start_time` en ~24h y `reminder_sent=False` → marca `reminder_sent=True` + crea `NotificationOutbox(type="reminder")` en **lote atómico**.
4. Job `process_outbox` (cada 60s) → `SELECT ... FOR UPDATE SKIP LOCKED` → por cada evento:
   - Carga booking + tenant (para timezone)
   - `WhatsAppService.send_confirmation()` o `send_reminder()` (template Meta Utility)
   - Si OK → `status="sent"`; si falla (cualquier excepción) → `status="failed"`, `retry_count+=1`, `error_message=exc`
   - **Commit por evento**: cada evento en su propia transacción; un error no afecta a los demás.
   - **Reintentos**: los `failed` con `retry_count < 7` y menos de 2 h se reintentan a los `2^n - 1` min de encolados (1, 3, 7, 15, 31, 63). Ver D-022.

---

## 9. Scheduler (4 jobs, in-process + lock Redis / DB)

| Job | Frecuencia | Concurrencia | Qué hace |
|-----|------------|--------------|----------|
| `process_outbox` | 60s | `SELECT ... FOR UPDATE SKIP LOCKED` (DB) | Envía WhatsApp pendientes |
| `process_reminders` | 5 min | Redis `SET NX EX 30s` (`reminder-job-lock`) | Encola recordatorios 24h |
| `process_deposit_expiration` | 1 min | Redis `SET NX EX 30s` (`deposit-expiration-job-lock`) | Expira `pending` sin pago |
| `process_mp_token_refresh` | 24h | Redis `SET NX EX 30s` (`mp-token-refresh-job-lock`) | Renueva tokens OAuth <30 días |

- **Locks Redis**: `uuid4().hex` como valor, TTL 30s (seguridad anti-deadlock) — para 3 jobs.
- **Outbox**: usa `FOR UPDATE SKIP LOCKED` a nivel DB (no Redis lock) — evita doble procesamiento sin lock externo.
- **NO arranca** si `TEST_DATABASE_URL` está seteada (evita colisiones en tests — ver lifespan en `app/main.py`).
- Comparte `async_session_maker` con la app (sin IPC).

---

## 10. Mercado Pago

### 10.1 OAuth por tenant (`app/mp_connect.py`)
- **Flujo Authorization Code**:
  1. `POST /panel/mp/connect/start` (cookie + CSRF) → `mp_authorization_redirect(tenant_id)` genera `state` (nonce 32 bytes) → guarda en Redis `mp_connect_state:{state}` = `tenant_id` (string, TTL 600s, un solo uso) y setea la cookie HttpOnly `mp_oauth_state` = `state` (path `/mp/connect/callback`, max-age 600, SameSite=Lax, `Secure` en producción; `Domain` = host de `PUBLIC_BASE_URL` si el callback es subdominio de él, si no host-only).
  2. Responde 302 a la autorización de MP con `state`, `redirect_uri`, `client_id`.
  3. Dueño autoriza en MP → MP redirige a `GET /mp/connect/callback?code=...&state=...`
  4. Callback exige que la cookie `mp_oauth_state` coincida con el `state` (`hmac.compare_digest`), consume `state` (Redis `GETDEL` → un solo uso), canjea `code` por tokens en `POST /oauth/token`.
  5. Cifra `access_token` y `refresh_token` con **Fernet** (`MP_TOKEN_ENCRYPTION_KEY`) → guarda en `tenant.mp_access_token_enc`, `mp_refresh_token_enc`.
  6. Guarda `mp_user_id` (collector_id), `mp_alias` (nickname), `mp_token_expires_at = now + expires_in`. El `user_id` sale de la respuesta del canje o, si falta, de `GET /users/me`; si MP no devuelve ninguno no se guarda nada (antes se persistía el string `"None"`) y se redirige a `?mp=error`.
  7. Una cuenta MP pertenece a un solo negocio: índice único parcial `uq_tenant_mp_user_id` sobre `tenant(mp_user_id) WHERE mp_user_id IS NOT NULL` (migración `c7d8e9f0a1b2`; los `NULL` conviven). Si el `commit` del callback lanza `IntegrityError`, hace `rollback` (no se guardan tokens, el otro tenant no cambia) y redirige a `?mp=account_in_use` (D-021).
- **Única vía de conexión**: el panel (`GET /panel/settings` con cookie + `POST /panel/mp/connect/start` con cookie + CSRF). `GET /mp/connect/start` por API key se eliminó (404): una URL de autorización devuelta por API no tiene navegador al cual atar el state (account-linking). `GET`/`DELETE /tenants/me/mp` (API key) siguen.
- **Callback**: siempre 302 a `PUBLIC_BASE_URL/panel/settings?mp=connected|error|other_browser|account_in_use`. `account_in_use` = la cuenta MP ya está vinculada a otro tenant. `other_browser` = cookie ausente o distinta del state: no se consume el state ni se vincula nada. Siguen siendo 400 JSON (falta code/state, state inválido/vencido/usado) y 502 (falla el canje en MP). Ojo: si el host de `PUBLIC_BASE_URL` no es padre del host del callback, la cookie no llega y toda conexión termina en `other_browser`.
- **Desconexión desde el panel** (`POST /panel/mp/disconnect`): limpia en local `mp_access_token_enc`, `mp_refresh_token_enc`, `mp_token_expires_at`, `mp_user_id`, `mp_public_key` y `mp_alias`; no revoca la autorización en MP. Se bloquea (302 `?mp=pending`) si hay un turno `pending` con `Payment.mp_preference_id` y el plazo de seña (`created_at + deposit_expiration_minutes`, mismo criterio que `process_deposit_expiration`) no venció; con minutos `NULL` siempre bloquea, porque el webhook necesita el token para verificar el pago.
- **Regla de cobro (D-012)**: `resolve_mp_access_token(tenant)`:
  - Tenant con cuenta → su `access_token` descifrado.
  - Sandbox + sin cuenta → `MP_ACCESS_TOKEN` de la plataforma (dinero de prueba).
  - **Producción + sin cuenta → `None` → `POST /public/bookings` responde 422 `ERR_PAGO_NO_CONFIGURADO`**.
- **Refresh proactivo**: job diario renueva tokens con `mp_token_expires_at <= now + 30d` (`REFRESH_AHEAD_DAYS=30`). MP rota par completo (access + refresh).

### 10.2 Webhook MP (`app/mp_webhooks.py`)
- **Endpoint**: `POST /webhooks/mercadopago`
- **Verificaciones**:
  - HMAC SHA256: header `x-signature` = `ts=timestamp,v1=hmac` → `manifest = "id:{data_id};request-id:{x_request_id};ts:{ts};"` (`data_id` = `?data.id` del query en minúsculas; si no viene se omite `id:...;` y no se procesa nada; el `data.id` del body no está firmado y se ignora) → `hmac.compare_digest`.
  - Replay protection: `|now - ts| <= 300s` (5 min).
  - Idempotencia: tabla `payment_events` con `event_id` PK = `{data.id}:{x-request-id}` (solo valores firmados: reenviar un request firmado con otro `id` en el body no saltea el dedupe). Estados: `received` → `processing` → `processed`|`failed`. Reintentos legítimos (estado `processing`/`failed`) reprocesan.
- **Resolución de token** (`_resolve_token_for_payment`):
  - Payload MP trae `user_id` (collector_id) en raíz o en `data.user_id`.
  - Busca `Tenant.mp_user_id == user_id` → usa su token descifrado (a lo sumo una fila, por `uq_tenant_mp_user_id`).
  - Fallback: `None` → usa `MP_ACCESS_TOKEN` de la plataforma.
- **Auto-creación Payment**: si webhook `approved` y no existe `Payment` con ese `mp_payment_id` → crea con datos de MP (`transaction_amount`, `payment_method_id`, `date_approved`).
- **Guard de amount**: usa `booking.deposit_at_booking` si está seteado; si es `NULL` (bookings anteriores a la migración `55526fb8c0f9`) cae al fallback `effective_deposit(service.price, service.deposit_amount)`.
- **Confirmación booking**: si `approved` y booking en `pending` (o `expired` y slot libre) → `transition_booking_status(booking, "confirmed", actor="webhook_mp")` + outbox confirmation.

### 10.3 Creación de preferencia (`create_mp_preference`)
- Usa API `/checkout/preferences` (Checkout Pro, "legacy" pero estable).
- `external_reference = "booking-{id}"`, `notification_url = MP_NOTIFICATION_URL` (omitida si está vacía; obligatoria en prod), `back_urls` con `PUBLIC_BASE_URL/t/{slug}?booking={id}`.
- `checkout_url` = `sandbox_init_point` si `MP_SANDBOX=true`, sino `init_point`.
- Lanza `HTTPException 502` si MP rechaza → caller hace rollback del booking.

---

## 11. WhatsApp (Meta Business API)

- **Webhook**: `GET /webhooks/whatsapp` (verificación `hub.verify_token` → devuelve `hub.challenge`), `POST /webhooks/whatsapp` (HMAC `X-Hub-Signature-256` con `META_APP_SECRET`).
- **Plantillas Utility** (aprobadas por Meta, categoría "Utility", funcionan fuera de ventana 24h):
  - `booking_confirmation`: parámetros `{nombre, fecha, booking_id}`
  - `booking_reminder`: parámetros `{nombre, fecha}`
- **Envío**: `WhatsAppService` → `POST https://graph.facebook.com/v19.0/{phone_number_id}/messages`
  - Singleton `httpx.AsyncClient` (pool conexiones).
  - Retry con backoff exponencial (1s, 2s, 4s) en 429/5xx/timeout.
  - Loggea body completo en 4xx no-rate-limit.
- **Normalización teléfono para Meta** (`normalize_phone_for_meta`):
  - Meta **requiere** números argentinos móviles SIN el `9` (formato E.164 tradicional `54XXXXXXXXXX`).
  - `phone.py` normaliza a `549XXXXXXXXXX` (canónico AR) para guardar en DB.
  - `whatsapp_service.py:9-18` (`normalize_phone_for_meta`) remueve el `9` de `549...` antes de enviar a Meta.
  - Convención de integración: DB = `549...`, Meta = `54...`.

---

## 12. Estructura de archivos relevante

```
app/
├── main.py              — lifespan, APScheduler, middlewares, exception handlers, include_router()
├── limiter.py           — singleton Limiter de slowapi (separado para evitar imports circulares)
├── templates.py         — singleton Jinja2Templates (+ filtro Jinja `money`: "$ 18.000")
├── templates/           — plantillas Jinja2 (diseño neón: base.html responsive, landing.html standalone)
├── static/              — montado en /static (StaticFiles): fonts/ (Sora + DM Sans woff2 + fonts.css), landing/*.webp. Público sin auth
├── schemas.py           — schemas Pydantic compartidos: SlotQuery, AvailableSlotsResponse, BookingCreate
├── routers/
│   ├── public.py        — sin auth: GET /, GET /health, GET /public/*, POST /public/bookings, GET /t/{slug}
│   ├── auth.py          — formularios/cookies: GET+POST /register, GET+POST /login, POST /logout
│   ├── api.py           — API Key (X-Tenant-API-Key): /bookings/available-slots, POST /bookings, PATCH /tenants/me
│   └── panel.py         — cookie auth: GET /dashboard (resumen del día, próximos turnos, checklist; todo por tenant_id), GET+POST /panel/*
├── mp_connect.py        — OAuth MP: /mp/connect/callback, GET/DELETE /tenants/me/mp
├── mp_webhooks.py       — POST /webhooks/mercadopago
├── booking_actions.py   — máquina de estados booking (transition_booking_status)
├── services.py          — lógica de negocio (compute_available_slots, etc.)
├── config.py            — Settings (pydantic-settings), validación de env vars en startup
└── ...
```

**Regla de agrupación de routers**: el router se elige según el mecanismo de auth del endpoint:
- Sin auth → `routers/public.py`
- Cookie firmada (`juturno_session`) → `routers/panel.py` o `routers/auth.py`
- Header `X-Tenant-API-Key` → `routers/api.py`

---

## 13. Convenciones de código (obligatorias)

| Convención | Ejemplo |
|------------|---------|
| **Async everywhere** | `async def` en endpoints, servicios, jobs |
| **SQLModel** | Modelos = Pydantic + SQLAlchemy unificado |
| **No commit en servicios** | `session.add(obj)`; caller hace `await session.commit()` |
| **Excepciones específicas** | `InvalidTransitionError`, `BookingNotStartedError`, `MPTokenCryptoError`, `InvalidPhoneError` |
| **Logging** | `logger = logging.getLogger(__name__)` |
| **Timing-safe** | `hmac.compare_digest` para firmas/tokens |
| **Type hints** | `X | None`, `list[X]`, `dict[K, V]` (no `Optional`, `List`, `Dict`) |
| **Timezones** | `datetime.now(timezone.utc)` siempre; `zoneinfo.ZoneInfo(tenant.timezone)` para tenant |
| **Decimal** | `Decimal` para dinero (nunca `float`) |
| **Queries** | **Siempre** filtran `tenant_id` en endpoints autenticados |

---

## Ver también

- [`README.md`](README.md) — Quickstart, env vars, comandos
- [`DECISIONS.md`](DECISIONS.md) — 18 ADRs (D-001 a D-018)
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Deploy, migraciones, backups, CI
- [`API_REFERENCE.md`](API_REFERENCE.md) — 27 endpoints con schemas
- [`RUNBOOK.md`](RUNBOOK.md) — Incidentes y diagnóstico
- [`ONBOARDING.md`](ONBOARDING.md) — Setup y convenciones para dev nuevo
