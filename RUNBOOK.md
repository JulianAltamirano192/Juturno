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
