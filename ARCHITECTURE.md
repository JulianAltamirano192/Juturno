# Arquitectura de Juturno

Documento de referencia técnica. Explica **qué** hace cada componente y **por qué**
está diseñado así. Para decisiones específicas, ver `DECISIONS.md`.

## 1. Visión general

Juturno es un SaaS **multi-tenant** donde cada negocio (tenant) gestiona sus
turnos de forma aislada. Los clientes reservan desde un link público, pagan
seña con Mercado Pago, y reciben confirmación por WhatsApp.

## 2. Diagrama de componentes

```
                    ┌──────────────────────────────────────────┐
                    │              Docker Network               │
                    │                                          │
   ┌────────┐       │  ┌────────────┐     ┌──────────────────┐ │
   │ Cliente├───────┼─►│   FastAPI  │────►│   PostgreSQL 16  │ │
   │  Web   │       │  │   (uvicorn)│     │   + btree_gist   │ │
   └────────┘       │  │            │     └──────────────────┘ │
                    │  │            │                          │
   ┌────────┐       │  │            │     ┌──────────────────┐ │
   │WhatsApp├───────┼─►│  + APSched │────►│     Redis 7      │ │
   │  (Meta)│       │  │  (jobs)    │     │  locks + cache   │ │
   └────────┘       │  └────────────┘     └──────────────────┘ │
                    │        │                                 │
   ┌────────┐       │        │                                 │
   │ Mercado├───────┼────────┘                                 │
   │  Pago  │       │                                          │
   └────────┘       └──────────────────────────────────────────┘
```

## 3. Multi-tenancy

**Modelo elegido**: shared database + `tenant_id` en cada tabla.

**Por qué**:
- Menor costo operativo (una DB en lugar de N).
- Menos migraciones que mantener.
- Aislamiento garantizado por lógica de aplicación + constraints.

**Cómo se enforce**:
1. Todos los endpoints validan `tenant_id == current_tenant.id` (retorna 404 si no).
2. El `ExcludeConstraint` de bookings incluye `tenant_id` para evitar
   colisiones cross-tenant.
3. La dependencia `get_current_tenant` se inyecta en todos los endpoints
   protegidos.

## 4. Autenticación

**Mecanismo**: header `X-Tenant-API-Key`.

**Flujo**:
1. Cliente envía la key.
2. `auth.py` hashea con SHA-256 y busca en `api_key`.
3. Si existe y no está revocada, devuelve el `Tenant`.
4. Cache en Redis (TTL 60s) para evitar round-trip a Postgres en cada request.

**Por qué SHA-256 y no bcrypt**:
Las keys son secretos de alta entropía generados por CLI (256 bits). No
necesitan hashing lento, y el determinismo permite buscar por índice único.

**Revocación**: soft delete (`revoked_at`). Las keys nunca se borran.

## 5. Reservas y anti-solapamiento

**Constraint en DB**:

```sql
EXCLUDE USING gist (
  tenant_id WITH =,
  COALESCE(staff_id, -1) WITH =,
  tstzrange(start_time, end_time) WITH &&
)
```

**Por qué**:
- **`tenant_id` primero**: dos tenants nunca colisionan.
- **`COALESCE(staff_id, -1)`**: bookings sin staff se agrupan en `-1` para
  que no puedan solaparse entre sí (dentro del mismo tenant).
- **`tstzrange` + `&&`**: detecta solapamiento de intervalos.

**Alternativa rechazada**: validar en aplicación. No es atómico bajo
concurrencia — dos requests simultáneos pueden pasar la validación y crear
bookings superpuestos. El constraint de DB lo previene a nivel motor.

## 6. Patrón Outbox

**Problema**: enviar WhatsApp en el mismo request HTTP que crea el booking
haría que la latencia de Meta afecte la respuesta al cliente. Si Meta se cae,
la reserva falla sin razón.

**Solución**:
1. Al crear el booking, se inserta un `NotificationOutbox` en la **misma
   transacción** con `status='pending'`.
2. Un job de APScheduler (`process_outbox`) corre cada 60s, toma los
   pendientes con `SELECT ... FOR UPDATE SKIP LOCKED`, y envía.
3. Actualiza a `sent` o `failed` con `error_message`.

**Garantía**: la reserva y la notificación son atómicas. Si el envío falla,
el booking ya está confirmado y el outbox queda en `failed` para
investigación manual.

## 7. Webhooks y seguridad

### WhatsApp (Meta)

- **Verificación inicial** (`GET`): se compara `hub.verify_token` con
  `META_VERIFY_TOKEN`.
- **Eventos** (`POST`): se valida firma `X-Hub-Signature-256` con
  `META_APP_SECRET` usando HMAC-SHA256 sobre el body crudo.
- **Rechazo**: si la firma no coincide, retorna 403.

### Mercado Pago

- **Firma**: `x-signature` (HMAC-SHA256) sobre manifest
  `id:{data_id};request-id:{x_request_id};ts:{ts};` con `MP_SECRET_KEY`.
- **Replay protection**: rechaza timestamps > 5 min.
- **Idempotencia**: tabla `payment_events` con `event_id` como PK. Si el
  evento ya existe y está `processed`, retorna `DUPLICATE_EVENT_IGNORED`.
- **Auto-creación de `Payment`**: si el webhook trae un pago aprobado y no
  existe `Payment` en la DB, lo crea a partir de los datos de MP.
- **Confirmación automática**: si el pago está `approved`, confirma el
  booking y encola el WhatsApp de confirmación.

## 8. Scheduler y locks

**Problema**: si corren 2+ instancias de la API, cada una ejecuta el
scheduler. El outbox podría procesarse dos veces.

**Solución**: lock distribuido en Redis (`SET NX EX`) al inicio de cada job.
Si otra instancia tiene el lock, el job sale silenciosamente.

**Mejora futura**: migrar a worker separado (Celery, ARQ) para deployments
multi-instancia. Hoy funciona con 1 réplica.

## 9. Zonas horarias

**Almacenamiento**: `TIMESTAMPTZ` en UTC.

**Cálculo**:
- Cada tenant tiene un campo `timezone` (ej. `America/Argentina/Buenos_Aires`).
- Los slots disponibles se calculan en el timezone del tenant.
- Los mensajes de WhatsApp muestran la fecha formateada al timezone del tenant.

**Input de clientes**: se acepta `datetime` con o sin timezone. Si viene naive,
se interpreta como hora local del tenant.

## 10. Observabilidad

- **Errores**: Sentry captura excepciones con stack trace, endpoint, usuario.
- **Logs**: logging estructurado con `logging.basicConfig` (INFO).
- **Health check**: `GET /health` retorna `{"status": "ok"}` (TODO: extender
  para verificar DB y Redis).
- **Métricas**: pendiente (ver `DECISIONS.md` → roadmap).

## 11. Tests

**20 tests organizados por dominio**:

| Archivo | Qué cubre |
|---|---|
| `test_auth.py` | Autenticación, cross-tenant, `last_used_at` |
| `test_booking_constraints.py` | ExcludeConstraint cross-tenant |
| `test_integration.py` | Flujo de reserva end-to-end, webhooks |
| `test_mp_webhooks.py` | Idempotencia, replay, timestamp |
| `test_server_defaults.py` | Defaults a nivel DB |
| `test_slots.py` | Cálculo de slots disponibles |

**Fixtures compartidos** en `tests/conftest.py`.

## 12. Deuda técnica conocida

- **Worker separado**: hoy el scheduler corre dentro de la API.
- **Rate limiting**: sin protección contra abuso.
- **`BusinessHours`**: horarios hardcoded 09-18.
- **API de Orders de MP**: usar Preferencias (legacy) en lugar de Orders.
- **Health check profundo**: verificar dependencias, no solo `{"status": "ok"}`.

Ver `DECISIONS.md` para el roadmap completo.
