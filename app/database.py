# app/database.py
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings
from sqlalchemy.pool import NullPool

# Selector simple en tiempo de import: si TEST_DATABASE_URL está seteada,
# usamos el engine de test (NullPool), si no, el de producción (pooling normal).
# Esto evita que el validador de mypy se pierde con un wrapper lazy.
if getattr(settings, "TEST_DATABASE_URL", ""):
    engine = create_async_engine(
        settings.TEST_DATABASE_URL,
        echo=False,
        poolclass=NullPool,
    )
else:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_size=10,
        max_overflow=20,
    )

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency para FastAPI."""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
