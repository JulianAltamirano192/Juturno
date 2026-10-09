# Referencia de API

> Especificación endpoint por endpoint. Para integradores y devs que consumen o extienden la API.
> Fuente de verdad: el código en `app/` (routers registrados en `app/main.py`). Total: 47 rutas de aplicación + el mount `/static`.

---

## 1. Autenticación

### 1.1 API Key (`X-Tenant-API-Key`)
- **Header**: `X-Tenant-API-Key: <key_plana>`
- **Key**: 256 bits generada por CLI (`app/cli.py`), alta entropía.
- **Validación**: SHA-256 determinístico → lookup en `ApiKey.key_hash` (índice único) → cache Redis 60s (`auth:apikey:{hash}`). Una key recién revocada puede seguir aceptándose hasta 60s.
- **`last_used_at`**: se actualiza con throttle (máximo una vez cada 5 min).
- **Errores**: 401 "Falta el header X-Tenant-API-Key" | 401 "Invalid API key" (genérico: no distingue inexistente vs revocada vs tenant borrado).

### 1.2 Panel web (cookie firmada)
- **Cookie**: `juturno_session={tenant_id}.{session_version}.{expires_at}.{signature}` (HttpOnly, SameSite=Lax, `Secure` en producción).
- **Firma**: HMAC-SHA256 con `SECRET_KEY`.
- **Expiración**: 14 días (`SESSION_MAX_AGE_SECONDS`).
- **Invalidación**: cambiar `Tenant.session_version` invalida todas las cookies del tenant (hoy no hay flujo de cambio de contraseña que lo haga).
- **Dependencia**: `get_current_tenant_from_session` → lanza `RedirectToLoginException` (303 → `/login?next=...`, borra la cookie) si falta, es inválida, venció, el tenant no existe o la `session_version` no coincide.

### 1.3 CSRF (formularios panel)
- **Cookie**: `csrf_token` (no HttpOnly, SameSite=Lax, Secure en prod, 14 días como la sesión). La emite cada GET que renderiza un formulario, reusando el token existente.
- **Campo formulario**: `<input type="hidden" name="csrf_token" value="...">`
- **Dos flujos de validación** (ambos double-submit con `hmac.compare_digest`):
  - **`POST /register` y `POST /login`**: `validate_csrf_double_submit` → si falla, **400** con formulario re-renderizado (nuevo token) y mensaje de error.
  - **`POST /logout` y todos los `POST /panel/*`**: `validate_csrf` lee el form body y compara contra la cookie → **403** (`CSRF cookie missing` / `CSRF token invalid`).

### 1.4 Webhooks y público
- Webhook Meta: HMAC `X-Hub-Signature-256` con `META_APP_SECRET`.
- Webhook Mercado Pago: HMAC `x-signature` con `MP_SECRET_KEY` + ventana de 5 min.
- Endpoints públicos: sin auth (ver sección 4). `GET /mp/connect/callback` se autentica con la cookie `mp_oauth_state` + `state` de un solo uso.

---

## 2. Convenciones globales

| Convención | Detalle |
|------------|---------|
| **Idempotencia** | `idempotency_key` (string, **UNIQUE compuesto `(tenant_id, idempotency_key)`** — `uq_booking_idempotency_key`) en `POST /bookings` y `POST /public/bookings`. Un retry con la misma key del mismo tenant → **200** + booking existente (`message: "Reserva recuperada (idempotente)"`). Keys de distintos tenants no colisionan. También cubre la carrera: si el `INSERT` choca por `IntegrityError` y existe el booking con esa key, devuelve 200. |
| **Timestamps** | ISO 8601 UTC: `2026-10-15T14:30:00+00:00` o `2026-10-15T14:30:00Z`. Un `start_time` sin zona se interpreta en la timezone del tenant. |
| **Decimales** | DB y Python usan `Decimal` (`Numeric(10,2)`). `PublicServiceRead.price` y `.deposit_amount` son `Decimal` en el schema pero un `field_serializer` los serializa como **número JSON** (ej: `5000.0`). En el panel, los montos se parsean con `Decimal` (acepta coma o punto). |
| **Errores** | `{ "detail": "mensaje en español" }` (4xx/5xx de `HTTPException`). Errores de validación de FastAPI/Pydantic: 422 con `detail` como lista. |
| **Rate limiting** | `slowapi` por IP del cliente (`get_remote_address`). Excedido → **429** `{"error": "Rate limit exceeded: ..."}`. Solo tres endpoints tienen límite (ver tabla). Se desactiva si `TEST_DATABASE_URL` está seteada. |
| **Códigos** | 200 OK, 201 Created, 302 Found (flujo MP), 303 See Other (redirects del panel), 400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found, 409 Conflict, 422 Unprocessable Entity, 429 Too Many Requests, 500, 502 Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout. |
| **Fechas en queries** | `day=YYYY-MM-DD` (ej: `day=2026-10-15`). |
| **Docs automáticas** | FastAPI expone `/docs`, `/redoc` y `/openapi.json` (no están deshabilitadas en `app/main.py`). |
| **CORS** | `CORSMiddleware` con `allow_origins=CORS_ORIGINS` (lista vacía por default), credenciales permitidas, métodos y headers `*`. |

**Rate limits (únicos decoradores `@limiter.limit`):**

| Endpoint | Límite |
|----------|--------|
| `POST /login` | 10/minute |
| `POST /register` | 5/minute |
| `POST /public/bookings` | 20/minute |

---

## 3. Endpoints — Panel (HTML, cookie auth + CSRF)

| Método | Path | Descripción | Auth |
|--------|------|-------------|------|
| GET | `/register` | Formulario registro negocio | — |
| POST | `/register` | Crear tenant + owner. Form: `name`, `owner_email`, `password`, `whatsapp_number?`, `slug?`, `csrf_token`. Valida CSRF (400), pwd ≥ 8 (400), email único normalizado en minúsculas (400); el slug colisionado se resuelve solo (`generate_unique_slug`). Éxito: 303 `/login?registered=1`. Límite 5/minute | CSRF (400) |
| GET | `/login` | Formulario login (303 a `next` saneado si ya hay sesión válida). Query: `registered?`, `next?` | — |
| POST | `/login` | Form: `owner_email`, `password`, `next?`, `csrf_token`. Setea cookie `juturno_session` y 303 a `next` (saneado, default `/dashboard`). Credenciales malas o CSRF inválido: **400** con form re-renderizado (mensaje genérico). Límite 10/minute | CSRF (400) |
| POST | `/logout` | Borra la cookie de sesión → 303 `/login` | CSRF (403) |
| GET | `/panel/settings` | Ajustes: estado de la conexión con Mercado Pago. `?mp=connected\|disconnected\|pending\|error\|other_browser\|account_in_use` muestra un mensaje fijo (nunca se refleja el valor del query) | Cookie |
| POST | `/panel/mp/connect/start` | Guarda el state en Redis, setea la cookie `mp_oauth_state` (HttpOnly) y responde 302 a MP. 503 si falta `MP_MARKETPLACE_CLIENT_ID` | Cookie + CSRF |
| POST | `/panel/mp/disconnect` | Borra localmente tokens y metadata MP → 302 `/panel/settings?mp=disconnected`. Bloqueado con 302 `?mp=pending` si hay un turno `pending` con `Payment.mp_preference_id` y plazo de seña vigente (`created_at + deposit_expiration_minutes`; minutos `NULL` = siempre bloquea). No revoca la autorización en MP | Cookie + CSRF |
| GET | `/dashboard` | Vista principal: resumen del día, próximos turnos, checklist de configuración y link público de reserva (oculto si el tenant no tiene slug). Todo filtrado por `tenant_id` | Cookie |
| GET | `/panel/services` | Listar servicios (activos/inactivos) con seña efectiva | Cookie |
| GET | `/panel/services/new` | Formulario nuevo servicio | Cookie |
| POST | `/panel/services/new` | Crear servicio. Form: `name` (obligatorio), `duration_minutes` (entero ≥ 1), `price` (`Decimal` entre 0.01 y 99999999.99), `deposit_amount?` (`Decimal` entre 0 y el precio; vacío = 30% del precio). Ambos se redondean a 2 decimales antes de validar; `Infinity`/`NaN` se rechazan. Error de validación: 200 con form re-renderizado. Éxito: 303 `/panel/services` | Cookie + CSRF |
| GET | `/panel/services/{service_id}/edit` | Formulario editar servicio (404 "Servicio no encontrado" si no es del tenant) | Cookie |
| POST | `/panel/services/{service_id}/edit` | Actualizar servicio (mismas validaciones; 404 si no es del tenant, se chequea antes que el CSRF) | Cookie + CSRF |
| POST | `/panel/services/{service_id}/toggle` | Activar/desactivar servicio (404 si no es del tenant) | Cookie + CSRF |
| GET | `/panel/staff` | Listar staff | Cookie |
| GET | `/panel/staff/new` | Formulario nuevo staff | Cookie |
| POST | `/panel/staff/new` | Crear staff (`name` obligatorio) | Cookie + CSRF |
| GET | `/panel/staff/{staff_id}/edit` | Formulario editar staff (404 "Miembro no encontrado" si no es del tenant) | Cookie |
| POST | `/panel/staff/{staff_id}/edit` | Actualizar staff (`name` obligatorio) | Cookie + CSRF |
| POST | `/panel/staff/{staff_id}/toggle` | Activar/desactivar staff | Cookie + CSRF |
| GET | `/panel/horarios` | Listar horarios del negocio (`staff_id IS NULL`) | Cookie |
| GET | `/panel/horarios/new` | Formulario nuevo horario | Cookie |
| POST | `/panel/horarios/new` | Crear horario. Form: `day_of_week` (0-6), `start_time`, `end_time` (`HH:MM`, start < end). Rechaza solapamiento con un horario existente del día (y `IntegrityError` → "Ya existe un horario para este día") re-renderizando el form con error | Cookie + CSRF |
| GET | `/panel/horarios/{bh_id}/edit` | Formulario editar horario (404 "Horario no encontrado" si no es del tenant) | Cookie |
| POST | `/panel/horarios/{bh_id}/edit` | Actualizar horario (mismas validaciones y chequeo de solapamiento) | Cookie + CSRF |
| POST | `/panel/horarios/{bh_id}/delete` | **Borrar** horario (404 si no existe, no es del tenant o tiene `staff_id`) | Cookie + CSRF |
| GET | `/panel/agenda` | Vista día: todos los turnos que solapan el día (cualquier status), ordenados por inicio; `?day=YYYY-MM-DD` opcional (default hoy en la TZ del tenant; un valor inválido cae a hoy) | Cookie |
| POST | `/panel/agenda/{booking_id}/confirm` | `pending → confirmed` (actor `owner`); 404 "Turno no encontrado" si no es del tenant; 409 si transición inválida; 303 a `/panel/agenda` | Cookie + CSRF |
| POST | `/panel/agenda/{booking_id}/cancel` | `→ cancelled`; form field opcional `reason`; cancela outbox sin enviar (`pending`/`failed`); 409 si inválida | Cookie + CSRF |
| POST | `/panel/agenda/{booking_id}/no-show` | `confirmed → no_show`; 409 si el turno aún no empezó (`BookingNotStartedError`) o transición inválida | Cookie + CSRF |
| POST | `/panel/agenda/{booking_id}/complete` | `confirmed → completed`; 409 si el turno aún no empezó o transición inválida | Cookie + CSRF |

**Respuestas HTML**: `TemplateResponse` (Jinja2). Redirects del panel: 303 See Other (excepto el flujo MP: 302). Las acciones de agenda aceptan un form field opcional `day` (`YYYY-MM-DD`) y vuelven a `/panel/agenda?day=...`.

---

## 4. Endpoints — Público (sin auth)

| Método | Path | Descripción | Request | Response |
|--------|------|-------------|---------|----------|
| GET | `/public/tenants/{identifier}` | Info pública tenant + servicios activos (`identifier` = ID numérico o slug) | — | `PublicTenantDetailResponse` |
| GET | `/public/available-slots` | Slots libres para servicio/día | Query: `tenant_id`, `service_id`, `day`, `staff_id?` | `AvailableSlotsResponse` |
| POST | `/public/bookings` | Crear booking `pending` + preferencia MP → `payment_url`. Límite 20/minute | `BookingCreate` | `PublicBookingResponse` (201) |
| GET | `/t/{slug}` | Página HTML reserva (mobile-first) | — | `HTMLResponse` |
| GET | `/` | Landing de marketing (`landing.html`, standalone; la demo corre solo en el cliente, sin llamadas al backend). No aparece en OpenAPI | — | `HTMLResponse` |
| GET | `/static/*` | Assets estáticos de `app/static/` (fuentes Sora/DM Sans self-hosted en `fonts/`, imágenes `.webp` del landing). ⚠️ **Públicos sin auth: solo poner assets acá, nunca datos ni secrets** | — | archivo |

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
`deposit_amount` es la seña efectiva (`effective_deposit`: el valor explícito del servicio o 30% del precio redondeado a 2 decimales). Solo se listan servicios con `is_active = true`.

**Errores**: 404 "Tenant not found".

### 4.2 `GET /public/available-slots`

**Query params:**
- `tenant_id` (int, >0)
- `service_id` (int, >0)
- `day` (date, `YYYY-MM-DD`; no puede ser anterior a hoy en la TZ del tenant)
- `staff_id` (int, opcional)

**Response 200:**
```json
{
  "date": "2026-10-15",
  "service_duration_min": 30,
  "timezone": "America/Argentina/Buenos_Aires",
  "slots": ["09:00", "09:30", "10:00", "10:30", "14:00", "14:30"]
}
```

**Errores**: 400 "No se pueden consultar fechas pasadas", 404 "Tenant not found" / "Service not found" (el servicio debe pertenecer al tenant), 422 (parámetros inválidos).

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
- `end_time` es opcional en el schema pero **se ignora**: siempre se calcula como `start_time + service.duration_minutes`.
- El precio y la seña se toman del servicio (`price_at_booking = service.price`, `deposit_at_booking = effective_deposit(...)`, snapshot al crear); no hay campos de monto en el request.
- Teléfono normalizado a `549XXXXXXXXXX` (`phone.py`). Se valida primero (422 antes que cualquier 404).

**Response 201 (`PublicBookingResponse`):**
```json
{
  "message": "Reserva creada",
  "booking_id": 42,
  "payment_url": "https://sandbox.mercadopago.com/checkout/preference/..."
}
```
`payment_url` es el `sandbox_init_point` si `MP_SANDBOX` y el `init_point` en producción.

**Errores:**
- 404 "Tenant not found" / "Service not found for tenant" / "Staff not found for tenant"
- 409 "Slot ya reservado o superpuesto" (ExcludeConstraint `IntegrityError` sin booking previo con esa `idempotency_key`)
- 422 "El WhatsApp no parece completo..." (teléfono inválido)
- 422 `ERR_PAGO_NO_CONFIGURADO` (producción + tenant sin MP conectado; se hace rollback, no queda reserva). Texto: "Este negocio todavía no configuró su cuenta de Mercado Pago. Avisale al local para que conecte su cuenta y vuelvas a reservar."
- 429 rate limit (20/minute por IP)
- 502 "Error al procesar el cobro del negocio; contactá al administrador de la plataforma." (`MPTokenCryptoError` al descifrar el token del tenant)
- 502 "Timeout creando preferencia en Mercado Pago" (timeout en `create_mp_preference`)
- 502 "Mercado Pago rechazó la preferencia: {status}" (error no-2xx de MP)

Si la preferencia de MP falla, el booking se revierte (rollback).

**Idempotencia**: mismo `idempotency_key` + `tenant_id` → **200** + `{message: "Reserva recuperada (idempotente)", booking_id, payment_url}` con la `payment_url` ya generada (cadena vacía si no hay `Payment` de MP con checkout URL).

### 4.4 `GET /t/{slug}`

**Response 200**: HTML página reserva (renderiza `public_booking.html` con tenant + servicios).
**Response 404**: HTML "No encontramos ese negocio".

---

## 5. Endpoints — API Key (`X-Tenant-API-Key`)

| Método | Path | Descripción | Request | Response |
|--------|------|-------------|---------|----------|
| GET | `/bookings/available-slots` | Slots libres (valida `tenant_id == current_tenant.id`) | Query: `tenant_id`, `service_id`, `day`, `staff_id?` | `AvailableSlotsResponse` |
| POST | `/bookings` | Crear booking `pending` (deriva `end_time`, sin MP) | `BookingCreate` | `{message, booking_id}` (201) |
| PATCH | `/tenants/me` | Actualizar `deposit_expiration_minutes` | `TenantSettingsUpdate` | `{tenant_id, deposit_expiration_minutes}` |

Además, con API key: `GET /tenants/me/mp` y `DELETE /tenants/me/mp` (sección 7).

### 5.1 `GET /bookings/available-slots`

Igual que el público pero **requiere API Key** y valida que `tenant_id` coincida con el de la key. 404 "Service not found" si no coincide (no revela existencia). 400 si la fecha es pasada, 404 si el servicio no es del tenant.

### 5.2 `POST /bookings`

**Request (`BookingCreate`):** igual que el público pero sin MP (no genera preferencia ni `Payment`).
- `end_time` se deriva de `service.duration_minutes` (el campo del request se ignora).
- Status inicial: `pending`.

**Response 201:** `{ "message": "Reserva creada", "booking_id": 42 }`
**Response 200 (retry idempotente):** `{ "message": "Reserva recuperada (idempotente)", "booking_id": 42 }`

**Errores:** 401 (API key), 404 ("Tenant not found" si `tenant_id` no es el de la key; "Service not found for tenant"; "Staff not found for tenant"), 409 "Slot ya reservado o superpuesto", 422 (teléfono inválido: "El teléfono del cliente no es válido...").

### 5.3 `PATCH /tenants/me`

**Request:**
```json
{ "deposit_expiration_minutes": 30 }
```
- `null` desactiva la expiración. `ge=1` si es un entero (0 o negativos → 422).
- Si el body no trae el campo, no cambia nada y devuelve el valor actual.

**Response 200:** `{ "tenant_id": 1, "deposit_expiration_minutes": 30 }`

Es la única forma de editar el tenant por API key; el alta/edición de datos del negocio no tiene endpoint (ver "Estado conocido" en `CLAUDE.md`).

---

## 6. Endpoints — Webhooks

### 6.1 `GET /webhooks/whatsapp`

**Verificación Meta (handshake):**
- Query: `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge`
- Si `hub.mode == subscribe` y `hub.verify_token` coincide con `META_VERIFY_TOKEN` (comparación en tiempo constante, `hmac.compare_digest`) → 200 PlainText `hub.challenge`
- Si vienen `hub.mode` y `hub.verify_token` pero no coinciden → 403 "Forbidden: Token mismatch"
- Si falta `hub.mode` o `hub.verify_token` → 400 "Bad Request"

### 6.2 `POST /webhooks/whatsapp`

**Eventos Meta (WhatsApp Business Account):**
- **Mensajes entrantes**: `entry[].changes[].value.messages[]` → loggea `from`, `text.body`, `id` (no se procesan ni persisten).
- **Status de mensajes**: `entry[].changes[].value.statuses[]` → loggea `id`, `status` (sent/delivered/read/failed). No actualiza `NotificationOutbox`.

**Headers:**
- `X-Hub-Signature-256: sha256=<hmac>` → HMAC-SHA256 del body crudo con `META_APP_SECRET` + `hmac.compare_digest`. La firma solo se exige si `META_APP_SECRET` está seteada (en producción es obligatoria al arrancar). Falta o es inválida → **401**.

**Response**:
- 200 `EVENT_RECEIVED` (procesado) o `ERROR_PARSING_BUT_RECEIVED` (excepción al parsear) para que Meta no reintente.
- 404 (sin body) si el JSON no tiene `object == "whatsapp_business_account"`.
- 401 por firma.

### 6.3 `POST /webhooks/mercadopago`

**Webhook de pagos MP (Checkout Pro).** Sin rate limit.

**Headers:**
- `x-signature: ts=<timestamp>,v1=<hmac_sha256>`
- `x-request-id: <uuid>`

**Validaciones (en este orden):**
0. IPN: si el query trae `topic` y no `data.id` → 200 `IPN_IGNORED` sin procesar (no trae firma validable; el mismo evento llega también como Webhook firmado).
1. Body: JSON objeto (y `data`, si está, también objeto). Si no → 400 "Payload de webhook inválido".
2. HMAC: `manifest = "id:{data_id};request-id:{x_request_id};ts:{ts};"`, con `data_id` = `?data.id` del query en minúsculas (si falta `data.id`, se omite `id:...;`) → `hmac.compare_digest` con `MP_SECRET_KEY`. 401 "Firma de Mercado Pago inválida" si falla.
3. Replay: `|now - ts| <= 300s` (5 min). 403 si fuera de ventana.
4. Sin `data.id` (firma válida) → 200 `EVENT_IGNORED_NO_DATA_ID`.
5. Idempotencia: `payment_events.event_id` (PK) = `{data.id}:{x-request-id}` (solo valores firmados; el `id` del body no se usa). Estados: `processing` → `processed` | `failed`. Un duplicado `processed` → 200 `DUPLICATE_EVENT_IGNORED`; uno `processing`/`failed` se reprocesa.

**Payload:** Formato Webhook (`data.id`, `type`, `action`), con firma `x-signature`.

**Procesamiento:**
- Extrae `data_id` (payment ID) → consulta MP con token resuelto (`_resolve_token_for_payment`):
  - Si payload trae `user_id` (collector_id, en raíz o en `data.user_id`) → busca `Tenant.mp_user_id` → usa token descifrado del tenant.
  - Si no matchea ningún tenant o falta → fallback a `MP_ACCESS_TOKEN` plataforma.
  - Si `decrypt_token` falla (`MPTokenCryptoError`) → la excepción se propaga: el evento queda `failed` y MP reintenta (500).
- Si pago `approved`:
  - Auto-crea `Payment` si no existe uno con ese `mp_payment_id` (con `transaction_amount`, `payment_method_id`, `date_approved`).
  - Si booking en `pending` (o `expired` y slot libre) → `transition_booking_status(booking, "confirmed", actor="webhook_mp")`.
  - Crea `NotificationOutbox(type="confirmation")` si no existe.
- Si el pago no está `approved`: el `Payment` se crea/actualiza con el status actual; el booking queda como está.
- Guards de seguridad (collector_id, moneda ARS, monto finito, `deposit_at_booking`): ver `ARCHITECTURE.md` y `DECISIONS.md`.

**Response codes:**
- 200 `EVENT_PROCESSED` / `IPN_IGNORED` / `DUPLICATE_EVENT_IGNORED` / `PAYMENT_NOT_FOUND_ON_MP` (evento queda `failed`: una entrega posterior se reprocesa) / `NO_BOOKING_LINKED` / `EVENT_IGNORED_NO_DATA_ID`
- 400 Body vacío, JSON inválido o que no es un objeto
- 401 Firma inválida
- 403 Timestamp fuera de ventana
- 500 Error inesperado (incluye MP API rechazos no-404 y `MPTokenCryptoError`): el evento queda `failed` y MP reintenta
- 504 Timeout consultando el pago a MP (`get_payment_details`): el evento queda `failed`

---

## 7. Endpoints — MP OAuth (por tenant)

| Método | Path | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/mp/connect/callback` | — (público, cookie `mp_oauth_state`) | Callback OAuth: canjea `code` → tokens cifrados en tenant y responde 302 al panel |
| GET | `/tenants/me/mp` | API Key | Estado conexión MP (connected, mp_user_id, mp_alias, expires_at) |
| DELETE | `/tenants/me/mp` | API Key | Desconectar MP (borra tokens + metadata) |

### 7.1 Inicio de la conexión (solo desde el panel)

❌ `GET /mp/connect/start` (API key) **ya no existe** (responde 404): una `authorization_url` devuelta por API no tiene un navegador al cual atar el `state` OAuth (account-linking). La única forma de conectar MP es `POST /panel/mp/connect/start` (cookie + CSRF, ver sección 3), que:
- Genera `state` (`secrets.token_urlsafe(32)`) → Redis `mp_connect_state:{state}` = `tenant_id` como string (TTL 600s, un solo uso).
- Setea la cookie `mp_oauth_state` = `state` (HttpOnly, path `/mp/connect/callback`, max-age 600, SameSite=Lax, `Secure` en producción). `Domain` = host de `PUBLIC_BASE_URL` cuando el host del callback es un subdominio de ese host (p. ej. panel `juturno.com` + callback `api.juturno.com`); si no, cookie host-only.
- Responde 302 a la `authorization_url` de MP (`client_id`, `response_type=code`, `platform_id=mp`, `state`, `redirect_uri=MP_MARKETPLACE_REDIRECT_URL`).
- 503 si falta `MP_MARKETPLACE_CLIENT_ID`.

`GET /tenants/me/mp` y `DELETE /tenants/me/mp` (API key) siguen disponibles.

### 7.2 `GET /mp/connect/callback`

**Query:** `code`, `state`, `error?`

Casi siempre responde **302** a `{PUBLIC_BASE_URL}/panel/settings?mp=<flag>` (no hay respuesta JSON de éxito). `PUBLIC_BASE_URL` tiene que estar bien seteada en cada entorno.

| Flag | Cuándo |
|------|--------|
| `connected` | Éxito; se borra la cookie `mp_oauth_state` |
| `error` | MP devolvió `error=`, o el canje no devolvió `user_id` (no se guarda nada). Con `error=`, el state se consume y la cookie se borra solo si la cookie coincide con el state |
| `other_browser` | Cookie ausente o distinta del `state`: el state NO se consume y no se vincula nada; el panel pide completar la autorización en el mismo navegador |
| `account_in_use` | La cuenta de MP ya está vinculada a otro tenant (índice único `uq_tenant_mp_user_id`): rollback, no se guardan tokens y el otro tenant no cambia |

Errores JSON que se mantienen:
- 400 si falta `code` o `state` (y no vino `error`), o si el `state` es inválido/vencido/ya usado (Redis `GETDEL`, un solo uso).
- 404 "No existe el tenant para este state." si el tenant del state ya no existe.
- 502 si el canje con MP es rechazado (HTTP no-2xx) o no devuelve `access_token`.
- 503 si faltan `MP_MARKETPLACE_CLIENT_ID` / `MP_MARKETPLACE_CLIENT_SECRET`.
- 504 timeout canjeando el código con MP.

Flujo de éxito: canjea `code` en `POST /oauth/token` con `client_id`, `client_secret`, `redirect_uri`, `test_token=true` si `MP_SANDBOX`; consulta el perfil (`/users/me`, no crítico); cifra tokens (Fernet) y guarda en tenant: `mp_access_token_enc`, `mp_refresh_token_enc`, `mp_user_id`, `mp_alias`, `mp_token_expires_at`. Nunca devuelve tokens.

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
Si no está conectado, `mp_user_id`, `mp_alias` y `mp_token_expires_at` son `null`. Nunca expone tokens.

### 7.4 `DELETE /tenants/me/mp`

**Response 200:** `{ "disconnected": true }` (idempotente). Borra tokens y metadata local; no revoca la autorización en MP.

**409** mientras pueda llegar un pago que el webhook tenga que verificar (link de pago vigente o pago de MP sin estado final), igual que `POST /panel/mp/disconnect`. Después, en producción, el tenant no puede cobrar (422 `ERR_PAGO_NO_CONFIGURADO`) hasta reconectar.

---

## 8. Health

| Método | Path | Descripción |
|--------|------|-------------|
| GET | `/health` | Deep check: API + DB (`SELECT 1`) + Redis (`PING`). 200 ok / 503 degraded. |

```json
{"status": "ok", "checks": {"api": "ok", "database": "ok", "redis": "ok"}}
```
En fallo, el check afectado vale `error: <TipoDeExcepción>` y `status` es `degraded`.

---

## 9. Schemas Pydantic (request/response)

### `AvailableSlotsResponse` (`app/schemas.py`)
```python
date: date
service_duration_min: int
timezone: str
slots: list[str]  # ["HH:MM", ...]
```

### `BookingCreate` (`app/schemas.py`)
```python
tenant_id: int
service_id: int
staff_id: int | None = None
client_name: str
client_phone: str
start_time: datetime
end_time: datetime | None = None  # se ignora: end_time = start_time + duración del servicio
idempotency_key: str
```

### `PublicTenantDetailResponse` (`app/routers/public.py`)
```python
id: int
name: str
slug: str | None
timezone: str
services: list[PublicServiceRead]
```

### `PublicServiceRead` (`app/routers/public.py`)
```python
id: int
name: str
duration_minutes: int
price: Decimal           # serializado como número JSON (float)
deposit_amount: Decimal  # seña efectiva; serializado como número JSON (float)
```

### `PublicBookingResponse` (`app/routers/public.py`)
```python
message: str
booking_id: int
payment_url: str
```

### `TenantSettingsUpdate` (`app/routers/api.py`)
```python
deposit_expiration_minutes: int | None = None  # ge=1
```

---

## 10. Códigos de error frecuentes

| Código | Endpoint típico | Causa |
|--------|-----------------|-------|
| 400 | `POST /register`, `POST /login` | CSRF inválido, contraseña corta, email duplicado, credenciales incorrectas (form re-renderizado) |
| 400 | `/public/available-slots`, `/bookings/available-slots` | Fecha pasada |
| 400 | `/webhooks/mercadopago` | Body vacío, JSON inválido o que no es un objeto |
| 400 | `/mp/connect/callback` | Falta `code`/`state` o `state` inválido/vencido/ya usado |
| 400 | `GET /webhooks/whatsapp` | Falta `hub.mode` o `hub.verify_token` |
| 401 | `/bookings/*`, `/tenants/me*` | API key faltante/inválida/revocada |
| 401 | `/webhooks/mercadopago` | HMAC MP inválido |
| 401 | `POST /webhooks/whatsapp` | HMAC Meta faltante/inválido |
| 403 | `/webhooks/mercadopago` | Timestamp > 5 min (replay) |
| 403 | `GET /webhooks/whatsapp` | `hub.verify_token` incorrecto |
| 403 | `POST /logout`, `POST /panel/*` | CSRF token inválido/ausente |
| 404 | `/public/tenants/*`, `/public/available-slots`, `/bookings/*` | Tenant/Service/Staff no existe o no pertenece al tenant |
| 404 | `/panel/*/{id}/...` | Recurso no es del tenant de la sesión |
| 404 | `POST /webhooks/whatsapp` | `object` distinto de `whatsapp_business_account` |
| 409 | `POST /bookings`, `POST /public/bookings` | ExcludeConstraint violation (slot ocupado). Una colisión de `idempotency_key` del mismo tenant devuelve 200, no 409 |
| 409 | `POST /panel/agenda/{id}/*` | Transición de estado inválida / turno no empezado |
| 422 | `POST /public/bookings`, `POST /bookings` | Teléfono inválido (`InvalidPhoneError`) |
| 422 | `POST /public/bookings` (prod) | `ERR_PAGO_NO_CONFIGURADO` (tenant sin MP conectado) |
| 422 | Cualquiera con body/query | Validación Pydantic (tipos, `gt=0`, `ge=1`) |
| 429 | `POST /login`, `POST /register`, `POST /public/bookings` | Rate limit (10 / 5 / 20 por minuto por IP) |
| 500 | Webhook MP | Error inesperado (MP API rechazo no-404, `MPTokenCryptoError`): evento queda `failed`, MP reintenta |
| 502 | `POST /public/bookings` | `MPTokenCryptoError` al resolver token del tenant / MP rechaza preferencia / timeout creando preferencia |
| 502 | `/mp/connect/callback` | Canje de `code` rechazado o sin `access_token` |
| 503 | `/health` | DB o Redis caídos |
| 503 | `POST /panel/mp/connect/start`, `/mp/connect/callback` | OAuth de MP no configurado en la plataforma |
| 504 | Webhook MP | Timeout consultando el pago a MP (`get_payment_details`) |
| 504 | `/mp/connect/callback` | Timeout canjeando el código |

---

## Ver también

- [`README.md`](README.md): Quickstart, env vars
- [`ARCHITECTURE.md`](ARCHITECTURE.md): Flujos, auth, slots, outbox, scheduler, MP, WhatsApp
- [`DECISIONS.md`](DECISIONS.md): D-002, D-003, D-004, D-005, D-012, D-013
- [`RUNBOOK.md`](RUNBOOK.md): Diagnóstico de errores 401/403/409/502/503
- [`ONBOARDING.md`](ONBOARDING.md): Cómo agregar endpoint, testear
