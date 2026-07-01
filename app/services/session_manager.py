import asyncio
import logging
from typing import Optional
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import settings
from app.schemas.session import SessionSchema

logger = logging.getLogger(__name__)


class RedisSessionError(Exception):
    """
    Custom exception raised when Redis session operations fail.
    """
    pass


class RedisSessionManager:
    """
    Thread-safe, connection-pooled asynchronous Redis session manager.
    Supports tenant-segregated session caching with a 24-hour TTL.
    """

    def __init__(self, redis_url: str = settings.redis_url, redis_client: Optional[aioredis.Redis] = None):
        self.redis_url = redis_url
        self._redis = redis_client
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
        except RedisError as e:
            logger.error(f"Redis get failed: {e}")
            raise RedisSessionError(f"Redis operation failed: {e}") from e

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
        except RedisError as e:
            logger.error(f"Redis set failed: {e}")
            raise RedisSessionError(f"Redis operation failed: {e}") from e

    async def clear_session(self, organization_id: str, phone_number: str) -> None:
        """
        Deletes a session from Redis.
        """
        key = self._get_key(organization_id, phone_number)
        try:
            client = await self.get_client()
            await client.delete(key)
        except RedisError as e:
            logger.error(f"Redis delete failed: {e}")
            raise RedisSessionError(f"Redis operation failed: {e}") from e

    async def close(self) -> None:
        """
        Safely disposes of the connection pool on application shutdown.
        """
        async with self._lock:
            if self._redis is not None:
                await self._redis.aclose()
                self._redis = None


session_manager = RedisSessionManager()
