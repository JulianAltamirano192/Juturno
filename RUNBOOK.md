# Runbook de Turnify

Guía operativa para incidentes comunes. Cada sección es un "si pasa X, hacé Y".

**Regla de oro**: si algo no está acá, primero mirá los logs (`docker compose logs api --tail=100`) y Sentry antes de tocar nada.

---

## 🔥 La API no responde

### Síntomas
- `curl http://localhost:8000/health` da timeout o connection refused.
- Los clientes no pueden reservar.
- Los webhooks de Meta/MP fallan.

### Diagnóstico

```bash
cd ~/turnify

# 1. ¿Los contenedores están corriendo?
docker compose ps
```

**Esperado**: `saas_api`, `saas_db`, `saas_redis` en `Up (healthy)`.

```bash
# 2. ¿Qué dicen los logs?
docker compose logs api --tail=50
```

### Fixes por causa

**Causa A — El contenedor está reiniciándose en loop**

```bash
docker compose logs api --tail=100 | grep -i "error\|traceback\|exception"
```

Buscá el error específico. Causas comunes:
- Falta una env var (ej. `SENTRY_DSN` mal formada).
- Error de sintaxis en algún `.py` (revisar el último commit).
- Migración pendiente que rompe el startup.

**Causa B — La DB no está healthy**

```bash
docker compose logs db --tail=50
docker compose restart db
sleep 10
docker compose ps
```

**Causa C — Puerto 8000 ocupado por otro proceso**

```bash
sudo lsof -i :8000
# Matar el proceso intruso o cambiar el puerto en docker-compose.yml
```

**Causa D — Crash silencioso de uvicorn**

```bash
docker compose restart api
sleep 10
curl http://localhost:8000/health
```

### Verificación

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Si responde, el incidente está resuelto.

---

## 💾 La base de datos no responde

### Síntomas
- La API devuelve `500 Internal Server Error` en todos los endpoints.
- Sentry muestra `asyncpg.exceptions.ConnectionDoesNotExistError`.

### Diagnóstico

```bash
docker compose logs db --tail=50
docker compose exec db pg_isready
```

### Fixes

**Causa A — Contenedor caído**

```bash
docker compose restart db
sleep 10
docker compose exec db pg_isready
```

**Causa B — Disco lleno**

```bash
df -h
docker system df
```

Si el disco está >90%:

```bash
# Limpiar imágenes y contenedores huérfanos
docker system prune -f
```

**Causa C — Corrupción de datos**

```bash
# Ver si Postgres puede iniciar en modo recovery
docker compose logs db --tail=100 | grep -i "corrupt\|panic\|fatal"
```

Si hay corrupción, **restaurar backup** (ver sección Backups).

---

## 🔴 Redis no responde

### Síntomas
- El scheduler falla con `ConnectionRefusedError` a Redis.
- El auth tiene latencia (cache miss constante).

### Diagnóstico

```bash
docker compose exec redis redis-cli ping
# Esperado: PONG
```

### Fixes

```bash
# Si no responde PONG:
docker compose restart redis
sleep 5
docker compose exec redis redis-cli ping
```

Redis no tiene datos críticos (solo locks y cache), así que **perder el contenido no es grave**. El sistema se recupera solo.

---

## 📨 Los WhatsApp no llegan

### Síntomas
- Los clientes no reciben confirmaciones ni recordatorios.
- La tabla `notification_outbox` tiene filas en `failed`.

### Diagnóstico

```bash
# ¿Cuántos outbox fallidos hay?
docker compose exec db psql -U postgres -d saas_db -c \
  "SELECT id, booking_id, notification_type, status, retry_count, error_message
   FROM notification_outbox
   WHERE status = 'failed'
   ORDER BY id DESC
   LIMIT 10;"
```

```bash
# Ver los logs del worker
docker compose logs api --tail=100 | grep -i "outbox\|whatsapp\|meta"
```

### Fixes por error de Meta

**Error `131030` — Recipient not in allowed list**
El número del cliente no está autorizado en el panel de Meta (solo aplica en modo sandbox). En producción con número real, este error no aparece.

**Error `132000` — Template not found**
La plantilla `booking_confirmation` o `booking_reminder` no existe o cambió de idioma. Verificar en:
- Panel de Meta → WhatsApp → Plantillas.
- `app/whatsapp_service.py` → campo `name` y `language.code`.

**Error `132001` — Template param mismatch**
Los parámetros `{{1}}, {{2}}, {{3}}` no coinciden con la plantilla. Verificar cantidad y orden en `app/whatsapp_service.py`.

**Error `400 Bad Request` genérico**
Mirar el log completo (Sentry captura el body). Puede ser número mal formado o token expirado.

**Error `401 Unauthorized` de Meta**
El `WHATSAPP_TOKEN` expiró o fue revocado. Regenerar en:
- Meta Business Suite → Usuarios del sistema → Generar token.
- Actualizar `.env` → `WHATSAPP_TOKEN=`.
- `docker compose up -d --force-recreate api`.

### Reintentar outbox fallidos

Una vez resuelto el problema subyacente:

```bash
docker compose exec db psql -U postgres -d saas_db -c \
  "UPDATE notification_outbox
   SET status='pending', retry_count=0, error_message=NULL
   WHERE status='failed';"
```

En el próximo ciclo del worker (60s), se reintentarán.

---

## 💳 Los pagos de MP no confirman bookings

### Síntomas
- Cliente pagó en MP pero el booking sigue en `pending`.
- La tabla `payment` está vacía o tiene un registro sin `approved`.

### Diagnóstico

```bash
# ¿Llegó el webhook de MP?
docker compose logs api --tail=200 | grep -i "mercadopago"
```

**Si no hay ningún log de `/webhooks/mercadopago`**, el webhook no llegó. Verificar:

1. **Panel de MP → Webhooks → Modo prueba** apunta a la URL correcta del túnel.
2. **El túnel de Cloudflare sigue activo**:
   ```bash
   ps aux | grep cloudflared
   curl -i https://tu-url.trycloudflare.com/webhooks/mercadopago \
     -H "Content-Type: application/json" -d '{}'
   # Esperado: 401 (firma inválida, pero llega)
   ```

**Si el webhook llegó pero da 401**, el `MP_SECRET_KEY` no coincide con el del panel. Regenerar en el panel y actualizar `.env`.

**Si el webhook llegó con 200 pero el booking sigue `pending`**:

```bash
# Ver payment_events recientes
docker compose exec db psql -U postgres -d saas_db -c \
  "SELECT event_id, status, processed_at FROM payment_events ORDER BY received_at DESC LIMIT 5;"

# Ver payments
docker compose exec db psql -U postgres -d saas_db -c \
  "SELECT id, booking_id, mp_payment_id, status FROM payment ORDER BY id DESC LIMIT 5;"
```

Si `payment_events.status = 'processed'` pero no hay `payment` con `approved`, hubo un error interno. Mirar Sentry.

---

## 🔧 Comandos útiles

### Ver estado general

```bash
cd ~/turnify
docker compose ps
docker compose logs api --tail=30
```

### Reiniciar todo (sin perder datos)

```bash
docker compose down
docker compose up -d
sleep 15
curl http://localhost:8000/health
```

### Reiniciar todo (perdiendo datos — solo para desarrollo)

```bash
docker compose down -v  # ⚠️ Borra el volumen de Postgres
docker compose up -d
docker compose exec api alembic upgrade head
```

### Aplicar migraciones pendientes

```bash
docker compose exec api alembic upgrade head
docker compose exec api alembic current
```

### Correr tests

```bash
docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://postgres:$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@db:5432/saas_test" \
  api pytest -v
```

### Ver el estado del outbox

```bash
docker compose exec db psql -U postgres -d saas_db -c \
  "SELECT status, COUNT(*) FROM notification_outbox GROUP BY status;"
```

### Ver el estado de los bookings

```bash
docker compose exec db psql -U postgres -d saas_db -c \
  "SELECT status, COUNT(*) FROM booking GROUP BY status;"
```

### Entrar a la DB

```bash
docker compose exec db psql -U postgres -d saas_db
```

### Entrar al contenedor de la API

```bash
docker compose exec api bash
```

---

## 🆘 Si nada de esto funciona

1. **Mirá Sentry primero**: https://sentry.io → proyecto `turnify`.
2. **Guardá los logs**:
   ```bash
   docker compose logs api > /tmp/api_logs_$(date +%Y%m%d_%H%M).txt
   docker compose logs db > /tmp/db_logs_$(date +%Y%m%d_%H%M).txt
   ```
3. **Reiniciá todo**:
   ```bash
   docker compose down
   docker compose up -d --build
   sleep 20
   curl http://localhost:8000/health
   ```
4. **Si sigue sin andar**: hay que revisar el código. Revisar el último commit en Git y hacer `git revert` si es necesario.

---

## Backups (ver también Día 5)

### Backup manual

```bash
docker compose exec -T db pg_dump -U postgres saas_db > backup_$(date +%Y%m%d_%H%M).sql
```

### Restaurar backup

```bash
cat backup_20260915_1200.sql | docker compose exec -T db psql -U postgres -d saas_db
```

**⚠️ Cuidado**: la restauración **borra** los datos actuales. Hacer backup antes de restaurar.

---

## Contactos

- **Sentry**: https://sentry.io
- **Meta Developers**: https://developers.facebook.com/apps
- **Mercado Pago Developers**: https://www.mercadopago.com.ar/developers/panel/app
- **Cloudflare Dashboard**: https://one.dash.cloudflare.com
```

---

## `DECISIONS.md`

```bash
nano DECISIONS.md
```

Pegá esto:

````markdown
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
