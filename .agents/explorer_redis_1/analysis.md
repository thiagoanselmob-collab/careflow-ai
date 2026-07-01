# Design and Implementation Strategy Report: Pydantic Session Schemas & Fakeredis Dependency

## 1. Executive Summary
This report defines the design and implementation strategy for introducing Redis-based patient session state schemas in the CareFlow AI backend. The project runs on Pydantic v2 (`^2.7.4`) and python `^3.11`. We design schemas that integrate smoothly with JSON serialization for Redis caching and recommend the addition of `fakeredis` under the dev dependency group for mock testing.

---

## 2. Pydantic Session Schemas Design (`app/schemas/session.py`)

### 2.1 Schema Definition Code Snippet
The following code represents the proposed design for `app/schemas/session.py`:

```python
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class MessageSchema(BaseModel):
    """
    Represents an individual message exchanged within the patient session.
    """
    role: str
    content: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when the message was sent, defaulting to UTC now."
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        """
        Validates that the role is restricted to 'user' or 'assistant'.
        """
        valid_roles = {"user", "assistant"}
        if value not in valid_roles:
            raise ValueError(f"role must be one of {valid_roles}, got '{value}'")
        return value


class CollectedDataSchema(BaseModel):
    """
    Captures patient demographic and consultation details gathered during the session.
    """
    full_name: Optional[str] = Field(default=None, description="Patient's full name.")
    cpf: Optional[str] = Field(default=None, description="Patient's CPF number.")
    grievance: Optional[str] = Field(default=None, description="Patient's primary health concern or grievance.")
    preferred_doctor: Optional[str] = Field(default=None, description="Name of the preferred doctor.")
    selected_datetime: Optional[datetime] = Field(
        default=None, 
        description="Date and time selected for consultation."
    )


class SessionSchema(BaseModel):
    """
    Encapsulates the complete state of a patient chat session.
    """
    messages_history: List[MessageSchema] = Field(
        default_factory=list,
        description="Chronological log of messages in the session."
    )
    bot_active: bool = Field(
        default=True,
        description="Indicates whether the automated chatbot is actively handling the session."
    )
    collected_data: CollectedDataSchema = Field(
        default_factory=CollectedDataSchema,
        description="The structured patient data collected during this session so far."
    )
    last_message_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of the most recent message exchanged in the session."
    )
```

### 2.2 Key Design Considerations & Justifications
- **Pydantic v2 Compatibility**: We use `@field_validator` with `@classmethod` decorator in accordance with Pydantic v2 guidelines. The legacy `@validator` decorator is deprecated.
- **Timezone Awareness**: The default factory for `timestamp` uses `lambda: datetime.now(timezone.utc)`. This is preferred over `datetime.utcnow()` because `utcnow()` is deprecated in Python 3.12+ and produces naive datetimes, whereas `now(timezone.utc)` creates timezone-aware UTC datetimes. Timezone-aware datetimes prevent errors during cross-timezone comparisons.
- **Mutable Default Values**: Instead of assigning list or sub-model objects directly as defaults (e.g. `messages_history = []`), we use `Field(default_factory=list)` and `Field(default_factory=CollectedDataSchema)`. This prevents shared-reference side-effects in Python.
- **Serialization for Redis**:
  - Redis operates as a key-value store, meaning Pydantic models must be serialized to strings when saving and parsed back on retrieval.
  - Pydantic v2 handles this natively with:
    - **Serialization**: `session_data.model_dump_json()` - Converts the nested schema into a clean, minified JSON string. `datetime` fields are automatically serialized to ISO-8601 string format.
    - **Deserialization**: `SessionSchema.model_validate_json(cached_str)` - Converts the cached JSON string back into fully typed Python Pydantic objects, including parsing the ISO-8601 strings back into native Python `datetime` objects.
  - This avoids manual dictionary traversal or custom JSON encoders.

### 2.3 Proposed Exports in `app/schemas/__init__.py`
To make the schemas clean to import, we should export them in `app/schemas/__init__.py`:
```python
from .session import MessageSchema, CollectedDataSchema, SessionSchema

__all__ = [
    "MessageSchema",
    "CollectedDataSchema",
    "SessionSchema",
]
```

---

## 3. Dependency Configuration (`pyproject.toml`)

### 3.1 Existing Configuration Analysis
The project currently relies on `redis = "^5.0.6"` for async Redis operations.
To support unit testing without needing a running Redis server instance, `fakeredis` must be added.

### 3.2 Strategy for `fakeredis`
- **Dependency Group**: Because `fakeredis` is strictly utilized for mock testing, it should be appended to the development dependency group `[tool.poetry.group.dev.dependencies]`.
- **Version Compatibility**: We propose adding `fakeredis = { version = "^2.23.2", extras = ["asyncio"] }` (or simply `fakeredis = "^2.23.2"`). This version supports `redis-py` 5.x, Python 3.11+, and includes asyncio support (`FakeAsyncRedis`).
- **Proposed Insertion Section**:
  In `pyproject.toml`, line 22-26 currently contains:
  ```toml
  [tool.poetry.group.dev.dependencies]
  pytest = "^8.2.2"
  pytest-asyncio = "^0.23.7"
  aiosqlite = "^0.22.1"
  ```
  The proposed modification is:
  ```toml
  [tool.poetry.group.dev.dependencies]
  pytest = "^8.2.2"
  pytest-asyncio = "^0.23.7"
  aiosqlite = "^0.22.1"
  fakeredis = { version = "^2.23.2", extras = ["asyncio"] }
  ```
