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
Aislamiento garantizado por lógica de aplicación + constraints de DB. Las tablas auxiliares (`payment`, `notification_outbox`, `payment_events`) cuelgan del tenant vía `booking_id`.

**Alternativas**:
- **Database per tenant**: máximo aislamiento, pero requiere N conexiones, N pools, N veces los mismos Alembic migrations. Operacionalmente caro.
- **Schema per tenant**: posible en Postgres, pero complejo de migrar y de gestionar con SQLModel/Alembic.

**Consecuencias**:
- **Ventaja** — Operación simple: una sola DB, un solo pool de conexiones, un solo backup.
- **Ventaja** — Costo bajo: un servidor atiende a N tenants sin N veces el overhead.
- **Ventaja** — Migraciones únicas: una sola versión del schema para todos.
- **Riesgo** — Si hay un bug en el filtrado por `tenant_id`, un tenant puede ver datos de otro. Mitigado con tests cross-tenant y con las dependencias de auth obligatorias (`get_current_tenant` para API key, `get_current_tenant_from_session` para el panel) que entregan el `Tenant` ya resuelto; cada query autenticada filtra `tenant_id` a mano, así que sigue dependiendo de la disciplina de quien escribe el endpoint.
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
- **Deuda** — El panel web ya no usa API key sino cookie firmada (D-013), pero sigue sin haber usuarios internos ni permisos por rol: hay un solo dueño por tenant. `PATCH /tenants/me` y `/tenants/me/mp` de la API siguen exigiendo API key aunque el dueño use el panel.

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
) WHERE (status IN ('pending', 'confirmed'))
```

El constraint se llama `excl_overlapping_bookings`; el filtro por status entró en la migración `9a1b2c3d4e5f` y `tenant_id` en `3c4d5e6f7a8b`. Solo `pending` y `confirmed` bloquean el horario: al pasar a `cancelled`/`expired`/`no_show`/`completed` el slot se libera.

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
- **Riesgo** — `expired` y `cancelled` liberan el horario, así que un pago tardío sobre un `expired` puede encontrar el slot ocupado: el webhook chequea con `_slot_still_free` antes de reconfirmar y, si lo tomó otro cliente, el pago queda sin turno (ver D-023).
- **Deuda** — Si en el futuro se quiere permitir overlap condicional (ej. servicios grupales), hay que revisar el constraint.

---

## D-004: Patrón Outbox para notificaciones WhatsApp

**Fecha**: Septiembre 2026

**Contexto**: Enviar WhatsApp en el mismo request HTTP que crea el booking introduce la latencia de Meta
en la respuesta al cliente. Si Meta se cae, la reserva falla. Inaceptable.

**Decisión**: Insertar `NotificationOutbox` en la misma transacción que la operación que la dispara
(confirmación de pago vía webhook MP o reconciliación, o encolar recordatorio via scheduler), y procesar los
pendientes con un job de APScheduler cada 60 segundos.

**Alternativas**:
- **Envío síncrono en el request**: mala UX, acopla la reserva a la disponibilidad de Meta.
- **`BackgroundTasks` de FastAPI**: la tarea se pierde si el proceso se reinicia antes de ejecutarla.
- **Celery / RQ**: más robusto para multi-instancia, pero agrega un worker extra, un broker, y otro proceso que monitorear. No justificado para el volumen actual.

**Consecuencias**:
- **Ventaja** — Atomicidad: confirmación de pago y notificación son una sola transacción de DB (webhook MP).
- **Ventaja** — Resiliencia: si Meta se cae, el outbox queda en `failed` y se reintenta con backoff (ver D-022).
- **Ventaja** — Desacoplamiento: la latencia de Meta no afecta la respuesta al cliente.
- **Riesgo** — La notificación no es inmediata: puede tardar hasta 60s. No afecta la experiencia de reserva.
- **Riesgo** — Una confirmación manual desde el panel (`/panel/agenda/{id}/confirm`) no encola notificación: solo la confirman por pago MP/reconciliación.
- **Deuda** — Si se migra a un worker separado, este código se mueve al worker.

> **Nota**: `POST /public/bookings` y `POST /bookings` crean `Booking` (+ `Payment` en el público), pero **no** crean `NotificationOutbox` en esa transacción. El outbox se crea cuando el webhook MP (o la reconciliación previa a expirar, D-023) confirma el pago y transiciona el booking a `confirmed`; el recordatorio lo encola `process_reminders`.

---

## D-005: APScheduler dentro del proceso de la API

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos jobs periódicos (outbox, recordatorios, expiración de señas, refresh de tokens MP). ¿Proceso separado o in-process?

**Decisión**: `APScheduler AsyncIOScheduler` corriendo en el `lifespan` de FastAPI, con 4 jobs `interval`: `process_outbox` (1 min), `process_reminders` (5 min), `process_deposit_expiration` (1 min) y `process_mp_token_refresh` (24 h). No arranca si `TEST_DATABASE_URL` está seteada.

**Alternativas**:
- **Celery + Redis broker**: más robusto, pero requiere un worker aparte, configuración de broker, y toda una nueva capa de infraestructura.
- **Cron externo (crontab del sistema)**: complicado de coordinar dentro de Docker; no tiene contexto de la app.
- **RQ / Dramatiq**: alternativas a Celery con las mismas desventajas para este caso.

**Consecuencias**:
- **Ventaja** — Simplicidad: un solo proceso, un solo Dockerfile.
- **Ventaja** — Sin infraestructura extra: no hay broker, no hay worker, no hay procesos adicionales que puedan fallar.
- **Ventaja** — Comparte el contexto de la app (DB session, config) sin IPC.
- **Riesgo** — Si corren N réplicas, cada una ejecuta el cron. Mitigado con lock Redis (`SET NX EX`) en reminders, expiración y refresh, y con `FOR UPDATE SKIP LOCKED` en outbox y expiración. Los locks evitan el doble procesamiento, no la carga duplicada.
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
- **Deuda** — Migrar a Orders API en Q2 2027 (con los primeros 20-30 clientes reales). Hoy el cobro se hace con `unit_price` = seña en ARS; el resto del precio se cobra en el local.

---

## D-007: WhatsApp con plantillas de categoría Utility

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos notificar confirmaciones de reserva y recordatorios 24h antes.

**Decisión**: Usar plantillas aprobadas por Meta de categoría "Utility" (`booking_confirmation` con `{nombre, fecha, booking_id}`, `booking_reminder` con `{nombre, fecha}`), enviadas por Graph API v19.0 desde `WhatsAppService`. A Meta el número argentino va sin el `9` (`normalize_phone_for_meta`); en DB se guarda `549...`.

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
- **Riesgo** — Los tokens OAuth vencen (~180 días). `process_mp_token_refresh` los renueva diariamente con `refresh_token` cuando faltan menos de 30 días; si el refresh falla (p. ej. el tenant revocó el acceso) hay que reconectar a mano y, mientras tanto, sus pagos quedan bloqueados en producción (comportamiento correcto, pero hay que comunicarlo al dueño).
- **Riesgo** — El webhook, sin `user_id` o sin tenant que matchee, cae al token de la plataforma; en producción el guard de `collector_id` (D-019) rechaza el pago si el tenant no tiene `mp_user_id`.
- **Deuda** — Resuelto desde esta decisión: renovación proactiva por scheduler y botón de conectar/desconectar en el panel (`/panel/settings`, D-026). Sigue abierto: `PATCH /tenants/me` y `/tenants/me/mp` por API key, y que tokens sin `expires_in` nunca se renueven (D-015). Pagos por alias manual quedan fuera de alcance.

Plan de implementación aprobado: [`PLAN_MP_POR_TENANT.md`](PLAN_MP_POR_TENANT.md) (histórico: usa nombres de variables viejos).

---

## D-013: Sesiones firmadas para panel web + session_version

**Fecha**: Septiembre 2026

**Contexto**: El panel del negocio (dueño/staff) necesita autenticación web separada del API key que usan los clientes. El dueño debe poder iniciar sesión con email + password, y al cambiar su contraseña todas las sesiones activas deben invalidarse inmediatamente.

**Decisión**: Cookie firmada HMAC-SHA256 con payload `{tenant_id}.{session_version}.{expires_at}`. La cookie se llama `juturno_session`. La tabla `tenant` tiene un campo `session_version` (int, default 1, server_default) pensado para incrementarse al cambiar contraseña o hacer logout masivo (hoy esos flujos no existen, ver Deuda).

**Alternativas consideradas**:
- **JWT**: requiere blacklist para revocación (complejidad extra en Redis/DB).
- **Session server-side en Redis**: agrega dependencia para sesiones, pero funciona.
- **Cookie sin firma**: obviamente inaceptable.

**Consecuencias**:
- **Ventaja** — Stateless: no hay storage de sesiones, la cookie lleva toda la info.
- **Ventaja** — Invalidación instantánea: cambiar `session_version` invalida TODAS las sesiones del tenant en un solo UPDATE.
- **Ventaja** — Timing-safe: usa `hmac.compare_digest` en la validación.
- **Ventaja** — Sin dependencia extra: HMAC está en stdlib.
- **Riesgo** — ⚠️ Rotar `SECRET_KEY` invalida todas las sesiones activas (comportamiento deseado, pero requiere aviso a usuarios).
- **Riesgo** — `POST /logout` solo borra la cookie del navegador; una cookie robada sigue valiendo hasta que expire (14 días) o cambie `session_version`.
- **Deuda** — Hoy ningún código incrementa `session_version`: no existe cambio ni recupero de contraseña, ni endpoint de "cerrar sesión en todos los dispositivos". El mecanismo de invalidación existe pero solo se acciona con un UPDATE manual en la DB.

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
- **Ventaja** — Falla ruidosamente: imposible deployar a producción con el default silenciosamente.
- **Ventaja** — Fuerza la configuración explícita: el operador tiene que tomar una decisión consciente.
- **Riesgo** — Requiere documentación clara: si no está en el RUNBOOK, el siguiente operador puede perder 30 min diagnosticando el mismo crash.
- **Deuda** — La rotación de `SECRET_KEY` no está documentada como procedimiento (invalidaría todas las sesiones activas — aceptable pero requiere aviso previo).

**Relacionado**: `META_APP_SECRET`, `MP_TOKEN_ENCRYPTION_KEY` y compañía tenían la misma categoría de "crítica en producción" sin validador; se agregaron en D-018.

---

## D-015: Refresh de tokens MP sin expires_in

**Fecha**: Octubre 2026

**Contexto**: El job `process_mp_token_refresh` (scheduler diario) filtra tenants con `mp_token_expires_at IS NOT NULL AND mp_token_expires_at <= now + 30d`. MP devuelve `expires_in` (~180 días) al canjear el code OAuth, pero si MP no lo incluye (edge case, cambio de API, cuenta de prueba), `mp_token_expires_at` queda `NULL` y el token **nunca se renueva**. Vence a los ~180 días sin aviso.

**Decisión**: En `refresh_tenant_mp_token` (`app/mp_connect.py`), si `expires_in` no viene en la respuesta de MP, setear `mp_token_expires_at = now + 180 días` (valor por defecto documentado por MP). Además, agregar fallback en el job: tenants con `mp_refresh_token_enc IS NOT NULL AND mp_token_expires_at IS NULL` → intentar refresh igual.

> **Estado**: Decisión registrada, **pendiente de implementación** (verificado contra el código actual). Tanto `refresh_tenant_mp_token` como el callback OAuth solo setean `mp_token_expires_at` `if expires_in:`, sin fallback de 180 días; y `process_mp_token_refresh` (`app/scheduler.py`) filtra `mp_token_expires_at IS NOT NULL`, así que esos tenants nunca se renuevan.

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

**Estado**: Reemplazada (superseded) por D-022 (commit por evento + reintentos). Se conserva como registro histórico; el código ya no se comporta así.

**Contexto**: `process_outbox` (`app/outbox_worker.py`, versión anterior a D-022) usaba un solo `async with session.begin()` que engloba todo el loop de eventos. Si un evento falla (ej. WhatsApp timeout), **todos** los eventos del batch hacen rollback — incluso los que se enviaron OK. Con commit por evento (diseño original), cada evento commiteaba su estado independiente.

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

**Contexto**: `get_available_slots` (auth API key) y `get_public_available_slots` (público, sin auth) en `app/main.py` eran **casi idénticos** (~100 líneas duplicadas). Diferencias: auth dependency, validación `tenant_id == current_tenant.id` vs lookup por ID, y manejo de 404 vs 401.

**Decisión**: Extraer lógica compartida a `app/services.py` como `compute_available_slots(*, session, tenant_id, service, day, staff_id, tenant_timezone)` y llamar desde ambos endpoints. Los endpoints solo manejan auth, validación de tenant y respuesta HTTP.

> **Estado**: **Resuelta**. `compute_available_slots` vive en `app/services.py` y la usan `GET /bookings/available-slots` (`app/routers/api.py`) y `GET /public/available-slots` (`app/routers/public.py`); cada endpoint solo valida auth y tenant. Cubierta por `tests/test_slots.py`.

**Alternativas**:
- **Dejar duplicado**: simple pero riesgo de drift (fix en uno no llega al otro).
- **Decorator/auth dependency**: más complejo, no elimina duplicación de lógica de negocio.

**Consecuencias**:
- **Ventaja** — Single source of truth para cálculo de slots.
- **Ventaja** — Tests cubren una sola función; endpoints testean solo auth/validación.
- **Riesgo** — Refactor toca código crítico (slots). Requiere tests de regresión exhaustivos.
- **Deuda** — Ninguna pendiente de esta decisión. La grilla de 30 min está fija en `compute_available_slots` (`granularity_min=30`), no es configurable por servicio ni por tenant.

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

> **Estado**: **Resuelta**. `Settings.model_post_init` (`app/config.py`) en producción exige no vacías `META_APP_SECRET`, `MP_TOKEN_ENCRYPTION_KEY`, `MP_SECRET_KEY`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` y `MP_NOTIFICATION_URL` (esta última con formato `https://.../webhooks/mercadopago`, ver D-029), y rechaza `MP_SANDBOX=true` (commit `7be0fcd`). Cubierta por `tests/test_config.py`.

**Alternativas**:
- **Warnings en logs**: no bloquea arranque, pero falla en runtime — peor UX operativa.
- **Defaults de desarrollo**: peligroso en prod si se olvida setear.

**Consecuencias**:
- **Ventaja** — Falla ruidosa al arranque, no en runtime.
- **Ventaja** — Mensaje de error accionable ("setea X en Coolify").
- **Riesgo** — Bloquea arranque si falta una var; requiere checklist de deploy actualizado.
- **Deuda** — Los validadores solo chequean que la variable no esté vacía: una `MP_TOKEN_ENCRYPTION_KEY` con formato Fernet inválido recién falla al cifrar/descifrar (`MPTokenCryptoError`). `MP_MARKETPLACE_CLIENT_ID`/`SECRET` y `MP_ACCESS_TOKEN` no se validan; sin client id el inicio del OAuth responde 503.

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
- **Deuda resuelta** — Race condition de dos webhooks simultáneos del mismo `mp_payment_id`:
  commits `072ab22` y `345d564`. `uq_payment_mp_payment_id` (migración `be7d31a422d8`) más
  `SELECT ... FOR UPDATE` sobre `Payment` y sobre `Booking`; un INSERT duplicado lanza
  `DuplicatePaymentError`, que el webhook trata como idempotente.
- **Riesgo** — En sandbox/dev un tenant sin `mp_user_id` saltea el guard de tenant, así que la
  protección real depende de `ENVIRONMENT=production` (y de `MP_SANDBOX=false`, que `config.py`
  fuerza en producción).

**Implementación**: `app/mp_webhooks.py` (hoy en `apply_payment_details`, compartida con la reconciliación de D-023). Tests en `tests/test_mp_webhooks.py`.
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
Se agrega `CHECK (deposit_at_booking IS NULL OR deposit_at_booking >= 0)` (`ck_booking_deposit_at_booking_non_negative`) en DB.

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

**Implementación**: `app/models.py`, `app/routers/api.py`, `app/routers/public.py`, `app/mp_webhooks.py`.
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

**Decisión**: `process_outbox` lista los ids elegibles y procesa cada evento en su propia transacción (`FOR UPDATE SKIP LOCKED` por fila); cualquier excepción marca solo ese evento `failed` y suma `retry_count` (el envío corre en un savepoint, así un error de base no impide marcarlo). Los `failed` se reintentan hasta `MAX_OUTBOX_ATTEMPTS = 7` intentos, con backoff calculado desde `created_at`: tras n fallos el próximo es a los `2^n - 1` minutos (1, 3, 7, 15, 31, 63). Fallidos con más de 2 h no se reintentan.

**Alternativas**:
- **Columna `next_attempt_at`**: calendario explícito, pero requiere migración; el backoff desde `created_at` alcanza mientras los reintentos los genere solo este job.
- **Reenvío manual desde el panel**: útil para soporte, se puede sumar después; no reemplaza el reintento automático.

**Consecuencias**:
- **Ventaja** — Un evento roto no bloquea al resto y lo enviado queda commiteado.
- **Ventaja** — Sin migración.
- **Riesgo** — Errores permanentes de Meta (número inválido, 131030) se reintentan igual hasta agotar los intentos (~1 h); es ruido en logs, no reenvíos.
- **Riesgo** — Un reintento no revisa si el turno sigue confirmado (igual que el primer envío).
- **Riesgo** — Entrega *at-least-once*: si Meta aceptó el mensaje pero la respuesta no llegó (timeout) o el proceso muere antes del commit, el reintento lo duplica. Preferible a perderlo.
- **Deuda** — Fallidos agotados quedan en `failed` sin alerta; mirar RUNBOOK.

**Actualización (commit `d72af08`)**: cancelar un turno ahora también cancela los eventos `failed` (antes solo `pending`), porque `process_outbox` los reintenta y mandaría un WhatsApp de un turno cancelado. Ver D-030.

**Implementación**: `app/outbox_worker.py`, `tests/test_outbox_worker.py`.

---

## D-023: Reconciliación con MP antes de expirar una seña

**Fecha**: Octubre 2026

**Contexto**: Si el webhook firmado de un pago aprobado nunca llega (caída, reintentos agotados, o un reenvío con `user_id` falso que antes quemaba la clave), `process_deposit_expiration` vencía la reserva aunque el cliente hubiera pagado.

**Decisión**: Antes de expirar, el job busca en MP con el token del tenant (`GET /v1/payments/search?external_reference=booking-{id}`), vuelve a pedir cada pago aprobado a `/v1/payments/{id}` y lo aplica con `apply_payment_details`, la misma función (guards de `collector_id`, moneda y monto, upsert de `Payment`, confirmación y outbox) que usa el webhook. Si se confirma, no expira. Las llamadas a MP van antes de bloquear la fila; después se bloquea, se re-chequea `pending` y se aplica o expira, cada reserva en su propia transacción. Solo cuentan pagos cuyo `external_reference` es esa reserva. Si MP (o el token) falla, la reserva queda `pending` hasta 1 h después de su vencimiento (`RECONCILE_GRACE`) y después se expira igual. Sin token (tenant sin MP en producción) expira como antes. Solo el INSERT duplicado del `Payment` (`DuplicatePaymentError`) se trata como idempotente en el webhook.

**Alternativas**:
- **Job de reconciliación independiente**: más general (también pagos de reservas sin límite de seña), pero más superficie; el momento crítico es justo antes de liberar el horario.
- **Expirar igual y confiar en la reconfirmación tardía**: el webhook ya reconfirma `expired` si el slot sigue libre, pero si lo tomó otro cliente el pago queda huérfano.

**Consecuencias**:
- **Ventaja** — Un pago aprobado sin webhook ya no pierde el turno.
- **Ventaja** — Una sola implementación de los guards para webhook y reconciliación.
- **Riesgo** — Una o dos llamadas a MP por reserva vencida y por corrida mientras MP falle; volumen bajo.
- **Riesgo** — Con MP caído o un token revocado, una reserva vencida retiene su horario hasta 1 h más; pasado eso se expira, y si el pago aparece el webhook reconfirma si el horario sigue libre.
- **Riesgo** — El lock Redis del job dura 300 s (no 30 s como los otros) para cubrir las llamadas a MP del lote; si una corrida se cuelga más que eso, otra puede arrancar. La fila se protege igual con `FOR UPDATE SKIP LOCKED`.
- **Deuda** — Reservas de tenants sin `deposit_expiration_minutes` no se reconcilian (ni expiran).

**Implementación**: `app/scheduler.py` (`_reconcile_or_expire`), `app/mp_webhooks.py` (`apply_payment_details`, `search_approved_payment_ids`).

---

## D-024: Rate limiting con slowapi en memoria

**Fecha**: Octubre 2026

**Contexto**: Los endpoints sin API key (`/login`, `/register`, `/public/bookings`) quedaban expuestos a fuerza bruta de contraseñas, alta masiva de negocios y spam de reservas `pending` que bloquean horarios.

**Decisión**: `slowapi` con un singleton `limiter` (`app/limiter.py`), clave = IP del cliente (`get_remote_address`) y handler `RateLimitExceeded` → 429. Límites: `POST /login` 10/min, `POST /register` 5/min, `POST /public/bookings` 20/min. Se desactiva cuando `TEST_DATABASE_URL` está seteada; `tests/test_rate_limiting.py` lo reactiva a propósito para probar el cableado real. Commit `6ab9cac`.

**Alternativas**:
- **Límite en Traefik/Cloudflare**: no depende del código, pero no distingue rutas por semántica de negocio y vive fuera del repo.
- **slowapi con storage Redis (`storage_uri`)**: contador compartido entre réplicas y que sobrevive a restarts. No se hizo porque hoy hay una sola réplica.
- **Bloqueo de cuenta por intentos fallidos**: protege mejor un login puntual, pero exige estado en DB y permite bloquear a un dueño a propósito.

**Consecuencias**:
- **Ventaja** — Sin infraestructura nueva y con un decorador por endpoint.
- **Ventaja** — Los tests no se ven afectados por defecto.
- **Riesgo** — El storage es memoria del proceso: se resetea en cada restart/deploy y no se comparte entre réplicas (límite efectivo = N veces el configurado).
- **Riesgo** — Detrás de Traefik, `get_remote_address` solo ve la IP real si uvicorn confía en el proxy. El comando de prod usa `--proxy-headers` pero sin `--forwarded-allow-ips`, así que puede limitar por la IP del proxy y bloquear a todos juntos.
- **Deuda** — Configurar `--forwarded-allow-ips=<IP_Traefik>` en Coolify (pendiente operativo) y pasar a storage Redis antes de escalar a más de una réplica. Los endpoints con API key y los webhooks no tienen límite.

**Implementación**: `app/limiter.py`, `app/main.py`, `app/routers/auth.py`, `app/routers/public.py`. Tests en `tests/test_rate_limiting.py`.

---

## D-025: CSRF por double-submit cookie en el panel

**Fecha**: Octubre 2026

**Contexto**: El panel se autentica con cookie (`juturno_session`), así que cualquier POST con efecto (cancelar un turno, desconectar MP, logout) es vulnerable a CSRF. La primera versión de `validate_csrf` solo exigía que existiera la cookie `csrf_token`, sin compararla con el formulario: protegía de nada.

**Decisión**: Double-submit cookie. Los GET que renderizan formularios generan `secrets.token_hex(32)`, lo ponen en la cookie `csrf_token` (no HttpOnly, `SameSite=Lax`, `Secure` en producción, 2 h) y en un campo oculto `csrf_token`. En el POST, `validate_csrf` lee el form y compara con `hmac.compare_digest`; falta la cookie o no coincide → 403. `/register` y `/login` usan `validate_csrf_double_submit` y re-renderizan con error. `POST /logout` también valida. Commits `7be0fcd` y `10b6654`.

**Alternativas**:
- **Token sincronizado en sesión del servidor**: más fuerte, pero requiere sesión server-side (se descartó en D-013).
- **Solo `SameSite=Lax`**: frena la mayoría de los casos, pero no cubre subdominios ni navegadores viejos y no es una defensa explícita.
- **Token firmado con HMAC y ligado a la sesión**: elimina el ataque de inyección de cookie desde un subdominio, a costa de más código.

**Consecuencias**:
- **Ventaja** — Stateless: sin storage de tokens.
- **Ventaja** — Comparación en tiempo constante; falla cerrado.
- **Riesgo** — Quien pueda setear cookies en un subdominio de `juturno.com` puede fijar su propio par cookie/campo y pasar la validación (limitación conocida del double-submit sin firma).
- **Riesgo** — Cada endpoint del panel tiene que acordarse de llamar a `validate_csrf`; no es un middleware, así que un endpoint nuevo sin la llamada queda desprotegido. `ONBOARDING.md` lo menciona como convención.
- **Deuda** — Considerar token firmado ligado a `juturno_session`. Los endpoints con API key y los webhooks no usan CSRF por diseño.

**Implementación**: `app/csrf.py`, `app/routers/panel.py`, `app/routers/auth.py`. Tests en `tests/test_login.py`, `tests/test_register.py` y los tests del panel.

---

## D-026: State de OAuth atado al navegador y conexión de MP solo desde el panel

**Fecha**: Octubre 2026

**Contexto**: `GET /mp/connect/start` (API key) devolvía una URL de autorización con un `state` guardado en Redis. Esa URL puede viajar a cualquier navegador: si un atacante logra que la víctima autorice con una URL del atacante (o al revés), la cuenta de MP queda vinculada al tenant equivocado (account-linking).

**Decisión**: El inicio del flujo es solo `POST /panel/mp/connect/start` (cookie + CSRF). `mp_authorization_redirect` guarda el `state` en Redis (`mp_connect_state:{state}` → `tenant_id`, TTL 600 s, `GETDEL` = un solo uso) **y** lo deja en la cookie HttpOnly `mp_oauth_state` (path `/mp/connect/callback`, SameSite=Lax, `Secure` en producción, `Domain` = host del panel si el callback es un subdominio). El callback exige que la cookie coincida con el `state` (`hmac.compare_digest`); si no, redirige a `?mp=other_browser` sin consumir ni vincular nada. Se eliminó `GET /mp/connect/start` (404). El callback siempre vuelve al panel con un flag fijo (`connected|error|other_browser|account_in_use`); la URL de vuelta la arma el servidor, nunca viaja en el `state`. Commits `638357b` y `6a0ef10`.

**Alternativas**:
- **Mantener el start por API key**: no hay navegador al cual atar el state.
- **Atar el `state` a la sesión `juturno_session`**: depende de que esa cookie llegue al callback (otro host); la cookie dedicada con `Domain` explícito es más simple.
- **State firmado con HMAC sin Redis**: evita el storage, pero no da un solo uso sin estado compartido.

**Consecuencias**:
- **Ventaja** — Cierra el account-linking: solo el navegador que inició el flujo puede completarlo.
- **Ventaja** — El state es de un solo uso y expira a los 10 min; no hay open redirect.
- **Riesgo** — Si el host de `PUBLIC_BASE_URL` no es padre del host de `MP_MARKETPLACE_REDIRECT_URL`, la cookie no llega y todas las conexiones terminan en `other_browser`. En el celular, si MP vuelve por otra app/navegador, falla igual.
- **Riesgo** — La conexión ya no se puede automatizar por API: los clientes que usaban `GET /mp/connect/start` se rompen (404).
- **Deuda** — `PATCH /tenants/me` y `GET`/`DELETE /tenants/me/mp` siguen por API key; `DELETE` desconecta sin el chequeo de señas pendientes que sí tiene el panel.

**Implementación**: `app/mp_connect.py`, `app/routers/panel.py`. Tests en `tests/test_mp_connect.py` y `tests/test_panel_mp_connect.py`.

---

## D-027: Webhook MP: firma sobre el query y clave de idempotencia con valores firmados

**Fecha**: Octubre 2026

**Contexto**: Auditando `POST /webhooks/mercadopago` aparecieron cuatro problemas: MP firma el `data.id` del query string, no el del body, pero el código leía el del body; la clave de idempotencia usaba el `id` del body, que no está firmado (reenviando un request firmado con otro `id` se saltea el dedupe y se "quema" la clave); MP también manda IPN viejos sin firma que producían 401 y reintentos; y un body vacío o inválido daba 500.

**Decisión**: (1) El manifiesto es `id:{data.id del query en minúsculas};request-id:{x-request-id};ts:{ts};` y, si no hay `data.id`, se omite esa parte y el evento no se procesa (200 `EVENT_IGNORED_NO_DATA_ID`). (2) `event_id` = `{data.id}:{x-request-id}`, solo con valores firmados. (3) Un IPN (`?topic=` sin `data.id`) se confirma con 200 `IPN_IGNORED` sin procesar. (4) Body que no es un objeto JSON → 400. (5) Si MP responde 404 al consultar el pago, el evento queda `failed` (reprocesable) y se responde 200 `PAYMENT_NOT_FOUND_ON_MP`. Commits `a9e6d78`, `e9f62e7`, `719eccc`, `1088851` y `a0f9e92`.

**Alternativas**:
- **Confiar en el body**: es lo que causaba el bug.
- **Rechazar IPN con 4xx**: MP reintentaría sin fin un evento que de todos modos llega firmado por el otro canal.
- **Marcar `processed` ante un 404**: un `user_id` falso en el body (no firmado) alcanzaría para quemar la clave y perder el pago real.

**Consecuencias**:
- **Ventaja** — Un atacante sin la clave no puede forjar ni quemar eventos.
- **Ventaja** — MP deja de reintentar IPN y payloads rotos.
- **Riesgo** — El `user_id` del body, que elige el token con el que se consulta a MP, sigue sin estar firmado. Lo acota el guard de `collector_id` de D-019, pero un `user_id` ajeno deja eventos `failed` hasta una nueva entrega.
- **Riesgo** — La tolerancia de replay es ±5 min: con el reloj del servidor desfasado se rechazan webhooks legítimos (403).
- **Deuda** — La tabla `payment_events` guarda el payload completo y nunca se purga.

**Implementación**: `app/mp_webhooks.py`. Tests en `tests/test_mp_webhooks.py` y `tests/test_mp_webhook_tenant.py`.

---

## D-028: Routers separados por mecanismo de autenticación

**Fecha**: Octubre 2026

**Contexto**: `app/main.py` había crecido hasta mezclar endpoints públicos, de API key y de panel con cookie (y por eso D-017 tenía código duplicado entre dos de ellos). Era fácil poner un endpoint con la dependencia de auth equivocada.

**Decisión**: Partir los endpoints en `app/routers/` según cómo se autentican: `public.py` (sin auth), `auth.py` y `panel.py` (cookie firmada), `api.py` (header `X-Tenant-API-Key`). `main.py` queda con lifespan, scheduler, middlewares y `include_router`. Las integraciones (`mp_connect.py`, `mp_webhooks.py`, `webhooks.py`) mantienen su router junto a su lógica. Commit `6990a4b`.

**Alternativas**:
- **Un router por dominio (bookings, services, ...)**: agrupa por funcionalidad, pero cada archivo mezclaría mecanismos de auth.
- **Dejar todo en `main.py`**: lo que había.

**Consecuencias**:
- **Ventaja** — La auth de un endpoint se deduce del archivo donde está.
- **Ventaja** — `main.py` se puede leer entero.
- **Riesgo** — `panel.py` tiene más de 1.200 líneas y mezcla settings, servicios, staff, horarios y agenda.
- **Deuda** — Partir `panel.py` por sección si sigue creciendo.

**Implementación**: `app/routers/`, `app/main.py`.

---

## D-029: `notification_url` de MP por entorno (`MP_NOTIFICATION_URL`)

**Fecha**: Octubre 2026

**Contexto**: La `notification_url` de cada preferencia estaba fija a producción. Un pago de sandbox hecho en local o staging notificaba al webhook de producción, que no encontraba la reserva.

**Decisión**: Nueva variable `MP_NOTIFICATION_URL`, una por entorno. `create_mp_preference` la manda solo si no está vacía (en local sin túnel MP no notifica, en vez de mandar el evento a producción). En producción es obligatoria y `Settings` exige que sea `https://.../webhooks/mercadopago` para que un typo no deje reservas pagas sin confirmar. Commits `d218c60`, `09ca2d7` y `e01ccfb`.

**Alternativas**:
- **Derivarla de `PUBLIC_BASE_URL`**: panel y API viven en hosts distintos (`juturno.com` y `api.juturno.com`).
- **Configurarla en el panel de MP Developers**: aplica a toda la aplicación, no por entorno.

**Consecuencias**:
- **Ventaja** — Los eventos de cada entorno llegan solo a ese entorno.
- **Ventaja** — Un typo en producción impide el arranque en vez de fallar en silencio.
- **Riesgo** — Hay que acordarse de definirla en cada entorno; en local sin túnel el webhook nunca llega y el pago no se confirma solo (la reconciliación de D-023 lo cubre una vez vencida la seña).
- **Deuda** — Una URL con otro path que igual cumpla el patrón (`https`, termina en `/webhooks/mercadopago`) pasa la validación.

**Implementación**: `app/config.py`, `app/mp_webhooks.py` (`create_mp_preference`), `.env.example`. Tests en `tests/test_config.py`.

---

## D-030: Cancelar un turno cancela también sus notificaciones fallidas

**Fecha**: Octubre 2026

**Contexto**: Desde D-022, `process_outbox` reintenta los eventos `failed`. Al cancelar un turno, `transition_booking_status` solo cancelaba los `pending`, así que un evento `failed` de un turno ya cancelado podía reintentarse y mandarle al cliente una confirmación o un recordatorio de un turno inexistente.

**Decisión**: Al pasar a `cancelled`, `transition_booking_status` marca como `cancelled` (con `error_message="booking_cancelled"`) todos los `NotificationOutbox` del booking en estado `pending` o `failed`. Commit `d72af08`.

**Alternativas**:
- **Chequear el estado del turno en `process_outbox` antes de enviar**: cubriría también `expired`, `no_show` y `completed`, pero agrega una lectura por evento y mezcla reglas de negocio en el worker.

**Consecuencias**:
- **Ventaja** — Una sola regla, en el único lugar por donde pasan todas las cancelaciones.
- **Ventaja** — Sin migración.
- **Riesgo** — Solo cubre `cancelled`. Un `failed` de un turno que pasa a `expired` (por la expiración de seña) sigue siendo elegible para reintento, y el worker no revisa el estado del turno (ya anotado en D-022).
- **Riesgo** — Si `process_outbox` ya tomó el evento y está enviándolo cuando se cancela, el WhatsApp sale igual (la cancelación no lo detiene, solo evita reintentos y envíos futuros).
- **Deuda** — Decidir si `expired` también debe cancelar las notificaciones pendientes.

**Implementación**: `app/booking_actions.py`. Tests en `tests/test_booking_actions.py`.

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
4. ~~**Deploy a producción** (VPS + dominio + Cloudflare Named Tunnel).~~ — **Hecho con otro stack**: VPS Hetzner con Coolify + Traefik (`api.juturno.com`, panel en `juturno.com`), ver D-010 y `DEPLOYMENT.md`. Pendiente operativo: `--forwarded-allow-ips` (D-024) y branch protection en GitHub.
5. ~~**Frontend público + panel admin** (Next.js) para booking y gestión.~~ — **Resuelto de otra forma**: landing (`/`), página de reserva (`/t/{slug}`) y panel (`/dashboard`, `/panel/*`) se renderizan en el servidor con Jinja2 (`app/templates/`), sin Next.js.
6. **Onboarding self-service** para nuevos tenants — **parcial**: existen `/register`, `/login`, y conectar/desconectar MP desde el panel. Faltan cambio/recupero de contraseña, verificación de email y que `PATCH /tenants/me` funcione con la cookie (hoy exige API key).

### Q2 2027 (mes 4-6)
7. **Worker separado** del scheduler (Celery o ARQ) para >1 réplica (D-005). Hoy corren 4 jobs en el proceso de la API.
8. ~~**Rate limiting** en endpoints públicos (slowapi).~~ — **Resuelto parcialmente en `6ab9cac`** (D-024): login, register y reservas públicas; storage en memoria.
9. ~~`BusinessHours` configurable~~ — **implementado** (modelo + panel CRUD en `app/routers/panel.py`, `/panel/horarios`). El fallback 09-18 solo aplica cuando el tenant no tiene ninguna fila en `business_hours`.
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
