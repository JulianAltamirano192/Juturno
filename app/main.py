# app/main.py
import logging
from contextlib import asynccontextmanager

import sentry_sdk
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
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
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        integrations=[
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
            HttpxIntegration(),
        ],
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
        send_default_pii=False,
    )


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
