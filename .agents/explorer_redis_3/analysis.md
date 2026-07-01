# Redis Session Testing Strategy Analysis

## Overview
This document details the design and implementation strategy for unit testing the Redis Session Manager (`app/services/session_manager.py`) in `tests/test_session_manager.py` using `fakeredis`.

---

## 1. Core Testing Architecture & Principles

### Why `fakeredis`?
`fakeredis` is a lightweight, in-memory Python implementation of Redis. Using it ensures:
1. **Speed**: Tests run extremely fast (<50ms) as they avoid real network socket overhead.
2. **Isolation**: No external running Redis server dependency, preventing tests from failing due to environment configuration.
3. **Determinism**: The Redis database state can be cleanly cleared and inspected programmatically.

### Async Redis Compatibility
Since the service uses `redis.asyncio`, we must use `fakeredis.aioredis.FakeRedis` to ensure compatibility.
- With modern `redis-py` (v5.x), the class `fakeredis.aioredis.FakeRedis` behaves as an asynchronous client that implements the `redis.asyncio.Redis` interface contract.
- In tests, we can instantiate it as follows:
  ```python
  from fakeredis.aioredis import FakeRedis
  fake_client = FakeRedis(decode_responses=True)
  ```

---

## 2. Test Design Patterns

### AAA (Arrange, Act, Assert) & Given-When-Then Structure
Every unit test must adhere to the AAA pattern:
- **Arrange**: Set up the `FakeRedis` client, initialize `SessionManager`, and mock any dependencies or environment states.
- **Act**: Call the lifespan or CRUD method under test.
- **Assert**: Verify the return type, correctness of side-effects (e.g. data written to Redis), expiration (TTL), and exception type.

---

## 3. Test Cases Specification

### Test Case 1: Session CRUD Lifecycle
- **Objective**: Verify that a user session can be retrieved when empty (returning `None`), successfully updated/saved, retrieved with correct values, updated again, and cleared.
- **Verification points**:
  - Uninitialized session returns `None`.
  - Saved session parses correct fields (nested objects inside Pydantic model: `messages_history` list and `collected_data`).
  - Cleared session is successfully removed from the memory store.

### Test Case 2: Tenant & User Isolation (Composite Key separation)
- **Objective**: Enforce complete data segregation using composite keys in the format `{organization_id}:{phone_number}`.
- **Verification points**:
  - Tenant A and Tenant B using the same phone number must not bleed data.
  - Tenant A with Phone 1 and Tenant A with Phone 2 must remain isolated.
  - Assert that keys stored inside the Redis client are literally formatted as `"{organization_id}:{phone_number}"` (checked using `fake_redis.keys("*")`).

### Test Case 3: TTL (Time-To-Live) Write Verification
- **Objective**: Ensure that every update/write operation on the session sets a 24-hour expiration time (TTL) to prevent memory bloating.
- **Verification points**:
  - The TTL for the composite key is queried using `await fake_redis.ttl(key)`.
  - Assert that `86390 <= ttl <= 86400` (allowing a small window for execution duration).

### Test Case 4: Redis Offline & Resilience handling
- **Objective**: Assert that connection or offline errors raised by `redis.exceptions.RedisError` are captured defensively, raising a custom `RedisSessionError`.
- **Verification points**:
  - Mock client methods (`get`, `setex`, `delete`) to raise `redis.exceptions.ConnectionError`.
  - Verify that invoking `get_session`, `update_session`, or `clear_session` raises `RedisSessionError`.

---

## 4. Proposed Test Code Design (`tests/test_session_manager.py`)

Here is the proposed structure for `tests/test_session_manager.py`:

```python
import datetime
import pytest
import redis.exceptions
from unittest import mock
from fakeredis.aioredis import FakeRedis

from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from app.services.session_manager import SessionManager, RedisSessionError


@pytest.fixture
def fake_redis_client():
    """
    Provides an isolated, async-compatible FakeRedis instance for testing.
    """
    client = FakeRedis(decode_responses=True)
    yield client
    # Clean up state after each test
    client.flushall()


@pytest.fixture
def session_manager(fake_redis_client):
    """
    Initializes SessionManager with the FakeRedis client injected.
    """
    return SessionManager(redis_client=fake_redis_client)


@pytest.mark.asyncio
async def test_session_lifecycle_crud(session_manager):
    org_id = "org_test_1"
    phone_number = "+5511999999999"
    
    # 1. Arrange: verify initially empty
    empty_session = await session_manager.get_session(org_id, phone_number)
    assert empty_session is None

    # 2. Act & Assert: Create new session
    now = datetime.datetime.now(datetime.timezone.utc)
    session_data = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Hi, I need an appointment.", timestamp=now)
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(full_name="John Doe"),
        last_message_at=now
    )
    await session_manager.update_session(org_id, phone_number, session_data)

    # 3. Verify retrieved session matches exactly
    retrieved = await session_manager.get_session(org_id, phone_number)
    assert retrieved is not None
    assert retrieved.bot_active is True
    assert retrieved.collected_data.full_name == "John Doe"
    assert len(retrieved.messages_history) == 1
    assert retrieved.messages_history[0].role == "user"
    assert retrieved.messages_history[0].content == "Hi, I need an appointment."

    # 4. Clear/Delete session
    await session_manager.clear_session(org_id, phone_number)
    
    # 5. Verify session is gone
    deleted = await session_manager.get_session(org_id, phone_number)
    assert deleted is None


@pytest.mark.asyncio
async def test_session_composite_key_isolation(session_manager, fake_redis_client):
    # Setup isolated keys
    org_1, org_2 = "org_1", "org_2"
    phone_a, phone_b = "phone_a", "phone_b"

    now = datetime.datetime.now(datetime.timezone.utc)
    session_1 = SessionSchema(
        messages_history=[],
        bot_active=True,
        collected_data=CollectedDataSchema(full_name="User Tenant 1"),
        last_message_at=now
    )
    session_2 = SessionSchema(
        messages_history=[],
        bot_active=False,
        collected_data=CollectedDataSchema(full_name="User Tenant 2"),
        last_message_at=now
    )

    # Act
    await session_manager.update_session(org_1, phone_a, session_1)
    await session_manager.update_session(org_2, phone_a, session_2)

    # Assert: Tenant segregation
    ret_1 = await session_manager.get_session(org_1, phone_a)
    ret_2 = await session_manager.get_session(org_2, phone_a)
    assert ret_1.collected_data.full_name == "User Tenant 1"
    assert ret_2.collected_data.full_name == "User Tenant 2"

    # Assert: Phone segregation on same Tenant
    session_3 = SessionSchema(
        messages_history=[],
        bot_active=True,
        collected_data=CollectedDataSchema(full_name="User 2 Tenant 1"),
        last_message_at=now
    )
    await session_manager.update_session(org_1, phone_b, session_3)
    ret_3 = await session_manager.get_session(org_1, phone_b)
    assert ret_3.collected_data.full_name == "User 2 Tenant 1"

    # Assert underlying keys format
    keys = await fake_redis_client.keys("*")
    assert set(keys) == {f"{org_1}:{phone_a}", f"{org_2}:{phone_a}", f"{org_1}:{phone_b}"}


@pytest.mark.asyncio
async def test_session_ttl_expiration(session_manager, fake_redis_client):
    org_id = "org_1"
    phone_number = "phone_a"
    
    session = SessionSchema(
        messages_history=[],
        bot_active=True,
        collected_data=CollectedDataSchema(),
    )
    
    # Act
    await session_manager.update_session(org_id, phone_number, session)
    
    # Assert
    key = f"{org_id}:{phone_number}"
    ttl = await fake_redis_client.ttl(key)
    # TTL should be 24 hours (86400 seconds)
    assert 86390 <= ttl <= 86400


@pytest.mark.asyncio
async def test_session_manager_offline_resilience(session_manager, fake_redis_client):
    org_id = "org_1"
    phone_number = "phone_a"
    session = SessionSchema(
        messages_history=[],
        bot_active=True,
        collected_data=CollectedDataSchema(),
    )

    # Mock FakeRedis methods to raise connection errors
    with mock.patch.object(fake_redis_client, "get", side_effect=redis.exceptions.ConnectionError("Redis connection offline")):
        with pytest.raises(RedisSessionError) as exc_info:
            await session_manager.get_session(org_id, phone_number)
        assert "Redis operation failed" in str(exc_info.value)

    with mock.patch.object(fake_redis_client, "setex", side_effect=redis.exceptions.ConnectionError("Redis connection offline")):
        with pytest.raises(RedisSessionError):
            await session_manager.update_session(org_id, phone_number, session)

    with mock.patch.object(fake_redis_client, "delete", side_effect=redis.exceptions.ConnectionError("Redis connection offline")):
        with pytest.raises(RedisSessionError):
            await session_manager.clear_session(org_id, phone_number)
```

---

## 5. Design Recommendations for Implementation

1. **Dependency Injection**:
   The `SessionManager` class constructor should be structured as follows:
   ```python
   import redis.asyncio
   from app.core.config import settings

   class SessionManager:
       def __init__(self, redis_client=None):
           if redis_client is not None:
               self.redis = redis_client
           else:
               self.redis = redis.asyncio.from_url(settings.redis_url, decode_responses=True)
   ```
   This allows transparent injection of `FakeRedis` without patching import structures, while remaining fully backward-compatible with standard initialization.

2. **Pydantic Serialization**:
   Store the session as a JSON string using Pydantic's serialization:
   - Writing: `await self.redis.setex(key, 86400, session_data.model_dump_json())`
   - Reading: `data = await self.redis.get(key); return SessionSchema.model_validate_json(data) if data else None`

3. **Exception Wrapper**:
   Define `RedisSessionError` cleanly:
   ```python
   class RedisSessionError(Exception):
       """Custom exception raised when Redis session operations fail."""
       pass
   ```
   Wrap each redis call with `try ... except (redis.exceptions.RedisError, ConnectionError) as e: raise RedisSessionError(f"Redis operation failed: {str(e)}")`.
