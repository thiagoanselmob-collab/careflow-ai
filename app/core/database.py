from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# Create database engine using the configured database URL.
# Since we are using an async driver (postgresql+asyncpg), we use create_async_engine.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
)

# Configure the sessionmaker to create AsyncSession instances.
SessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency generator that yields an active asynchronous database session
    and ensures it is closed after the request completes.
    """
    async with SessionLocal() as session:
        yield session
