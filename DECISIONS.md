# Decisiones de diseño

Este documento registra las decisiones técnicas importantes y **por qué** se
tomaron. Es útil cuando en el futuro alguien (vos, un colaborador, o yo mismo)
se pregunte "¿por qué está hecho así?".

## Formato

Cada decisión sigue este formato:
- **Fecha**: cuándo se tomó.
- **Contexto**: qué problema había que resolver.
- **Decisión**: qué se hizo.
- **Alternativas consideradas**: qué otras opciones había.
- **Consecuencias**: ventajas, desventajas, deuda técnica generada.

---

## D-001: Multi-tenancy con shared database

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos que múltiples negocios usen la misma aplicación sin
ver los datos de otros.

**Decisión**: Shared database con columna `tenant_id` en todas las tablas
relevantes. Aislamiento garantizado por lógica de aplicación + constraints.

**Alternativas**:
- **Database per tenant**: más aislado pero requiere N conexiones y migraciones.
- **Schema per tenant**: complejo en Postgres, difícil de migrar.

**Consecuencias**:
- ✅ Operación simple: una sola DB, un solo pool de conexiones.
- ✅ Costo bajo: un servidor atiende a N tenants.
- ⚠️ Riesgo: si hay un bug en el filtrado por `tenant_id`, un tenant puede ver
  datos de otro. Mitigado con tests + `get_current_tenant` obligatorio.
- 📌 Deuda: si un tenant crece mucho, no hay forma de aislarlo fácilmente.

---

## D-002: Autenticación con API key (no JWT)

**Fecha**: Septiembre 2026

**Contexto**: Los clientes (dueños de negocios) necesitan autenticarse contra
la API.

**Decisión**: Header `X-Tenant-API-Key` con key hasheada (SHA-256) en DB.

**Alternativas**:
- **JWT con email/password**: más complejo, requiere gestión de sesiones.
- **OAuth2**: overkill para un SaaS B2B.
- **Basic Auth**: no permite rotación ni revocación granular.

**Consecuencias**:
- ✅ Simple: una key, un lookup.
- ✅ Rotación sin downtime: múltiples keys activas por tenant.
- ✅ Revocación granular: `revoked_at` por key.
- ⚠️ Un cliente con una key puede acceder a todo su tenant (no hay permisos
  granulares por usuario).
- 📌 Deuda: cuando agreguemos usuarios admin en el frontend, vamos a necesitar
  algo más (JWT probablemente).

**Por qué SHA-256 y no bcrypt**:
Las keys son secretos de 256 bits generados por CLI. No necesitan hashing
lento (no hay riesgo de diccionario). El determinismo permite búsqueda por
índice único. Bcrypt obligaría a iterar todas las keys en cada request.

---

## D-003: ExcludeConstraint para anti-solapamiento

**Fecha**: Septiembre 2026

**Contexto**: No puede haber dos bookings superpuestos para el mismo staff.

**Decisión**: Usar `EXCLUDE USING gist` de PostgreSQL con `btree_gist` y
`tstzrange`.

**Alternativas**:
- **Validar en aplicación**: race condition bajo concurrencia.
- **Trigger de DB**: complejo de mantener.
- **Lock advisory**: peor performance.

**Consecuencias**:
- ✅ Atómico: la DB garantiza la unicidad incluso bajo concurrencia.
- ✅ Declarativo: el constraint está en la migración, versionado.
- ✅ Cross-tenant safe: incluye `tenant_id` como primera dimensión.
- ⚠️ Requiere extensión `btree_gist` (incluida en Postgres 16, pero hay que
  `CREATE EXTENSION`).
- 📌 Deuda: si en algún momento se quiere permitir overlap condicional (ej.
  servicios que no bloquean), hay que revisar el constraint.

---

## D-004: Patrón Outbox para notificaciones

**Fecha**: Septiembre 2026

**Contexto**: Enviar WhatsApp en el request de creación del booking mete
latencia de Meta en la respuesta al cliente. Si Meta se cae, la reserva falla.

**Decisión**: Insertar `NotificationOutbox` en la misma transacción que el
booking, y procesar los pendientes con un job de APScheduler.

**Alternativas**:
- **Enviar síncrono en el request**: mala UX, acopla la reserva a Meta.
- **BackgroundTasks de FastAPI**: se pierde si el proceso se reinicia.
- **Celery / RQ**: mejor para multi-instancia, pero agrega complejidad y otro
  proceso que mantener.

**Consecuencias**:
- ✅ Atomicidad: reserva y notificación son una sola transacción.
- ✅ Resiliencia: si Meta se cae, el outbox queda `failed` y se reintenta.
- ✅ Desacoplamiento: la latencia de Meta no afecta al cliente.
- ⚠️ La notificación no es inmediata: puede tardar hasta 60s.
- 📌 Deuda: si migramos a worker separado, hay que mover este código.

---

## D-005: APScheduler dentro del proceso de la API

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos jobs periódicos (outbox, recordatorios).

**Decisión**: APScheduler `AsyncIOScheduler` corriendo en el `lifespan` de
FastAPI.

**Alternativas**:
- **Celery + Redis broker**: más robusto, pero requiere un worker aparte.
- **Cron externo**: complicado de coordinar con Docker.
- **RQ / Dramatiq**: alternativas a Celery.

**Consecuencias**:
- ✅ Simplicidad: un solo proceso, un solo Dockerfile.
- ✅ Sin infraestructura extra: no hay broker que mantener.
- ⚠️ Si corren N réplicas, cada una ejecuta el cron. Mitigado con lock Redis.
- ⚠️ Si el proceso de la API se cae, los jobs se detienen (hasta que se reinicie).
- 📌 Deuda: migrar a worker separado cuando tengamos >1 réplica o cuando la
  carga lo justifique.

---

## D-006: Mercado Pago con API de Preferencias (no Orders)

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos cobrar señas.

**Decisión**: Usar la API de Preferencias (`POST /checkout/preferences`) de MP.

**Alternativas**:
- **API de Orders**: la nueva, recomendada para proyectos nuevos.
- **Checkout Bricks**: embebido en el frontend.
- **Checkout API**: requiere construir todo el flujo en el backend.

**Consecuencias**:
- ✅ Funciona hoy y está documentada.
- ✅ Amplia base de ejemplos.
- ⚠️ MP la considera "legacy" y no recibe nuevas features.
- 📌 Deuda: migrar a Orders API cuando tengamos 20-30 clientes (primer trimestre
  2027).

---

## D-007: WhatsApp con plantillas (no mensajes de sesión)

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos notificar confirmaciones y recordatorios.

**Decisión**: Usar plantillas de categoría "Utility" (`booking_confirmation`,
`booking_reminder`).

**Alternativas**:
- **Mensajes de sesión** (texto libre): solo dentro de ventana de 24h.
- **Mensajes de Marketing**: rechazados para notificaciones transaccionales.

**Consecuencias**:
- ✅ Aprobadas por Meta, sin riesgo de bloqueo.
- ✅ Categoría correcta (no se cobra como Marketing).
- ✅ Funcionan fuera de la ventana de 24h.
- ⚠️ Los cambios a plantillas requieren re-aprobación (24h-72h).
- ⚠️ A partir de octubre 2026, Meta cobra por estos mensajes (ver D-008).

---

## D-008: Modelo de negocio con suscripción + absorción de costos de WhatsApp

**Fecha**: Septiembre 2026

**Contexto**: Meta empezará a cobrar por mensajes de utilidad a partir del
1 de octubre de 2026.

**Decisión**: Absorber el costo de WhatsApp en la suscripción mensual
(Pro = $15.000 ARS/mes).

**Alternativas**:
- **Cobrar por mensaje**: complejo administrativamente, los clientes lo odian.
- **Limitar cantidad de mensajes**: frustra al cliente.
- **Plan premium sin WhatsApp**: pierde el valor diferencial del producto.

**Consecuencias**:
- ✅ Modelo simple: un solo precio mensual.
- ✅ Costo de WhatsApp (centavos por mensaje) es despreciable frente al ingreso.
- ✅ Foco en valor, no en consumo.
- 📌 Revisar cuando tengamos >1000 mensajes/mes por tenant (ahí el costo empieza
  a ser significativo).

**Proyección**:
- Tenant promedio: 100 turnos/mes = 200 mensajes (confirmación + recordatorio).
- Costo por mensaje: ~$0.007 USD.
- Costo por tenant: ~$1.4 USD/mes.
- Precio Pro: $15.000 ARS/mes (~$15 USD).
- Margen: >90%.

---

## D-009: Timezones con `ZoneInfo` (no `pytz`)

**Fecha**: Septiembre 2026

**Contexto**: Cada tenant tiene su timezone. Hay que convertir correctamente.

**Decisión**: Usar `zoneinfo` de la stdlib + columna `timezone` por tenant.

**Alternativas**:
- **`pytz`**: librería externa, maneja timezones de forma distinta.
- **Hardcodear UTC**: no sirve para LATAM con DST.

**Consecuencias**:
- ✅ Sin dependencia externa.
- ✅ Timezone real por tenant.
- ✅ Correcto en cambios de horario (DST).
- ⚠️ Requiere que `tzdata` esté instalado (incluido en Docker).
- 📌 Deuda: si un tenant tiene múltiples ubicaciones, va a necesitar timezone
  por staff o por servicio.

---

## D-010: Docker Compose (no Kubernetes)

**Fecha**: Septiembre 2026

**Contexto**: Necesitamos correr la app en un servidor.

**Decisión**: Docker Compose en un solo VPS.

**Alternativas**:
- **Kubernetes**: overkill para 1 servidor.
- **Fly.io / Railway**: managed, pero menos control.
- **Bare metal**: peor experiencia.

**Consecuencias**:
- ✅ Simple: `docker compose up` y listo.
- ✅ Reproducible: mismo entorno en dev y prod.
- ✅ Económico: un VPS chico alcanza para 50-100 tenants.
- ⚠️ No escala horizontalmente sin refactor (necesitaríamos worker separado).
- 📌 Deuda: cuando lleguemos a 500+ tenants, evaluar migrar a Kubernetes o
  Fly.io.

---

## Roadmap de deuda técnica

Ordenado por impacto/urgencia estimada:

### Q1 2027 (mes 1-3)
1. **Frontend público + panel admin** (Next.js).
2. **Deploy a producción** (VPS + dominio + Cloudflare Named Tunnel).
3. **Backups automáticos** de Postgres.

### Q2 2027 (mes 4-6)
4. **Worker separado** del scheduler (Celery o ARQ).
5. **Rate limiting** en endpoints públicos (slowapi).
6. **`BusinessHours` table** (horarios configurables por tenant).
7. **Migrar a MP Orders API**.

### Q3 2027 (mes 7-9)
8. **Métricas de producto** (PostHog).
9. **Analytics para dueños de negocios** (dashboards).
10. **Multi-staff avanzado** (turnos compartidos, rotaciones).
11. **Notificaciones configurables** por tenant (24h, 2h, ambas).

### Q4 2027 (mes 10-12)
12. **Escalabilidad multi-instancia** (Kubernetes o Fly.io).
13. **Facturación AFIP** (integración).
14. **API pública** para integraciones externas.

---

## ¿Cómo agregar una nueva decisión?

1. Copiar el formato de las decisiones existentes.
2. Numerar como `D-0XX` (siguiente número).
3. Ser honesto sobre las consecuencias negativas y la deuda generada.
4. Commitear con `docs: add D-0XX decision about X`.
