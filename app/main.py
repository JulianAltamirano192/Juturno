# app/main.py
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.auth import RedirectToLoginException
from app.config import settings
from app.database import async_session_maker
from app.limiter import limiter
from app.mp_connect import router as mp_connect_router
from app.mp_webhooks import router as mp_router
from app.outbox_worker import process_outbox
from app.routers.api import router as api_router
from app.routers.auth import router as auth_router
from app.routers.panel import router as panel_router
from app.routers.public import router as public_router
from app.scheduler import (
    process_deposit_expiration,
    process_mp_token_refresh,
    process_reminders,
)
from app.session import delete_session_cookie, sanitize_next_url
from app.webhooks import router as whatsapp_router


# --- SENTRY (inicializar antes de crear la app) ---
def _strip_query_string(event, hint):
    # El query string puede traer el code OAuth de MP o el verify token de Meta.
    event.get("request", {}).pop("query_string", None)
    return event


def sentry_options(dsn: str) -> dict:
    return {
        "dsn": dsn,
        "environment": settings.ENVIRONMENT,
        "integrations": [
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
            HttpxIntegration(),
        ],
        "traces_sample_rate": 0.1,
        "profiles_sample_rate": 0.1,
        "send_default_pii": False,
        # Los frames del refresh MP tienen client_secret y tokens en claro;
        # el scrubber de Sentry no es recursivo, así que no mandamos locals.
        "include_local_variables": False,
        # send_default_pii=False no filtra X-Tenant-API-Key; recursive cubre
        # claves anidadas.
        "event_scrubber": EventScrubber(
            denylist=[*DEFAULT_DENYLIST, "x-tenant-api-key"], recursive=True
        ),
        # Los bodies traen teléfonos y nombres de clientes (reservas, webhooks).
        "max_request_body_size": "never",
        "before_send": _strip_query_string,
        # before_send no corre sobre transacciones, que también llevan request.
        "before_send_transaction": _strip_query_string,
    }


if settings.SENTRY_DSN:
    sentry_sdk.init(**sentry_options(settings.SENTRY_DSN))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# --- SCHEDULER + LIFESPAN ---

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestiona el ciclo de vida de la aplicación FastAPI.
    Arranca el scheduler de recordatorios al iniciar y lo apaga limpiamente al cerrar.
    En entorno de test (TEST_DATABASE_URL seteada) NO arranca el scheduler
    para evitar colisiones de conexión con los tests.
    """
    test_db_url = getattr(settings, "TEST_DATABASE_URL", None)
    if not test_db_url:
        scheduler.add_job(
            process_reminders,
            "interval",
            minutes=5,
            args=[async_session_maker],
            id="reminder_job",
            replace_existing=True,
        )
        scheduler.add_job(
            process_outbox,
            "interval",
            minutes=1,
            args=[async_session_maker],
            id="outbox_job",
            replace_existing=True,
        )
        scheduler.add_job(
            process_deposit_expiration,
            "interval",
            minutes=1,
            args=[async_session_maker],
            id="deposit_expiration_job",
            replace_existing=True,
        )
        scheduler.add_job(
            process_mp_token_refresh,
            "interval",
            minutes=1440,
            args=[async_session_maker],
            id="mp_token_refresh_job",
            replace_existing=True,
        )
        scheduler.start()
        print("Scheduler distribuido iniciado correctamente.")
    else:
        print("Entorno de test detectado: scheduler NO iniciado.")

    yield

    if not getattr(settings, "TEST_DATABASE_URL", None):
        scheduler.shutdown()
        print("Scheduler detenido de forma segura.")
    else:
        print("Entorno de test: nada que apagar.")


# --- APP ---

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The slim image has no /etc/mime.types, so these would be served as octet-stream.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("font/woff2", ".woff2")
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)
app.include_router(public_router)
app.include_router(auth_router)
app.include_router(api_router)
app.include_router(panel_router)
app.include_router(mp_router)
app.include_router(mp_connect_router)
app.include_router(whatsapp_router)


@app.exception_handler(RedirectToLoginException)
async def redirect_to_login_handler(request: Request, exc: RedirectToLoginException):
    """Redirige automáticamente a /login si no hay sesión activa en el panel."""
    safe_next = sanitize_next_url(exc.next_url)
    response = RedirectResponse(
        url=f"/login?next={safe_next}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    delete_session_cookie(response)
    return response
