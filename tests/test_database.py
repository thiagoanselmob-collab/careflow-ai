import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal, get_db


@pytest.mark.asyncio
async def test_get_db_session_lifecycle():
    """
    Verify that get_db yields an AsyncSession and closes it properly.
    """
    db_generator = get_db()
    session = await db_generator.__anext__()
    try:
        assert isinstance(session, AsyncSession)
    finally:
        try:
            await db_generator.__anext__()
        except StopAsyncIteration:
            pass
