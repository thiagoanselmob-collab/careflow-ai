# Scope: Redis Session Management

## Architecture
- **Schemas**: `app/schemas/session.py` defining patient session states.
- **Service**: `app/services/session_manager.py` utilizing `redis.asyncio` for CRUD operations on tenant-segregated session data.
- **Config**: Settings in `app/core/config.py` provides the Redis connection URL (`redis_url`).
- **Tests**: `tests/test_session_manager.py` using `fakeredis` to verify connection and CRUD functionality under offline and online states.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Environment & Schemas | Add `fakeredis` dev dependency, create `app/schemas/session.py` schemas | None | DONE |
| 2 | Redis Session Manager | Implement `app/services/session_manager.py` with CRUD and error handling | M1 | DONE |
| 3 | Unit Testing & Verification | Implement `tests/test_session_manager.py` and run tests | M1, M2 | DONE |

## Interface Contracts
### `app/schemas/session.py`
- `MessageSchema`:
  - `role`: str (validation: must be `"user"` or `"assistant"`)
  - `content`: str
  - `timestamp`: datetime
- `CollectedDataSchema`:
  - `full_name`: Optional[str]
  - `cpf`: Optional[str]
  - `grievance`: Optional[str]
  - `preferred_doctor`: Optional[str]
  - `selected_datetime`: Optional[datetime]
- `SessionSchema`:
  - `messages_history`: List[MessageSchema]
  - `bot_active`: bool (default: `True`)
  - `collected_data`: CollectedDataSchema
  - `last_message_at`: Optional[datetime]

### `app/services/session_manager.py`
- Initialization:
  - Connection pool configuration using `settings.redis_url`.
- Methods:
  - `async def get_session(organization_id: str, phone_number: str) -> Optional[SessionSchema]`
  - `async def update_session(organization_id: str, phone_number: str, session_data: SessionSchema) -> None`
  - `async def clear_session(organization_id: str, phone_number: str) -> None`
- Key format: `"{organization_id}:{phone_number}"`
- TTL: 24 hours (`86400` seconds)
- Errors: Catch connection/offline errors from `redis.exceptions.RedisError` and raise custom exception (e.g. `RedisSessionError`).
