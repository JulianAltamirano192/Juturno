# Arquitectura de Juturno

> *Documento de referencia técnica. Explica **qué** hace cada componente y **por qué**
> está diseñado así. Para las decisiones individuales con contexto histórico, ver `DECISIONS.md`.*

---

## 1. Visión general

Juturno es un SaaS **multi-tenant** donde cada negocio (tenant) gestiona sus turnos de forma aislada.
Los clientes reservan desde un link público, pagan seña con Mercado Pago, y reciben confirmación por WhatsApp.

El diseño prioriza **operación simple** sobre escalabilidad prematura: un solo VPS con Docker Compose,
sin colas externas, sin Kubernetes, sin reuniones de arquitectura de 3 horas. Por ahora.

---

## 2. Diagrama de componentes

```
                    ┌──────────────────────────────────────────────┐
                    │              Docker Network (juturno)         │
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
```

**Flujos externos**:
- Clientes Web → endpoints REST (reservas, pagos, slots).
- WhatsApp (Meta) → `POST /webhooks/whatsapp` (eventos de mensajes).
- Mercado Pago → `POST /webhooks/mercadopago` (confirmaciones de pago).
- FastAPI → WhatsApp API (envío de plantillas de confirmación y recordatorio).
- FastAPI → Mercado Pago API (creación de preferencias de pago).

---

## 3. Multi-tenancy

**Modelo elegido**: shared database con columna `tenant_id` en todas las tablas relevantes.

**Por qué no database-per-tenant**: menor costo operativo (una DB en lugar de N), menos migraciones
que coordinar, y el volumen actual de datos no justifica aislamiento físico.

**Cómo se enforce el aislamiento**:

1. La dependencia `get_current_tenant` se inyecta en todos los endpoints protegidos.
2. Todos los endpoints validan que el recurso pertenece al `tenant_id` del request (retorna 404 si no — nunca 403, para no filtrar existencia).
3. El `ExcludeConstraint` de bookings incluye `tenant_id` como primera dimensión, garantizando que la protección de solapamiento nunca colisione entre tenants.

**Cabeza de playa contra bugs de filtrado**: si alguien olvida el filtro de `tenant_id` en una query,
Sentry lo captura y los tests de cross-tenant lo detectan.

---

## 4. Autenticación

**Mecanismo**: header `X-Tenant-API-Key`.

**Flujo**:

```
Request → auth.py:
  1. Lee el header X-Tenant-API-Key
  2. Hash SHA-256 de la key recibida
  3. Busca en tabla api_key (cache Redis, TTL 60s)
  4. Si existe y no está revocada → devuelve Tenant
  5. Si no → 401
```

**Por qué SHA-256 y no bcrypt**: las keys son secretos de alta entropía (256 bits) generados por CLI.
No hay riesgo de ataque de diccionario, el determinismo permite índice único en DB, y el hash lento
de bcrypt obligaría a iterar todas las keys en cada request. SHA-256 es la elección correcta aquí.

**Revocación**: soft delete con `revoked_at`. Las keys nunca se borran físicamente — sirven de
auditoría para saber qué se usó y cuándo.

**Cache Redis**: evita un round-trip a Postgres en cada request. TTL de 60s — si una key se revoca,
el peor caso es que siga funcionando 60s más. Aceptable.

---

## 5. Reservas y anti-solapamiento

**Constraint en DB** (extensión `btree_gist`):

```sql
EXCLUDE USING gist (
  tenant_id WITH =,
  COALESCE(staff_id, -1) WITH =,
  tstzrange(start_time, end_time) WITH &&
)
```

**Por qué cada parte**:

| Parte | Razón |
|---|---|
| `tenant_id WITH =` | Dos tenants distintos nunca colisionan entre sí |
| `COALESCE(staff_id, -1)` | Bookings sin staff se agrupan en `-1` para que no se solapen dentro del mismo tenant |
| `tstzrange && ` | Detecta solapamiento de intervalos de tiempo |

**Por qué no validar en aplicación**: dos requests simultáneos pueden pasar la validación a nivel código
y crear bookings superpuestos (race condition). El `EXCLUDE` de Postgres lo previene a nivel motor,
con semántica atómica. No hay workaround más robusto que este.

---

## 6. Patrón Outbox para notificaciones

**Problema**: enviar WhatsApp en el mismo request que crea el booking acopla la latencia de Meta
a la respuesta del cliente. Si Meta se cae, la reserva falla sin razón de negocio.

**Solución (Outbox pattern)**:

```
POST /bookings
  └── BEGIN TRANSACTION
        ├── INSERT INTO booking (...)         → booking creado
        └── INSERT INTO notification_outbox (status='pending')
      COMMIT
  └── Response 201 al cliente (rápido, sin depender de Meta)

APScheduler (cada 60s):
  └── SELECT ... FROM notification_outbox WHERE status='pending' FOR UPDATE SKIP LOCKED
        ├── Enviar WhatsApp vía API de Meta
        └── UPDATE notification_outbox SET status='sent'/'failed'
```

**Garantía**: la reserva y la notificación son atómicas respecto a la DB. Si el envío falla,
el booking ya está confirmado y el outbox queda en `failed` para reintentar o investigar.

**Compensación**: la notificación puede tardar hasta 60s. En la práctica no importa — el cliente
ya sabe que reservó (recibió el 201).

---

## 7. Webhooks y seguridad

### WhatsApp (Meta)

| Operación | Detalle |
|---|---|
| Verificación (`GET`) | Compara `hub.verify_token` con `META_VERIFY_TOKEN` del env |
| Eventos (`POST`) | Valida `X-Hub-Signature-256` con HMAC-SHA256 sobre el body crudo |
| Rechazo | Si la firma no coincide → 403 inmediato |

### Mercado Pago

| Operación | Detalle |
|---|---|
| Firma | `x-signature` (HMAC-SHA256) sobre manifest `id:{data_id};request-id:{x_request_id};ts:{ts};` con `MP_SECRET_KEY` |
| Replay protection | Rechaza timestamps > 5 minutos |
| Idempotencia | Tabla `payment_events` con `event_id` como PK — si ya existe y está `processed`, retorna `DUPLICATE_EVENT_IGNORED` |
| Auto-creación | Si el webhook trae un pago aprobado sin `Payment` en DB, lo crea on-the-fly desde los datos de MP |
| Confirmación | Si el pago está `approved` → confirma el booking y encola el WhatsApp de confirmación |

---

## 8. Scheduler y locks distribuidos

**Problema**: si corren 2+ instancias de la API (o en el futuro), cada una ejecuta el scheduler.
El outbox podría procesarse dos veces, y los clientes recibirían WhatsApp duplicados.
Nadie quiere eso.

**Solución**: lock distribuido en Redis con `SET NX EX` al inicio de cada job.
Si otra instancia tiene el lock, el job sale silenciosamente. Sin logging de ruido, sin errores.

```python
lock = await redis.set(f"lock:{job_name}", "1", nx=True, ex=ttl_seconds)
if not lock:
    return  # Otra instancia se encargó
```

**Limitación actual**: el scheduler vive dentro del proceso de la API. Si la API se reinicia,
los jobs se detienen hasta que el proceso vuelva. En producción con 1 réplica, esto es aceptable.

**Futuro**: migrar a worker separado (Celery o ARQ) cuando tengamos >1 réplica o la carga lo justifique.

---

## 9. Zonas horarias

**Almacenamiento**: `TIMESTAMPTZ` (con zona horaria) en UTC. Siempre.

**Cálculo de slots**: en el timezone del tenant (campo `timezone`, ej. `America/Argentina/Buenos_Aires`).
Usamos `zoneinfo` de la stdlib — sin dependencia de `pytz`.

**Input de clientes**: se acepta `datetime` con o sin timezone. Si viene naive (sin info de zona),
se interpreta como hora local del tenant.

**Mensajes de WhatsApp**: la fecha se formatea al timezone del tenant antes de incluirla en la plantilla.

---

## 10. Observabilidad

| Canal | Qué captura |
|---|---|
| **Sentry** | Excepciones con stack trace, endpoint, tenant |
| **Logs** | `logging.basicConfig` nivel INFO, estructurado |
| **Health check** | `GET /health` → `{"status": "ok"}` (básico por ahora) |
| **Métricas** | Pendiente (PostHog en roadmap Q3 2027) |

> **TODO**: extender `/health` para verificar DB y Redis, no solo que uvicorn respire.
> Está en la lista de Fase 0.

---

## 11. Backups

El script `scripts/backup_db.sh` hace un `pg_dump` comprimido con gzip y rota backups con más de 30 días.

```bash
# Backup manual
./scripts/backup_db.sh

# Backup a directorio específico
./scripts/backup_db.sh /mnt/backup-externo

# Restaurar (⚠️ borra los datos actuales)
gunzip -c backups/saas_db_20260922_120000.sql.gz | \
  docker compose exec -T db psql -U postgres -d saas_db
```

**Automatización**: pendiente como cron en el VPS de producción (Fase 0).

---

## 12. Tests

**8 archivos de test, +41 tests, todos pytest-asyncio**:

| Archivo | Qué cubre |
|---|---|
| `test_auth.py` | Autenticación, cross-tenant isolation, `last_used_at` |
| `test_booking_constraints.py` | ExcludeConstraint cross-tenant, race conditions |
| `test_integration.py` | Flujo de reserva end-to-end + webhooks |
| `test_mp_webhooks.py` | Idempotencia, replay protection, timestamp |
| `test_public_endpoints.py` | Endpoints públicos + integración MP completa |
| `test_server_defaults.py` | Defaults a nivel DB + TIMESTAMPTZ |
| `test_slots.py` | Cálculo de slots disponibles |
| `test_whatsapp_webhooks.py` | Firma HMAC y manejo de eventos WA |

**Fixtures compartidos** en `tests/conftest.py`.
**DB de tests**: `saas_test` (aislada de `saas_db`).
**CI**: GitHub Actions corre todos los tests en cada push a `main`.

---

## 13. Deuda técnica conocida

| Deuda | Impacto | Cuándo atacarla |
|---|---|---|
| Health check profundo (DB + Redis) | Medio — los deploys ciegos no detectan dependencias caídas | Fase 0 (ahora) |
| Worker separado para el scheduler | Alto — necesario para >1 réplica | Q2 2027 |
| Rate limiting en endpoints públicos | Medio — sin protección contra abuso | Q2 2027 |
| `BusinessHours` configurable | Medio — horarios hardcoded 09-18 | Q2 2027 |
| Migrar a MP Orders API | Bajo — Preferencias funciona pero es "legacy" | Q2 2027 |
| Métricas de producto | Bajo — no hay dashboards para tenants | Q3 2027 |

Ver `DECISIONS.md` → Roadmap para el timeline completo.
