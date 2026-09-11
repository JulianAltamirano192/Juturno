# Turnify

API de gestión de turnos construida con FastAPI, PostgreSQL, Redis, WhatsApp y Mercado Pago.

## Setup

1. Copiá `.env.example` a `.env` y completá los valores.
2. Arrancá los servicios:

   ```bash
   docker compose up --build
   ```

## Variables requeridas

`DATABASE_URL`, `REDIS_URL`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`MP_ACCESS_TOKEN`, `MP_SECRET_KEY`, `META_VERIFY_TOKEN` y `CORS_ORIGINS`.

## Migraciones

Con los servicios levantados, ejecutá:

```bash
docker compose exec api alembic upgrade head
```

## Tests

Los tests de integración usan exclusivamente `saas_test`. Creala antes de correrlos:

```bash
docker compose exec db createdb -U postgres saas_test
docker compose exec -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/saas_test api pytest -v
```

## Worker de outbox

El worker de outbox se ejecuta como job de APScheduler cada minuto al iniciar la API. Por lo tanto,
`docker compose up` también inicia el procesamiento de notificaciones pendientes.
