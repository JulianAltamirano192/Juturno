# Referencia de API

> Especificación endpoint por endpoint. Para integradores y devs que consumen o extienden la API.

---

## 1. Autenticación

### 1.1 API Key (`X-Tenant-API-Key`)
- **Header**: `X-Tenant-API-Key: <key_plana>`
- **Key**: 256 bits generada por CLI, alta entropía.
- **Validación**: SHA-256 determinístico → lookup en `ApiKey.key_hash` (índice único) → cache Redis 60s.
- **Errores**: 401 "Falta el header X-Tenant-API-Key" | 401 "Invalid API key" (genérico: no distingue inexistente vs revocada).

### 1.2 Panel web (cookie firmada)
- **Cookie**: `juturno_session={tenant_id}.{session_version}.{expires_at}.{signature}`
- **Firma**: HMAC-SHA256 con `SECRET_KEY`.
- **Expiración**: 14 días (`SESSION_MAX_AGE_SECONDS`).
- **Invalidación**: cambiar `Tenant.session_version` (al cambiar password) invalida todas las cookies.
- **Dependencia**: `get_current_tenant_from_session` → lanza `RedirectToLoginException` (303 → `/login?next=...`).

### 1.3 CSRF (formularios panel)
- **Cookie**: `csrf_token` (no HttpOnly, SameSite=Lax, Secure en prod, 2h).
- **Campo formulario**: `<input type="hidden" name="csrf_token" value="...">`
- **Dos flujos de validación**:
  - **`/register` y `/login` (double-submit completo)**: `validate_csrf_double_submit` compara form vs cookie con `hmac.compare_digest` → si falla, **400** con formulario re-renderizado (nuevo token) y mensaje de error.
  - **Endpoints `/panel/*`**: `validate_csrf` lee el form body y compara el token contra la cookie `csrf_token` con `hmac.compare_digest` (double-submit) → **403** si falta o no coincide.

---

## 2. Convenciones globales

| Convención | Detalle |
|------------|---------|
| **Idempotencia** | `idempotency_key` (string, **UNIQUE compuesto `(tenant_id, idempotency_key)`** — `uq_booking_idempotency_key`) en `POST /bookings` y `POST /public/bookings`. Un retry con la misma key del mismo tenant → 200 + booking existente. Keys de distintos tenants no colisionan. |
| **Timestamps** | ISO 8601 UTC: `2026-10-15T14:30:00+00:00` o `2026-10-15T14:30:00Z`. |
| **Decimales** | DB y Python usan `Decimal` (`Numeric(10,2)`). Las responses públicas serializan montos como **float JSON** (`price: float`, `deposit_amount: float`) — los schemas actuales (`PublicServiceRead`, `BookingCreate`) declaran `float`. |
| **Errores** | `{ "detail": "mensaje en español" }` (4xx) o `{ "detail": "interno" }` (5xx). |
| **Códigos** | 200 OK, 201 Created, 303 See Other (redirects), 400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found, 409 Conflict, 422 Unprocessable Entity, 502 Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout. |
| **Fechas en queries** | `day=YYYY-MM-DD` (ej: `day=2026-10-15`). |

---

## 3. Endpoints — Panel (HTML, cookie auth + CSRF)

| Método | Path | Descripción | Auth |
|--------|------|-------------|------|
| GET | `/register` | Formulario registro negocio | — |
| POST | `/register` | Crear tenant + owner (valida CSRF, pwd ≥8, email único, slug único) | CSRF |
| GET | `/login` | Formulario login (redirige a `/dashboard` si sesión válida) | — |
| POST | `/login` | Validar credenciales, setear cookie `juturno_session` | CSRF |
| POST | `/logout` | Borrar cookie sesión | — |
| GET | `/panel/settings` | Ajustes: estado de la conexión con Mercado Pago. `?mp=connected\|disconnected\|pending\|error\|other_browser\|account_in_use` muestra un mensaje fijo (nunca se refleja el valor del query) | Cookie |
| POST | `/panel/mp/connect/start` | Guarda el state en Redis, setea la cookie `mp_oauth_state` (HttpOnly) y responde 302 a MP. 503 si falta `MP_MARKETPLACE_CLIENT_ID` | Cookie + CSRF |
| POST | `/panel/mp/disconnect` | Borra localmente tokens y metadata MP → 302 `/panel/settings?mp=disconnected`. Bloqueado con 302 `?mp=pending` si hay un turno `pending` con `Payment.mp_preference_id` y plazo de seña vigente (`created_at + deposit_expiration_minutes`; minutos `NULL` = siempre bloquea). No revoca la autorización en MP | Cookie + CSRF |
| GET | `/dashboard` | Vista principal: resumen del día, próximos turnos, checklist de configuración y link público de reserva (oculto si el tenant no tiene slug). Todo filtrado por `tenant_id` | Cookie |
| GET | `/panel/services` | Listar servicios (activos/inactivos) | Cookie |
| GET | `/panel/services/new` | Formulario nuevo servicio | Cookie |
| POST | `/panel/services/new` | Crear servicio (valida CSRF, name, duration>0, price>0) | Cookie + CSRF |
| GET | `/panel/services/{service_id}/edit` | Formulario editar servicio (404 si no es del tenant) | Cookie |
| POST | `/panel/services/{service_id}/edit` | Actualizar servicio (valida CSRF) | Cookie + CSRF |
| POST | `/panel/services/{service_id}/toggle` | Activar/desactivar servicio | Cookie + CSRF |
| GET | `/panel/staff` | Listar staff | Cookie |
| GET | `/panel/staff/new` | Formulario nuevo staff | Cookie |
| POST | `/panel/staff/new` | Crear staff (name obligatorio) | Cookie + CSRF |
| GET | `/panel/staff/{staff_id}/edit` | Formulario editar staff (404 si no es del tenant) | Cookie |
| POST | `/panel/staff/{staff_id}/edit` | Actualizar staff (name obligatorio) | Cookie + CSRF |
| POST | `/panel/staff/{staff_id}/toggle` | Activar/desactivar staff | Cookie + CSRF |
| GET | `/panel/horarios` | Listar horarios del negocio (`staff_id IS NULL`) | Cookie |
| GET | `/panel/horarios/new` | Formulario nuevo horario | Cookie |
| POST | `/panel/horarios/new` | Crear horario (day_of_week 0-6, start<end; rechaza solapamiento con horario existente del día → re-renderiza form con error) | Cookie + CSRF |
| GET | `/panel/horarios/{bh_id}/edit` | Formulario editar horario (404 si no es del tenant) | Cookie |
| POST | `/panel/horarios/{bh_id}/edit` | Actualizar horario | Cookie + CSRF |
| POST | `/panel/horarios/{bh_id}/delete` | **Borrar** horario (404 si no existe o tiene `staff_id`) | Cookie + CSRF |
| GET | `/panel/agenda` | Vista día — todos los turnos que solapan el día (cualquier status), ordenados por inicio; `?day=YYYY-MM-DD` opcional (default hoy, TZ del tenant) | Cookie |
| POST | `/panel/agenda/{booking_id}/confirm` | `pending → confirmed` (409 si transición inválida); redirect 303 a agenda | Cookie + CSRF |
| POST | `/panel/agenda/{booking_id}/cancel` | `→ cancelled`; form field opcional `reason`; cancela outbox pendientes; 409 si inválida | Cookie + CSRF |
| POST | `/panel/agenda/{booking_id}/no-show` | `confirmed → no_show`; 409 si el turno aún no empezó o transición inválida | Cookie + CSRF |
| POST | `/panel/agenda/{booking_id}/complete` | `confirmed → completed`; 409 si el turno aún no empezó o transición inválida | Cookie + CSRF |

**Respuestas HTML**: `TemplateResponse` (Jinja2). Redirects: 303 See Other.

---

## 4. Endpoints — Público (sin auth)

| Método | Path | Descripción | Request | Response |
|--------|------|-------------|---------|----------|
| GET | `/public/tenants/{identifier}` | Info pública tenant + servicios activos (`identifier` = ID o slug) | — | `PublicTenantDetailResponse` |
| GET | `/public/available-slots` | Slots libres para servicio/día | Query: `tenant_id`, `service_id`, `day`, `staff_id?` | `AvailableSlotsResponse` |
| POST | `/public/bookings` | Crear booking `pending` + preferencia MP → `payment_url` | `BookingCreate` | `PublicBookingResponse` (201) |
| GET | `/t/{slug}` | Página HTML reserva (mobile-first) | — | `HTMLResponse` |
| GET | `/` | Landing de marketing (`landing.html`, standalone; la demo corre solo en el cliente, sin llamadas al backend). No aparece en OpenAPI | — | `HTMLResponse` |
| GET | `/static/*` | Assets estáticos de `app/static/` (fuentes Sora/DM Sans self-hosted en `fonts/`, imágenes `.webp` del landing). **Públicos sin auth: solo poner assets acá, nunca datos ni secrets** | — | archivo |

### 4.1 `GET /public/tenants/{identifier}`

**Response 200:**
```json
{
  "id": 1,
  "name": "Peluquería Ana",
  "slug": "peluqueria-ana",
  "timezone": "America/Argentina/Buenos_Aires",
  "services": [
    {
      "id": 1,
      "name": "Corte",
      "duration_minutes": 30,
      "price": 5000.0,
      "deposit_amount": 1500.0
    }
  ]
}
```

### 4.2 `GET /public/available-slots`

**Query params:**
- `tenant_id` (int, >0)
- `service_id` (int, >0)
- `day` (date, YYYY-MM-DD, no pasado)
- `staff_id` (int, >0, opcional)

**Response 200:**
```json
{
  "date": "2026-10-15",
  "service_duration_min": 30,
  "timezone": "America/Argentina/Buenos_Aires",
  "slots": ["09:00", "09:30", "10:00", "10:30", "14:00", "14:30"]
}
```

**Errores**: 400 "No se pueden consultar fechas pasadas", 404 "Tenant/Service not found".

### 4.3 `POST /public/bookings`

**Request (`BookingCreate`):**
```json
{
  "tenant_id": 1,
  "service_id": 1,
  "staff_id": null,
  "client_name": "Juan Pérez",
  "client_phone": "+54 9 11 5555 5555",
  "start_time": "2026-10-15T10:00:00-03:00",
  "idempotency_key": "uuid-v4-del-cliente"
}
```
- `end_time` opcional: se deriva de `service.duration_minutes`.
- El precio se toma siempre de `service.price`; no hay campo `price_at_booking` en el schema del cliente.
- Teléfono normalizado a `549XXXXXXXXXX` (`phone.py`). 422 si inválido.

**Response 201:**
```json
{
  "message": "Reserva creada",
  "booking_id": 42,
  "payment_url": "https://sandbox.mercadopago.com/checkout/preference/..."
}
```

**Errores:**
- 404 "Tenant/Service/Staff not found"
- 409 "Slot ya reservado o superpuesto" (ExcludeConstraint o colisión de `idempotency_key` del mismo tenant)
- 422 "El WhatsApp no parece completo..." (teléfono inválido)
- 422 `ERR_PAGO_NO_CONFIGURADO` (prod + tenant sin MP conectado)
- 502 "Error al procesar el cobro..." (`MPTokenCryptoError` al descifrar el token del tenant)
- 502 "Timeout creando preferencia en Mercado Pago" (timeout en `create_mp_preference`)
- 502 "Mercado Pago rechazó la preferencia: {status}" (error no-2xx de MP)

**Idempotencia**: mismo `idempotency_key` + `tenant_id` → 200 + booking existente + `payment_url` ya generada.

### 4.4 `GET /t/{slug}`

**Response 200**: HTML página reserva (renderiza `public_booking.html` con tenant + servicios).
**Response 404**: HTML "No encontramos ese negocio".

---

## 5. Endpoints — API Key (`X-Tenant-API-Key`)

| Método | Path | Descripción | Request | Response |
|--------|------|-------------|---------|----------|
| GET | `/bookings/available-slots` | Slots libres (valida `tenant_id == current_tenant.id`) | Query: `tenant_id`, `service_id`, `day`, `staff_id?` | `AvailableSlotsResponse` |
| POST | `/bookings` | Crear booking `pending` (deriva `end_time`) | `BookingCreate` | `{message, booking_id}` (201) |
| PATCH | `/tenants/me` | Actualizar `deposit_expiration_minutes` | `TenantSettingsUpdate` | `{tenant_id, deposit_expiration_minutes}` |

### 5.1 `GET /bookings/available-slots`

Igual que público pero **requiere API Key** y valida que `tenant_id` coincida con el de la key. 404 si no coincide (no revela existencia).

### 5.2 `POST /bookings`

**Request (`BookingCreate`):** igual que público pero sin MP (no genera preferencia).
- `end_time` se deriva de `service.duration_minutes`.
- Status inicial: `pending`.

**Response 201:** `{ "message": "Reserva creada", "booking_id": 42 }`

**Errores:** 401 (API key), 404, 409, 422 (teléfono).

### 5.3 `PATCH /tenants/me`

**Request:**
```json
{ "deposit_expiration_minutes": 30 }
```
- `null` desactiva expiración. `ge=1` si presente.

---

## 6. Endpoints — Webhooks

### 6.1 `GET /webhooks/whatsapp`

**Verificación Meta (handshake):**
- Query: `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge`
- Si `hub.verify_token == META_VERIFY_TOKEN` → 200 PlainText `hub.challenge`
- Else → 403

### 6.2 `POST /webhooks/whatsapp`

**Eventos Meta (WhatsApp Business Account):**
- **Mensajes entrantes**: `entry[].changes[].value.messages[]` → loggea `from`, `text.body`, `id`.
- **Status de mensajes**: `entry[].changes[].value.statuses[]` → loggea `id`, `status` (sent/delivered/read/failed).

**Headers:**
- `X-Hub-Signature-256: sha256=<hmac>` → validado con `META_APP_SECRET` + `hmac.compare_digest`.
- Body crudo para HMAC.

**Response**: Siempre 200 (`EVENT_RECEIVED` o `ERROR_PARSING_BUT_RECEIVED`) para que Meta no reintente.

### 6.3 `POST /webhooks/mercadopago`

**Webhook de pagos MP (Checkout Pro).**

**Headers:**
- `x-signature: ts=<timestamp>,v1=<hmac_sha256>`
- `x-request-id: <uuid>`

**Validaciones:**
1. HMAC: `manifest = "id:{data_id};request-id:{x_request_id};ts:{ts};"` → `hmac.compare_digest` con `MP_SECRET_KEY`. 401 si falla.
2. Replay: `|now - ts| <= 300s` (5 min). 403 si fuera de ventana.
3. Idempotencia: `payment_events.event_id` (PK). Estados: `received` → `processing` → `processed`|`failed`.

**Payload:** Soporta formatos viejo (`id`, `topic`) y nuevo (`data.id`, `type`, `action`).

**Procesamiento:**
- Extrae `data_id` (payment ID) → consulta MP con token resuelto (`_resolve_token_for_payment`):
  - Si payload trae `user_id` (collector_id, en raíz o en `data.user_id`) → busca `Tenant.mp_user_id` → usa token descifrado del tenant.
  - Si no matchea ningún tenant o falta → fallback a `MP_ACCESS_TOKEN` plataforma.
  - Si `decrypt_token` falla (`MPTokenCryptoError`) → la excepción se propaga: el evento queda `failed` y MP reintenta.
- Si pago `approved`:
  - Auto-crea `Payment` si no existe uno con ese `mp_payment_id` (con `transaction_amount`, `payment_method_id`, `date_approved`).
  - Si booking en `pending` (o `expired` y slot libre) → `transition_booking_status(booking, "confirmed", actor="webhook_mp")`.
  - Crea `NotificationOutbox(type="confirmation")` si no existe.
- Si el pago no está `approved`: el `Payment` se crea/actualiza con el status actual; el booking queda como está.

**Response codes:**
- 200 `EVENT_PROCESSED` / `DUPLICATE_EVENT_IGNORED` / `PAYMENT_NOT_FOUND_ON_MP` / `NO_BOOKING_LINKED` / `EVENT_IGNORED_NO_DATA_ID`
- 401 Firma inválida
- 403 Timestamp fuera de ventana
- 500 Error inesperado (incluye MP API rechazos no-404 y `MPTokenCryptoError`) — el evento queda `failed` y MP reintenta
- 504 Timeout consultando el pago a MP (`get_payment_details`)

---

## 7. Endpoints — MP OAuth (por tenant)

| Método | Path | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/mp/connect/callback` | — (público, cookie `mp_oauth_state`) | Callback OAuth: canjea `code` → tokens cifrados en tenant y responde 302 al panel |
| GET | `/tenants/me/mp` | API Key | Estado conexión MP (connected, mp_user_id, mp_alias, expires_at) |
| DELETE | `/tenants/me/mp` | API Key | Desconectar MP (borra tokens + metadata) |

### 7.1 Inicio de la conexión (solo desde el panel)

`GET /mp/connect/start` (API key) fue **eliminado** (hoy responde 404): una `authorization_url` devuelta por API no tiene un navegador al cual atar el `state` OAuth (account-linking). La única forma de conectar MP es `POST /panel/mp/connect/start` (cookie + CSRF, ver sección 3), que:
- Genera `state` (nonce 32 bytes) → Redis `mp_connect_state:{state}` = `tenant_id` como string (TTL 600s, un solo uso).
- Setea la cookie `mp_oauth_state` = `state` (HttpOnly, path `/mp/connect/callback`, max-age 600, SameSite=Lax, `Secure` en producción). `Domain` = host de `PUBLIC_BASE_URL` cuando el host del callback es un subdominio de ese host (p. ej. panel `juturno.com` + callback `api.juturno.com`); si no, cookie host-only.
- Responde 302 a la `authorization_url` de MP.

`GET /tenants/me/mp` y `DELETE /tenants/me/mp` (API key) siguen disponibles.

### 7.2 `GET /mp/connect/callback`

**Query:** `code`, `state`, `error?`

Siempre responde **302** a `{PUBLIC_BASE_URL}/panel/settings?mp=<flag>` (no hay respuesta JSON de éxito). `PUBLIC_BASE_URL` tiene que estar bien seteada en cada entorno.

| Flag | Cuándo |
|------|--------|
| `connected` | Éxito; se borra la cookie `mp_oauth_state` |
| `error` | MP devolvió `error=`, o el canje no devolvió `user_id` (no se guarda nada); el state se consume y la cookie se borra solo si la cookie coincide con el state |
| `other_browser` | Cookie ausente o distinta del `state`: el state NO se consume y no se vincula nada; el panel pide completar la autorización en el mismo navegador |
| `account_in_use` | La cuenta de MP ya está vinculada a otro tenant (índice único `uq_tenant_mp_user_id`): rollback, no se guardan tokens y el otro tenant no cambia |

Errores JSON que se mantienen:
- 400 si falta `code` o `state`, o si el `state` es inválido/vencido/ya usado (Redis `GETDEL`, un solo uso).
- 502 si falla el canje con MP.

Flujo de éxito: canjea `code` en `POST /oauth/token` con `client_id`, `client_secret`, `redirect_uri`, `test_token=true` si `MP_SANDBOX`; cifra tokens (Fernet) y guarda en tenant: `mp_access_token_enc`, `mp_refresh_token_enc`, `mp_user_id`, `mp_alias`, `mp_token_expires_at`.

### 7.3 `GET /tenants/me/mp`

**Response 200:**
```json
{
  "connected": true,
  "mp_user_id": "123456789",
  "mp_alias": "Mi Negocio",
  "mp_token_expires_at": "2027-04-15T10:30:00+00:00"
}
```

### 7.4 `DELETE /tenants/me/mp`

**Response 200:** `{ "disconnected": true }` (idempotente).

---

## 8. Health

| Método | Path | Descripción |
|--------|------|-------------|
| GET | `/health` | Deep check: API + DB (`SELECT 1`) + Redis (`PING`). 200 ok / 503 degraded. |

---

## 9. Schemas Pydantic (request/response)

### `AvailableSlotsResponse`
```python
date: date
service_duration_min: int
timezone: str
slots: list[str]  # ["HH:MM", ...]
```

### `PublicTenantDetailResponse`
```python
id: int
name: str
slug: str | None
timezone: str
services: list[PublicServiceRead]
```

### `PublicServiceRead`
```python
id: int
name: str
duration_minutes: int
price: float
deposit_amount: float  # effective_deposit(price, deposit_amount)
```

### `PublicBookingResponse`
```python
message: str
booking_id: int
payment_url: str
```

### `BookingCreate`
```python
tenant_id: int
service_id: int
staff_id: int | None = None
client_name: str
client_phone: str
start_time: datetime
end_time: datetime | None = None
price_at_booking: float | None = None
idempotency_key: str
```

### `TenantSettingsUpdate`
```python
deposit_expiration_minutes: int | None = None  # ge=1
```

---

## 10. Códigos de error frecuentes

| Código | Endpoint típico | Causa |
|--------|-----------------|-------|
| 401 | `/bookings/*`, `/tenants/me/*` | API key faltante/inválida/revocada |
| 401 | `/webhooks/mercadopago` | HMAC MP inválido |
| 401 | `/webhooks/whatsapp` | HMAC Meta inválido |
| 403 | `/webhooks/mercadopago` | Timestamp > 5 min (replay) |
| 403 | Panel POST | CSRF token inválido/ausente |
| 404 | `/public/tenants/*`, `/public/available-slots`, `/bookings/*` | Tenant/Service/Staff no existe o no pertenece al tenant |
| 409 | `POST /bookings`, `POST /public/bookings` | ExcludeConstraint violation (slot ocupado) o colisión de `idempotency_key` global |
| 409 | `POST /panel/agenda/{id}/*` | Transición de estado inválida / turno no empezado |
| 422 | `POST /public/bookings`, `POST /bookings` | Teléfono inválido (`InvalidPhoneError`) |
| 422 | `POST /public/bookings` (prod) | `ERR_PAGO_NO_CONFIGURADO` (tenant sin MP conectado) |
| 500 | Webhook MP | Error inesperado (MP API rechazo no-404, `MPTokenCryptoError`) — evento queda `failed`, MP reintenta |
| 502 | `POST /public/bookings` | `MPTokenCryptoError` al resolver token del tenant / MP rechaza preferencia / timeout creando preferencia |
| 503 | `/health` | DB o Redis caídos |
| 504 | Webhook MP | Timeout consultando el pago a MP (`get_payment_details`) |

---

## Ver también

- [`README.md`](README.md) — Quickstart, env vars
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Flujos, auth, slots, outbox, scheduler, MP, WhatsApp
- [`DECISIONS.md`](DECISIONS.md) — D-002, D-003, D-004, D-005, D-012, D-013
- [`RUNBOOK.md`](RUNBOOK.md) — Diagnóstico de errores 401/403/409/502/503
- [`ONBOARDING.md`](ONBOARDING.md) — Cómo agregar endpoint, testear
