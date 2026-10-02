# Runbook de Juturno

Guía operativa para incidentes comunes. Cada sección indica "si ocurre X, realizar Y".

**Principio**: si un escenario no está documentado acá, revisar primero los logs (`docker compose logs api --tail=100`) y Sentry antes de modificar nada.

---

## La API no responde

### Síntomas
- `curl http://localhost:8000/health` da timeout o connection refused.
- Los clientes no pueden reservar.
- Los webhooks de Meta/MP fallan.

### Diagnóstico

```bash
cd ~/juturno

# 1. ¿Los contenedores están corriendo?
docker compose ps
```

**Esperado**: `saas_api`, `saas_db`, `saas_redis` en `Up (healthy)`.

```bash
# 2. ¿Qué dicen los logs?
docker compose logs api --tail=50
```

### Remediación por causa

**Causa A — El contenedor está reiniciándose en loop**

```bash
docker compose logs api --tail=100 | grep -i "error\|traceback\|exception"
```

Buscar el error específico. Causas comunes:
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
# Terminar el proceso intruso o cambiar el puerto en docker-compose.yml
```

**Causa D — Crash silencioso de uvicorn**

```bash
docker compose restart api
sleep 10
curl http://localhost:8000/health
```

**Causa E — El contenedor crashea con `ValidationError` (env var faltante en producción)**

**Síntoma específico**: en producción, `curl https://api.juturno.com/health` devuelve 503 y Traefik responde `no available server`. En el VPS, el contenedor de la API aparece en `Restarting` (crash loop).

```bash
# En el VPS:
ssh julian@46.224.147.8

# 1. Ver el estado del contenedor de la API
sudo docker ps -a | grep api-

# 2. Ver los logs del contenedor en crash loop
sudo docker logs <container_api_id> --tail=50
```

**Buscar en los logs**: `pydantic_core.ValidationError` con mensaje tipo:
```
Value error, SECRET_KEY cannot be the default value in production!
```

**Causa**: alguna variable de entorno requerida falta o tiene el valor default en un entorno con `ENVIRONMENT=production`. El validador de `config.py` rechaza el arranque para prevenir un deploy inseguro (ver D-014 en `DECISIONS.md`).

**Variables críticas a verificar en Coolify**:
- `SECRET_KEY` — obligatoria en producción, no puede ser el default `change-this-secret-key-in-production-juturno`.
- `META_APP_SECRET` — obligatoria si hay webhooks de Meta activos.
- `MP_TOKEN_ENCRYPTION_KEY` — obligatoria si hay tenants con OAuth MP conectado (clave Fernet válida).

**Fix**:
1. Ir al panel de Coolify: `http://46.224.147.8:8000`
2. Proyecto `Juturno` → `juturno-api` → menú lateral **Environment Variables**.
3. Agregar la variable faltante con un valor seguro. Para `SECRET_KEY`, generar uno:
   ```bash
   openssl rand -hex 32
   ```
   Guardarlo en el gestor de secretos y pegarlo en Coolify.
4. **Actions** → **Deploy**. Esperar 2-3 minutos (build + arranque).
5. Verificar desde el host:
   ```bash
   curl -s https://api.juturno.com/health
   # Esperado: {"status":"ok","checks":{"api":"ok","database":"ok","redis":"ok"}}
   ```

**Prevención**: antes de cada deploy a producción, verificar que TODAS las variables requeridas por `app/config.py` (clase `Settings`) estén seteadas en Coolify. La lista completa está en el archivo; las críticas para arranque son `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`.

**Referencia**: D-014 en `DECISIONS.md`.

### Verificación

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Si responde, el incidente está resuelto.

---

## La base de datos no responde

### Síntomas
- La API devuelve `500 Internal Server Error` en todos los endpoints.
- Sentry muestra `asyncpg.exceptions.ConnectionDoesNotExistError`.

### Diagnóstico

```bash
docker compose logs db --tail=50
docker compose exec db pg_isready
```

### Remediación

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

## Redis no responde

### Síntomas
- El scheduler falla con `ConnectionRefusedError` a Redis.
- El auth tiene latencia (cache miss constante).

### Diagnóstico

```bash
docker compose exec redis redis-cli ping
# Esperado: PONG
```

### Remediación

```bash
# Si no responde PONG:
docker compose restart redis
sleep 5
docker compose exec redis redis-cli ping
```

Redis no almacena datos críticos (solo locks y cache), por lo que perder su contenido no compromete el servicio: el sistema se recupera de forma automática.

---

## Los WhatsApp no llegan

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

### Remediación según error de Meta

**Error `131030` — Recipient not in allowed list**
El número del cliente no está autorizado en el panel de Meta (solo aplica en modo sandbox). En producción con número real, este error no aparece.

**Error `132000` — Template not found**
La plantilla `booking_confirmation` o `booking_reminder` no existe o cambió de idioma. Verificar en:
- Panel de Meta → WhatsApp → Plantillas.
- `app/whatsapp_service.py` → campo `name` y `language.code`.

**Error `132001` — Template param mismatch**
Los parámetros `{{1}}, {{2}}, {{3}}` no coinciden con la plantilla. Verificar cantidad y orden en `app/whatsapp_service.py`.

**Error `400 Bad Request` genérico**
Revisar el log completo (Sentry captura el body). Puede ser número mal formado o token expirado.

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

## Los pagos de MP no confirman bookings

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

Si `payment_events.status = 'processed'` pero no hay `payment` con `approved`, hubo un error interno. Revisar Sentry.

### MP OAuth: tokens vencidos o refresh fallido

**Contexto**: cada tenant conecta su propia cuenta de Mercado Pago vía OAuth (ver D-012 en `DECISIONS.md`). Los tokens viven ~180 días y se renuevan automáticamente vía el job diario `process_mp_token_refresh`. Si MP rechaza el refresh (usuario revocó permisos, cambió contraseña, etc.), el tenant queda desconectado hasta que reconecte manualmente.

**Síntomas**:
- Un negocio no puede cobrar: `POST /public/bookings` devuelve 400 con mensaje de MP.
- Los webhooks de MP del tenant no procesan pagos.
- Sentry reporta errores de `MPTokenCryptoError` o `refresh_tenant_mp_token` fallando.
- El panel del dueño muestra "Mercado Pago desconectado".

**Diagnóstico**:

```bash
# Ver el estado de conexión de cada tenant
docker compose exec -T db psql -U postgres -d saas_db << 'EOF'
SELECT
  id,
  name,
  mp_user_id,
  mp_alias,
  mp_token_expires_at,
  CASE
    WHEN mp_access_token_enc IS NULL THEN 'sin conexión'
    WHEN mp_token_expires_at < NOW() THEN 'vencido'
    WHEN mp_token_expires_at < NOW() + INTERVAL '7 days' THEN 'por vencer'
    ELSE 'ok'
  END AS estado
FROM tenant
ORDER BY mp_token_expires_at NULLS FIRST;
EOF
```

**Interpretación**:
- `sin conexión`: el tenant nunca conectó su cuenta MP. Los pagos usan el fallback de plataforma (`MP_ACCESS_TOKEN`).
- `vencido`: el token venció y el refresh falló. Requiere reconexión manual.
- `por vencer`: el job diario lo va a renovar en la próxima corrida.
- `ok`: no hay nada que hacer.

**Remediación**:

**Caso 1 — El tenant nunca conectó MP (esperado)**:
- Los pagos van a la cuenta de la plataforma (`MP_ACCESS_TOKEN`).
- El dueño debería ir a `/mp/connect/start` para conectar su cuenta.

**Caso 2 — El token venció y el refresh falló**:
- El dueño del negocio debe ir a su panel → **Conectar Mercado Pago** → `/mp/connect/start` para reconectar.
- No se puede forzar el refresh desde el backend; MP requiere interacción del usuario (OAuth consent).

**Caso 3 — El refresh se ejecutó pero falló por error transitorio**:
- Correr el job manualmente para forzar el retry:
  ```bash
  docker compose exec api python -c "
  import asyncio
  from app.database import async_session_maker
  from app.scheduler import process_mp_token_refresh
  asyncio.run(process_mp_token_refresh(async_session_maker))
  "
  ```
- Verificar en logs: `docker compose logs api --tail=20 | grep "Refresh de tokens MP"`.

**Caso 4 — `MP_TOKEN_ENCRYPTION_KEY` cambió (imposible descifrar)**:
- Si se rota la clave Fernet, los tokens guardados son indescifrables.
- Todos los tenants con MP conectado deben reconectar manualmente.
- **Prevención**: rotar `MP_TOKEN_ENCRYPTION_KEY` requiere un plan de migración previo (no implementado hoy — deuda técnica anotada).

**Prevención**:
- El job diario `process_mp_token_refresh` renueva tokens con 30 días de anticipación (`REFRESH_AHEAD_DAYS` en `app/mp_connect.py`).
- Monitorear logs de MP OAuth para detectar fallos tempranos.
- El panel del dueño muestra el estado de conexión MP en `/dashboard`.

---

## Comandos útiles

### Ver estado general

```bash
cd ~/juturno
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

## Si nada de esto funciona

1. **Revisar Sentry**: https://sentry.io → proyecto `juturno`.
2. **Conservar los logs**:
   ```bash
   docker compose logs api > /tmp/api_logs_$(date +%Y%m%d_%H%M).txt
   docker compose logs db > /tmp/db_logs_$(date +%Y%m%d_%H%M).txt
   ```
3. **Reiniciar todo**:
   ```bash
   docker compose down
   docker compose up -d --build
   sleep 20
   curl http://localhost:8000/health
   ```
4. **Si sigue sin responder**: revisar el código. Evaluar el último commit en Git y hacer `git revert` si corresponde.

---

## Backups

Procedimiento canónico: `scripts/backup_db.sh` (pg_dump comprimido + rotación a 30 días).
Contexto de diseño: `ARCHITECTURE.md` → sección 11.

### Crear un backup

```bash
# Ruta por defecto: ./backups/saas_db_YYYYMMDD_HHMMSS.sql.gz
./scripts/backup_db.sh

# Directorio alternativo (ej. un volumen externo)
./scripts/backup_db.sh /mnt/backup-externo
```

El script sale con código de error si el dump queda vacío y elimina los backups con más de 30 días.

### Restaurar un backup

```bash
gunzip -c backups/saas_db_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose exec -T db psql -U postgres -d saas_db
```

**⚠️ Cuidado**: la restauración **reemplaza** los datos actuales (el dump se genera con `--clean --if-exists`).
Verificar que exista un backup vigente antes de restaurar.

### Backup puntual sin el script

```bash
docker compose exec -T db pg_dump -U postgres saas_db | gzip > backup_$(date +%Y%m%d_%H%M%S).sql.gz
```

Este archivo no participa de la rotación automática: limpiarlo manualmente.

---

## Contactos

- **Sentry**: https://sentry.io
- **Meta Developers**: https://developers.facebook.com/apps
- **Mercado Pago Developers**: https://www.mercadopago.com.ar/developers/panel/app
- **Cloudflare Dashboard**: https://one.dash.cloudflare.com
