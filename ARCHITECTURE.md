# Arquitectura de Juturno

> *Documento de referencia técnica. Explica qué hace cada componente y por qué
> está diseñado así. Para las decisiones individuales con contexto histórico,
> ver `DECISIONS.md`.*

***

## 1. Visión general

Juturno es un SaaS multi-tenant donde cada negocio (tenant) gestiona sus turnos
de forma aislada. Los clientes reservan desde un link público, pagan seña con
Mercado Pago, y reciben confirmación por WhatsApp.

El diseño prioriza **operación simple sobre escalabilidad prematura**: un solo
VPS con Docker Compose (orquestado por Coolify + Traefik como reverse proxy),
sin colas externas ni orquestación de contenedores.

**Stack**:
- **Backend**: FastAPI + SQLModel + Pydantic v2
- **DB**: PostgreSQL 16 con extensión `btree_gist`
- **Cache/locks**: Redis 7
- **Scheduler**: APScheduler in-process
- **Pagos**: Mercado Pago (Checkout Pro + OAuth por tenant)
- **Notificaciones**: WhatsApp Business API (Meta)
- **Observabilidad**: Sentry + logs estructurados + health check profundo
- **Deploy**: Coolify en VPS + Traefik + Cloudflare DNS

***

## 2. Diagrama de componentes

```
                    ┌──────────────────────────────────────────────┐
                    │              Docker Network                   │
                    │                                              │
   ┌────────┐       │  ┌────────────┐         ┌──────────────────┐ │
   │ Cliente├───────┼─►│   FastAPI  │────────►│   PostgreSQL 16  │ │
   │  Web   │       │  │  (uvicorn) │         │   + btree_gist   │ │
   └────────┘       │  │            │         └──────────────────┘ │
                    │  │ APScheduler│                              │
   ┌────────┐       │  │  (in-proc) │         ┌──────────────────┐ │
   │WhatsApp├───────┼─►│            │────────►│     Redis 7      │ │
   │  (Meta)│       │  │            │         │  locks + cache   │ │
   └────────┘       │  └─────┬──────┘         └──────────────────┘ │
                    │        │                                     │
   ┌────────┐       │        ▼                                     │
   │ Mercado├───────┼─►  /webhooks/mercadopago                     │
   │  Pago  │       │                                              │
   └────────┘       └──────────────────────────────────────────────┘
                            ▲
                            │ (prod)
                    ┌───────┴───────┐
                    │  Traefik v3   │  ← reverse proxy + TLS (Let's Encrypt)
                    │  (Coolify)    │
                    └───────────────┘
```

**Flujos externos**:
- Clientes Web → endpoints REST (reservas, pagos, slots).
- WhatsApp (Meta) → `POST /webhooks/whatsapp` (eventos de mensajes).
- Mercado Pago → `POST /webhooks/mercadopago` (confirmaciones de pago).
- FastAPI → WhatsApp API (envío de plantillas de confirmación y recordatorio).
- FastAPI → Mercado Pago API (creación de preferencias de pago, OAuth refresh).

***

## 3. Multi-tenancy

**Modelo elegido**: shared database con columna `tenant_id` en todas las tablas
relevantes.

**Por qué no database-per-tenant**: menor costo operativo (una DB en lugar de N),
menos migraciones que coordinar, y el volumen actual de datos no justifica
aislamiento físico.

**Cómo se enforce el aislamiento**:

1. La dependencia `get_current_tenant` (API key) o `get_current_tenant_from_session`
   (panel) se inyecta en todos los endpoints protegidos.
2. Todos los endpoints validan que el recurso pertenece al `tenant_id` del request
   (retorna **404** si no — nunca 403, para no filtrar existencia).
3. El `ExcludeConstraint` de `booking` incluye `tenant_id` como primera dimensión,
   garantizando que la protección de solapamiento nunca colisione entre tenants.
4. El campo `tenant.session_version` (int, default 1) permite invalidar todas las
   sesiones del panel de un tenant con un solo `UPDATE` (ver D-013).

**Defensa contra errores de filtrado**: si un filtro de `tenant_id` se omite en una
query, Sentry lo registra y los tests de aislamiento entre tenants lo detectan
(`test_auth.py::test_tenant_a_cannot_read_tenant_b_data`).

***

## 4. Autenticación

El proyecto tiene **dos mecanismos de autenticación** según el tipo de cliente:

### 4.1 — API key (clientes externos, integraciones)

**Mecanismo**: header `X-Tenant-API-Key`.

**Flujo** (`app/auth.py::get_current_tenant`):

```
Request → auth.py:
  1. Lee el header X-Tenant-API-Key
  2. Hash SHA-256 de la key recibida
  3. Busca en tabla api_key (cache Redis, TTL 60s)
  4. Si existe y no está revocada → devuelve Tenant
  5. Si no → 401
```

**Por qué SHA-256 y no bcrypt**: las keys son secretos de alta entropía (256 bits)
generados por CLI. No hay riesgo de ataque de diccionario, el determinismo permite
índice único en DB, y el hash lento de bcrypt obligaría a iterar todas las keys en
cada request. SHA-256 es la elección correcta aquí (ver D-002).

**Revocación**: soft delete con `revoked_at`. Las keys nunca se borran físicamente —
sirven de auditoría para saber qué se usó y cuándo.

**Cache Redis**: evita un round-trip a Postgres en cada request. TTL de 60s — si una
key se revoca, el peor caso es que siga funcionando 60s más. Aceptable.

### 4.2 — Cookie de sesión firmada (panel del negocio)

**Mecanismo**: cookie HTTP-only `juturno_session` firmada con HMAC-SHA256.

**Formato del payload**: `{tenant_id}.{session_version}.{expires_at}`.

**Flujo** (`app/auth.py::get_current_tenant_from_session`):

```
Request → auth.py:
  1. Lee la cookie juturno_session
  2. Parsea y valida la firma HMAC (timing-safe con hmac.compare_digest)
  3. Verifica que no haya expirado
  4. Carga el Tenant de la DB
  5. Compara tenant.session_version con el de la cookie
  6. Si no coinciden → invalida sesión (redirect a /login)
```

**Invalidación masiva**: al cambiar la contraseña (o hacer "logout en todos los
dispositivos"), se incrementa `tenant.session_version`. Todas las cookies activas
quedan invalidadas de inmediato (ver D-013).

**Ventajas frente a JWT**:
- No requiere blacklist ni storage (stateless).
- Invalidación instantánea con un solo `UPDATE`.
- `hmac.compare_digest` es timing-safe.
- Sin dependencia externa (HMAC está en la stdlib).

**Protección CSRF**: los endpoints del panel que modifican estado usan el módulo
`app/csrf.py` con token en formulario. Verifica origen además de token.

***

## 5. Reservas y anti-solapamiento

**Constraint en DB** (extensión `btree_gist`):

```sql
EXCLUDE USING gist (
  tenant_id WITH =,
  COALESCE(staff_id, -1) WITH =,
  tstzrange(start_time, end_time) WITH &&
) WHERE status IN ('pending', 'confirmed')
```

**Por qué cada parte**:

| Parte | Razón |
|---|---|
| `tenant_id WITH =` | Dos tenants distintos nunca colisionan entre sí |
| `COALESCE(staff_id, -1)` | Bookings sin staff se agrupan en `-1` para que no se solapen dentro del mismo tenant |
| `tstzrange &&` | Detecta solapamiento de intervalos de tiempo |
| `WHERE status IN (...)` | Solo `pending` y `confirmed` bloquean el slot. `cancelled`, `no_show`, `completed` y `expired` liberan el horario automáticamente |

**Por qué no validar en aplicación**: dos requests simultáneos pueden pasar la
validación a nivel código y crear bookings superpuestos (race condition). El
`EXCLUDE` de Postgres lo previene a nivel motor, con semántica atómica: es el
mecanismo más robusto disponible para este caso (ver D-003).

***

## 6. Patrón Outbox para notificaciones

**Problema**: enviar WhatsApp en el mismo request que crea el booking acopla la
latencia de Meta a la respuesta del cliente. Si Meta se cae, la reserva falla sin
razón de negocio.

**Solución (Outbox pattern)**:

```
POST /bookings
  └── BEGIN TRANSACTION
        ├── INSERT INTO booking (...)         → booking creado
        └── INSERT INTO notification_outbox (status='pending')
      COMMIT
  └── Response 201 al cliente (rápido, sin depender de Meta)

APScheduler (cada 60s):
  └── SELECT ... FROM notification_outbox WHERE status='pending'
      FOR UPDATE SKIP LOCKED
        ├── Enviar WhatsApp vía API de Meta
        └── UPDATE notification_outbox SET status='sent'/'failed'
```

**Tipos de notificación soportados**:
- `confirmation`: al crear un booking (o al confirmarlo manualmente).
- `reminder`: 24h antes del turno (job `process_reminders`).

**Garantía**: la reserva y la notificación son atómicas respecto a la DB. Si el envío
falla, el booking ya está confirmado y el outbox queda en `failed` para reintentar o
investigar (ver D-004).

**Compensación**: la notificación puede tardar hasta 60s. Es aceptable porque el
cliente ya recibe la confirmación de la reserva en la respuesta 201.

***

## 7. Webhooks y seguridad

### WhatsApp (Meta)

| Operación | Detalle |
|---|---|
| Verificación (GET) | Compara `hub.verify_token` con `META_VERIFY_TOKEN` del env |
| Eventos (POST) | Valida `X-Hub-Signature-256` con HMAC-SHA256 sobre el body crudo |
| Rechazo | Si la firma no coincide → 403 inmediato |

### Mercado Pago

| Operación | Detalle |
|---|---|
| Firma | `x-signature` (HMAC-SHA256) sobre manifest `id:{data_id};request-id:{x_request_id};ts:{ts};` con `MP_SECRET_KEY` |
| Replay protection | Rechaza timestamps > 5 minutos |
| Idempotencia | Tabla `payment_events` con `event_id` como PK — si ya existe y está `processed`, retorna `DUPLICATE_EVENT_IGNORED` |
| **Resolución de tenant** | El webhook identifica al tenant dueño del pago por el `user_id` del payload (que es el `collector_id` de la cuenta que recibió el dinero). Busca `tenant.mp_user_id` y usa `tenant.mp_access_token_enc` para consultar el pago |
| Fallback | Si ningún tenant matchea el `user_id`, se usa `MP_ACCESS_TOKEN` de plataforma (legacy, ver D-012) |
| Auto-creación | Si el webhook trae un pago aprobado sin `Payment` en DB, lo crea on-the-fly desde los datos de MP |
| Confirmación | Si el pago está `approved` → confirma el booking y encola el WhatsApp de confirmación |

### OAuth de Mercado Pago por tenant

**Contexto**: cada negocio cobra en su **propia** cuenta de Mercado Pago. El dinero
NO pasa por la cuenta de la plataforma (ver D-012).

**Flujo OAuth**:

```
1. El dueño va a /panel/mp → click en "Conectar Mercado Pago"
2. GET /mp/connect/start → redirect a MP con client_id de la plataforma
3. Usuario autoriza → MP redirige a /mp/connect/callback?code=...
4. Backend intercambia code por tokens (access + refresh)
5. Tokens se cifran con Fernet (MP_TOKEN_ENCRYPTION_KEY) y se persisten en tenant:
   - mp_user_id
   - mp_alias
   - mp_access_token_enc
   - mp_refresh_token_enc
   - mp_token_expires_at
6. A partir de ahí, los pagos del tenant usan SU access_token, no el de la plataforma
```

**Renovación automática**: el job diario `process_mp_token_refresh` renueva los tokens
que vencen en menos de 30 días (`REFRESH_AHEAD_DAYS`). Si MP rechaza el refresh
(usuario revocó permisos), el tenant debe reconectar manualmente (ver RUNBOOK).

**Cifrado**: `app/mp_crypto.py` usa Fernet (AES-128 en CBC con HMAC-SHA256). La clave
`MP_TOKEN_ENCRYPTION_KEY` se genera una sola vez con
`Fernet.generate_key().decode()` y se guarda en el env.

***

## 8. Scheduler y locks distribuidos

**Problema**: si corren 2+ instancias de la API (o en el futuro), cada una ejecuta el
scheduler. El outbox podría procesarse dos veces y los clientes recibirían WhatsApp
duplicados.

**Solución**: lock distribuido en Redis con `SET NX EX` al inicio de cada job. Si otra
instancia tiene el lock, el job finaliza sin ejecutarse.

```python
lock = await redis.set(f"lock:{job_name}", uuid, nx=True, ex=ttl_seconds)
if not lock:
    return  # Otra instancia se encargó
```

**Jobs registrados** (`app/scheduler.py`):

| Job | Frecuencia | Qué hace |
|---|---|---|
| `process_outbox` | 60s | Encola y envía notificaciones WhatsApp pendientes |
| `process_reminders` | 5 min | Busca bookings confirmados que empiezan en ~24h y encola recordatorios |
| `process_deposit_expiration` | 5 min | Expira bookings `pending` cuyo deadline de seña (`tenant.deposit_expiration_minutes`) venció |
| `process_mp_token_refresh` | 24h | Renueva tokens OAuth de MP que vencen en <30 días |

**Deshabilitado en tests**: si `TEST_DATABASE_URL` está seteada, el lifespan NO
arranca el scheduler, para evitar que los jobs interfieran con los tests.

**Limitación actual**: el scheduler vive dentro del proceso de la API. Si la API se
reinicia, los jobs se detienen hasta que el proceso vuelva. En producción con 1
réplica, esto es aceptable (ver D-005).

**Futuro**: migrar a worker separado (Celery, ARQ o Dramatiq) cuando tengamos >1
réplica o la carga lo justifique.

***

## 9. Zonas horarias

**Almacenamiento**: `TIMESTAMPTZ` (con zona horaria) en UTC. Siempre.

**Cálculo de slots**: en el timezone del tenant (campo `timezone`, ej.
`America/Argentina/Buenos_Aires`). Usamos `zoneinfo` de la stdlib — sin dependencia
de `pytz` (ver D-009).

**Input de clientes**: se acepta datetime con o sin timezone. Si viene naive (sin info
de zona), se interpreta como hora local del tenant.

**Mensajes de WhatsApp**: la fecha se formatea al timezone del tenant antes de
incluirla en la plantilla (`app/outbox_worker.py::format_booking_datetime`).

***

## 10. Observabilidad

| Canal | Qué captura |
|---|---|
| Sentry | Excepciones con stack trace, endpoint, tenant. Sample rate 10% para traces y profiles |
| Logs | `logging.basicConfig` nivel INFO, formato `%(asctime)s [%(levelname)s] %(name)s: %(message)s` |
| Health check | `GET /health` → verifica **API + DB + Redis**, retorna 503 si alguno falla |
| Métricas | Pendiente (PostHog en roadmap Q3 2027) |

**Health check profundo** (implementado):
```json
{
  "status": "ok",
  "checks": {
    "api": "ok",
    "database": "ok",
    "redis": "ok"
  }
}
```

Si algún check falla, retorna `503` con `"status": "degraded"`. Esto permite que
monitores externos (UptimeRobot, Better Stack) y el healthcheck de Coolify detecten
caídas reales de dependencias, no solo que uvicorn respire.

***

## 11. Backups

El script `scripts/backup_db.sh` hace un `pg_dump` comprimido con `gzip --clean
--if-exists` y rota backups con más de 30 días. Es el procedimiento canónico de
backup: README.md y RUNBOOK.md referencian este mismo flujo.

```bash
# Backup manual (ruta por defecto: ./backups/)
./scripts/backup_db.sh

# Backup a directorio específico
./scripts/backup_db.sh /mnt/backup-externo

# Restaurar (⚠️ reemplaza los datos actuales; verificar backup previo)
gunzip -c backups/saas_db_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose exec -T db psql -U postgres -d saas_db
```

**Cobertura del backup**: incluye datos de tenants, bookings, payments, outbox,
api_keys, y los tokens OAuth de MP (que están cifrados con Fernet, así que el backup
requiere la `MP_TOKEN_ENCRYPTION_KEY` para ser restaurado en un entorno funcional).

**Automatización**: pendiente como cron en el VPS de producción (Fase 0).

***

## 12. Tests

**20 archivos de test, 169 tests**, todos con `pytest-asyncio`:

| Archivo | Qué cubre |
|---|---|
| `test_auth.py` | API key auth, cross-tenant isolation, `last_used_at` |
| `test_booking_constraints.py` | `ExcludeConstraint` cross-tenant, race conditions |
| `test_integration.py` | Flujo de reserva end-to-end + webhooks |
| `test_slots.py` | Cálculo de slots disponibles |
| `test_server_defaults.py` | Defaults a nivel DB + `TIMESTAMPTZ` |
| `test_phone.py` | Normalización de números de teléfono |
| `test_deposit_expiration.py` | Job de expiración de señas |
| `test_mp_crypto.py` | Cifrado/descifrado Fernet de tokens MP |
| `test_mp_connect.py` | Flujo OAuth de conexión con MP |
| `test_mp_token_refresh.py` | Job de renovación de tokens OAuth |
| `test_mp_webhooks.py` | Firma HMAC, replay protection, idempotencia |
| `test_mp_webhook_tenant.py` | Resolución de tenant por `user_id` del webhook |
| `test_mp_tenant_payment.py` | Cobro con token del tenant (no de la plataforma) |
| `test_public_endpoints.py` | Endpoints públicos + integración MP completa |
| `test_whatsapp_webhooks.py` | Firma HMAC y manejo de eventos de Meta |
| `test_login.py` | Login del panel, sesiones firmadas |
| `test_register.py` | Registro de tenant + onboarding |
| `test_services_panel.py` | CRUD de servicios del panel |
| `test_staff_panel.py` | CRUD de staff + horarios |
| `test_agenda_panel.py` | Vista de agenda por día |
| `test_business_hours_panel.py` | Configuración de horarios de atención |

**Fixtures compartidos** en `tests/conftest.py`.
**DB de tests**: `saas_test` (aislada de `saas_db`, con engine `NullPool`).
**Scheduler deshabilitado** en tests (chequea `TEST_DATABASE_URL`).
**CI**: GitHub Actions corre los 169 tests en cada push a `main`.

***

## 13. Deuda técnica conocida

| Deuda | Impacto | Cuándo atacarla |
|---|---|---|
| Worker separado para el scheduler | Alto — necesario para >1 réplica | Q2 2027 |
| Rate limiting en endpoints públicos | Medio — sin protección contra abuso | Q2 2027 |
| Rotación de `MP_TOKEN_ENCRYPTION_KEY` | Medio — requiere plan de migración de tokens | Q2 2027 |
| Migrar a MP Orders API | Bajo — Preferencias funciona pero es "legacy" | Q2 2027 |
| Rotación de `SECRET_KEY` documentada | Bajo — no hay procedimiento; invalidaría todas las sesiones | Q1 2027 |
| Cache Redis en health check | Bajo — crea cliente nuevo por request en lugar de reusar pool | Q1 2027 |
| Multi-stage Dockerfile | Bajo — imagen final más chica | Q1 2027 |
| Resource limits en `docker-compose.prod.yml` | Bajo — sin límites de CPU/RAM por contenedor | Q1 2027 |
| Métricas de producto (PostHog) | Bajo — no hay dashboards para tenants | Q3 2027 |

**Cerrado recientemente**:
- ✅ Auth por tenant vía `X-Tenant-API-Key` con cache Redis (`app/auth.py::get_current_tenant`).
- ✅ `BusinessHours` configurable por tenant (modelo + panel + tests).
- ✅ `pytest` fuera de `requirements.txt` (movido a `requirements-dev.txt`).

Ver `DECISIONS.md` → Roadmap para el timeline completo.
