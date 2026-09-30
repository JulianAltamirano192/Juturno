# app/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings
from sqlalchemy.pool import NullPool

# Lazy engines - created on first access
_prod_engine = None
_prod_session_maker = None
_test_engine = None
_test_session_maker = None


def _get_prod_engine():
    global _prod_engine, _prod_session_maker
    if _prod_engine is None:
        _prod_engine = create_async_engine(
            settings.DATABASE_URL,
            echo=False,
            pool_size=10,
            max_overflow=20,
        )
        _prod_session_maker = async_sessionmaker(
            _prod_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _prod_engine, _prod_session_maker


def _get_test_engine():
    global _test_engine, _test_session_maker
    if _test_engine is None and getattr(settings, "TEST_DATABASE_URL", None):
        _test_engine = create_async_engine(
            settings.TEST_DATABASE_URL,
            echo=False,
            poolclass=NullPool,
        )
        _test_session_maker = async_sessionmaker(
            _test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _test_engine, _test_session_maker


def _get_session_maker():
    test_engine, test_session_maker = _get_test_engine()
    if test_session_maker:
        return test_session_maker
    _, prod_session_maker = _get_prod_engine()
    return prod_session_maker


def get_engine():
    test_engine, _ = _get_test_engine()
    if test_engine:
        return test_engine
    prod_engine, _ = _get_prod_engine()
    return prod_engine


def get_session_maker():
    return _get_session_maker()


# Compatibilidad: atributos que el código existente espera (lazy initialization)
class _LazyEngine:
    def __getattr__(self, name):
        return getattr(get_engine(), name)


class _LazySessionMaker:
    def __getattr__(self, name):
        return getattr(_get_session_maker(), name)


engine = _LazyEngine()
async_session_maker = _LazySessionMaker()


async def get_db():
    session_maker = _get_session_maker()
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
