import pytest
import httpx
from unittest import mock
from sqlalchemy.ext.asyncio import AsyncSession
from scripts.simulate_load import send_webhook, simulate_phone_load, run_load, verify_database

@pytest.mark.asyncio
async def test_send_webhook_success():
    def mock_handler(request: httpx.Request):
        assert request.url.path == "/api/v1/webhook/whatsapp"
        assert "organization_id=test_tenant" in request.url.query.decode()
        return httpx.Response(200, json={"status": "queued"})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        latency = await send_webhook(client, "http://localhost:8000", "test_tenant", "12345", "hello")
        assert latency >= 0

@pytest.mark.asyncio
async def test_simulate_phone_load(monkeypatch):
    called = []
    async def mock_send(client, base_url, tenant_id, phone, content):
        called.append((phone, content))
        return 0.01

    monkeypatch.setattr("scripts.simulate_load.send_webhook", mock_send)
    
    async with httpx.AsyncClient() as client:
        latencies = await simulate_phone_load(client, "http://localhost:8000", "test_tenant", "12345", 2)
        assert len(latencies) == 2
        assert latencies == [0.01, 0.01]
        assert called == [
            ("12345", "Fragment 1 from number 12345"),
            ("12345", "Fragment 2 from number 12345")
        ]

@pytest.mark.asyncio
async def test_run_load(monkeypatch):
    async def mock_simulate(client, base_url, tenant_id, phone, num_messages):
        return [0.02] * num_messages

    monkeypatch.setattr("scripts.simulate_load.simulate_phone_load", mock_simulate)
    
    latencies, phones = await run_load("http://localhost:8000", "test_tenant", 3, 2)
    assert len(latencies) == 6
    assert latencies == [0.02] * 6
    assert phones == [
        "+5511990000001",
        "+5511990000002",
        "+5511990000003"
    ]

@pytest.mark.asyncio
async def test_verify_database_success(monkeypatch):
    # Mock settings query and decryption
    class MockSetting:
        tenant_connection_string = "encrypted_connection_str"
        
    mock_session = mock.MagicMock()
    
    # Use regular MagicMock for database query result to avoid returning coroutines on sync calls
    mock_result = mock.MagicMock()
    mock_result.scalar_one_or_none.return_value = MockSetting()
    
    # execute is an AsyncMock returning the mock_result
    mock_session.execute = mock.AsyncMock(return_value=mock_result)
    
    # Mock central sessionmaker context manager
    class FakeSessionmaker:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
            
    monkeypatch.setattr("scripts.simulate_load.async_sessionmaker", lambda *args, **kwargs: FakeSessionmaker)
    monkeypatch.setattr("scripts.simulate_load.settings", mock.MagicMock(database_url="sqlite+aiosqlite:///:memory:"))
    
    # Mock decrypt_data
    monkeypatch.setattr("scripts.simulate_load.decrypt_data", lambda x: "sqlite+aiosqlite:///:memory:")
    
    # Mock tenant database engine and connection
    mock_conn = mock.AsyncMock()
    
    # Mock result for buffer count (0)
    mock_buffer_result = mock.MagicMock()
    mock_buffer_result.scalar.return_value = 0
    
    # Mock result for client records
    mock_client_result = mock.MagicMock()
    mock_client_result.fetchall.return_value = [
        ("+5511990000001", "EM_CONTATO"),
        ("+5511990000002", "EM_CONTATO")
    ]
    
    # execute side effects
    async def mock_execute(statement, *args, **kwargs):
        stmt_str = str(statement)
        if "message_buffer" in stmt_str:
            return mock_buffer_result
        elif "dados_cliente" in stmt_str:
            return mock_client_result
        return mock.MagicMock()
        
    mock_conn.execute = mock_execute
    
    # Mock context manager for connect
    class FakeConnContext:
        async def __aenter__(self):
            return mock_conn
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
            
    mock_engine = mock.MagicMock()
    mock_engine.dispose = mock.AsyncMock()
    mock_engine.connect.return_value = FakeConnContext()
    
    monkeypatch.setattr("scripts.simulate_load.create_async_engine", lambda *args, **kwargs: mock_engine)
    
    simulated_phones = ["+5511990000001", "+5511990000002"]
    res = await verify_database("wh_tenant", simulated_phones)
    assert res["success"] is True
    assert res["buffer_count"] == 0
    assert res["registered_count"] == 2
