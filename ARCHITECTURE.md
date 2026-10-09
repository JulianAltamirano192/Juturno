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
            │ whatsapp      │                   │ /mp/connect/    │                 │ /webhooks/      │
            │ /webhooks/    │                   │ callback        │                 │ whatsapp        │
            │ mercadopago   │                   │ /webhooks/      │                 │                 │
            └───────────────┘                   │ mercadopago     │                 └─────────────────┘
                                                └─────────────────┘
```

**Flujo de un booking típico:**
1. Cliente accede a `/t/{slug}` → ve servicios → elige slot → `POST /public/bookings`
2. Se crea `Booking` (status `pending`, con `deposit_at_booking` snapshoteado) + `Payment` (status `pending`, con `mp_checkout_url`) en **misma transacción**
3. Se crea preferencia MP con token del tenant (OAuth) → devuelve `payment_url`. Si el tenant no tiene MP conectado en producción: 422 `ERR_PAGO_NO_CONFIGURADO` y no se crea nada
4. Cliente paga en MP → MP envía webhook a `/webhooks/mercadopago`
5. Webhook valida HMAC, replay protection, idempotencia → consulta MP con token del tenant → `apply_payment_details` (guards de `collector_id`, moneda y monto)
6. Si `approved` → `transition_booking_status(booking, "confirmed")` + crea `NotificationOutbox` (tipo `confirmation`) en **misma transacción**
7. Job `process_outbox` (cada 1 min) envía WhatsApp via Meta Graph API
8. Job `process_reminders` (cada 5 min) encola recordatorio 24h antes → outbox reminder
9. Job `process_deposit_expiration` (cada 1 min) → antes de expirar un `pending` vencido busca en MP (token del tenant, `external_reference=booking-{id}`) un pago aprobado perdido y lo aplica con los guards del webhook (`apply_payment_details`); si no hay, expira y libera el slot; si MP no responde, lo deja `pending` hasta 1 h después del vencimiento y luego expira (D-023)
10. Job `process_mp_token_refresh` (cada 24 h) renueva tokens OAuth que vencen en <30 días

---

## 2. Modelo de datos (tablas + relaciones)

```
Tenant (1) ──────< Service
     │                │
     │                ├── duration_minutes, price, deposit_amount (nullable, default 30% del precio)
     │                ├── is_active
     │                └── CHECK ck_service_deposit_amount_non_negative (deposit_amount IS NULL OR >= 0)
     │
     ├──< Staff
     │       └── is_active (default true)
     │
     ├──< BusinessHours
     │       ├── staff_id NULL = horario del negocio
     │       ├── staff_id = ID = horario del profesional (prioridad)
     │       ├── day_of_week (0=Lun..6=Dom)
     │       ├── start_time / end_time (Time, sin TZ)
     │       └── unique uq_business_hours_tenant_staff_day (tenant_id, staff_id, day_of_week)
     │
     ├──< Booking
     │       ├── service_id, staff_id (nullable, ON DELETE SET NULL)
     │       ├── client_name, client_phone (formato 549XXXXXXXXXX)
     │       ├── start_time / end_time (TIMESTAMPTZ)
     │       ├── price_at_booking (Decimal)
     │       ├── deposit_at_booking (Decimal, nullable) — snapshot de effective_deposit al crear
     │       │     └── CHECK ck_booking_deposit_at_booking_non_negative (NULL o >= 0)
     │       ├── status: pending | confirmed | cancelled | expired | no_show | completed
     │       ├── idempotency_key (unique compuesto con tenant_id — `uq_booking_idempotency_key`)
     │       ├── reminder_sent (bool), created_at
     │       ├── EXCLUDE excl_overlapping_bookings (ver abajo)
     │       └── Auditoría (Tarea 8):
     │           status_changed_at, status_changed_by,
     │           cancellation_reason, no_show_at, completed_at
     │
     ├──< Payment (1:N por Booking)
     │       ├── amount, method (mercado_pago, o el payment_method_id de MP si lo creó el webhook), status
     │       ├── mp_payment_id (unique `uq_payment_mp_payment_id`), mp_preference_id, mp_checkout_url
     │       └── paid_at
     │
     ├──< ApiKey
     │       ├── key_hash (SHA-256, unique), label
     │       ├── last_used_at (throttle 5min), revoked_at
     │
     ├──< NotificationOutbox
     │       ├── notification_type: confirmation | reminder
     │       ├── status: pending | sent | failed | cancelled
     │       └── retry_count, error_message, created_at
     │
     └──< ProcessedWebhookEvent (tabla `payment_events`, idempotencia MP)
             ├── event_id (PK), booking_id (nullable), event_type, payload (JSON)
             ├── status: received | processing | processed | failed
             └── received_at, processed_at
```

El tenant guarda además: `slug` (único), `timezone` (default `America/Argentina/Buenos_Aires`; no se puede cambiar desde el panel), `deposit_expiration_minutes` (default 15, `NULL` = sin expiración), `owner_email` (único) + `password_hash` (PBKDF2), `session_version` y las credenciales MP (`mp_user_id`, `mp_alias`, `mp_public_key`, `mp_access_token_enc`, `mp_refresh_token_enc`, `mp_token_expires_at`; los tokens van cifrados con Fernet).

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
- `uq_booking_idempotency_key` sobre `(tenant_id, idempotency_key)`: la misma clave en dos tenants distintos no choca (migración `b0e5b8028ae7`).
- `uq_tenant_mp_user_id`: índice único **parcial** sobre `tenant(mp_user_id) WHERE mp_user_id IS NOT NULL` (migración `c7d8e9f0a1b2`). Una cuenta MP pertenece a un solo negocio; los `NULL` conviven (D-021).
- `uq_payment_mp_payment_id` sobre `payment(mp_payment_id)`: red de seguridad contra dos webhooks concurrentes que inserten el mismo pago (migración `be7d31a422d8`).
- `ck_booking_deposit_at_booking_non_negative` y `ck_service_deposit_amount_non_negative`: la seña nunca es negativa (migraciones `55526fb8c0f9` y `346cd2b92a65`).

---

## 3. Multi-tenancy (shared DB + tenant_id + constraints)

- **Una sola DB** (`saas_db`) para todos los tenants.
- **Tablas principales** (`service`, `staff`, `business_hours`, `booking`, `api_key`) tienen `tenant_id` con FK `ON DELETE CASCADE`. Las tablas auxiliares (`payment`, `notification_outbox`, `payment_events`) referencian el tenant indirectamente vía `booking_id` (`payment_events.booking_id` es nullable: se completa cuando el webhook vincula el pago a un booking).
- **Aislamiento**: lógica de aplicación + constraints de DB.
- **API Key auth**: `X-Tenant-API-Key` → SHA-256 → lookup en `ApiKey` (índice único) → cache Redis 60s.
- **Panel web**: cookie `juturno_session` firmada HMAC-SHA256 con `tenant_id.session_version.expires_at.signature`.
- ❌ **Nunca** hacer queries sin filtrar `tenant_id` en endpoints autenticados.

---

## 4. Autenticación

### 4.1 API Key (`X-Tenant-API-Key`)
- Header `X-Tenant-API-Key` con key generada por CLI (`python -m app.cli create-api-key | list-api-keys | revoke-api-key`; 256 bits, alta entropía).
- Hash: `SHA-256` determinístico (no bcrypt/argon2) → permite índice único en `ApiKey.key_hash`.
- Cache Redis: `auth:apikey:{hash}` → `tenant_id` (TTL 60s). Trade-off: key revocada puede seguir aceptándose hasta 60s.
- `last_used_at` actualizado con throttle (máx 1 vez cada 5 min).
- Key inexistente o revocada: 401 `Invalid API key` (mensaje genérico, no distingue los casos).

### 4.2 Panel web (cookie firmada)
- Cookie `juturno_session` = `{tenant_id}.{session_version}.{expires_at}.{signature}`
- Firma: HMAC-SHA256 con `SECRET_KEY`.
- `session_version` en `Tenant` (default 1, `server_default`). La dependencia `get_current_tenant_from_session` compara el valor de la cookie con el de la DB; si difiere, redirige a `/login` y borra la cookie. Incrementarlo invalida **todas** las cookies activas (D-013). ⚠️ Hoy ningún código lo incrementa: no hay cambio de contraseña ni "cerrar sesión en todos lados"; `POST /logout` solo borra la cookie del navegador.
- Validación: `hmac.compare_digest` (timing-safe), expiración, `tenant_id` existe, `session_version` coincide.
- Cookie: `HttpOnly`, `SameSite=Lax`, `Secure` en prod, `max_age=14d`.
- Contraseñas: PBKDF2-HMAC-SHA256, 600.000 iteraciones (`app/password.py`); mínimo 8 caracteres al registrarse. Solo se verifica en login/registro, nunca por request.
- Redirect post-login: `sanitize_next_url` acepta solo rutas internas (evita open redirect).

### 4.3 CSRF (double-submit cookie)
- Token generado con `secrets.token_hex(32)` en GET que renderiza formulario.
- Cookie `csrf_token` (no HttpOnly, `SameSite=Lax`, `Secure` en prod, 14 días como la sesión). Los GET reusan el token de la cookie si tiene el formato válido (64 hex), así abrir otra página no invalida formularios de otras pestañas. El login exitoso emite un token nuevo y `/logout` borra la cookie, para que no pase de un usuario a otro en un navegador compartido.
- Campo oculto `csrf_token` en formulario con mismo token.
- POST valida `hmac.compare_digest(form_token, cookie_token)`: `validate_csrf` (panel y `/logout`) lee el body del form y responde 403 si falta la cookie o no coincide; `/register` y `/login` usan `validate_csrf_double_submit` y re-renderizan el formulario con error.
- Solo aplica a formularios del panel. Los endpoints con API key y los webhooks no usan CSRF (se autentican por header/firma).

### 4.4 Rate limiting (slowapi)
- Singleton `limiter` en `app/limiter.py`, clave = IP del cliente (`get_remote_address`), handler `RateLimitExceeded` → 429.
- Límites: `POST /login` 10/min, `POST /register` 5/min, `POST /public/bookings` 20/min.
- Se desactiva cuando `TEST_DATABASE_URL` está seteada (los tests de `test_rate_limiting.py` lo reactivan a propósito).
- ⚠️ Storage en memoria del proceso (sin `storage_uri`): el contador no se comparte entre réplicas ni sobrevive a un restart.
- ⚠️ Detrás de Traefik, `get_remote_address` solo ve la IP real si uvicorn confía en el proxy. El comando de prod usa `--proxy-headers`, pero sin `--forwarded-allow-ips=<IP_Traefik>` (pendiente de configurar en Coolify) puede ver la IP del proxy y limitar a todos juntos.

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

### 5.3 `compute_available_slots(*, session, tenant_id, service, day, staff_id, tenant_timezone)`
Lógica única compartida (D-017) por `GET /bookings/available-slots` (API key) y `GET /public/available-slots` (público); cada endpoint valida auth y tenant antes de llamarla.
1. Resuelve las ventanas del día con `resolve_day_windows`; si no hay, devuelve `[]`.
2. Trae los bookings `pending`/`confirmed` del tenant que pisan el rango `[min(inicio), max(fin)]` de las ventanas (filtra por `staff_id` si viene).
3. Pasa todo a `calculate_available_slots` con grilla de 30 min.
4. Si `day` es hoy en la TZ del tenant, descarta slots anteriores a `now_local`.

### 5.4 `calculate_available_slots(windows, bookings, duration_min, granularity_min=30)`
- `windows`: lista de `(window_start, window_end)` del día (puede ser mañana + tarde).
- `bookings`: lista de `(start, end)` de reservas `pending`/`confirmed` del día.
- **Mergea solapamientos** en bookings para simplificar búsqueda de gaps.
- **Alineación**: ancla la grilla al `window_start` de cada ventana (no a medianoche).
  - `offset = (gap_start - window_start).total_seconds()`
  - `remainder = offset % granularity_seconds`
  - Primer slot = `gap_start` si `remainder==0`, si no `gap_start + (granularity - remainder)`.
- Retorna `list[str]` formato `"HH:MM"` en orden cronológico.
- **Nota**: diseñado para un día de un tenant/staff (pocas ventanas, pocas reservas). No llamar con cientos de ventanas.
- Los slots son una sugerencia: la garantía real contra doble reserva es el constraint de la sección 6.

---

## 6. Anti-solapamiento (ExcludeConstraint)

- **PostgreSQL `EXCLUDE USING gist`** + extensión `btree_gist` + `tstzrange`.
- Constraint vive en migración `9a1b2c3d4e5f` (filtro por status `pending/confirmed`; tenant_id agregado en `3c4d5e6f7a8b`) y está declarado en `Booking.__table_args__`:
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
- Endpoints capturan `IntegrityError` → recuperan por `(tenant_id, idempotency_key)` (200 "Reserva recuperada (idempotente)") o, si no existe, 409 "Slot ya reservado o superpuesto".
- Un `expired` que se reconfirma por pago tardío vuelve a competir por el horario: el webhook chequea antes con `_slot_still_free` (misma semántica que el constraint) y solo confirma si sigue libre.

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

- ❌ No cambiar `booking.status` a mano: siempre `transition_booking_status(session, booking, new_status, actor, reason)`.
- Transición inválida → `InvalidTransitionError`; `no_show`/`completed` antes de `start_time` → `BookingNotStartedError`. Los endpoints del panel (`/panel/agenda/{id}/confirm|cancel|no-show|complete`) los traducen a 409.
- Quién la llama: panel (`actor="owner"`), webhook MP y reconciliación (`actor="webhook_mp"`), scheduler (`actor="system"`, solo `expired`).

**Auditoría (Tarea 8):**
- `status_changed_at` (TIMESTAMPTZ), `status_changed_by` (actor: `"owner"|"system"|"webhook_mp"`)
- `cancellation_reason` (solo si `cancelled`)
- `no_show_at`, `completed_at` (TIMESTAMPTZ)
- Al cancelar: marca todos los `NotificationOutbox` del booking en `pending` o `failed` (estos últimos los reintenta `process_outbox`) como `cancelled` (`error_message="booking_cancelled"`), para no mandar un WhatsApp después de cancelar (D-030).

**NO hace commit**: el caller decide cuándo `await session.commit()` (permite agrupar con otras operaciones).

---

## 8. Patrón Outbox (`NotificationOutbox` + `process_outbox`)

**Tabla `notification_outbox`:**
- `booking_id`, `notification_type` (`confirmation`|`reminder`), `status` (`pending`|`sent`|`failed`|`cancelled`)
- `retry_count`, `error_message`, `created_at`

**Flujo:**
1. Al crear booking (`POST /bookings` o `/public/bookings`) → inserta `Booking` (y en flujo público también `Payment`). **No** se crea `NotificationOutbox` acá.
2. Al confirmar por webhook MP o por reconciliación (`approved`) → marca `confirmed` + crea `NotificationOutbox(type="confirmation")` si no existe — **misma transacción** que la confirmación. Una confirmación manual desde el panel (`/panel/agenda/{id}/confirm`) no encola nada.
3. Job `process_reminders` (cada 5 min) → busca bookings `confirmed` con `start_time` en la ventana [+24 h, +24 h 5 min] y `reminder_sent=False` → marca `reminder_sent=True` + crea `NotificationOutbox(type="reminder")` en **lote atómico** (un solo commit).
4. Job `process_outbox` (cada 1 min) → lista los ids elegibles y procesa cada uno en su propia sesión/transacción con `SELECT ... FOR UPDATE SKIP LOCKED`:
   - Carga booking + tenant (para timezone)
   - `WhatsAppService.send_confirmation()` o `send_reminder()` (template Meta Utility)
   - El envío corre dentro de un savepoint (`begin_nested`): si falla la base adentro, igual se puede marcar el evento `failed`.
   - Si OK → `status="sent"`; si falla (cualquier excepción) → `status="failed"`, `retry_count+=1`, `error_message=exc`
   - **Commit por evento**: un error (de Meta o de datos, ej. timezone inválida) no afecta a los demás ni deja un "mensaje veneno" que rompa el lote.
   - **Reintentos (D-022)**: un `failed` es elegible si `retry_count < MAX_OUTBOX_ATTEMPTS = 7`, tiene menos de 2 h desde `created_at` y ya pasaron `2^retry_count - 1` minutos desde `created_at` (reintentos a los 1, 3, 7, 15, 31, 63 min). Los `failed` más viejos de 2 h no se reenvían.
   - Entrega *at-least-once*: un timeout con mensaje ya aceptado por Meta puede duplicarlo en el reintento.

---

## 9. Scheduler (4 jobs, in-process + lock Redis / DB)

Registrados en el `lifespan` de `app/main.py` con `AsyncIOScheduler` (trigger `interval`).

| Job | Frecuencia | Concurrencia | Qué hace |
|-----|------------|--------------|----------|
| `process_outbox` (id `outbox_job`) | 1 min | `SELECT ... FOR UPDATE SKIP LOCKED` por evento (DB) | Envía WhatsApp pendientes y reintenta `failed` con backoff |
| `process_reminders` (id `reminder_job`) | 5 min | Redis `SET NX EX 30s` (`reminder-job-lock`) | Encola recordatorios 24h |
| `process_deposit_expiration` (id `deposit_expiration_job`) | 1 min | Redis `SET NX EX 300s` (`deposit-expiration-job-lock`) + `FOR UPDATE SKIP LOCKED` por reserva | Reconcilia con MP y expira `pending` sin pago (una transacción por reserva) |
| `process_mp_token_refresh` (id `mp_token_refresh_job`) | 24 h (1440 min) | Redis `SET NX EX 30s` (`mp-token-refresh-job-lock`) | Renueva tokens OAuth <30 días |

- **Locks Redis**: `uuid4().hex` como valor, TTL de seguridad anti-deadlock; se libera en `finally` solo si el valor sigue siendo el propio. El TTL de expiración es 300 s (no 30 s) porque cubre las llamadas a MP del lote.
- **Outbox**: usa `FOR UPDATE SKIP LOCKED` a nivel DB (no Redis lock) — evita doble procesamiento sin lock externo.
- **Expiración**: solo considera tenants con `deposit_expiration_minutes` no nulo; el deadline es `Booking.created_at + deposit_expiration_minutes`. Las llamadas a MP van antes de bloquear la fila; después se re-chequea `pending` y se aplica o expira. Si MP/token fallan, la reserva espera hasta `RECONCILE_GRACE` (1 h) tras el deadline; después expira igual. Sin token (tenant sin MP en producción) expira sin consultar.
- **NO arranca** si `TEST_DATABASE_URL` está seteada (evita colisiones en tests — ver lifespan en `app/main.py`).
- Comparte `async_session_maker` con la app (sin IPC).
- ⚠️ Con más de 1 réplica los locks evitan el doble procesamiento, pero el scheduler corre en cada una. Escalar horizontalmente requiere un worker separado (D-005).

---

## 10. Mercado Pago

### 10.1 OAuth por tenant (`app/mp_connect.py`)
- **Flujo Authorization Code**:
  1. `POST /panel/mp/connect/start` (cookie + CSRF) → `mp_authorization_redirect(tenant_id)` (503 si falta `MP_MARKETPLACE_CLIENT_ID`) genera `state` (`secrets.token_urlsafe(32)`) → guarda en Redis `mp_connect_state:{state}` = `tenant_id` (string, TTL 600s, un solo uso) y setea la cookie HttpOnly `mp_oauth_state` = `state` (path `/mp/connect/callback`, max-age 600, SameSite=Lax, `Secure` en producción; `Domain` = host de `PUBLIC_BASE_URL` si el callback es subdominio de él, si no host-only).
  2. Responde 302 a la autorización de MP con `state`, `redirect_uri`, `client_id`.
  3. Dueño autoriza en MP → MP redirige a `GET /mp/connect/callback?code=...&state=...`
  4. Callback exige que la cookie `mp_oauth_state` coincida con el `state` (`hmac.compare_digest`), consume `state` (Redis `GETDEL` → un solo uso), canjea `code` por tokens en `POST /oauth/token`.
  5. Cifra `access_token` y `refresh_token` con **Fernet** (`MP_TOKEN_ENCRYPTION_KEY`) → guarda en `tenant.mp_access_token_enc`, `mp_refresh_token_enc`.
  6. Guarda `mp_user_id` (collector_id), `mp_alias` (nickname), `mp_token_expires_at = now + expires_in`. El `user_id` sale de la respuesta del canje o, si falta, de `GET /users/me`; si MP no devuelve ninguno no se guarda nada (antes se persistía el string `"None"`) y se redirige a `?mp=error`.
  7. Una cuenta MP pertenece a un solo negocio: índice único parcial `uq_tenant_mp_user_id` sobre `tenant(mp_user_id) WHERE mp_user_id IS NOT NULL` (migración `c7d8e9f0a1b2`; los `NULL` conviven). Si el `commit` del callback lanza `IntegrityError`, hace `rollback` (no se guardan tokens, el otro tenant no cambia) y redirige a `?mp=account_in_use` (D-021).
- **Única vía de conexión**: el panel (`GET /panel/settings` con cookie + `POST /panel/mp/connect/start` con cookie + CSRF). `GET /mp/connect/start` por API key se eliminó (404): una URL de autorización devuelta por API no tiene navegador al cual atar el state (account-linking, D-026). `GET`/`DELETE /tenants/me/mp` (API key) siguen.
- **Callback**: siempre 302 a `PUBLIC_BASE_URL/panel/settings?mp=connected|error|other_browser|account_in_use`. `account_in_use` = la cuenta MP ya está vinculada a otro tenant. `other_browser` = cookie ausente o distinta del state: no se consume el state ni se vincula nada. Si MP vuelve con `error=` (el dueño canceló), solo el navegador con la cookie correcta consume el state; igual se redirige a `?mp=error`. Siguen siendo 400 JSON (falta code/state, state inválido/vencido/usado), 404 (tenant del state no existe) y 502 (falla el canje en MP o no devuelve `access_token`). Ojo: si el host de `PUBLIC_BASE_URL` no es padre del host del callback, la cookie no llega y toda conexión termina en `other_browser`.
- **Desconexión desde el panel** (`POST /panel/mp/disconnect`, cookie + CSRF): limpia en local `mp_access_token_enc`, `mp_refresh_token_enc`, `mp_token_expires_at`, `mp_user_id`, `mp_public_key` y `mp_alias`; no revoca la autorización en MP. Se bloquea (302 `?mp=pending`) si hay un turno `pending` con `Payment.mp_preference_id` y el plazo de seña (`created_at + deposit_expiration_minutes`, mismo criterio que `process_deposit_expiration`) no venció; con minutos `NULL` siempre bloquea, porque el webhook necesita el token para verificar el pago. `DELETE /tenants/me/mp` (API key) desconecta sin ese chequeo.
- **Regla de cobro (D-012)**: `resolve_mp_access_token(tenant)`:
  - Tenant con cuenta → su `access_token` descifrado.
  - Sandbox + sin cuenta → `MP_ACCESS_TOKEN` de la plataforma (dinero de prueba).
  - **Producción + sin cuenta → `None` → `POST /public/bookings` responde 422 `ERR_PAGO_NO_CONFIGURADO`**.
  - Si el token no se puede descifrar (`MPTokenCryptoError`) → 502.
- **Refresh proactivo**: job diario renueva tokens con `mp_refresh_token_enc` no nulo y `mp_token_expires_at <= now + 30d` (`REFRESH_AHEAD_DAYS=30`). MP rota par completo (access + refresh). Si el refresh falla, devuelve `False` y la reconexión es manual. ⚠️ Tenants con `mp_token_expires_at IS NULL` (MP no mandó `expires_in`) quedan fuera del job (D-015, pendiente).

### 10.2 Webhook MP (`app/mp_webhooks.py`)
- **Endpoint**: `POST /webhooks/mercadopago`
- **Pre-chequeos**:
  - IPN viejo (`?topic=...` sin `data.id`): responde 200 `IPN_IGNORED` sin procesar (el mismo evento llega firmado como Webhook).
  - Body no-JSON o que no es un objeto: 400.
- **Verificaciones**:
  - HMAC SHA256: header `x-signature` = `ts=timestamp,v1=hmac` → `manifest = "id:{data_id};request-id:{x_request_id};ts:{ts};"` (`data_id` = `?data.id` del query en minúsculas; si no viene se omite `id:...;` y no se procesa nada: 200 `EVENT_IGNORED_NO_DATA_ID`; el `data.id` del body no está firmado y se ignora) → `hmac.compare_digest`. Firma inválida: 401.
  - Replay protection: `|now - ts| <= 300s` (5 min). Fuera de ventana: 403.
  - Idempotencia: tabla `payment_events` con `event_id` PK = `{data.id}:{x-request-id}` (solo valores firmados: reenviar un request firmado con otro `id` en el body no saltea el dedupe, D-027). Estados: `received` → `processing` → `processed`|`failed`. Un evento ya `processed` responde 200 `DUPLICATE_EVENT_IGNORED`; reintentos legítimos (estado `processing`/`failed`) reprocesan.
- **Resolución de token** (`_resolve_token_for_payment`):
  - Payload MP trae `user_id` (collector_id) en raíz o en `data.user_id`.
  - Busca `Tenant.mp_user_id == user_id` → usa su token descifrado (a lo sumo una fila, por `uq_tenant_mp_user_id`).
  - Fallback: `None` → usa `MP_ACCESS_TOKEN` de la plataforma.
  - El `user_id` no está firmado: si MP responde 404 (token equivocado, ID del simulador) el evento queda `failed` y responde 200 `PAYMENT_NOT_FOUND_ON_MP`; una entrega posterior con la misma clave se reprocesa.
- **`apply_payment_details(session, mp_payment_id, details)`**: la comparten el webhook y la reconciliación del scheduler. No commitea. Devuelve `(outcome, booking_id)`; `outcome` es `EVENT_PROCESSED` o el guard que frenó (`NO_BOOKING_LINKED`, `TENANT_MISMATCH`, `CURRENCY_NOT_SUPPORTED`, `AMOUNT_INSUFFICIENT`). Orden estricto, **todo antes de crear/actualizar el `Payment`** (D-019):
  1. `SELECT ... FOR UPDATE` del `Payment` por `mp_payment_id` y del `Booking` (serializa webhooks concurrentes del mismo pago; el UNIQUE cubre el INSERT simultáneo → `DuplicatePaymentError`, tratado como idempotente).
  2. **Guard de tenant (todos los estados)**: `collector_id` de la respuesta autenticada de MP debe ser igual a `tenant.mp_user_id` del booking. Producción + tenant sin `mp_user_id` → `TENANT_MISMATCH` (fail-closed); sandbox/dev sin `mp_user_id` → se omite.
  3. **Guard de moneda (solo `approved`)**: `currency_id` debe ser `ARS`.
  4. **Guard de monto (solo `approved`)**: `transaction_amount` finito (`is_finite()`) y `>= booking.deposit_at_booking`; si es `NULL` (bookings anteriores a la migración `55526fb8c0f9`) cae al fallback `effective_deposit(service.price, service.deposit_amount)`; si el servicio no existe, fail-closed.
- **Auto-creación Payment**: si no existe `Payment` con ese `mp_payment_id` → crea con datos de MP (`transaction_amount`, `payment_method_id`, `date_approved`). Si existe y cambió el estado, lo actualiza (y completa `paid_at` si pasó a `approved`).
- **Confirmación booking**: si `approved` y booking en `pending` (o `expired` y slot libre) → `transition_booking_status(booking, "confirmed", actor="webhook_mp")` + outbox confirmation (si no existe ya uno).
- **Cierre**: el endpoint marca el evento `processed` y commitea todo junto; ante excepción hace `rollback` y deja el evento `failed` para que MP reintente.

### 10.3 Creación de preferencia (`create_mp_preference`)
- Usa API `/checkout/preferences` (Checkout Pro, "legacy" pero estable).
- `external_reference = "booking-{id}"`, `unit_price` = seña (`deposit_at_booking`), `currency_id = ARS`, `notification_url = MP_NOTIFICATION_URL` (omitida si está vacía; obligatoria en prod y debe ser `https://.../webhooks/mercadopago`, D-029), `back_urls` con `PUBLIC_BASE_URL/t/{slug}?booking={id}` (más `result=failure|pending`) y `auto_return=approved`.
- `checkout_url` = `sandbox_init_point` si `MP_SANDBOX=true`, sino `init_point`.
- Lanza `HTTPException 502` si MP rechaza o da timeout → caller hace rollback del booking.

---

## 11. WhatsApp (Meta Business API)

- **Webhook**: `GET /webhooks/whatsapp` (verificación `hub.verify_token` → devuelve `hub.challenge`), `POST /webhooks/whatsapp` (HMAC `X-Hub-Signature-256` con `META_APP_SECRET`; si la variable está vacía, solo posible fuera de producción, la firma no se chequea). Hoy solo loguea mensajes entrantes y estados; siempre responde 200 salvo firma inválida.
- **Plantillas Utility** (aprobadas por Meta, categoría "Utility", funcionan fuera de ventana 24h):
  - `booking_confirmation`: parámetros `{nombre, fecha, booking_id}`
  - `booking_reminder`: parámetros `{nombre, fecha}`
- **Envío**: `WhatsAppService` → `POST https://graph.facebook.com/v19.0/{phone_number_id}/messages`
  - Singleton `httpx.AsyncClient` (pool conexiones, timeout 10 s).
  - Hasta 3 intentos con backoff exponencial (espera 1 s y 2 s entre ellos) en 429/5xx/timeout; el tercer fallo propaga la excepción y `process_outbox` marca el evento `failed`.
  - Loggea body completo en 4xx no-rate-limit; `_send_event` hace `raise_for_status()` sobre la respuesta, así que un 4xx también termina en `failed`.
- **Normalización teléfono para Meta** (`normalize_phone_for_meta`):
  - Meta **requiere** números argentinos móviles SIN el `9` (formato E.164 tradicional `54XXXXXXXXXX`).
  - `phone.py` (`normalize_whatsapp_phone`) normaliza a `549XXXXXXXXXX` (canónico AR) para guardar en DB; teléfono inválido → 422.
  - `whatsapp_service.py` (`normalize_phone_for_meta`) remueve el `9` de `549...` antes de enviar a Meta.
  - Convención de integración: DB = `549...`, Meta = `54...`.

---

## 12. Estructura de archivos relevante

```
app/
├── main.py              — lifespan, APScheduler, Sentry, CORS, middlewares, exception handlers, include_router()
├── config.py            — Settings (pydantic-settings), validación de env vars críticas en producción (model_post_init)
├── database.py          — engine async + async_session_maker + get_db
├── limiter.py           — singleton Limiter de slowapi (separado para evitar imports circulares)
├── templates.py         — singleton Jinja2Templates (+ filtro Jinja `money`: "$ 18.000")
├── templates/           — plantillas Jinja2 (diseño neón: base.html responsive, landing.html standalone)
├── static/              — montado en /static (StaticFiles): fonts/ (Sora + DM Sans woff2 + fonts.css), landing/*.webp. Público sin auth
├── schemas.py           — schemas Pydantic compartidos: SlotQuery, AvailableSlotsResponse, BookingCreate
├── models.py            — modelos SQLModel y constraints
├── routers/
│   ├── public.py        — sin auth: GET /, GET /health, GET /public/*, POST /public/bookings, GET /t/{slug}
│   ├── auth.py          — formularios/cookies: GET+POST /register, GET+POST /login, POST /logout
│   ├── api.py           — API Key (X-Tenant-API-Key): /bookings/available-slots, POST /bookings, PATCH /tenants/me
│   └── panel.py         — cookie auth: GET /dashboard (resumen del día, próximos turnos, checklist; todo por tenant_id), GET+POST /panel/* (settings + MP, servicios, staff, horarios, agenda)
├── mp_connect.py        — OAuth MP: /mp/connect/callback, GET/DELETE /tenants/me/mp, refresh de tokens, resolve_mp_access_token
├── mp_webhooks.py       — POST /webhooks/mercadopago, apply_payment_details, create_mp_preference
├── mp_crypto.py         — cifrado Fernet de tokens MP (MPTokenCryptoError)
├── webhooks.py          — GET/POST /webhooks/whatsapp
├── whatsapp_service.py  — cliente Meta Graph API + normalize_phone_for_meta
├── outbox_worker.py     — process_outbox (commit por evento, reintentos)
├── scheduler.py         — process_reminders, process_deposit_expiration, process_mp_token_refresh
├── booking_actions.py   — máquina de estados booking (transition_booking_status)
├── services.py          — lógica de negocio (effective_deposit, resolve_day_windows, calculate_available_slots, compute_available_slots)
├── auth.py              — dependencias get_current_tenant (API key) y get_current_tenant_from_session (cookie)
├── session.py / csrf.py / password.py — cookie firmada, CSRF double-submit, PBKDF2
├── phone.py / slug.py   — normalización de teléfono AR, slugs únicos
└── cli.py               — python -m app.cli create-api-key | list-api-keys | revoke-api-key
```

**Regla de agrupación de routers** (D-028): el router se elige según el mecanismo de auth del endpoint:
- Sin auth → `routers/public.py`
- Cookie firmada (`juturno_session`) → `routers/panel.py` o `routers/auth.py`
- Header `X-Tenant-API-Key` → `routers/api.py`

Los routers de `mp_connect.py`, `mp_webhooks.py` y `webhooks.py` viven junto a su integración. La suite tiene 313 tests en 31 archivos (`tests/`), que corren con `./scripts/test.sh`.

---

## 13. Convenciones de código (obligatorias)

| Convención | Ejemplo |
|------------|---------|
| **Async everywhere** | `async def` en endpoints, servicios, jobs |
| **SQLModel** | Modelos = Pydantic + SQLAlchemy unificado |
| **No commit en servicios** | `session.add(obj)`; caller hace `await session.commit()` |
| **Excepciones específicas** | `InvalidTransitionError`, `BookingNotStartedError`, `MPTokenCryptoError`, `InvalidPhoneError`, `DuplicatePaymentError` |
| **Logging** | `logger = logging.getLogger(__name__)` |
| **Timing-safe** | `hmac.compare_digest` para firmas/tokens |
| **Type hints** | `X | None`, `list[X]`, `dict[K, V]` (no `Optional`, `List`, `Dict`) |
| **Timezones** | `datetime.now(timezone.utc)` siempre; `zoneinfo.ZoneInfo(tenant.timezone)` para tenant |
| **Decimal** | `Decimal` para dinero (nunca `float`) |
| **Queries** | **Siempre** filtran `tenant_id` en endpoints autenticados |

---

## Ver también

- [`README.md`](README.md) — Quickstart, env vars, comandos
- [`DECISIONS.md`](DECISIONS.md) — ADRs (D-001 a D-030)
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Deploy, migraciones, backups, CI
- [`API_REFERENCE.md`](API_REFERENCE.md) — Endpoints con schemas
- [`RUNBOOK.md`](RUNBOOK.md) — Incidentes y diagnóstico
- [`ONBOARDING.md`](ONBOARDING.md) — Setup y convenciones para dev nuevo
