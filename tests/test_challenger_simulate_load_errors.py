import pytest
import httpx
from unittest import mock
from scripts.simulate_load import send_webhook, verify_database

@pytest.mark.asyncio
async def test_send_webhook_timeout(monkeypatch):
    """
    Verifies that when a timeout exception occurs during send_webhook,
    it returns -1.0 and does not crash.
    """
    async def mock_post(*args, **kwargs):
        raise httpx.TimeoutException("Request timed out")

    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(client, "post", mock_post)
        
        # We also mock print to verify that the error log is printed
        printed_messages = []
        monkeypatch.setattr("builtins.print", lambda msg: printed_messages.append(msg))
        
        latency = await send_webhook(
            client,
            base_url="http://localhost:8000",
            tenant_id="test_tenant",
            phone="+5511990000001",
            content="test message"
        )
        
        assert latency == -1.0
        assert any("Connection failed" in msg or "timed out" in msg for msg in printed_messages)


@pytest.mark.asyncio
async def test_send_webhook_connection_error(monkeypatch):
    """
    Verifies that when a connection error occurs during send_webhook,
    it returns -1.0 and does not crash.
    """
    async def mock_post(*args, **kwargs):
        raise httpx.ConnectError("Connection refused")

    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(client, "post", mock_post)
        
        printed_messages = []
        monkeypatch.setattr("builtins.print", lambda msg: printed_messages.append(msg))
        
        latency = await send_webhook(
            client,
            base_url="http://localhost:8000",
            tenant_id="test_tenant",
            phone="+5511990000001",
            content="test message"
        )
        
        assert latency == -1.0
        assert any("Connection failed" in msg or "refused" in msg for msg in printed_messages)


@pytest.mark.asyncio
async def test_verify_database_not_found(monkeypatch):
    """
    Verifies that verify_database raises ValueError if the tenant
    configuration is not found in the central database.
    """
    mock_session = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = mock.AsyncMock(return_value=mock_result)
    
    class FakeSessionmaker:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
            
    monkeypatch.setattr("scripts.simulate_load.async_sessionmaker", lambda *args, **kwargs: FakeSessionmaker)
    monkeypatch.setattr("scripts.simulate_load.settings", mock.MagicMock(database_url="sqlite+aiosqlite:///:memory:"))
    
    with pytest.raises(ValueError, match="Tenant configuration not found for organization"):
        await verify_database("non_existent_tenant", ["+5511990000001"])


@pytest.mark.asyncio
async def test_verify_database_connection_failure(monkeypatch):
    """
    Verifies that when database connection/query fails in verify_database,
    it propagates the exception (which should be caught in main).
    """
    class MockSetting:
        tenant_connection_string = "encrypted_connection_str"
        
    mock_session = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.scalar_one_or_none.return_value = MockSetting()
    mock_session.execute = mock.AsyncMock(return_value=mock_result)
    
    class FakeSessionmaker:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
            
    monkeypatch.setattr("scripts.simulate_load.async_sessionmaker", lambda *args, **kwargs: FakeSessionmaker)
    monkeypatch.setattr("scripts.simulate_load.settings", mock.MagicMock(database_url="sqlite+aiosqlite:///:memory:"))
    monkeypatch.setattr("scripts.simulate_load.decrypt_data", lambda x: "sqlite+aiosqlite:///:memory:")
    
    # Mock tenant engine connection to raise exception
    mock_engine = mock.MagicMock()
    mock_engine.connect = mock.MagicMock(side_effect=Exception("Database connection failure"))
    mock_engine.dispose = mock.AsyncMock()
    
    monkeypatch.setattr("scripts.simulate_load.create_async_engine", lambda *args, **kwargs: mock_engine)
    
    with pytest.raises(Exception, match="Database connection failure"):
        await verify_database("test_tenant", ["+5511990000001"])
