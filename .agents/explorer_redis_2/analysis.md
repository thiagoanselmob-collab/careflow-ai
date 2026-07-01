# Design and Implementation Strategy for Asynchronous Redis Session Manager

## Executive Summary
This document defines the architectural design, serialization schemas, resilient connection lifecycle, and verification plan for the asynchronous Redis Session Manager in the CareFlow AI Backend.

---

## 1. Pydantic Session Schemas (`app/schemas/session.py`)

To ensure typed schema validation and seamless JSON serialization/deserialization when reading from/writing to Redis, we propose three Pydantic models.

### Proposed Code for `app/schemas/session.py`
```python
from datetime import datetime
from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class MessageSchema(BaseModel):
    """
    Schema representing a single message in the chat history.
    """
    role: Literal["user", "assistant"] = Field(
        ...,
        description="The sender role, restricted to 'user' or 'assistant'"
    )
    content: str = Field(..., description="The content text of the message")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="The UTC timestamp when the message was sent"
    )

class CollectedDataSchema(BaseModel):
    """
    Schema representing user-provided screening information gathered by the bot.
    """
    full_name: Optional[str] = Field(default=None, description="Patient's full name")
    cpf: Optional[str] = Field(default=None, description="Patient's CPF document number")
    grievance: Optional[str] = Field(default=None, description="Patient's reported health grievance")
    preferred_doctor: Optional[str] = Field(default=None, description="Patient's preferred doctor name")
    selected_datetime: Optional[datetime] = Field(
        default=None,
        description="Selected datetime for appointment scheduling"
    )

class SessionSchema(BaseModel):
    """
    Top-level schema representing the entire state of a tenant-segregated patient chat session.
    """
    messages_history: List[MessageSchema] = Field(
        default_factory=list,
        description="Chronological log of messages"
    )
    bot_active: bool = Field(
        default=True,
        description="Whether the AI assistant bot is currently responding to this patient"
    )
    collected_data: CollectedDataSchema = Field(
        default_factory=CollectedDataSchema,
        description="Structured data extracted during patient intake"
    )
    last_message_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp of the last message in the session"
    )
```

### Key Schema Design Decisions:
1. **Role Constraint**: `Literal["user", "assistant"]` implements native validation during model instantiation, raising a clear `ValidationError` if anything else is passed.
2. **Utc-Default Factory**: `timestamp` default-factory uses `datetime.utcnow` to prevent static value generation.
3. **Pydantic v2 JSON Serialization**: Storing the Pydantic schemas in Redis is performed utilizing `session_data.model_dump_json()`. Parsing the data back uses `SessionSchema.model_validate_json(serialized_str)`. This is highly optimized and handles datetime conversion automatically.

---

## 2. Asynchronous Redis Session Manager (`app/services/session_manager.py`)

The session manager acts as the interface to the Redis instance, with connections pooled asynchronously.

### Proposed Code for `app/services/session_manager.py`
```python
import asyncio
import redis.asyncio as aioredis
from typing import Optional
from app.core.config import settings
from app.schemas.session import SessionSchema

class RedisSessionError(Exception):
    """Custom exception raised when Redis operations fail."""
    pass

class RedisSessionManager:
    """
    Resilient, connection-pooled asynchronous session manager using redis.asyncio.
    """

    def __init__(self, redis_url: str = settings.redis_url):
        self.redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None
        self._lock = asyncio.Lock()

    async def get_client(self) -> aioredis.Redis:
        """
        Retrieves or initializes the async Redis client instance.
        Thread-safe through asyncio.Lock to avoid redundant connections.
        """
        if self._redis is not None:
            return self._redis

        async with self._lock:
            if self._redis is None:
                # Use decode_responses=True so that return values are automatically
                # parsed as strings instead of raw bytes.
                self._redis = aioredis.from_url(
                    self.redis_url,
                    encoding="utf-8",
                    decode_responses=True
                )
            return self._redis

    def _get_key(self, organization_id: str, phone_number: str) -> str:
        """
        Generates the composite key format '{organization_id}:{phone_number}' to segregate tenants.
        """
        return f"{organization_id}:{phone_number}"

    async def get_session(self, organization_id: str, phone_number: str) -> Optional[SessionSchema]:
        """
        Retrieves a session from Redis by organization_id and phone_number.
        """
        key = self._get_key(organization_id, phone_number)
        try:
            client = await self.get_client()
            data = await client.get(key)
            if data is None:
                return None
            return SessionSchema.model_validate_json(data)
        except aioredis.RedisError as e:
            raise RedisSessionError(f"Redis get failed: {e}") from e

    async def update_session(self, organization_id: str, phone_number: str, session_data: SessionSchema) -> None:
        """
        Saves or updates a session in Redis and enforces a 24-hour expiration time (TTL).
        """
        key = self._get_key(organization_id, phone_number)
        try:
            client = await self.get_client()
            # Serialize the Pydantic model directly to JSON string
            serialized_data = session_data.model_dump_json()
            # Set key with TTL of 24 hours (86400 seconds)
            await client.set(key, serialized_data, ex=86400)
        except aioredis.RedisError as e:
            raise RedisSessionError(f"Redis set failed: {e}") from e

    async def clear_session(self, organization_id: str, phone_number: str) -> None:
        """
        Deletes a session from Redis.
        """
        key = self._get_key(organization_id, phone_number)
        try:
            client = await self.get_client()
            await client.delete(key)
        except aioredis.RedisError as e:
            raise RedisSessionError(f"Redis delete failed: {e}") from e

    async def close(self) -> None:
        """
        Safely disposes of the connection pool on application shutdown.
        """
        async with self._lock:
            if self._redis is not None:
                await self._redis.aclose()
                self._redis = None

# Global instance for app-wide use and integration with FastAPI lifespan
session_manager = RedisSessionManager()
```

### Critical Implementation Details:
1. **Lifespan Hook**: Add `await session_manager.close()` to the `lifespan` function in `app/main.py` to ensure that connection pools are safely cleaned up and closed:
   ```python
   @asynccontextmanager
   async def lifespan(app: FastAPI):
       yield
       # Cleanups
       await tenant_db_manager.shutdown_all_pools()
       await session_manager.close()
   ```
2. **Decoding**: Setting `decode_responses=True` on instantiation removes the need to manually decode UTF-8 byte responses to strings before passing to Pydantic.
3. **Resilience**: The client wraps all operations in `try-except` blocks catching `redis.asyncio.RedisError` (the base class for all redis-py driver exceptions, including `ConnectionError` and `TimeoutError`) and bubbles them up as a custom domain exception `RedisSessionError`.

---

## 3. Verification Plan (`tests/test_session_manager.py`)

A set of asynchronous tests will verify functionality, tenant isolation, session TTL, and error handling.

### Proposed Code for `tests/test_session_manager.py`
```python
import pytest
import pytest_asyncio
import redis.exceptions
from unittest.mock import patch
from datetime import datetime

from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from app.services.session_manager import RedisSessionManager, RedisSessionError

@pytest_asyncio.fixture
async def fake_session_manager(monkeypatch):
    """
    Provides a RedisSessionManager connected to a local FakeRedis instance.
    Requires the 'fakeredis' library.
    """
    import fakeredis
    # Instantiate an in-memory async-compatible FakeRedis client
    fake_client = fakeredis.FakeRedis(decode_responses=True)
    
    manager = RedisSessionManager(redis_url="redis://localhost:6379/0")
    
    # Override get_client to return our fake client instance
    async def mock_get_client():
        return fake_client
        
    monkeypatch.setattr(manager, "get_client", mock_get_client)
    yield manager
    await fake_client.aclose()

@pytest.mark.asyncio
async def test_session_crud_operations(fake_session_manager):
    # Prepare session data
    session = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Hello, doctor", timestamp=datetime.utcnow())
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(full_name="John Doe"),
        last_message_at=datetime.utcnow()
    )

    # 1. Verify get on non-existent session returns None
    retrieved = await fake_session_manager.get_session("org_1", "5511999999999")
    assert retrieved is None

    # 2. Update/Save the session
    await fake_session_manager.update_session("org_1", "5511999999999", session)

    # 3. Retrieve and assert correctness
    retrieved = await fake_session_manager.get_session("org_1", "5511999999999")
    assert retrieved is not None
    assert retrieved.bot_active is True
    assert retrieved.collected_data.full_name == "John Doe"
    assert len(retrieved.messages_history) == 1
    assert retrieved.messages_history[0].content == "Hello, doctor"

    # 4. Clear/Delete session
    await fake_session_manager.clear_session("org_1", "5511999999999")
    retrieved = await fake_session_manager.get_session("org_1", "5511999999999")
    assert retrieved is None

@pytest.mark.asyncio
async def test_tenant_isolation(fake_session_manager):
    # Two tenants with the same phone number
    session_org1 = SessionSchema(
        collected_data=CollectedDataSchema(full_name="Patient for Tenant 1")
    )
    session_org2 = SessionSchema(
        collected_data=CollectedDataSchema(full_name="Patient for Tenant 2")
    )

    await fake_session_manager.update_session("org_1", "999999999", session_org1)
    await fake_session_manager.update_session("org_2", "999999999", session_org2)

    # Verify they are isolated and don't overwrite each other
    ret_org1 = await fake_session_manager.get_session("org_1", "999999999")
    ret_org2 = await fake_session_manager.get_session("org_2", "999999999")

    assert ret_org1.collected_data.full_name == "Patient for Tenant 1"
    assert ret_org2.collected_data.full_name == "Patient for Tenant 2"

@pytest.mark.asyncio
async def test_session_ttl_ex_parameter(fake_session_manager):
    session = SessionSchema()
    await fake_session_manager.update_session("org_1", "123456789", session)

    # Check the TTL of the key in fakeredis
    client = await fake_session_manager.get_client()
    ttl = await client.ttl("org_1:123456789")
    
    # TTL should be exactly 86400 seconds (or slightly lower if execution is delayed, but always > 86300)
    assert 86300 <= ttl <= 86400

@pytest.mark.asyncio
async def test_redis_offline_error_handling(fake_session_manager, monkeypatch):
    # Inject a failing method that simulates an offline Redis instance
    async def mock_fail(*args, **kwargs):
        raise redis.exceptions.ConnectionError("Redis connection refused (offline test)")

    client = await fake_session_manager.get_client()
    monkeypatch.setattr(client, "get", mock_fail)
    monkeypatch.setattr(client, "set", mock_fail)
    monkeypatch.setattr(client, "delete", mock_fail)

    # Assert get_session catches the connection failure and raises RedisSessionError
    with pytest.raises(RedisSessionError) as exc_info:
        await fake_session_manager.get_session("org_1", "123456789")
    assert "Redis get failed" in str(exc_info.value)

    # Assert update_session catches the connection failure and raises RedisSessionError
    with pytest.raises(RedisSessionError) as exc_info:
        await fake_session_manager.update_session("org_1", "123456789", SessionSchema())
    assert "Redis set failed" in str(exc_info.value)

    # Assert clear_session catches the connection failure and raises RedisSessionError
    with pytest.raises(RedisSessionError) as exc_info:
        await fake_session_manager.clear_session("org_1", "123456789")
    assert "Redis delete failed" in str(exc_info.value)
```

---

## 4. Next Steps for Implementation
1. Add `fakeredis` dependency to `pyproject.toml` dev group:
   ```bash
   poetry add --group dev fakeredis
   ```
2. Create the Pydantic schemas in `app/schemas/session.py`.
3. Create the session manager service in `app/services/session_manager.py`.
4. Register the Redis Session Manager shutdown hook in the FastAPI `lifespan` in `app/main.py`.
5. Implement unit tests in `tests/test_session_manager.py` and verify using `poetry run pytest tests/test_session_manager.py`.
