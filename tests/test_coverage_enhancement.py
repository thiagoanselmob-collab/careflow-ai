import os
import json
import uuid
from io import BytesIO
import pytest
import pytest_asyncio
import httpx
import logging
from unittest import mock
from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.main import app
from app.services.medflow_client import (
    MedflowClient,
    MedflowClientError,
    MedflowClientHTTPError,
    MedflowClientConnectionError
)
from app.services.embedding import aget_embedding
from app.core.tenant_database import _init_tenant_db, tenant_db_manager
from app.services.whatsapp_client import whatsapp_client
from app.models.settings import Settings
from app.schemas.session import MessageSchema, SessionSchema, CollectedDataSchema
from app.services.session_manager import RedisSessionManager, session_manager
from app.api.knowledge import extract_text_from_pdf, check_embedding_column_exists
from app.api.webhook import process_message_debounce

# Initialize TestClient
client = TestClient(app)

original_book_appointment = MedflowClient.book_appointment


@pytest.fixture(autouse=True)
def mock_global_services(monkeypatch):
    """Autouse fixture to mock external APIs (LLM and Central CRM) globally for all tests in this module."""
    # Mock LangGraph invoke
    def dummy_invoke(state, config=None):
        return state
    monkeypatch.setattr("app.services.agents.graph.graph.invoke", dummy_invoke)

    # Mock MedflowClient.book_appointment
    async def mock_book(self, *args, **kwargs):
        return {"appointmentId": "dummy-wh-appt"}
    monkeypatch.setattr(MedflowClient, "book_appointment", mock_book)

# ---------------------------------------------------------------------------
# 1. MedflowClient Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_medflow_client_update_status_success(monkeypatch):
    """Verify update_appointment_status sends correct request and returns JSON response."""
    def mock_handler(request: httpx.Request):
        assert request.method == "PATCH"
        assert request.url.path == "/api/appointments/appt-123/status"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-Tenant-ID"] == "tenant-456"
        assert request.headers["Idempotency-Key"] == "idemp-key"
        
        body = json.loads(request.read().decode())
        assert body["status"] == "CONFIRMED"
        assert body["source"] == "CRM"
        
        return httpx.Response(200, json={"status": "updated", "id": "appt-123"})

    transport = httpx.MockTransport(mock_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token", tenant_id="tenant-456")
    res = await medflow.update_appointment_status(
        appointment_id="appt-123",
        status="confirmed",
        source="CRM",
        idempotency_key="idemp-key"
    )
    assert res["status"] == "updated"


@pytest.mark.asyncio
async def test_medflow_client_patch_status_wrapper(monkeypatch):
    """Verify patch_appointment_status wrapper calls update_appointment_status internally."""
    called = []
    async def mock_update(self, appointment_id, status, tenant_id=None, idempotency_key=None):
        called.append((appointment_id, status, tenant_id, idempotency_key))
        return {"status": "patched"}
        
    monkeypatch.setattr(MedflowClient, "update_appointment_status", mock_update)
    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token")
    res = await medflow.patch_appointment_status("appt-123", "cancelled", tenant_id="tenant-abc", idempotency_key="key-123")
    assert res["status"] == "patched"
    assert called == [("appt-123", "cancelled", "tenant-abc", "key-123")]


@pytest.mark.asyncio
async def test_medflow_client_confirm_appointment_success(monkeypatch):
    """Verify confirm_appointment posts to correct endpoint and handles success."""
    def mock_handler(request: httpx.Request):
        assert request.method == "POST"
        assert request.url.path == "/api/webhooks/n8n/confirm-appointment/appt-abc"
        return httpx.Response(200, json={"confirmed": True})

    transport = httpx.MockTransport(mock_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token")
    res = await medflow.confirm_appointment("appt-abc")
    assert res["confirmed"] is True


@pytest.mark.asyncio
async def test_medflow_client_cancel_appointment_success(monkeypatch):
    """Verify cancel_appointment posts to correct endpoint and handles success."""
    def mock_handler(request: httpx.Request):
        assert request.method == "POST"
        assert request.url.path == "/api/webhooks/n8n/cancel-appointment/appt-xyz"
        return httpx.Response(200, json={"cancelled": True})

    transport = httpx.MockTransport(mock_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token")
    res = await medflow.cancel_appointment("appt-xyz")
    assert res["cancelled"] is True


@pytest.mark.asyncio
async def test_medflow_client_http_error_handling(monkeypatch):
    """Verify HTTP errors raise MedflowClientHTTPError for confirm/cancel/update."""
    def mock_handler(request: httpx.Request):
        return httpx.Response(400, text="Bad Request Details")

    transport = httpx.MockTransport(mock_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token")
    
    with pytest.raises(MedflowClientHTTPError) as exc_info:
        await medflow.confirm_appointment("appt-1")
    assert exc_info.value.status_code == 400
    assert "Bad Request Details" in str(exc_info.value)
    
    with pytest.raises(MedflowClientHTTPError):
        await medflow.cancel_appointment("appt-1")

    with pytest.raises(MedflowClientHTTPError):
        await medflow.update_appointment_status("appt-1", "cancelled")


@pytest.mark.asyncio
async def test_medflow_client_connection_error_handling(monkeypatch):
    """Verify network timeouts/connection issues raise MedflowClientConnectionError."""
    def mock_handler(request: httpx.Request):
        raise httpx.TimeoutException("Read timed out")

    transport = httpx.MockTransport(mock_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token")
    
    with pytest.raises(MedflowClientConnectionError):
        await medflow.confirm_appointment("appt-1")
    
    with pytest.raises(MedflowClientConnectionError):
        await medflow.cancel_appointment("appt-1")

    with pytest.raises(MedflowClientConnectionError):
        await medflow.update_appointment_status("appt-1", "cancelled")


# ---------------------------------------------------------------------------
# 2. app/services/embedding.py aget_embedding Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aget_embedding_empty():
    """Verify aget_embedding with empty input returns empty list immediately."""
    res = await aget_embedding("")
    assert res == []
    res_none = await aget_embedding(None)
    assert res_none == []


@pytest.mark.asyncio
async def test_aget_embedding_success(monkeypatch):
    """Verify aget_embedding gets model and calls async embed query successfully."""
    class MockEmbeddings:
        async def aembed_query(self, text):
            assert text == "test text"
            return [0.5] * 768

    monkeypatch.setattr("app.services.embedding.get_embedding_model", lambda: MockEmbeddings())
    
    res = await aget_embedding("test text")
    assert res == [0.5] * 768


@pytest.mark.asyncio
async def test_aget_embedding_exception(monkeypatch):
    """Verify aget_embedding re-raises and logs exceptions."""
    class MockEmbeddings:
        async def aembed_query(self, text):
            raise ValueError("Embedding computation error")

    monkeypatch.setattr("app.services.embedding.get_embedding_model", lambda: MockEmbeddings())
    
    with pytest.raises(ValueError) as exc_info:
        await aget_embedding("test text")
    assert "Embedding computation error" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. app/core/tenant_database.py Dialect & Connection Manager Tests
# ---------------------------------------------------------------------------

class FakePostgresEngine:
    """Fake SQLAlchemy AsyncEngine that implements required interfaces without Mock name."""
    def __init__(self, dialect_name="postgresql", raise_on_first=False, raise_on_second=False):
        self.dialect = FakeDialect(dialect_name)
        self.raise_on_first = raise_on_first
        self.raise_on_second = raise_on_second
        self.executed = []

    def begin(self):
        return FakeAsyncConnection(self)


class FakeDialect:
    def __init__(self, name):
        self.name = name


class FakeAsyncConnection:
    def __init__(self, engine):
        self.engine = engine

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, statement, *args, **kwargs):
        stmt_str = str(statement)
        self.engine.executed.append(stmt_str)
        if "vector" in stmt_str and self.engine.raise_on_first:
            raise Exception("pgvector installation error")
        if "clinic_knowledge" in stmt_str and "SERIAL" in stmt_str and self.engine.raise_on_second:
            raise Exception("Fallback table creation error")


@pytest.mark.asyncio
async def test_init_tenant_db_postgresql_pgvector():
    """Verify _init_tenant_db creates tables with pgvector on PostgreSQL."""
    engine = FakePostgresEngine(dialect_name="postgresql", raise_on_first=False)
    await _init_tenant_db(engine)
    
    # Check executions
    assert any("CREATE EXTENSION IF NOT EXISTS vector;" in s for s in engine.executed)
    assert any("embedding VECTOR(768)" in s for s in engine.executed)
    assert any("dados_cliente" in s for s in engine.executed)


@pytest.mark.asyncio
async def test_init_tenant_db_postgresql_fallback():
    """Verify _init_tenant_db falls back to tables without vector on PostgreSQL when pgvector fails."""
    engine = FakePostgresEngine(dialect_name="postgresql", raise_on_first=True)
    await _init_tenant_db(engine)
    
    # Check that pgvector was attempted but failed, triggering the fallback branch
    assert any("CREATE EXTENSION IF NOT EXISTS vector;" in s for s in engine.executed)
    assert any("clinic_knowledge" in s and "embedding" not in s for s in engine.executed)
    assert any("dados_cliente" in s for s in engine.executed)


@pytest.mark.asyncio
async def test_init_tenant_db_postgresql_all_fail():
    """Verify _init_tenant_db propagates exceptions when all Postgres creation attempts fail."""
    engine = FakePostgresEngine(dialect_name="postgresql", raise_on_first=True, raise_on_second=True)
    with pytest.raises(Exception, match="Fallback table creation error"):
        await _init_tenant_db(engine)
    assert len(engine.executed) > 0



@pytest.mark.asyncio
async def test_init_tenant_db_other_dialect():
    """Verify _init_tenant_db creates SQLite/other tables when dialect is not postgresql."""
    engine = FakePostgresEngine(dialect_name="sqlite")
    await _init_tenant_db(engine)
    
    assert any("clinic_knowledge" in s and "AUTOINCREMENT" in s for s in engine.executed)
    assert any("message_buffer" in s for s in engine.executed)
    assert any("dados_cliente" in s for s in engine.executed)


# ---------------------------------------------------------------------------
# 4. app/services/whatsapp_client.py Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whatsapp_client_send_message_success():
    """Verify send_message sets key in Redis and returns True."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    
    # Mock session_manager.get_client to return our fake redis client
    async def mock_get_client():
        return fake_redis
        
    with mock.patch.object(session_manager, "get_client", mock_get_client):
        res = await whatsapp_client.send_message(
            phone_number="5511999999999",
            text="Test message contents",
            organization_id="org-123"
        )
        assert res is True
        
        # Verify the key was set in Redis
        ttl_key = "bot_sending:org-123:5511999999999"
        val = await fake_redis.get(ttl_key)
        assert val == "1"


@pytest.mark.asyncio
async def test_whatsapp_client_send_message_redis_failure():
    """Verify send_message handles Redis connection failure gracefully and returns True."""
    async def mock_get_client_fail():
        raise Exception("Redis Connection Refused")
        
    with mock.patch.object(session_manager, "get_client", mock_get_client_fail):
        res = await whatsapp_client.send_message(
            phone_number="5511999999999",
            text="Test message contents",
            organization_id="org-123"
        )
        assert res is True  # Should return True despite Redis failure


# ---------------------------------------------------------------------------
# 5. app/models/settings.py repr Coverage
# ---------------------------------------------------------------------------

def test_settings_repr():
    """Verify __repr__ of Settings model is styled correctly."""
    setting = Settings(organization_id="org-test-uuid", tenant_connection_string="encrypted")
    assert repr(setting) == "<Settings(organization_id='org-test-uuid')>"


# ---------------------------------------------------------------------------
# 6. app/schemas/session.py MessageSchema Validation Gaps
# ---------------------------------------------------------------------------

def test_message_schema_invalid_role():
    """Verify MessageSchema role validator triggers validation error on invalid roles."""
    with pytest.raises(ValueError) as excinfo:
        MessageSchema.validate_role("system")
    assert "role must be one of" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 7. app/services/session_manager.py RedisSessionManager Coverage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_manager_real_init_and_close(monkeypatch):
    """Verify RedisSessionManager initializes and closes connections properly."""
    mock_redis = mock.AsyncMock()
    mock_from_url = mock.MagicMock(return_value=mock_redis)
    monkeypatch.setattr("redis.asyncio.from_url", mock_from_url)
    
    manager = RedisSessionManager(redis_url="redis://localhost:6379")
    assert manager._redis is None
    
    # Retrieve client to trigger initialization
    client = await manager.get_client()
    assert client is mock_redis
    mock_from_url.assert_called_once_with("redis://localhost:6379", encoding="utf-8", decode_responses=True)
    
    # Retrieve again, should hit local cache
    client2 = await manager.get_client()
    assert client2 is mock_redis
    assert mock_from_url.call_count == 1
    
    # Close connection
    await manager.close()
    mock_redis.aclose.assert_awaited_once()
    assert manager._redis is None


# ---------------------------------------------------------------------------
# 8. API route validation and error paths (app/api/knowledge.py)
# ---------------------------------------------------------------------------

def test_get_tenant_id_missing_raises_error():
    """Verify get_tenant_id validation endpoint raises 400 Bad Request if tenant ID is omitted."""
    # GET /api/v1/admin/knowledge without organization_id and X-Tenant-ID header
    response = client.get("/api/v1/admin/knowledge")
    assert response.status_code == 400
    assert "Tenant ID" in response.json()["detail"]


@pytest.mark.asyncio
async def test_check_embedding_column_exists_exception():
    """Verify check_embedding_column_exists returns False and rolls back on DB query exception."""
    mock_session = mock.AsyncMock(spec=AsyncSession)
    mock_session.execute.side_effect = Exception("Table column does not exist or table missing")
    
    res = await check_embedding_column_exists(mock_session)
    assert res is False
    mock_session.rollback.assert_awaited_once()


def test_extract_text_from_pdf():
    """Verify extract_text_from_pdf extracts parentheses strings or falls back to decode."""
    # Test valid parsing stream structure
    pdf_with_stream = b"stream\n(Clinic rules text contents)\nendstream"
    text = extract_text_from_pdf(pdf_with_stream)
    assert text == "Clinic rules text contents"
    
    # Test fallback branch scanning whole PDF
    pdf_raw_parens = b"Random content (Direct parenthesized string text) more content"
    text_fallback = extract_text_from_pdf(pdf_raw_parens)
    assert text_fallback == "Direct parenthesized string text"
    
    # Test fallback to utf-8 decode
    pdf_plain = b"Just plain text stream"
    text_plain = extract_text_from_pdf(pdf_plain)
    assert text_plain == "Just plain text stream"


@pytest_asyncio.fixture
async def mock_kb_db():
    """Mock tenant database helper to test database exceptions and list/delete."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Settings.metadata.create_all)
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS clinic_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                metadata TEXT
            );
        """))
    
    tenant_db_manager._engines["kb_tenant"] = engine
    tenant_db_manager._sessionmakers["kb_tenant"] = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    yield engine
    await engine.dispose()
    tenant_db_manager._engines.pop("kb_tenant", None)
    tenant_db_manager._sessionmakers.pop("kb_tenant", None)


def test_list_knowledge_blocks_db_error(mock_kb_db, monkeypatch):
    """Verify GET list_knowledge_blocks handles exceptions and returns 500."""
    async def mock_get_session_fail(tenant_id):
        raise Exception("Database reading error")
    monkeypatch.setattr(tenant_db_manager, "get_tenant_session", mock_get_session_fail)
    
    response = client.get("/api/v1/admin/knowledge?organization_id=kb_tenant")
    assert response.status_code == 500
    assert "Database reading error" in response.json()["detail"]


def test_upload_knowledge_empty_file(mock_kb_db):
    """Verify uploading an empty text file triggers a 400 error."""
    file_io = BytesIO(b"   ")
    response = client.post(
        "/api/v1/admin/knowledge/upload?organization_id=kb_tenant",
        files={"file": ("empty.txt", file_io, "text/plain")}
    )
    assert response.status_code == 400
    assert "no readable text content" in response.json()["detail"]


def test_upload_knowledge_pdf_success(mock_kb_db, monkeypatch):
    """Verify uploading a PDF file extracts text and saves it successfully."""
    # Mock embedding generator to avoid external API call
    monkeypatch.setattr("app.api.knowledge.get_embedding", lambda x: [0.1] * 768)
    
    pdf_bytes = b"stream\n(Clinica CareFlow PDF Content Info)\nendstream"
    file_io = BytesIO(pdf_bytes)
    
    response = client.post(
        "/api/v1/admin/knowledge/upload?organization_id=kb_tenant",
        files={"file": ("rules.pdf", file_io, "application/pdf")}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # Get knowledge blocks list to verify insertion
    get_res = client.get("/api/v1/admin/knowledge?organization_id=kb_tenant")
    assert len(get_res.json()) == 1
    assert "Clinica CareFlow PDF Content Info" in get_res.json()[0]["content"]


def test_upload_knowledge_embedding_fail_fallback_save(mock_kb_db, monkeypatch):
    """Verify upload falls back to saving without vector if embedding fails."""
    # Mock check_embedding_column_exists to return True (database has vector column)
    async def mock_exists(session):
        return True
    monkeypatch.setattr("app.api.knowledge.check_embedding_column_exists", mock_exists)
    
    # Mock embedding generation to raise error
    def mock_get_embedding_fail(text):
        raise ValueError("Gemini quota exceeded error")
    monkeypatch.setattr("app.api.knowledge.get_embedding", mock_get_embedding_fail)
    
    file_io = BytesIO(b"Content that fails embedding generation but should save successfully.")
    response = client.post(
        "/api/v1/admin/knowledge/upload?organization_id=kb_tenant",
        files={"file": ("fail_embed.txt", file_io, "text/plain")}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_upload_knowledge_db_general_error(mock_kb_db, monkeypatch):
    """Verify upload endpoint returns 500 when database transaction fails."""
    # Force sessionmaker to fail
    def mock_get_session_fail(tenant):
        raise Exception("Database transaction connection failed")
    monkeypatch.setattr(tenant_db_manager, "get_tenant_session", mock_get_session_fail)
    
    file_io = BytesIO(b"General doc content.")
    response = client.post(
        "/api/v1/admin/knowledge/upload?organization_id=kb_tenant",
        files={"file": ("fail_db.txt", file_io, "text/plain")}
    )
    assert response.status_code == 500
    assert "connection failed" in response.json()["detail"]


def test_delete_knowledge_block_not_found(mock_kb_db):
    """Verify deleting a non-existent knowledge block returns 404."""
    response = client.delete("/api/v1/admin/knowledge/9999?organization_id=kb_tenant")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_delete_knowledge_block_db_error(mock_kb_db, monkeypatch):
    """Verify delete endpoint returns 500 on database failure."""
    def mock_get_session_fail(tenant):
        raise Exception("Delete transaction failed")
    monkeypatch.setattr(tenant_db_manager, "get_tenant_session", mock_get_session_fail)
    
    response = client.delete("/api/v1/admin/knowledge/1?organization_id=kb_tenant")
    assert response.status_code == 500
    assert "Delete transaction failed" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 9. Webhook API and Debouncing Gaps (app/api/webhook.py)
# ---------------------------------------------------------------------------

def test_webhook_get_tenant_id_missing():
    """Verify webhook endpoint returns 400 if tenant organization ID is missing."""
    response = client.post("/api/v1/webhook/whatsapp", json={})
    assert response.status_code == 400


def test_webhook_statuses_update_ignored():
    """Verify status update notifications in webhook payload are ignored immediately."""
    payload = {"statuses": [{"id": "msg-123", "status": "delivered"}]}
    response = client.post("/api/v1/webhook/whatsapp?organization_id=org-123", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "status update"


def test_webhook_statuses_nested_ignored():
    """Verify nested status updates in standard WhatsApp JSON are ignored."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [{"id": "msg-123"}]
                }
            }]
        }]
    }
    response = client.post("/api/v1/webhook/whatsapp?organization_id=org-123", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "status update"


def test_webhook_unsupported_payload_ignored():
    """Verify payloads missing phone number or content are ignored."""
    response = client.post("/api/v1/webhook/whatsapp?organization_id=org-123", json={"random": "key"})
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "unsupported payload format"


@pytest_asyncio.fixture
async def mock_webhook_db():
    """Setup clean tenant SQLite tables for webhook database testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS message_buffer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS dados_cliente (
                phone_number TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
    
    tenant_db_manager._engines["wh_tenant"] = engine
    tenant_db_manager._sessionmakers["wh_tenant"] = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    yield engine
    await engine.dispose()
    tenant_db_manager._engines.pop("wh_tenant", None)
    tenant_db_manager._sessionmakers.pop("wh_tenant", None)


@pytest.mark.asyncio
async def test_webhook_bot_self_reply_ignored(mock_webhook_db, monkeypatch):
    """Verify outgoing messages from clinic that are bot self-replies are ignored."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)
    
    # Store bot_sending marker in redis
    await fake_redis.set("bot_sending:wh_tenant:5511999999999", "1")
    
    payload = {
        "phone_number": "5511999999999",
        "content": "This is an automated outgoing reply",
        "fromMe": True
    }
    response = client.post("/api/v1/webhook/whatsapp?organization_id=wh_tenant", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "bot self-reply"


@pytest.mark.asyncio
async def test_webhook_human_takeover_success(mock_webhook_db, monkeypatch):
    """Verify human takeover updates client status and deactivates the bot."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)
    
    # Store active session in Redis
    user_sess = SessionSchema(bot_active=True)
    await session_manager.update_session("wh_tenant", "5511999999999", user_sess)
    
    # Human inserts record in DB
    async with await tenant_db_manager.get_tenant_session("wh_tenant") as db_sess:
        await db_sess.execute(text("INSERT INTO dados_cliente (phone_number, status) VALUES ('5511999999999', 'EM_CONTATO')"))
        await db_sess.commit()
        
    payload = {
        "phone_number": "5511999999999",
        "content": "Human typing here",
        "fromMe": True
    }
    
    response = client.post("/api/v1/webhook/whatsapp?organization_id=wh_tenant", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "human takeover detected"
    
    # Verify status changed to ATENDIMENTO_HUMANO
    async with await tenant_db_manager.get_tenant_session("wh_tenant") as db_sess:
        res = await db_sess.execute(text("SELECT status FROM dados_cliente WHERE phone_number = '5511999999999'"))
        assert res.scalar() == "ATENDIMENTO_HUMANO"
        
    # Verify bot_active is False
    updated_sess = await session_manager.get_session("wh_tenant", "5511999999999")
    assert updated_sess.bot_active is False


@pytest.mark.asyncio
async def test_webhook_human_takeover_db_errors_handled_gracefully(mock_webhook_db, monkeypatch):
    """Verify database exceptions during human takeover are handled gracefully."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)
    
    # Mock tenant db manager to raise error when getting session
    def mock_get_session_fail(tenant):
        raise Exception("Database transaction error during human takeover")
    monkeypatch.setattr(tenant_db_manager, "get_tenant_session", mock_get_session_fail)
    
    payload = {
        "phone_number": "5511888888888",
        "content": "Human typing here",
        "fromMe": True
    }
    # Should still succeed and return takeover detected status
    response = client.post("/api/v1/webhook/whatsapp?organization_id=wh_tenant", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "human takeover detected"


@pytest.mark.asyncio
async def test_webhook_message_buffering_db_failure(mock_webhook_db, monkeypatch):
    """Verify webhook returns 500 if buffering message to database fails."""
    # Force database save to fail
    def mock_get_session_fail(tenant):
        raise Exception("Buffering DB Connection Failed")
    monkeypatch.setattr(tenant_db_manager, "get_tenant_session", mock_get_session_fail)
    
    payload = {
        "phone_number": "5511777777777",
        "content": "Patient incoming text"
    }
    response = client.post("/api/v1/webhook/whatsapp?organization_id=wh_tenant", json=payload)
    assert response.status_code == 500
    assert "Database buffering error" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_whatsapp_business_payload_parsing(mock_webhook_db, monkeypatch):
    """Verify parsing standard nested WhatsApp Business API message payload format."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)

    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "5511999999999",
                        "to": "5511888888888",
                        "text": {"body": "Hi CareFlow, I want to book an appointment"}
                    }]
                }
            }]
        }]
    }
    
    # We expect this to queue successfully
    response = client.post("/api/v1/webhook/whatsapp?organization_id=wh_tenant", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    
    # Verify the background task processed it and updated the session in Redis
    session_data = await session_manager.get_session("wh_tenant", "5511999999999")
    assert session_data is not None
    assert len(session_data.messages_history) == 1
    assert session_data.messages_history[0].content == "Hi CareFlow, I want to book an appointment"


@pytest.mark.asyncio
async def test_process_message_debounce_crm_registration_branches(mock_webhook_db, monkeypatch):
    """Verify various response layouts from MedflowClient.book_appointment during CRM registration."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)
    
    # 1. CRM returns appointmentId directly
    async def mock_book_1(self, **kwargs):
        return {"appointmentId": "appt-crm-1"}
    
    monkeypatch.setattr(MedflowClient, "book_appointment", mock_book_1)
    
    # Buffer a message
    async with await tenant_db_manager.get_tenant_session("wh_tenant") as db_sess:
        await db_sess.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('5511000000001', 'Test msg')"))
        await db_sess.commit()
        
    await process_message_debounce("wh_tenant", "5511000000001")
    # Verify session has original appointment id saved
    sess = await session_manager.get_session("wh_tenant", "5511000000001")
    assert sess.original_appointment_id == "appt-crm-1"
    
    # 2. CRM returns nested appointment dictionary
    async def mock_book_2(self, **kwargs):
        return {"appointment": {"id": "appt-crm-2"}}
        
    monkeypatch.setattr(MedflowClient, "book_appointment", mock_book_2)
    
    # Clear dados_cliente to trigger new registration again
    async with await tenant_db_manager.get_tenant_session("wh_tenant") as db_sess:
        await db_sess.execute(text("DELETE FROM dados_cliente"))
        await db_sess.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('5511000000001', 'Test msg 2')"))
        await db_sess.commit()
        
    await process_message_debounce("wh_tenant", "5511000000001")
    sess = await session_manager.get_session("wh_tenant", "5511000000001")
    assert sess.original_appointment_id == "appt-crm-2"

    # 3. CRM fails and throws exception
    async def mock_book_fail(self, **kwargs):
        raise ValueError("Connection timed out to central CRM")
        
    monkeypatch.setattr(MedflowClient, "book_appointment", mock_book_fail)
    
    # Clear and retry
    async with await tenant_db_manager.get_tenant_session("wh_tenant") as db_sess:
        await db_sess.execute(text("DELETE FROM dados_cliente"))
        await db_sess.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('5511000000001', 'Test msg 3')"))
        await db_sess.commit()
        
    # Should not crash, just log and proceed
    await process_message_debounce("wh_tenant", "5511000000001")


@pytest.mark.asyncio
async def test_process_message_debounce_custom_graph_config(mock_webhook_db, monkeypatch):
    """Verify passing a custom graph config is respected during debounce execution."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)
    
    # Spy on graph invoke config
    invoked_configs = []
    def mock_invoke(state, config=None):
        invoked_configs.append(config)
        return state
    monkeypatch.setattr("app.services.agents.graph.graph.invoke", mock_invoke)
    
    # Buffer message
    async with await tenant_db_manager.get_tenant_session("wh_tenant") as db_sess:
        await db_sess.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('5511000000005', 'Hi')"))
        await db_sess.commit()
        
    custom_cfg = {"configurable": {"tenant_id": "wh_tenant", "patient_phone": "5511000000005", "custom_flag": True}}
    await process_message_debounce("wh_tenant", "5511000000005", custom_graph_config=custom_cfg)
    
    assert len(invoked_configs) == 1
    assert invoked_configs[0]["configurable"]["custom_flag"] is True


# ---------------------------------------------------------------------------
# 10. Additional coverage for app/services/agents/graph.py
# ---------------------------------------------------------------------------

def test_reduce_messages_edge_cases():
    """Verify reduce_messages handle empty or missing left/right parameters."""
    from app.services.agents.graph import reduce_messages
    msg = MessageSchema(role="user", content="Hello")
    
    # right is empty
    assert reduce_messages([msg], []) == [msg]
    assert reduce_messages([msg], None) == [msg]
    
    # left is empty
    assert reduce_messages(None, [msg]) == [msg]
    assert reduce_messages([], [msg]) == [msg]


@pytest.mark.asyncio
async def test_async_escalate_human_missing_params():
    """Verify _async_escalate_human exits early when parameters are missing."""
    from app.services.agents.graph import _async_escalate_human
    await _async_escalate_human(None, "5511999999999", "appt-1")
    await _async_escalate_human("tenant-1", None, "appt-1")


@pytest.mark.asyncio
async def test_async_escalate_human_db_and_crm_errors(monkeypatch, mock_webhook_db):
    """Verify _async_escalate_human handles DB and central CRM API errors without throwing exceptions."""
    from app.services.agents.graph import _async_escalate_human
    
    # 1. DB success, CRM patch success
    called_patch = []
    async def mock_patch(self, appointment_id, status, tenant_id=None, idempotency_key=None):
        called_patch.append((appointment_id, status))
        return {}
    monkeypatch.setattr(MedflowClient, "patch_appointment_status", mock_patch)
    
    await _async_escalate_human("wh_tenant", "5511999999999", "appt-123")
    assert called_patch == [("appt-123", "ATENDIMENTO_HUMANO")]
    
    # 2. DB error, CRM error
    def mock_get_session_fail(tenant):
        raise Exception("Database escalation update error")
    monkeypatch.setattr(tenant_db_manager, "get_tenant_session", mock_get_session_fail)
    
    async def mock_patch_fail(self, appointment_id, status, tenant_id=None, idempotency_key=None):
        raise Exception("Central CRM patch status API error")
    monkeypatch.setattr(MedflowClient, "patch_appointment_status", mock_patch_fail)
    
    # Should complete without raising exception
    await _async_escalate_human("wh_tenant", "5511999999999", "appt-123")


def test_supervisor_node_escalate_pydantic_decision(monkeypatch):
    """Verify supervisor_node escalates conversation when Pydantic RoutingDecision contains human phase."""
    from app.services.agents.graph import supervisor_node, RoutingDecision
    
    # Test when last message is assistant
    state_assistant = {
        "messages": [MessageSchema(role="assistant", content="How can I help you?")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(),
        "wants_to_schedule": False,
        "original_appointment_id": None
    }
    res = supervisor_node(state_assistant)
    assert res["next_node"] == "END"
    
    # Test escalation routing with Pydantic decision
    class MockStructuredLLM:
        def invoke(self, prompt):
            return RoutingDecision(
                next_node="END",
                reasoning="Need human help",
                next_phase="human",
                suggested_action="escalar_humano"
            )
            
    class MockLLM:
        def with_structured_output(self, schema):
            return MockStructuredLLM()
            
    state_user = {
        "messages": [MessageSchema(role="user", content="I want to speak with a human")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(),
        "wants_to_schedule": False,
        "original_appointment_id": "appt-123"
    }
    
    called_escalate = []
    async def mock_escalate(tenant_id, patient_phone, appointment_id):
        called_escalate.append((tenant_id, patient_phone, appointment_id))
        
    monkeypatch.setattr("app.services.agents.graph._async_escalate_human", mock_escalate)
    
    config = {
        "configurable": {
            "llm": MockLLM(),
            "tenant_id": "tenant-abc",
            "patient_phone": "5511999999999"
        }
    }
    
    res = supervisor_node(state_user, config=config)
    assert res["bot_active"] is False
    assert res["next_node"] == "END"
    assert called_escalate == [("tenant-abc", "5511999999999", "appt-123")]


def test_supervisor_node_escalate_dict_decision(monkeypatch):
    """Verify supervisor_node escalates conversation when LLM returns dict decision with human phase."""
    from app.services.agents.graph import supervisor_node
    
    class MockStructuredLLM:
        def invoke(self, prompt):
            return {
                "next_node": "END",
                "reasoning": "Need human help",
                "next_phase": "human",
                "suggested_action": "escalar_humano"
            }
            
    class MockLLM:
        def with_structured_output(self, schema):
            return MockStructuredLLM()
            
    state_user = {
        "messages": [MessageSchema(role="user", content="I want to speak with a human")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(),
        "wants_to_schedule": False,
        "original_appointment_id": "appt-123"
    }
    
    called_escalate = []
    async def mock_escalate(tenant_id, patient_phone, appointment_id):
        called_escalate.append((tenant_id, patient_phone, appointment_id))
        
    monkeypatch.setattr("app.services.agents.graph._async_escalate_human", mock_escalate)
    
    config = {
        "configurable": {
            "llm": MockLLM(),
            "tenant_id": "tenant-abc",
            "patient_phone": "5511999999999"
        }
    }
    
    res = supervisor_node(state_user, config=config)
    assert res["bot_active"] is False
    assert res["next_node"] == "END"
    assert called_escalate == [("tenant-abc", "5511999999999", "appt-123")]


# ---------------------------------------------------------------------------
# 11. Additional coverage for app/services/chunking.py
# ---------------------------------------------------------------------------

def test_recursive_splitter_empty_text():
    """Verify RecursiveCharacterTextSplitter returns empty list for empty/whitespace text."""
    from app.services.chunking import RecursiveCharacterTextSplitter
    splitter = RecursiveCharacterTextSplitter()
    assert splitter.split_text("") == []
    assert splitter.split_text("   ") == []


def test_chunk_text_exception(monkeypatch):
    """Verify chunk_text falls back to returning input text as single chunk on splitter failure."""
    from app.services.chunking import chunk_text, RecursiveCharacterTextSplitter
    
    def mock_split(self, text):
        raise ValueError("Simulated splitter failure")
    monkeypatch.setattr(RecursiveCharacterTextSplitter, "split_text", mock_split)
    
    res = chunk_text("Some text that would trigger an exception")
    assert res == ["Some text that would trigger an exception"]


# ---------------------------------------------------------------------------
# 12. Additional coverage for app/api/knowledge.py
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_embedding_column_exists_success():
    """Verify check_embedding_column_exists returns True when central database check succeeds."""
    mock_session = mock.AsyncMock(spec=AsyncSession)
    mock_session.execute.return_value = None
    res = await check_embedding_column_exists(mock_session)
    assert res is True


# ---------------------------------------------------------------------------
# 13. Additional coverage for app/services/medflow_client.py
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_medflow_client_book_appointment_optional_fields(monkeypatch):
    """Verify book_appointment includes all optional patient details, notes and procedure parameters."""
    # Restore the original method using the module-level reference
    monkeypatch.setattr(MedflowClient, "book_appointment", original_book_appointment)
    
    captured_data = []
    def mock_handler(request: httpx.Request):
        body = json.loads(request.read().decode())
        captured_data.append(body)
        return httpx.Response(200, json={"id": "appt-captured"})

    transport = httpx.MockTransport(mock_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token")
    await medflow.book_appointment(
        doctor_id="doc-123",
        date="2026-06-30",
        time="15:30",
        patient_name="John Doe",
        patient_phone="12345",
        patient_cpf="111.111.111-11",
        patient_email="john@doe.com",
        procedure="Consulation",
        notes="First time patient"
    )
    assert len(captured_data) == 1
    assert captured_data[0]["patientPhone"] == "12345"
    assert captured_data[0]["patientCpf"] == "111.111.111-11"
    assert captured_data[0]["patientEmail"] == "john@doe.com"
    assert captured_data[0]["procedure"] == "Consulation"
    assert captured_data[0]["notes"] == "First time patient"


@pytest.mark.asyncio
async def test_medflow_client_get_crm_appointments_http_error(monkeypatch):
    """Verify get_crm_appointments raises MedflowClientHTTPError on API failure."""
    def mock_handler(request: httpx.Request):
        return httpx.Response(400, text="CRM query parameters invalid")

    transport = httpx.MockTransport(mock_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token")
    with pytest.raises(MedflowClientHTTPError) as exc_info:
        await medflow.get_crm_appointments(date="2026-06-30", doctor_id="doc-1")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_medflow_client_book_appointment_connection_error(monkeypatch):
    """Verify book_appointment raises MedflowClientConnectionError on request failure."""
    monkeypatch.setattr(MedflowClient, "book_appointment", original_book_appointment)
    
    def mock_handler(request: httpx.Request):
        raise httpx.ConnectError("Network is down")

    transport = httpx.MockTransport(mock_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    medflow = MedflowClient(base_url="http://medflow-api", jwt_token="test-token")
    with pytest.raises(MedflowClientConnectionError):
        await medflow.book_appointment(doctor_id="doc-1", date="2026-06-30", time="12:00", patient_name="Alice")


@pytest.mark.asyncio
async def test_list_knowledge_blocks_direct_rows(monkeypatch):
    """Verify list_knowledge_blocks parses database metadata strings and dictionaries correctly."""
    from app.api.knowledge import list_knowledge_blocks
    
    class FakeRow:
        def __init__(self, id, content, metadata):
            self._data = [id, content, metadata]
        def __getitem__(self, idx):
            return self._data[idx]
            
    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def execute(self, statement, *args, **kwargs):
            class Result:
                def all(self):
                    return [
                        FakeRow(1, "chunk 1", '{"source": "test.txt"}'),
                        FakeRow(2, "chunk 2", "invalid json string")
                    ]
            return Result()
            
    async def mock_get_session(tenant):
        return FakeSession()
        
    monkeypatch.setattr(tenant_db_manager, "get_tenant_session", mock_get_session)
    
    res = await list_knowledge_blocks(tenant_id="test-tenant")
    assert len(res) == 2
    assert res[0]["id"] == 1
    assert res[0]["metadata"] == {"source": "test.txt"}
    assert res[1]["metadata"] == "invalid json string"


def test_webhook_whatsapp_business_from_me_true(monkeypatch):
    """Verify WhatsApp webhook ignores standard nested Business API format payloads when fromMe is True."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)

    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "fromMe": True,
                        "to": "5511999999999",
                        "from": "5511888888888",
                        "text": {"body": "Hi there"}
                    }]
                }
            }]
        }]
    }
    response = client.post("/api/v1/webhook/whatsapp?organization_id=wh_tenant", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_webhook_whatsapp_business_to_none(monkeypatch):
    """Verify WhatsApp webhook uses msg.from when msg.to is empty/None for fromMe message parsing."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)

    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "fromMe": True,
                        "to": None,
                        "from": "5511888888888",
                        "text": {"body": "Hi there"}
                    }]
                }
            }]
        }]
    }
    response = client.post("/api/v1/webhook/whatsapp?organization_id=wh_tenant", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_webhook_human_takeover_deactivate_session_fail(mock_webhook_db, monkeypatch):
    """Verify webhook handles session manager exception gracefully during human takeover."""
    from fakeredis.aioredis import FakeRedis
    fake_redis = FakeRedis(decode_responses=True)
    async def mock_get_client():
        return fake_redis
    monkeypatch.setattr(session_manager, "get_client", mock_get_client)

    async def mock_update_fail(org_id, phone, session_data):
        raise Exception("Redis connection lost")
    monkeypatch.setattr(session_manager, "update_session", mock_update_fail)
    
    payload = {
        "phone_number": "5511999999999",
        "content": "Human typing here",
        "fromMe": True
    }
    response = client.post("/api/v1/webhook/whatsapp?organization_id=wh_tenant", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"



