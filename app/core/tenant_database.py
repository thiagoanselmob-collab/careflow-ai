import asyncio
import logging
from typing import Dict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import SessionLocal as default_sessionmaker
from app.models.settings import Settings
from app.services.encryption import decrypt_data

logger = logging.getLogger(__name__)


async def _init_tenant_db(engine: AsyncEngine) -> None:
    # Handle mock engines in tests gracefully
    if hasattr(engine, "_mock_self") or "Mock" in type(engine).__name__:
        return

    dialect_name = engine.dialect.name
    if dialect_name == "postgresql":
        # First attempt with pgvector
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS clinic_knowledge (
                        id SERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        metadata JSONB,
                        embedding VECTOR(768)
                    );
                """))
                await conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS clinic_knowledge_embedding_idx 
                    ON clinic_knowledge USING hnsw(embedding vector_cosine_ops);
                """))
                # WhatsApp Tables
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS message_buffer (
                        id SERIAL PRIMARY KEY,
                        phone_number VARCHAR(50) NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS dados_cliente (
                        phone_number VARCHAR(50) PRIMARY KEY,
                        status VARCHAR(50) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS agent_configs (
                        id SERIAL PRIMARY KEY,
                        agent_type VARCHAR(50) UNIQUE NOT NULL,
                        system_prompt TEXT,
                        system_prompt_noshow TEXT,
                        llm_provider VARCHAR(50) NOT NULL,
                        llm_model VARCHAR(100) NOT NULL,
                        is_active BOOLEAN DEFAULT TRUE NOT NULL,
                        reminder_time VARCHAR(10),
                        reminder_rules TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                    );
                """))
            return
        except Exception as e:
            logger.warning(f"PostgreSQL pgvector tenant DB initialization failed, falling back: {e}")

        # Fallback for PostgreSQL without vector
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS clinic_knowledge (
                        id SERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        metadata JSONB
                    );
                """))
                # WhatsApp Tables
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS message_buffer (
                        id SERIAL PRIMARY KEY,
                        phone_number VARCHAR(50) NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS dados_cliente (
                        phone_number VARCHAR(50) PRIMARY KEY,
                        status VARCHAR(50) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS agent_configs (
                        id SERIAL PRIMARY KEY,
                        agent_type VARCHAR(50) UNIQUE NOT NULL,
                        system_prompt TEXT,
                        system_prompt_noshow TEXT,
                        llm_provider VARCHAR(50) NOT NULL,
                        llm_model VARCHAR(100) NOT NULL,
                        is_active BOOLEAN DEFAULT TRUE NOT NULL,
                        reminder_time VARCHAR(10),
                        reminder_rules TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                    );
                """))
        except Exception as e:
            logger.error(f"PostgreSQL fallback tenant DB initialization failed: {e}")
            raise
    else:
        # SQLite fallback
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS clinic_knowledge (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content TEXT NOT NULL,
                        metadata TEXT
                    );
                """))
                # WhatsApp Tables
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
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS agent_configs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        agent_type VARCHAR(50) UNIQUE NOT NULL,
                        system_prompt TEXT,
                        system_prompt_noshow TEXT,
                        llm_provider VARCHAR(50) NOT NULL,
                        llm_model VARCHAR(100) NOT NULL,
                        is_active BOOLEAN DEFAULT TRUE NOT NULL,
                        reminder_time VARCHAR(10),
                        reminder_rules TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                    );
                """))
        except Exception as e:
            logger.error(f"SQLite tenant DB initialization failed: {e}")
            raise



class TenantConnectionManager:
    """
    Manages dynamic multi-tenant database connection pools.
    Queries the central settings table, decrypts connection strings,
    caches SQLAlchemy async engines and sessionmakers, and performs
    cleanups on shutdown.
    """

    def __init__(self, central_sessionmaker=default_sessionmaker):
        self._engines: Dict[str, AsyncEngine] = {}
        self._sessionmakers: Dict[str, async_sessionmaker[AsyncSession]] = {}
        self._central_sessionmaker = central_sessionmaker
        self._lock = asyncio.Lock()

    async def get_engine(self, organization_id: str) -> AsyncEngine:
        """
        Retrieves or creates the SQLAlchemy AsyncEngine for the specified tenant.
        """
        async with self._lock:
            if organization_id in self._engines:
                return self._engines[organization_id]

            # Query the central database for the connection string
            async with self._central_sessionmaker() as session:
                stmt = select(Settings).where(Settings.organization_id == organization_id)
                result = await session.execute(stmt)
                setting = result.scalar_one_or_none()
                if not setting:
                    raise ValueError(f"Tenant configuration not found for organization: {organization_id}")
                
                encrypted_conn_str = setting.tenant_connection_string

            # Decrypt the connection string
            decrypted_conn_str = decrypt_data(encrypted_conn_str)

            # Ensure SQLAlchemy uses the async driver for PostgreSQL
            if decrypted_conn_str.startswith("postgresql://"):
                decrypted_conn_str = decrypted_conn_str.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif decrypted_conn_str.startswith("postgres://"):
                decrypted_conn_str = decrypted_conn_str.replace("postgres://", "postgresql+asyncpg://", 1)

            # Create the AsyncEngine
            if "sqlite" in decrypted_conn_str:
                engine = create_async_engine(decrypted_conn_str, echo=False, future=True, connect_args={"uri": True})
            else:
                engine = create_async_engine(decrypted_conn_str, echo=False, future=True)
            await _init_tenant_db(engine)
            self._engines[organization_id] = engine

            # Create the async sessionmaker
            self._sessionmakers[organization_id] = async_sessionmaker(
                bind=engine,
                autoflush=False,
                expire_on_commit=False,
                class_=AsyncSession
            )

            return engine

    async def get_sessionmaker(self, organization_id: str) -> async_sessionmaker[AsyncSession]:
        """
        Retrieves the async sessionmaker for the specified tenant.
        """
        if organization_id not in self._sessionmakers:
            await self.get_engine(organization_id)
        return self._sessionmakers[organization_id]

    async def get_tenant_session(self, organization_id: str) -> AsyncSession:
        """
        Retrieves a new AsyncSession for the specified tenant.
        """
        sessionmaker = await self.get_sessionmaker(organization_id)
        return sessionmaker()

    async def shutdown_all_pools(self) -> None:
        """
        Closes all cached async engines/pools and clears the cache.
        """
        async with self._lock:
            for org_id, engine in self._engines.items():
                await engine.dispose()
            self._engines.clear()
            self._sessionmakers.clear()


# Export a global singleton instance for application lifespan hook and operational routing.
tenant_db_manager = TenantConnectionManager()
