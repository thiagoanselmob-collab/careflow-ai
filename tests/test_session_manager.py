import datetime
import pytest
import pytest_asyncio
import redis.exceptions
from unittest import mock
from fakeredis.aioredis import FakeRedis

from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from app.services.session_manager import RedisSessionManager, RedisSessionError


@pytest_asyncio.fixture
async def fake_redis_client():
    """
    Provides an isolated, async-compatible FakeRedis instance for testing.
    """
    client = FakeRedis(decode_responses=True)
    yield client
    # Clean up state after each test
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def session_manager(fake_redis_client):
    """
    Initializes RedisSessionManager with the FakeRedis client injected.
    """
    return RedisSessionManager(redis_client=fake_redis_client)


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

    # Assert: Tenant segregation (different orgs, same phone)
    ret_1 = await session_manager.get_session(org_1, phone_a)
    ret_2 = await session_manager.get_session(org_2, phone_a)
    assert ret_1.collected_data.full_name == "User Tenant 1"
    assert ret_2.collected_data.full_name == "User Tenant 2"

    # Assert: Phone segregation on same Tenant (different phones, same org)
    session_3 = SessionSchema(
        messages_history=[],
        bot_active=True,
        collected_data=CollectedDataSchema(full_name="User 2 Tenant 1"),
        last_message_at=now
    )
    await session_manager.update_session(org_1, phone_b, session_3)
    ret_3 = await session_manager.get_session(org_1, phone_b)
    assert ret_3.collected_data.full_name == "User 2 Tenant 1"

    # Assert underlying keys format is literally "{organization_id}:{phone_number}"
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

    with mock.patch.object(fake_redis_client, "get", side_effect=redis.exceptions.ConnectionError("Redis connection offline")):
        with pytest.raises(RedisSessionError) as exc_info:
            await session_manager.get_session(org_id, phone_number)
        assert "Redis operation failed" in str(exc_info.value)

    with mock.patch.object(fake_redis_client, "set", side_effect=redis.exceptions.ConnectionError("Redis connection offline")):
        with pytest.raises(RedisSessionError) as exc_info:
            await session_manager.update_session(org_id, phone_number, session)
        assert "Redis operation failed" in str(exc_info.value)

    with mock.patch.object(fake_redis_client, "delete", side_effect=redis.exceptions.ConnectionError("Redis connection offline")):
        with pytest.raises(RedisSessionError) as exc_info:
            await session_manager.clear_session(org_id, phone_number)
        assert "Redis operation failed" in str(exc_info.value)
