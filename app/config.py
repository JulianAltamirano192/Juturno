# app/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/saas"
    REDIS_URL: str = "redis://redis:6379/0"
    SECRET_KEY: str = "change-this-secret-key-in-production-juturno"
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    MP_ACCESS_TOKEN: str = ""
    MP_SECRET_KEY: str = ""
    # OAuth marketplace: credenciales de la aplicación registrada en MP
    # Developers (modelo de integración Checkout Pro + OAuth).
    MP_MARKETPLACE_CLIENT_ID: str = ""
    MP_MARKETPLACE_CLIENT_SECRET: str = ""
    MP_MARKETPLACE_REDIRECT_URL: str = "https://api.juturno.com/mp/connect/callback"
    # URL pública del webhook de MP de ESTE entorno (notification_url de cada
    # preferencia). Vacía = no se manda: en local sin túnel MP no notifica,
    # en vez de mandarle a producción los pagos de sandbox. Obligatoria en prod.
    MP_NOTIFICATION_URL: str = ""
    # Clave Fernet para cifrar en reposo los tokens OAuth de los tenants.
    # Se genera una vez: Fernet.generate_key() y vive solo en env.
    MP_TOKEN_ENCRYPTION_KEY: str = ""
    # true = credenciales de prueba: el checkout usa sandbox_init_point.
    # Pasar a false al migrar a credenciales de producción de MP.
    MP_SANDBOX: bool = True
    # Solo existe en entorno de test (la define el fixture conftest / docker compose exec).
    # Si está presente, el lifespan NO arranca el scheduler para evitar colisiones de conexión.
    TEST_DATABASE_URL: str = ""
    META_VERIFY_TOKEN: str = ""
    META_APP_SECRET: str = ""
    CORS_ORIGINS: list[str] = []
    SENTRY_DSN: str = ""
    ENVIRONMENT: str = "development"
    # URL pública donde vive la página de reserva (se usa para back_urls de MP)
    PUBLIC_BASE_URL: str = "https://juturno.com"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    def model_post_init(self, __context, /) -> None:
        if (
            self.is_production
            and self.SECRET_KEY == "change-this-secret-key-in-production-juturno"
        ):
            raise ValueError("SECRET_KEY cannot be the default value in production!")
        if self.is_production and self.MP_SANDBOX:
            raise ValueError("MP_SANDBOX must be False in production")
        if self.is_production:
            missing = [
                name
                for name, val in [
                    ("META_APP_SECRET", self.META_APP_SECRET),
                    ("META_VERIFY_TOKEN", self.META_VERIFY_TOKEN),
                    ("MP_TOKEN_ENCRYPTION_KEY", self.MP_TOKEN_ENCRYPTION_KEY),
                    ("MP_SECRET_KEY", self.MP_SECRET_KEY),
                    ("WHATSAPP_TOKEN", self.WHATSAPP_TOKEN),
                    ("WHATSAPP_PHONE_NUMBER_ID", self.WHATSAPP_PHONE_NUMBER_ID),
                    ("MP_NOTIFICATION_URL", self.MP_NOTIFICATION_URL),
                ]
                if not val
            ]
            if missing:
                raise ValueError(
                    f"Missing required env vars in production: {', '.join(missing)}"
                )
            # Un typo no rompe el arranque pero deja reservas pagas sin confirmar.
            url = self.MP_NOTIFICATION_URL
            if not (
                url.startswith("https://") and url.endswith("/webhooks/mercadopago")
            ):
                raise ValueError(
                    "MP_NOTIFICATION_URL must be https://.../webhooks/mercadopago "
                    "in production"
                )

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
