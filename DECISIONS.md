# Decisiones de diseño — Juturno

> *Este documento registra las decisiones técnicas importantes y **por qué** se tomaron.
> Es la referencia para cuando, en el futuro, haya que revisar o defender una decisión técnica,
> ya sea en una revisión de código o en una discusión de arquitectura.*

## Formato

Cada decisión sigue este formato:
- **Fecha**: cuándo se tomó.
- **Contexto**: qué problema había que resolver.
- **Decisión**: qué se hizo.
- **Alternativas consideradas**: qué otras opciones había y por qué no se eligieron.
- **Consecuencias**: ventajas, riesgos y deuda técnica generada, cada uno marcado con su etiqueta.

---

## D-001: Multi-tenancy con shared database

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos que múltiples negocios usen la misma aplicación sin ver los datos de otros.
El dilema habitual: ¿una DB por tenant o todos juntos?

**Decisión**: Shared database con columna `tenant_id` en todas las tablas relevantes.
Aislamiento garantizado por lógica de aplicación + constraints de DB.

**Alternativas**:
- **Database per tenant**: máximo aislamiento, pero requiere N conexiones, N pools, N veces los mismos Alembic migrations. Operacionalmente caro.
- **Schema per tenant**: posible en Postgres, pero complejo de migrar y de gestionar con SQLModel/Alembic.

**Consecuencias**:
- **Ventaja** — Operación simple: una sola DB, un solo pool de conexiones, un solo backup.
- **Ventaja** — Costo bajo: un servidor atiende a N tenants sin N veces el overhead.
- **Ventaja** — Migraciones únicas: una sola versión del schema para todos.
- **Riesgo** — Si hay un bug en el filtrado por `tenant_id`, un tenant puede ver datos de otro. Mitigado con tests cross-tenant y la dependencia `get_current_tenant` obligatoria en todos los endpoints.
- **Deuda** — Si un tenant crece mucho (millones de filas), no hay forma de aislarlo fácilmente sin migración disruptiva.

---

## D-002: Autenticación con API key (no JWT)

**Fecha**: Septiembre 2026

**Contexto**: Los dueños de negocios necesitan autenticarse contra la API. ¿Cuánta complejidad es necesaria?

**Decisión**: Header `X-Tenant-API-Key` con la key hasheada (SHA-256) almacenada en DB.

**Alternativas**:
- **JWT con email/password**: más complejo, requiere gestión de sesiones, refresh tokens, blacklist.
- **OAuth2**: no justificado para un SaaS B2B sin terceros involucrados en el auth.
- **Basic Auth**: no permite rotación ni revocación granular.

**Consecuencias**:
- **Ventaja** — Simple: una key, un lookup, un header.
- **Ventaja** — Rotación sin downtime: múltiples keys activas por tenant. Se crea la nueva, se migran los clientes y se revoca la vieja.
- **Ventaja** — Revocación granular: `revoked_at` por key individual.
- **Ventaja** — Cache en Redis (TTL 60s) — sin round-trip a Postgres en cada request.
- **Riesgo** — Una key puede acceder a todo el tenant — no hay permisos granulares por usuario o recurso.
- **Deuda** — Cuando se agregue el panel admin con usuarios internos, será necesario JWT o un mecanismo equivalente.

**Por qué SHA-256 y no bcrypt**:
Las keys son secretos de 256 bits generados por CLI (alta entropía). No hay ataque de diccionario posible.
El determinismo de SHA-256 permite buscar por índice único. Bcrypt obligaría a iterar todas las keys en cada request — exactamente lo contrario de lo que se busca aquí.

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
- **Validar en aplicación**: válido hasta que llegan dos requests simultáneos. La race condition es inevitable.
- **Trigger de DB**: funciona, pero es código en PL/pgSQL disperso entre las migraciones — difícil de testear y mantener.
- **Lock advisory de Postgres**: peor performance sin mejor garantía.

**Consecuencias**:
- **Ventaja** — Atómico: la DB garantiza unicidad incluso bajo concurrencia extrema — no hay race condition posible.
- **Ventaja** — Declarativo y versionado: el constraint vive en la migración de Alembic.
- **Ventaja** — Cross-tenant safe: `tenant_id` como primera dimensión garantiza que dos tenants nunca colisionen.
- **Ventaja** — Staff NULL seguro: `COALESCE(staff_id, -1)` agrupa los bookings sin staff en un "bucket" ficticio.
- **Riesgo** — Requiere extensión `btree_gist` (incluida en Postgres 16, hay que hacer `CREATE EXTENSION` en la migración inicial).
- **Deuda** — Si en el futuro se quiere permitir overlap condicional (ej. servicios grupales), hay que revisar el constraint.

---

## D-004: Patrón Outbox para notificaciones WhatsApp

**Fecha**: Septiembre 2026

**Contexto**: Enviar WhatsApp en el mismo request HTTP que crea el booking introduce la latencia de Meta
en la respuesta al cliente. Si Meta se cae, la reserva falla. Inaceptable.

**Decisión**: Insertar `NotificationOutbox` en la misma transacción que la operación que la dispara
(confirmación de pago vía webhook MP, o encolar recordatorio via scheduler), y procesar los
pendientes con un job de APScheduler cada 60 segundos.

**Alternativas**:
- **Envío síncrono en el request**: mala UX, acopla la reserva a la disponibilidad de Meta.
- **`BackgroundTasks` de FastAPI**: la tarea se pierde si el proceso se reinicia antes de ejecutarla.
- **Celery / RQ**: más robusto para multi-instancia, pero agrega un worker extra, un broker, y otro proceso que monitorear. No justificado para el volumen actual.

**Consecuencias**:
- **Ventaja** — Atomicidad: confirmación de pago y notificación son una sola transacción de DB (webhook MP).
- **Ventaja** — Resiliencia: si Meta se cae, el outbox queda en `failed` y se reintenta en el próximo ciclo.
- **Ventaja** — Desacoplamiento: la latencia de Meta no afecta la respuesta al cliente.
- **Riesgo** — La notificación no es inmediata: puede tardar hasta 60s. No afecta la experiencia de reserva.
- **Deuda** — Si se migra a un worker separado, este código se mueve al worker.

> **Nota**: `POST /public/bookings` y `POST /bookings` crean `Booking` (+ `Payment` en el público), pero **no** crean `NotificationOutbox` en esa transacción. El outbox se crea cuando el webhook MP confirma el pago y transiciona el booking a `confirmed`.

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
- **Ventaja** — Simplicidad: un solo proceso, un solo Dockerfile.
- **Ventaja** — Sin infraestructura extra: no hay broker, no hay worker, no hay procesos adicionales que puedan fallar.
- **Ventaja** — Comparte el contexto de la app (DB session, config) sin IPC.
- **Riesgo** — Si corren N réplicas, cada una ejecuta el cron. Mitigado con lock Redis (`SET NX EX`).
- **Riesgo** — Si el proceso de la API se cae, los jobs se detienen hasta que se reinicie.
- **Deuda** — Migrar a worker separado cuando haya >1 réplica estable o cuando la carga lo justifique.

---

## D-006: Mercado Pago con API de Preferencias (no Orders)

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos cobrar señas antes de confirmar el turno.

**Decisión**: Usar la API de Preferencias (`POST /checkout/preferences`) de MP, con generación
automática de preference al crear un booking público y campo `deposit_amount` configurable por servicio.

**Alternativas**:
- **API de Orders**: la nueva, recomendada por MP para proyectos nuevos. Más features, pero la documentación argentina está desactualizada y los ejemplos de la comunidad escasean.
- **Checkout Bricks**: exige construir el frontend — todavía no existe uno.
- **Checkout API**: máximo control y máxima complejidad de implementación. No aplica al alcance actual.

**Consecuencias**:
- **Ventaja** — Funciona hoy, está probada en sandbox end-to-end.
- **Ventaja** — Amplia base de ejemplos y comunidad en LATAM.
- **Ventaja** — Checkout Pro redirige a MP — no requiere frontend propio por ahora.
- **Riesgo** — MP la clasifica como "legacy" y no recibe nuevas features.
- **Deuda** — Migrar a Orders API en Q2 2027 (con los primeros 20-30 clientes reales).

---

## D-007: WhatsApp con plantillas de categoría Utility

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos notificar confirmaciones de reserva y recordatorios 24h antes.

**Decisión**: Usar plantillas aprobadas por Meta de categoría "Utility" (`booking_confirmation`, `booking_reminder`).

**Alternativas**:
- **Mensajes de sesión** (texto libre): solo disponibles dentro de la ventana de 24h después de que el cliente escriba. No aplica para confirmaciones automáticas.
- **Plantillas de Marketing**: rechazadas para notificaciones transaccionales y se cobran como Marketing (más caro, peor deliverability).

**Consecuencias**:
- **Ventaja** — Aprobadas por Meta — sin riesgo de bloqueo del número.
- **Ventaja** — Categoría correcta — no se cobran como Marketing.
- **Ventaja** — Funcionan fuera de la ventana de 24h — se pueden enviar en cualquier momento.
- **Riesgo** — Los cambios a plantillas requieren re-aprobación (24h a 72h de espera).
- **Riesgo** — A partir de octubre 2026, Meta cobra por estos mensajes (ver D-008).

---

## D-008: Modelo de negocio — absorber costos de WhatsApp en la suscripción

**Fecha**: Septiembre 2026

**Contexto**: Meta empezará a cobrar por mensajes de utilidad a partir del 1° de octubre de 2026.
¿Cobramos por mensaje, limitamos, o absorbemos?

**Decisión**: Absorber el costo de WhatsApp en la suscripción mensual (Pro = $15.000 ARS/mes ≈ $15 USD).

**Alternativas**:
- **Cobrar por mensaje**: administrativamente complejo, los clientes lo rechazan, difícil de predecir el costo.
- **Limitar cantidad de mensajes**: frustra al cliente y hace el producto menos valioso.
- **Plan sin WhatsApp**: pierde el principal diferenciador del producto.

**Consecuencias**:
- **Ventaja** — Modelo simple: un solo precio mensual, sin sorpresas.
- **Ventaja** — El costo de WhatsApp es despreciable frente al ingreso.
- **Ventaja** — Foco en valor entregado, no en consumo.
- **Deuda** — Revisar el modelo cuando se supere los 1000 mensajes/mes por tenant.

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
con DST (cambio de horario), históricamente una fuente de bugs.

**Decisión**: Usar `zoneinfo` de la stdlib de Python 3.9+ y columna `timezone` (string IANA) por tenant.

**Alternativas**:
- **`pytz`**: dependencia externa, maneja "aware datetimes" de forma diferente a la stdlib (el conocido problema de `localize()`). Se prefiere la stdlib cuando resuelve el caso.
- **Hardcodear UTC siempre**: ignora que los clientes en LATAM quieren ver horarios locales en sus mensajes.

**Consecuencias**:
- **Ventaja** — Sin dependencia externa para manejo de timezones.
- **Ventaja** — Timezone real por tenant — los mensajes muestran la hora local correcta.
- **Ventaja** — Correcto en cambios de horario (DST), tanto los que aplican como los que no (Argentina).
- **Riesgo** — Requiere que `tzdata` esté instalado en el sistema (incluido en el Dockerfile).
- **Deuda** — Si un tenant tiene múltiples ubicaciones físicas, va a necesitar timezone por staff o por sucursal.

---

## D-010: Docker Compose en un solo VPS (no Kubernetes)

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos un entorno de producción funcional y económico.
Kubernetes es el estándar de industria, pero su coste operativo no se justifica para el volumen actual.

**Decisión**: Docker Compose en un solo VPS con `docker-compose.prod.yml`.

**Alternativas**:
- **Kubernetes (K8s)**: el estándar de industria, pero desproporcionado para 1 servidor y 0 SREs. La curva de aprendizaje no se justifica hoy.
- **Fly.io / Railway**: managed, menor control, potencialmente más caro a escala.
- **Bare metal sin Docker**: menos reproducible, más diff entre dev y prod.

**Consecuencias**:
- **Ventaja** — Simplicidad: `docker compose up -d` y el sistema está corriendo.
- **Ventaja** — Reproducibilidad: mismo entorno en dev y prod (salvo el `.env`).
- **Ventaja** — Económico: un VPS de $10-20 USD/mes soporta 50-100 tenants cómodamente.
- **Riesgo** — No escala horizontalmente sin refactor (múltiples réplicas requieren worker separado).
- **Riesgo** — Si el VPS se cae, el servicio cae. No hay HA automático.
- **Deuda** — Evaluar migrar a Kubernetes o Fly.io cuando se llegue a 500+ tenants activos.

---

## D-011: Script de backup con rotación automática

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos backups de Postgres que no requieran intervención manual diaria,
y que no acumulen archivos eternamente en el disco.

**Decisión**: Script Bash `scripts/backup_db.sh` con `pg_dump | gzip` y rotación con `find -mtime`.

**Alternativas**:
- **`pg_basebackup`**: más adecuado para backups físicos y WAL shipping, pero más complejo para restaurar dumps simples.
- **Herramienta de backup dedicada (Barman, pgBackRest)**: excelente para HA y PITR, desproporcionado para un VPS pequeño.
- **Backup manual**: depende de la disciplina humana y falla justamente cuando más se necesita.

**Consecuencias**:
- **Ventaja** — Simple: un script, un cron, un directorio de backups.
- **Ventaja** — Comprimido con gzip — mucho menor tamaño en disco.
- **Ventaja** — Rotación automática (30 días) — no requiere limpieza manual.
- **Ventaja** — Falla de forma visible: si el dump queda vacío, el script sale con error (set -euo pipefail).
- **Riesgo** — Los backups están en el mismo disco que la DB — si el disco falla, se pierde todo. Mitigado: el script sube a S3 cuando `S3_BACKUP_BUCKET` está configurada (commit `746778c`).
- **Deuda** — Automatizar como cron en el VPS (configuración pendiente en el host). Copia a S3 resuelta en `746778c`. Test de restore documentado en `DEPLOYMENT.md` §7.

---

## D-012: Cuenta Mercado Pago por tenant (OAuth) con dinero directo al propietario

**Fecha**: Septiembre 2026

**Contexto**: Hoy todas las preferencias de pago se crean con el `MP_ACCESS_TOKEN` de la
plataforma, por lo que las señas de todos los tenants entran a la cuenta del operador del
SaaS. Con dinero real esto genera tres problemas: (1) recibir sistemáticamente dinero de
terceros viola los términos de MP, que puede retener fondos o cerrar la cuenta; (2) ese
dinero factura a nombre del operador (exposición fiscal en IVA/Ingresos Brutos); (3) el
operador retiene fondos que económicamente pertenecen al negocio, sin acuerdo que lo
ampare. Los reclamos y chargebacks de clientes ajenos además caen en el operador.

**Decisión**: Cada tenant conecta su propia cuenta de Mercado Pago vía OAuth (flujo
authorization code). El `access_token` y `refresh_token` se guardan cifrados (Fernet) por
tenant y toda preferencia de pago se crea con el token del tenant: el dinero aterriza
directo en la cuenta del propietario. Regla de fallback: con `MP_SANDBOX=true` un tenant
sin conectar usa la cuenta de la plataforma (dinero de prueba, irrelevante); con
`MP_SANDBOX=false` un tenant sin conectar no puede cobrar: la reserva pública se rechaza
hasta que conecte su cuenta. Conectar MP pasa a ser requisito de alta del negocio.

**Alternativas**:
- **Un solo token de la plataforma para todos** (status quo): el problema que motiva la decisión. Solo viable con dinero de prueba.
- **Split payments / marketplace de MP**: la plata pasa igualmente por la plataforma (mismo problema fiscal y de términos), y es un producto pensado para plataformas constituidas. Desproporcionado.
- **Alias por tenant con transferencia manual**: el checkout de MP no admite apuntar a un alias arbitrario; implicaría validar pagos manuales, un flujo distinto al actual.
- **Token pegado a mano por cada tenant (sin OAuth)**: funciona, pero exige que el dueño copie credenciales sensibles. OAuth es el mecanismo oficial y renueva sin volver a pedir permiso.

**Consecuencias**:
- **Ventaja** — El dinero de cada negocio entra directo a su cuenta: la plataforma no toca fondos de terceros por diseño.
- **Ventaja** — El riesgo legal/fiscal del cobro agregado desaparece en lugar de gestionarse.
- **Ventaja** — El alias del negocio queda disponible informativamente (vía `/users/me` al conectar) sin exponer credenciales.
- **Riesgo** — La consulta de pagos en el webhook depende de resolver el token correcto: el payload trae `user_id` (cuenta vendedora) que se matchea contra `tenant.mp_user_id`; sin match, fallback al token de la plataforma. Ítem a validar experimentalmente en el E2E.
- **Riesgo** — Los tokens OAuth vencen (180 días) y se renuevan on-demand con `refresh_token`; si un tenant revoca el acceso, sus pagos quedan bloqueados en producción (comportamiento correcto, pero hay que comunicarlo al dueño).
- **Deuda** — Renovación proactiva por scheduler (hoy solo on-demand), página de conexión con botón (dashboard, Fase 2), pagos por alias manual (fuera de alcance).

Plan de implementación aprobado: [`PLAN_MP_POR_TENANT.md`](PLAN_MP_POR_TENANT.md).

---

## D-013: Sesiones firmadas para panel web + session_version

**Fecha**: Septiembre 2026

**Contexto**: El panel del negocio (dueño/staff) necesita autenticación web separada del API key que usan los clientes. El dueño debe poder iniciar sesión con email + password, y al cambiar su contraseña todas las sesiones activas deben invalidarse inmediatamente.

**Decisión**: Cookie firmada HMAC-SHA256 con payload `{tenant_id}.{session_version}.{expires_at}`. La cookie se llama `juturno_session`. La tabla `tenant` tiene un campo `session_version` (int, default 1, server_default) que se incrementa al cambiar contraseña o hacer logout masivo.

**Alternativas consideradas**:
- **JWT**: requiere blacklist para revocación (complejidad extra en Redis/DB).
- **Session server-side en Redis**: agrega dependencia para sesiones, pero funciona.
- **Cookie sin firma**: obviamente inaceptable.

**Consecuencias**:
- ✅ **Stateless**: no hay storage de sesiones, la cookie lleva toda la info.
- ✅ **Invalidación instantánea**: cambiar `session_version` invalida TODAS las sesiones del tenant en un solo UPDATE.
- ✅ **Timing-safe**: usa `hmac.compare_digest` en la validación.
- ✅ **Sin dependencia extra**: HMAC está en stdlib.
- ⚠️ **Rotación de SECRET_KEY**: si se rota `SECRET_KEY`, todas las sesiones activas se invalidan (comportamiento deseado, pero requiere aviso a usuarios).
- 📌 **Deuda**: no hay endpoint de "cerrar sesión en todos los dispositivos" como tal — se hace incrementando `session_version` manualmente o desde un endpoint admin.

**Implementación**: `app/session.py` con `create_session_token()` y `parse_session_token()`. La dependencia `get_current_tenant_from_session` en `app/auth.py` valida la cookie y compara `session_version` con el valor en DB.

---

## D-014: SECRET_KEY no puede usar el valor default en producción

**Fecha**: Octubre 2026

**Contexto**: Durante el deploy inicial en el VPS con Coolify, el contenedor de la API entraba en crash loop con `pydantic_core.ValidationError: SECRET_KEY cannot be the default value in production`. El contenedor arrancaba, `Settings()` fallaba al validar, uvicorn no cargaba la app, y Coolify lo reiniciaba indefinidamente. Traefik respondía 503 "no available server" durante más de una hora.

La causa raíz: `config.py` tiene un validador que rechaza `SECRET_KEY == "change-this-secret-key-in-production-juturno"` cuando `ENVIRONMENT == "production"`. Es una defensa **correcta** (evita que un deploy en producción use el default), pero bloquea el arranque si nadie setea la variable.

**Decisión**: **Mantener el validador tal cual está.** Es la defensa correcta. El fix no es en código, es en el proceso de deploy:
1. `SECRET_KEY` debe estar listada como variable **obligatoria** en el `.env.example` y en el README.
2. Coolify debe tener `SECRET_KEY` seteada antes del primer deploy en producción.
3. El RUNBOOK documenta este incidente con su síntoma y diagnóstico.

**Alternativas consideradas**:
- **Permitir el default con warning**: peligroso, alguien podría deployar a producción con el default y firmar cookies con un secreto público.
- **Generar SECRET_KEY automáticamente al arrancar**: imposible, invalidaría sesiones en cada restart.
- **Fallback a un valor derivado**: complica debugging y crea dependencias entre variables.

**Consecuencias**:
- ✅ **Falla ruidosamente**: imposible deployar a producción con el default silenciosamente.
- ✅ **Fuerza la configuración explícita**: el operador tiene que tomar una decisión consciente.
- ⚠️ **Requiere documentación clara**: si no está en el RUNBOOK, el siguiente operador puede perder 30 min diagnosticando el mismo crash.
- 📌 **Deuda**: la rotación de `SECRET_KEY` no está documentada como procedimiento (invalidaría todas las sesiones activas — aceptable pero requiere aviso previo).

**Relacionado**: `META_APP_SECRET` y `MP_TOKEN_ENCRYPTION_KEY` tienen la misma categoría de "crítica en producción" pero sin validador. Deberían agregarse en un futuro pass.

---

## D-015: Refresh de tokens MP sin expires_in

**Fecha**: Octubre 2026

**Contexto**: El job `process_mp_token_refresh` (scheduler diario) filtra tenants con `mp_token_expires_at IS NOT NULL AND mp_token_expires_at <= now + 30d`. MP devuelve `expires_in` (~180 días) al canjear el code OAuth, pero si MP no lo incluye (edge case, cambio de API, cuenta de prueba), `mp_token_expires_at` queda `NULL` y el token **nunca se renueva**. Vence a los ~180 días sin aviso.

**Decisión**: En `refresh_tenant_mp_token` (app/mp_connect.py:183-233), si `expires_in` no viene en la respuesta de MP, setear `mp_token_expires_at = now + 180 días` (valor por defecto documentado por MP). Además, agregar fallback en el job: tenants con `mp_refresh_token_enc IS NOT NULL AND mp_token_expires_at IS NULL` → intentar refresh igual.

> **Estado**: Decisión registrada, **pendiente de implementación**. El código actual en `app/mp_connect.py:225-229` no aplica el fallback de 180 días si `expires_in` es nulo; el job `process_mp_token_refresh` (app/scheduler.py:181-187) excluye tenants con `mp_token_expires_at IS NULL`.

**Alternativas**:
- **Ignorar**: asumir que MP siempre manda `expires_in`. Riesgo: token vence silenciosamente.
- **Alertar y no refrescar**: requiere intervención manual. No escala.

**Consecuencias**:
- **Ventaja** — Tokens nunca quedan sin `expires_in`; refresh proactivo cubre edge cases.
- **Ventaja** — Default de 180 días alineado con documentación MP.
- **Riesgo** — Si MP cambia vida del token (ej. 90 días), el default queda desactualizado. Mitigado: MP notifica cambios de API con antelación.
- **Deuda** — Monitorear logs del job para detectar tenants con refresh fallido repetido.

---

## D-016: Atomicidad del outbox worker (commit por batch)

**Fecha**: Octubre 2026

**Estado**: Reemplazada por D-022 (commit por evento + reintentos).

**Contexto**: `process_outbox` (app/outbox_worker.py:32-91) usa un solo `async with session.begin()` que engloba todo el loop de eventos. Si un evento falla (ej. WhatsApp timeout), **todos** los eventos del batch hacen rollback — incluso los que se enviaron OK. Con commit por evento (diseño original), cada evento commiteaba su estado independiente.

**Decisión**: Mantener commit por batch (comportamiento actual). Rationale: volumen actual bajo (<200 eventos/min), simplicidad transaccional, y `FOR UPDATE SKIP LOCKED` ya aísla eventos entre workers. Si un evento falla, se reintentará en el próximo ciclo (60s).

**Alternativas**:
- **Commit por evento**: cada evento en su propia transacción. Más granular, pero más round-trips y complejidad de manejo de errores parciales.
- **Savepoints por evento**: rollback solo del evento fallido. Complejidad extra en SQLAlchemy async.

**Consecuencias**:
- **Ventaja** — Código simple, una transacción por corrida del job.
- **Ventaja** — `FOR UPDATE SKIP LOCKED` evita que dos workers procesen el mismo evento.
- **Riesgo** — Un fallo transitorio (red, rate limit) retrasa eventos exitosos del mismo batch hasta el próximo ciclo (60s).
- **Deuda** — Si volumen crece (>1000 eventos/min), migrar a commit por evento o worker separado (Celery/ARQ).

---

## D-017: Refactor unificar lógica available-slots

**Fecha**: Octubre 2026

**Contexto**: `get_available_slots` (panel, auth API key, línea 304) y `get_public_available_slots` (público, sin auth, línea 578) en `app/main.py` son **casi idénticos** (~100 líneas duplicadas). Diferencias: auth dependency, validación `tenant_id == current_tenant.id` vs lookup por ID, y manejo de 404 vs 401.

**Decisión**: Extraer lógica compartida a `app/services.py` como `_compute_available_slots(session, tenant_id, service_id, day, staff_id, tenant_timezone)` y llamar desde ambos endpoints. Los endpoints solo manejan auth, validación de tenant y respuesta HTTP.

> **Estado**: Decisión registrada, **pendiente de implementación**. El código actual mantiene la duplicación en `app/main.py` (líneas 304 y 578).

**Alternativas**:
- **Dejar duplicado**: simple pero riesgo de drift (fix en uno no llega al otro).
- **Decorator/auth dependency**: más complejo, no elimina duplicación de lógica de negocio.

**Consecuencias**:
- **Ventaja** — Single source of truth para cálculo de slots.
- **Ventaja** — Tests cubren una sola función; endpoints testean solo auth/validación.
- **Riesgo** — Refactor toca código crítico (slots). Requiere tests de regresión exhaustivos.
- **Deuda** — Hacer el refactor en PR dedicado con tests antes de nuevas features de slots.

---

## D-018: Validadores env vars críticas (extender D-014)

**Fecha**: Octubre 2026

**Contexto**: `config.py` valida `SECRET_KEY` no-default en producción (D-014), pero `META_APP_SECRET`, `MP_TOKEN_ENCRYPTION_KEY` y `MP_SECRET_KEY` son igual de críticas y no tienen validador. Deploy en prod sin ellas causa fallos silenciosos o errores crípticos en runtime (webhooks MP/WhatsApp rechazados, tokens MP no descifrables).

**Decisión**: Agregar validadores en `Settings.model_post_init` para:
- `META_APP_SECRET`: no vacío en prod (webhook WhatsApp falla 401).
- `MP_TOKEN_ENCRYPTION_KEY`: no vacío en prod (tokens MP no se pueden descifrar → 502).
- `MP_SECRET_KEY`: no vacío en prod (webhook MP falla 401).
- `WHATSAPP_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID`: no vacíos en prod (envío WhatsApp falla).

Mismo patrón que `SECRET_KEY`: `ValueError` con mensaje claro al arranque.

> **Estado**: Decisión registrada, **pendiente de implementación**. `app/config.py:40-45` solo valida `SECRET_KEY`; las demás variables aún no tienen validador.

**Alternativas**:
- **Warnings en logs**: no bloquea arranque, pero falla en runtime — peor UX operativa.
- **Defaults de desarrollo**: peligroso en prod si se olvida setear.

**Consecuencias**:
- **Ventaja** — Falla ruidosa al arranque, no en runtime.
- **Ventaja** — Mensaje de error accionable ("setea X en Coolify").
- **Riesgo** — Bloquea arranque si falta una var; requiere checklist de deploy actualizado.
- **Deuda** — Documentar checklist en DEPLOYMENT.md y RUNBOOK.md.

---

## D-019: Webhook MP — guard ordering, fail-closed en producción y aislamiento por tenant

**Fecha**: Octubre 2026

**Contexto**: La auditoría de seguridad del webhook `POST /webhooks/mercadopago` (Oct 2026)
detectó que los guards de tenant, currency y amount solo se aplicaban a pagos con
`payment_status == "approved"`. Los pagos en estado `pending`, `rejected` u otros podían
ser procesados sin verificar que el `collector_id` (cuenta vendedora) correspondiera al
tenant de la reserva, lo que permitía que un pago de un tenant apareciera como evento válido
en la fila `Payment` de otro. Adicionalmente, los guards corrían **después** de crear la
fila `Payment`, por lo que un rechazo posterior a la inserción dejaba una fila huérfana en
DB.

**Decisión**: Reestructurar el bloque de guards con el siguiente orden estricto, aplicado
**antes** de crear o actualizar cualquier fila `Payment`:

1. **Guard de tenant** (todos los estados): verificar que `collector_id` del response
   autenticado de MP coincide con `tenant.mp_user_id` de la reserva.
   - En **producción** (`ENVIRONMENT == "production"`): si el tenant no tiene `mp_user_id`
     configurado → `TENANT_MISMATCH` (fail-closed).
   - En **sandbox/dev**: si no hay `mp_user_id` → se omite el guard (retrocompatibilidad
     con entornos de prueba donde el tenant no pasó por el flujo OAuth).
2. **Guard de currency** (solo `approved`): rechazar si `currency_id != "ARS"`.
3. **Guard de amount** (solo `approved`):
   - Fail-closed si el servicio no existe (`service is None`).
   - Validar `paid_amount.is_finite()` antes de la comparación decimal (previene
     `InvalidOperation` con `NaN` o `Infinity`).
   - Rechazar si `paid_amount < deposit`.

**Alternativas**:
- **Guards solo en `approved`**: el status quo antes del fix. Dejaba `pending`/`rejected`
  sin validación de tenant — un pago de otro tenant podía quedar registrado con
  `booking_id` correcto.
- **Guard en la capa de routing antes del handler**: posible, pero complica la lectura del
  flujo y requiere conocer el `booking_id` antes del handler.
- **Fail-open siempre cuando no hay `mp_user_id`**: riesgo inaceptable en producción; un
  tenant que olvidó conectar MP procesaría pagos de cualquier cuenta vendedora.

**Consecuencias**:
- **Ventaja** — Ningún `Payment` se crea antes de validar que el pago pertenece al tenant
  correcto — sin filas huérfanas ni cross-tenant data.
- **Ventaja** — El comportamiento en producción es determinístico: sin `mp_user_id`
  configurado, los webhooks se rechazan hasta que el tenant complete el alta.
- **Ventaja** — La guardia de `is_finite()` elimina el riesgo de `InvalidOperation` con
  valores `NaN`/`Infinity` que MP podría enviar en edge cases.
- **Riesgo** — Un tenant que no completó la conexión OAuth en producción verá sus webhooks
  rechazados silenciosamente. Mitigation: la pantalla de onboarding debe mostrar el estado
  de conexión antes de publicar el link de reserva.
- **Deuda resuelta** — `deposit_at_booking`: resuelto en D-020 / commit `f128344`. El campo
  se snapshotea en `Booking` al momento de la creación; el guard de amount lo usa directamente
  y solo cae al fallback en filas anteriores a la migración.
- **Deuda** — Race condition: dos webhooks simultáneos del mismo `mp_payment_id` pueden
  crear dos filas `Payment`. Requiere UNIQUE constraint y `SELECT FOR UPDATE` (ver roadmap).

**Implementación**: `app/mp_webhooks.py`. Tests en `tests/test_mp_webhooks.py`.
Commit: `4e3af49`.

---

## D-020: Snapshot de `deposit_at_booking` en `Booking`

**Fecha**: Octubre 2026

**Contexto**: D-019 introdujo un guard de amount en el webhook MP que compara el monto
pagado contra `effective_deposit(service.price, service.deposit_amount)` calculado en tiempo
real. Si el dueño del negocio modifica el precio del servicio entre la creación del turno y
el pago, ese cálculo cambia: un pago realizado al monto original puede ser rechazado
retroactivamente. El monto de seña exigible es el vigente al momento de crear el turno, no
al momento del pago.

**Decisión**: Agregar `deposit_at_booking` (`Numeric(10,2)`, nullable) a `Booking` y
setearlo en la creación del turno via `effective_deposit(service.price, service.deposit_amount)`.
El guard de amount en el webhook lee `booking.deposit_at_booking`; si es `NULL` (filas
anteriores a la migración) cae al fallback de cálculo en tiempo real. La migración backfill
usa el precio actual del servicio para filas históricas (mejor aproximación disponible).
Se agrega `CHECK (deposit_at_booking >= 0)` en DB.

El campo se snapshotea en `Booking` (no en `Payment`) porque el monto acordado es una
propiedad del turno, no del pago: un turno puede tener múltiples pagos (reintentos, pagos
parciales futuros) y todos deben compararse contra el mismo monto pactado.

**Alternativas**:
- **Snapshot en `Payment`**: más cercano al evento de pago, pero requeriría que el webhook
  de creación de preferencia conozca el monto —o que el monto se recalcule al crear
  `Payment`— lo que no elimina la ventana de race condition si el precio cambia entre
  preferencia y pago.
- **No snapshotear; bloquear cambios de precio si hay bookings pending**: más restrictivo
  para el dueño del negocio; rechaza casos legítimos (actualizar precio para nuevos turnos).
- **Tolerar el desfase**: aceptable como deuda de baja severidad solo si no hay producción;
  rechazado ahora que hay pagos reales en juego.

**Consecuencias**:
- **Ventaja** — Un cambio de precio no rechaza retroactivamente un pago válido.
- **Ventaja** — El guard de amount es determinístico: no depende del estado actual del servicio.
- **Ventaja** — `CHECK >= 0` en DB previene valores negativos por bug en `effective_deposit`.
- **Riesgo** — El backfill usa el precio actual del servicio para filas históricas. Si el
  precio cambió antes de la migración, esas filas quedan con un valor aproximado —no el
  real al momento de la reserva. En producción con turnos existentes, pagos tardíos sobre
  esas filas podrían seguir fallando o aprobando según el delta.
- **Deuda** — `NULL` en filas históricas y en el backfill con precio cambiado. El fallback
  a `effective_deposit()` en tiempo real persiste para esos casos hasta que se cancelen o
  completen los turnos afectados.

**Implementación**: `app/models.py`, `app/main.py`, `app/mp_webhooks.py`.
Migración: `55526fb8c0f9_move_deposit_at_booking_to_booking.py`.
Tests en `tests/test_integration.py`, `tests/test_mp_webhooks.py`.
Commit: `f128344`.

---

## D-021: `Tenant.mp_user_id` único (índice parcial) y rechazo de cuentas MP ya vinculadas

**Fecha**: Octubre 2026

**Contexto**: El webhook de MP resuelve el tenant (y su token) buscando `Tenant.mp_user_id ==
user_id` (`_resolve_token_for_payment`, D-012/D-019). La columna no era única: si dos
tenants conectaban la misma cuenta de MP, la consulta lanzaba `MultipleResultsFound` y el
webhook respondía 500 para ambos, con pagos aprobados sin confirmar. Además, cuando MP no
devolvía `user_id`, el callback guardaba el string literal `"None"`, que rompe tanto el
guard de `collector_id` (D-019) como cualquier unicidad.

**Decisión**: Índice único parcial `uq_tenant_mp_user_id` sobre `tenant(mp_user_id) WHERE
mp_user_id IS NOT NULL` (migración `c7d8e9f0a1b2`, también declarado en `__table_args__` del
modelo). En `GET /mp/connect/callback`, si el `commit` lanza `IntegrityError` se hace
`rollback` (no se guardan tokens; el otro tenant no cambia) y se redirige a
`/panel/settings?mp=account_in_use`. Si MP no devuelve `user_id` (ni en el canje ni en
`GET /users/me`) no se guarda nada y se redirige a `?mp=error`.

**Alternativas**:
- **Chequeo en aplicación (SELECT antes de guardar)**: no es atómico; dos callbacks
  concurrentes pasan el chequeo. La DB es la única garantía real.
- **Constraint único no parcial**: en PostgreSQL también funcionaría (los `NULL` son distintos
  por defecto), pero el índice parcial deja explícito que solo cuentan las cuentas conectadas
  y no indexa los tenants sin MP.
- **Tolerar duplicados y desambiguar en el webhook** (p. ej. por `external_reference`):
  mantiene un modelo ambiguo y el guard de `collector_id` seguiría sin poder atribuir la cuenta.

**Consecuencias**:
- **Ventaja** — El 500 por `MultipleResultsFound` en el webhook deja de ser posible.
- **Ventaja** — La unicidad vale también para escrituras que no pasan por el callback.
- **Ventaja** — Ya no se persiste `"None"` como cuenta.
- **Riesgo** — Si prod ya tiene `mp_user_id` duplicados, `CREATE UNIQUE INDEX` falla, la
  migración no se aplica y el contenedor no arranca (el entrypoint corre `alembic upgrade
  head`). Hay que verificar antes de desplegar (ver `RUNBOOK.md` §3.3) y resolver a mano qué
  tenant conserva la cuenta. Filas con `''` o `'None'` repetidas también chocan.
- **Riesgo** — Un dueño que legítimamente maneja dos negocios con la misma cuenta MP ya no
  puede conectarla en ambos; hoy no hay flujo para transferirla salvo desconectar primero en
  el otro tenant.
- **Deuda** — El mensaje `account_in_use` no dice qué negocio tiene la cuenta (a propósito,
  para no filtrar datos entre tenants), así que el soporte tiene que investigarlo a mano.

**Implementación**: `app/models.py`, `app/mp_connect.py`, `app/routers/panel.py`.
Migración: `c7d8e9f0a1b2_add_unique_tenant_mp_user_id.py`.
Tests en `tests/test_panel_mp_connect.py`.
Commit: `92a24d9`.

---

## D-022: Outbox con commit por evento y reintentos con backoff

**Fecha**: Octubre 2026

**Contexto**: D-016 describía un rollback del batch que el código no hacía (cada envío estaba en su propio `try`), pero quedaban tres problemas reales: los eventos `failed` nunca se reintentaban (una caída de Meta de más de ~3 s dejaba al cliente sin confirmación), un error fuera del `try` (p. ej. timezone inválida del tenant) abortaba el lote entero cada minuto (mensaje veneno), y un crash a mitad del lote reenviaba lo ya enviado.

**Decisión**: `process_outbox` lista los ids elegibles y procesa cada evento en su propia transacción (`FOR UPDATE SKIP LOCKED` por fila); cualquier excepción marca solo ese evento `failed` y suma `retry_count`. Los `failed` se reintentan hasta `MAX_OUTBOX_ATTEMPTS = 7` intentos, con backoff calculado desde `created_at`: tras n fallos el próximo es a los `2^n - 1` minutos (1, 3, 7, 15, 31, 63). Fallidos con más de 2 h no se reintentan.

**Alternativas**:
- **Columna `next_attempt_at`**: calendario explícito, pero requiere migración; el backoff desde `created_at` alcanza mientras los reintentos los genere solo este job.
- **Reenvío manual desde el panel**: útil para soporte, se puede sumar después; no reemplaza el reintento automático.

**Consecuencias**:
- **Ventaja** — Un evento roto no bloquea al resto y lo enviado queda commiteado.
- **Ventaja** — Sin migración.
- **Riesgo** — Errores permanentes de Meta (número inválido, 131030) se reintentan igual hasta agotar los intentos (~1 h); es ruido en logs, no reenvíos.
- **Riesgo** — Un reintento no revisa si el turno sigue confirmado (igual que el primer envío).
- **Deuda** — Fallidos agotados quedan en `failed` sin alerta; mirar RUNBOOK.

**Implementación**: `app/outbox_worker.py`, `tests/test_outbox_worker.py`.

---

## Roadmap de deuda técnica

Ordenado por impacto/urgencia estimada:

### Fase 0 — Autonomía (Septiembre-Octubre 2026)
1. ~~**Health check profundo** que verifique DB y Redis.~~ — **Resuelto en `f2aae77`**. Endpoint `/health` con checks de DB y Redis; 2 tests agregados en `tests/test_health.py`.
2. ~~**Backup automatizado** como cron en el VPS de producción + copia a S3.~~ — **Resuelto parcialmente en `746778c`**. Script `scripts/backup_db.sh` sube a S3 cuando `S3_BACKUP_BUCKET` está configurada. Configuración del cron en el host: pendiente operacional (ver `DEPLOYMENT.md` §7).
3. ~~**Test de restore** del backup para verificar que funciona cuando importa.~~ — **Resuelto en `746778c`**. Nuevo script `scripts/restore_db.sh` (local o `s3://`, flag `--yes`). Procedimiento documentado en `DEPLOYMENT.md` §7.
4. ~~**`deposit_at_booking` en `Booking`**~~ — **Resuelto en D-020 / `f128344`**. Snapshot
   de `effective_deposit` al crear el turno; guard de amount en webhook MP lo consume.
5. ~~**`Payment.mp_payment_id` UNIQUE + `SELECT FOR UPDATE`**~~ — **Resuelto en `072ab22`**.
6. ~~**`CHECK (deposit_amount >= 0)` en `service`**~~ — **Resuelto en `6677acc`**.
7. ~~**`idempotency_key` UNIQUE global → UNIQUE compuesto `(tenant_id, idempotency_key)`**~~ — **Resuelto en `8cfa0d6`**. Migración `b0e5b8028ae7`; el constraint `uq_booking_idempotency_key` ahora abarca `(tenant_id, idempotency_key)`. Eliminado campo `price_at_booking: float | None` de `BookingCreate` (era ignorado; el endpoint siempre usa `service.price`).

### Q1 2027 (mes 1-3)
4. **Deploy a producción** (VPS + dominio + Cloudflare Named Tunnel).
5. **Frontend público + panel admin** (Next.js) para booking y gestión.
6. **Onboarding self-service** para nuevos tenants.

### Q2 2027 (mes 4-6)
7. **Worker separado** del scheduler (Celery o ARQ) para >1 réplica.
8. **Rate limiting** en endpoints públicos (slowapi).
9. ~~`BusinessHours` configurable~~ — **implementado** (modelo + panel CRUD en `app/main.py`). El fallback 09-18 solo aplica cuando el tenant no tiene ninguna fila en `business_hours`.
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
3. Documentar con honestidad las consecuencias negativas y la deuda generada. El valor de este registro reside en su precisión.
4. Actualizar el roadmap si la decisión genera deuda nueva.
5. Commitear con `docs: add D-0XX decision about X`.
