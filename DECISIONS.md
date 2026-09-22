# Decisiones de diseño — Juturno

> *Este documento registra las decisiones técnicas importantes y **por qué** se tomaron.*
> *Es útil cuando en el futuro alguien (vos, un colaborador, o yo mismo) se pregunte "¿por qué está hecho así?" — o cuando haya que defenderlo en una code review a las 11pm.*

## Formato

Cada decisión sigue este formato:
- **Fecha**: cuándo se tomó.
- **Contexto**: qué problema había que resolver.
- **Decisión**: qué se hizo.
- **Alternativas consideradas**: qué otras opciones había y por qué no se eligieron.
- **Consecuencias**: ventajas ✅, desventajas ⚠️, deuda técnica generada 📌.

---

## D-001: Multi-tenancy con shared database

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos que múltiples negocios usen la misma aplicación sin ver los datos de otros.
El clásico dilema: ¿una DB por tenant o todos juntos?

**Decisión**: Shared database con columna `tenant_id` en todas las tablas relevantes.
Aislamiento garantizado por lógica de aplicación + constraints de DB.

**Alternativas**:
- **Database per tenant**: máximo aislamiento, pero requiere N conexiones, N pools, N veces los mismos Alembic migrations. Operacionalmente caro.
- **Schema per tenant**: posible en Postgres, pero complejo de migrar y de gestionar con SQLModel/Alembic.

**Consecuencias**:
- ✅ Operación simple: una sola DB, un solo pool de conexiones, un solo backup.
- ✅ Costo bajo: un servidor atiende a N tenants sin N veces el overhead.
- ✅ Migraciones únicas: una sola versión del schema para todos.
- ⚠️ Riesgo: si hay un bug en el filtrado por `tenant_id`, un tenant puede ver datos de otro. Mitigado con tests cross-tenant y la dependencia `get_current_tenant` obligatoria en todos los endpoints.
- 📌 Deuda: si un tenant crece mucho (millones de filas), no hay forma de aislarlo fácilmente sin migración disruptiva.

---

## D-002: Autenticación con API key (no JWT)

**Fecha**: Septiembre 2026

**Contexto**: Los dueños de negocios necesitan autenticarse contra la API. ¿Cuánta complejidad es necesaria?

**Decisión**: Header `X-Tenant-API-Key` con la key hasheada (SHA-256) almacenada en DB.

**Alternativas**:
- **JWT con email/password**: más complejo, requiere gestión de sesiones, refresh tokens, blacklist.
- **OAuth2**: overkill para un SaaS B2B sin terceros involucrados en el auth.
- **Basic Auth**: no permite rotación ni revocación granular.

**Consecuencias**:
- ✅ Simple: una key, un lookup, un header.
- ✅ Rotación sin downtime: múltiples keys activas por tenant. Creás la nueva, migrás los clientes, revocás la vieja.
- ✅ Revocación granular: `revoked_at` por key individual.
- ✅ Cache en Redis (TTL 60s) — sin round-trip a Postgres en cada request.
- ⚠️ Una key puede acceder a todo el tenant — no hay permisos granulares por usuario o recurso.
- 📌 Deuda: cuando agreguemos panel admin con usuarios internos, vamos a necesitar JWT o similar.

**Por qué SHA-256 y no bcrypt**:
Las keys son secretos de 256 bits generados por CLI (alta entropía). No hay ataque de diccionario posible.
El determinismo de SHA-256 permite buscar por índice único. Bcrypt obligaría a iterar todas las keys en cada request — exactamente lo contrario de lo que queremos.

---

## D-003: ExcludeConstraint para anti-solapamiento de bookings

**Fecha**: Septiembre 2026

**Contexto**: No puede haber dos bookings superpuestos para el mismo staff en el mismo tenant.
¿Cómo garantizarlo bajo concurrencia?

**Decisión**: `EXCLUDE USING gist` de PostgreSQL con extensión `btree_gist` y `tstzrange`.

```sql
EXCLUDE USING gist (
  tenant_id WITH =,
  COALESCE(staff_id, -1) WITH =,
  tstzrange(start_time, end_time) WITH &&
)
```

**Alternativas**:
- **Validar en aplicación**: elegante hasta que llegan dos requests simultáneos. La race condition es inevitable.
- **Trigger de DB**: funciona, pero es código en PL/pgSQL disperso entre las migraciones — difícil de testear y mantener.
- **Lock advisory de Postgres**: peor performance sin mejor garantía.

**Consecuencias**:
- ✅ Atómico: la DB garantiza unicidad incluso bajo concurrencia extrema — no hay race condition posible.
- ✅ Declarativo y versionado: el constraint vive en la migración de Alembic.
- ✅ Cross-tenant safe: `tenant_id` como primera dimensión garantiza que dos tenants nunca colisionen.
- ✅ Staff NULL seguro: `COALESCE(staff_id, -1)` agrupa los bookings sin staff en un "bucket" ficticio.
- ⚠️ Requiere extensión `btree_gist` (incluida en Postgres 16, hay que hacer `CREATE EXTENSION` en la migración inicial).
- 📌 Deuda: si en el futuro se quiere permitir overlap condicional (ej. servicios grupales), hay que revisar el constraint.

---

## D-004: Patrón Outbox para notificaciones WhatsApp

**Fecha**: Septiembre 2026

**Contexto**: Enviar WhatsApp en el mismo request HTTP que crea el booking mete la latencia de Meta
en la respuesta al cliente. Si Meta se cae, la reserva falla. Inaceptable.

**Decisión**: Insertar `NotificationOutbox` en la misma transacción que el booking, y procesar los
pendientes con un job de APScheduler cada 60 segundos.

**Alternativas**:
- **Envío síncrono en el request**: mala UX, acopla la reserva a la disponibilidad de Meta.
- **`BackgroundTasks` de FastAPI**: la tarea se pierde si el proceso se reinicia antes de ejecutarla.
- **Celery / RQ**: más robusto para multi-instancia, pero agrega un worker extra, un broker, y otro proceso que monitorear. Overkill para ahora.

**Consecuencias**:
- ✅ Atomicidad: reserva y notificación son una sola transacción de DB.
- ✅ Resiliencia: si Meta se cae, el outbox queda en `failed` y se reintenta en el próximo ciclo.
- ✅ Desacoplamiento: la latencia de Meta (a veces generosa) no afecta al cliente.
- ⚠️ La notificación no es inmediata — puede tardar hasta 60s. En la práctica no importa.
- 📌 Deuda: si migramos a worker separado, este código se mueve al worker.

---

## D-005: APScheduler dentro del proceso de la API

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos jobs periódicos (outbox, recordatorios). ¿Proceso separado o in-process?

**Decisión**: `APScheduler AsyncIOScheduler` corriendo en el `lifespan` de FastAPI.

**Alternativas**:
- **Celery + Redis broker**: más robusto, pero requiere un worker aparte, configuración de broker, y toda una nueva capa de infraestructura.
- **Cron externo (crontab del sistema)**: complicado de coordinar dentro de Docker; no tiene contexto de la app.
- **RQ / Dramatiq**: alternativas a Celery con las mismas desventajas para este caso.

**Consecuencias**:
- ✅ Simplicidad absoluta: un solo proceso, un solo Dockerfile.
- ✅ Sin infraestructura extra: no hay broker, no hay worker, no hay más cosas que se pueden caer.
- ✅ Comparte el contexto de la app (DB session, config) sin IPC.
- ⚠️ Si corren N réplicas, cada una ejecuta el cron. Mitigado con lock Redis (`SET NX EX`).
- ⚠️ Si el proceso de la API se cae, los jobs se detienen hasta que se reinicie.
- 📌 Deuda: migrar a worker separado cuando tengamos >1 réplica estable o cuando la carga lo justifique.

---

## D-006: Mercado Pago con API de Preferencias (no Orders)

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos cobrar señas antes de confirmar el turno.

**Decisión**: Usar la API de Preferencias (`POST /checkout/preferences`) de MP, con generación
automática de preference al crear un booking público y campo `deposit_amount` configurable por servicio.

**Alternativas**:
- **API de Orders**: la nueva, recomendada por MP para proyectos nuevos. Más features, pero la documentación argentina está desactualizada y los ejemplos de la comunidad escasean.
- **Checkout Bricks**: exige construir el frontend — todavía no tenemos uno.
- **Checkout API**: máximo control, máximo trabajo. Para 2028.

**Consecuencias**:
- ✅ Funciona hoy, está probada en sandbox end-to-end.
- ✅ Amplia base de ejemplos y comunidad en LATAM.
- ✅ Checkout Pro redirige a MP — zero frontend necesario por ahora.
- ⚠️ MP la llama "legacy" y no recibe nuevas features.
- 📌 Deuda: migrar a Orders API en Q2 2027 (con los primeros 20-30 clientes reales).

---

## D-007: WhatsApp con plantillas de categoría Utility

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos notificar confirmaciones de reserva y recordatorios 24h antes.

**Decisión**: Usar plantillas aprobadas por Meta de categoría "Utility" (`booking_confirmation`, `booking_reminder`).

**Alternativas**:
- **Mensajes de sesión** (texto libre): solo disponibles dentro de la ventana de 24h después de que el cliente escriba. No aplica para confirmaciones automáticas.
- **Plantillas de Marketing**: rechazadas para notificaciones transaccionales y se cobran como Marketing (más caro, peor deliverability).

**Consecuencias**:
- ✅ Aprobadas por Meta — sin riesgo de bloqueo del número.
- ✅ Categoría correcta — no se cobran como Marketing.
- ✅ Funcionan fuera de la ventana de 24h — podemos enviar cuando queramos.
- ⚠️ Los cambios a plantillas requieren re-aprobación (24h a 72h de espera).
- ⚠️ A partir de octubre 2026, Meta cobra por estos mensajes (ver D-008).

---

## D-008: Modelo de negocio — absorber costos de WhatsApp en la suscripción

**Fecha**: Septiembre 2026

**Contexto**: Meta empezará a cobrar por mensajes de utilidad a partir del 1° de octubre de 2026.
¿Cobramos por mensaje, limitamos, o absorbemos?

**Decisión**: Absorber el costo de WhatsApp en la suscripción mensual (Pro = $15.000 ARS/mes ≈ $15 USD).

**Alternativas**:
- **Cobrar por mensaje**: administrativamente complejo, los clientes lo odian, difícil de predecir el costo.
- **Limitar cantidad de mensajes**: frustra al cliente y hace el producto menos valioso.
- **Plan sin WhatsApp**: pierde el principal diferenciador del producto.

**Consecuencias**:
- ✅ Modelo simple: un solo precio mensual, sin sorpresas.
- ✅ El costo de WhatsApp es despreciable frente al ingreso.
- ✅ Foco en valor entregado, no en consumo.
- 📌 Revisar cuando tengamos >1000 mensajes/mes por tenant.

**Proyección de costos**:

| Métrica | Valor |
|---|---|
| Turnos promedio/mes por tenant | ~100 |
| Mensajes por turno (confirmación + recordatorio) | 2 |
| Total mensajes/tenant/mes | ~200 |
| Costo por mensaje (Meta) | ~$0.007 USD |
| Costo WhatsApp/tenant/mes | ~$1.4 USD |
| Precio suscripción Pro | $15.000 ARS (~$15 USD) |
| Margen estimado | >90% |

---

## D-009: Timezones con `zoneinfo` (no `pytz`)

**Fecha**: Septiembre 2026

**Contexto**: Cada tenant tiene su timezone. Hay que convertir fechas correctamente — especialmente
con DST (cambio de horario), que en Argentina siempre fue una fuente de bugs.

**Decisión**: Usar `zoneinfo` de la stdlib de Python 3.9+ y columna `timezone` (string IANA) por tenant.

**Alternativas**:
- **`pytz`**: dependencia externa, maneja "aware datetimes" de forma diferente a la stdlib (el famoso problema de `localize()`). Mejor evitar si la stdlib lo resuelve.
- **Hardcodear UTC siempre**: ignora que los clientes en LATAM quieren ver horarios locales en sus mensajes.

**Consecuencias**:
- ✅ Sin dependencia externa para manejo de timezones.
- ✅ Timezone real por tenant — los mensajes muestran la hora local correcta.
- ✅ Correcto en cambios de horario (DST), tanto los que aplican como los que no (Argentina).
- ⚠️ Requiere que `tzdata` esté instalado en el sistema (incluido en el Dockerfile).
- 📌 Deuda: si un tenant tiene múltiples ubicaciones físicas, va a necesitar timezone por staff o por sucursal.

---

## D-010: Docker Compose en un solo VPS (no Kubernetes)

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos un entorno de producción funcional y económico.
Kubernetes tiene su momento, y este no es ese momento.

**Decisión**: Docker Compose en un solo VPS con `docker-compose.prod.yml`.

**Alternativas**:
- **Kubernetes (K8s)**: el estándar de industria, pero overkill para 1 servidor y 0 SREs. La curva de aprendizaje no se justifica hoy.
- **Fly.io / Railway**: managed, menor control, potencialmente más caro a escala.
- **Bare metal sin Docker**: menos reproducible, más diff entre dev y prod.

**Consecuencias**:
- ✅ Simplicidad: `docker compose up -d` y el sistema está corriendo.
- ✅ Reproducibilidad: mismo entorno en dev y prod (salvo el `.env`).
- ✅ Económico: un VPS de $10-20 USD/mes soporta 50-100 tenants cómodamente.
- ⚠️ No escala horizontalmente sin refactor (múltiples réplicas requieren worker separado).
- ⚠️ Si el VPS se cae, el servicio cae. No hay HA automático.
- 📌 Deuda: evaluar migrar a Kubernetes o Fly.io cuando lleguemos a 500+ tenants activos.

---

## D-011: Script de backup con rotación automática

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos backups de Postgres que no requieran intervención manual diaria,
y que no acumulen archivos eternamente en el disco.

**Decisión**: Script Bash `scripts/backup_db.sh` con `pg_dump | gzip` y rotación con `find -mtime`.

**Alternativas**:
- **`pg_basebackup`**: más adecuado para backups físicos y WAL shipping, pero más complejo para restaurar dumps simples.
- **Herramienta de backup dedicada (Barman, pgBackRest)**: excelente para HA y PITR, overkill para un VPS chico.
- **Backup manual**: existe hasta que uno se olvida el día que más lo necesita.

**Consecuencias**:
- ✅ Simple: un script, un cron, un directorio de backups.
- ✅ Comprimido con gzip — mucho menor tamaño en disco.
- ✅ Rotación automática (30 días) — no hay que acordarse de limpiar.
- ✅ Falla ruidosamente: si el dump queda vacío, el script sale con error (set -euo pipefail).
- ⚠️ Los backups están en el mismo disco que la DB — si el disco falla, perdemos todo. Para producción, copiar a S3 o similar.
- 📌 Deuda: automatizar como cron en el VPS y agregar copia a almacenamiento externo.

---

## Roadmap de deuda técnica

Ordenado por impacto/urgencia estimada:

### Fase 0 — Autonomía (Septiembre-Octubre 2026)
1. **Health check profundo** que verifique DB y Redis.
2. **Backup automatizado** como cron en el VPS de producción + copia a S3.
3. **Test de restore** del backup para verificar que funciona cuando importa.

### Q1 2027 (mes 1-3)
4. **Deploy a producción** (VPS + dominio + Cloudflare Named Tunnel).
5. **Frontend público + panel admin** (Next.js) para booking y gestión.
6. **Onboarding self-service** para nuevos tenants.

### Q2 2027 (mes 4-6)
7. **Worker separado** del scheduler (Celery o ARQ) para >1 réplica.
8. **Rate limiting** en endpoints públicos (slowapi).
9. **`BusinessHours` configurable** por tenant (hoy hardcoded 09-18).
10. **Migrar a MP Orders API**.

### Q3 2027 (mes 7-9)
11. **Métricas de producto** para dueños de negocios (PostHog o similar).
12. **Notificaciones configurables** por tenant (24h, 2h, ambas).
13. **Multi-staff avanzado** (rotaciones, turnos compartidos).

### Q4 2027 (mes 10-12)
14. **Escalabilidad multi-instancia** (Kubernetes o Fly.io).
15. **API pública** para integraciones externas.
16. **Facturación AFIP** (integración).

---

## ¿Cómo agregar una nueva decisión?

1. Copiar el formato de las decisiones existentes.
2. Numerar como `D-0XX` (siguiente número disponible).
3. Ser honesto sobre las consecuencias negativas y la deuda generada. La honestidad documental vale oro.
4. Actualizar el roadmap si la decisión genera deuda nueva.
5. Commitear con `docs: add D-0XX decision about X`.
