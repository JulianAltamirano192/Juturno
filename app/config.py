# app/config.py
from pydantic_settings import BaseSettings
import os

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/saas"
    REDIS_URL: str = "redis://localhost:6379/0"
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    MP_ACCESS_TOKEN: str = ""
    MP_SECRET_KEY: str = ""
    
    class Config:
        env_file = ".env"

settings = Settings()   