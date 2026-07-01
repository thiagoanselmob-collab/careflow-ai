import os
os.environ["DEBOUNCE_SECONDS"] = "0.01"

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.models.base import Base


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """
    Creates an asynchronous database engine for integration testing.
    """
    # Use SQLite in-memory for testing to avoid dependency on running Postgres server
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(test_engine):
    """
    Fixture that handles dynamic schema creation and rollback teardown.
    Yields an active AsyncSession.
    """
    # Dynamically create tables in the database before the test
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Initialize the sessionmaker
    async_session = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    async with async_session() as session:
        yield session

    # Drop the tables after the test finishes
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def cleanup_sqlite_files():
    """
    Autouse fixture to clean up physical sqlite database files created
    on disk due to URI-mode mismatch during tests.
    """
    import os
    import glob
    def _clean():
        for pattern in ["file:*", "file:*.*"]:
            for f in glob.glob(pattern):
                try:
                    os.remove(f)
                except Exception:
                    pass
    _clean()
    yield
    _clean()

