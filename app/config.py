# app/config.py
from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/saas"
    REDIS_URL: str = "redis://redis:6379/0"
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    MP_ACCESS_TOKEN: str = ""
    MP_SECRET_KEY: str = ""
    # true = credenciales de prueba: el checkout usa sandbox_init_point.
    # Pasar a false al migrar a credenciales de producción de MP.
    MP_SANDBOX: bool = True
    META_VERIFY_TOKEN: str = ""
    META_APP_SECRET: str = ""
    CORS_ORIGINS: List[str] = []
    SENTRY_DSN: str = ""
    ENVIRONMENT: str = "development"
    # URL pública donde vive la página de reserva (se usa para back_urls de MP)
    PUBLIC_BASE_URL: str = "https://juturno.com"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
