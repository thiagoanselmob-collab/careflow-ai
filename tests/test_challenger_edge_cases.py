import base64
import os
import time
import asyncio
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import ArgumentError

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.encryption import decrypt_data, derive_key

# Helper to encrypt data for testing
def encrypt_helper(plaintext: str, passphrase: str) -> str:
    key = derive_key(passphrase)
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    combined = iv + ciphertext_with_tag
    return base64.b64encode(combined).decode("utf-8")

def test_performance_benchmark(monkeypatch):
    """
    Measure performance and verify key derivation latency.
    """
    passphrase = "test-performance-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    start_time = time.perf_counter()
    key1 = derive_key(passphrase)
    duration = time.perf_counter() - start_time
    
    print(f"\n[BENCHMARK] derive_key took {duration:.4f} seconds.")
    
    # We expect PBKDF2 with 600,000 iterations to take at least 50ms (usually 100-300ms)
    assert duration > 0.05, f"PBKDF2 is unexpectedly fast: {duration:.4f}s"

def test_non_ascii_passphrase(monkeypatch):
    """
    Ensure non-ASCII characters in passphrase derive keys correctly
    and decrypt matches encryption.
    """
    passphrase = "🔑senha-complexa-áéíóú-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    original_text = "secret_data_with_unicode_key"
    encrypted_b64 = encrypt_helper(original_text, passphrase)
    
    decrypted_text = decrypt_data(encrypted_b64)
    assert decrypted_text == original_text

def test_payload_length_between_12_and_27(monkeypatch):
    """
    Verify behavior when payload is decoded to a length >= 12 but < 28 bytes.
    12 bytes is IV length, but the tag takes 16 bytes, so minimum total length is 28 bytes.
    If length is e.g. 15 bytes, ciphertext_with_tag is 3 bytes (too short).
    """
    monkeypatch.setenv("ENCRYPTION_KEY", "any-key")
    
    # 15 bytes payload (12 bytes IV + 3 bytes ciphertext/tag)
    raw_payload = b"123456789012abc"
    payload_b64 = base64.b64encode(raw_payload).decode("utf-8")
    
    # What exception is raised here? Let's capture it.
    try:
        decrypt_data(payload_b64)
        pytest.fail("Should have failed")
    except ValueError as e:
        # Check if the error message is Decryption failed or something else
        assert "Decryption failed" in str(e) or "too short" in str(e)
    except Exception as e:
        pytest.fail(f"Raised unexpected exception type: {type(e).__name__} - {str(e)}")

def test_unicode_decode_failure_handling(monkeypatch):
    """
    If decrypted bytes are not valid UTF-8, decrypt_data should raise a ValueError
    or handle it gracefully. Let's see what happens if we encrypt non-UTF-8 bytes.
    """
    passphrase = "test-key"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    # We encrypt invalid UTF-8 bytes: b"\xff\xfe\xfd\xfc"
    key = derive_key(passphrase)
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(iv, b"\xff\xfe\xfd\xfc", None)
    combined = iv + ciphertext_with_tag
    invalid_utf8_b64 = base64.b64encode(combined).decode("utf-8")
    
    # decrypt_data will decrypt successfully but then call .decode("utf-8")
    # which will raise ValueError due to wrapping. Let's verify.
    with pytest.raises(ValueError) as excinfo:
        decrypt_data(invalid_utf8_b64)
    assert "Decryption succeeded but content is not valid UTF-8" in str(excinfo.value)

def test_urlsafe_base64_payload(monkeypatch):
    """
    Check if URL-safe base64 payloads (using '-' and '_') are supported.
    """
    passphrase = "test-key"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    # Create standard encrypted payload
    original_text = "test_urlsafe"
    encrypted_b64 = encrypt_helper(original_text, passphrase)
    
    # Make it urlsafe manually if it contains '+' or '/'
    # Or generate raw bytes that we know will have characters requiring URL safety.
    # To be sure, we can just encode urlsafe and try to decrypt.
    key = derive_key(passphrase)
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(iv, original_text.encode("utf-8"), None)
    combined = iv + ciphertext_with_tag
    
    # Force urlsafe base64
    urlsafe_b64 = base64.urlsafe_b64encode(combined).decode("utf-8")
    
    # If the standard decoder is used, it might raise ValueError if urlsafe characters are present
    # Let's see if decrypt_data fails when we pass a urlsafe base64 string
    # (Note: b64decode can sometimes decode urlsafe if no '-' or '_' are present, but if they are, it fails).
    # Let's ensure urlsafe chars are present:
    # We can craft raw bytes that definitely produce '+' and '/' in standard base64.
    # Let's do that:
    urlsafe_payload = None
    for i in range(100):
        iv_test = os.urandom(12)
        cipher_test = aesgcm.encrypt(iv_test, original_text.encode("utf-8"), None)
        combined_test = iv_test + cipher_test
        std_b64 = base64.b64encode(combined_test).decode("utf-8")
        if '+' in std_b64 or '/' in std_b64:
            urlsafe_payload = base64.urlsafe_b64encode(combined_test).decode("utf-8")
            break
            
    if urlsafe_payload:
        with pytest.raises(ValueError) as excinfo:
            decrypt_data(urlsafe_payload)
        # It raises either "Decryption failed: Incorrect passphrase or tampered ciphertext"
        # or "Invalid Base64 input" depending on whether the discarded characters
        # result in an invalid base64 length/padding or decode to mangled bytes.
        assert "Decryption failed" in str(excinfo.value) or "Invalid Base64 input" in str(excinfo.value)


@pytest_asyncio.fixture
async def central_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    session_maker = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    yield session_maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_tenant_pool_creation(central_db, monkeypatch):
    """
    Test 5-10 concurrent tasks calling get_engine and get_session for the same tenant
    simultaneously to ensure asyncio.Lock works and no duplicate engines are created.
    """
    passphrase = "test-concurrent-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:concurrent_org?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="concurrent_org", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    from app.core.tenant_database import create_async_engine as real_create_async_engine
    call_count = 0
    engines_created = []
    
    def mock_create_engine(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        eng = real_create_async_engine(*args, **kwargs)
        engines_created.append(eng)
        return eng
        
    monkeypatch.setattr("app.core.tenant_database.create_async_engine", mock_create_engine)
    
    # Create 10 concurrent requests to get_engine and get_session
    async def get_resources():
        engine = await manager.get_engine("concurrent_org")
        session = await manager.get_tenant_session("concurrent_org")
        await session.close()
        return engine

    tasks = [get_resources() for _ in range(10)]
    results = await asyncio.gather(*tasks)
    
    # Check that only 1 engine was created
    assert call_count == 1
    # Check that all returned engines are the exact same instance
    first_engine = results[0]
    for eng in results:
        assert eng is first_engine
        
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_corrupted_decrypted_connection_string(central_db, monkeypatch):
    """
    Test when connection string is decrypted successfully but is an invalid SQLAlchemy URL.
    Verify create_async_engine raises ArgumentError (or relevant SQLAlchemy error)
    and the manager does not cache the engine or sessionmaker.
    """
    passphrase = "test-corrupt-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    # Decryptable but invalid SQLAlchemy scheme/URL
    corrupt_conn_str = "invalid_scheme://host/db"
    encrypted_conn = encrypt_helper(corrupt_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="corrupt_org", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Attempting to get the engine should fail with ArgumentError or NoSuchModuleError
    with pytest.raises((ArgumentError, Exception)) as excinfo:
        await manager.get_engine("corrupt_org")
        
    # The cache must remain empty for this org
    assert "corrupt_org" not in manager._engines
    assert "corrupt_org" not in manager._sessionmakers


@pytest.mark.asyncio
async def test_undecryptable_connection_string(central_db, monkeypatch):
    """
    Test when connection string in Settings table is completely undecryptable/corrupted base64.
    Verify get_engine raises ValueError from decrypt_data.
    """
    passphrase = "test-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    # Completely invalid base64 string
    corrupt_encrypted_conn = "not-valid-base64-!!!"
    
    async with central_db() as session:
        setting = Settings(organization_id="undecryptable_org", tenant_connection_string=corrupt_encrypted_conn)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    with pytest.raises(ValueError) as excinfo:
        await manager.get_engine("undecryptable_org")
    
    assert "Base64" in str(excinfo.value) or "Decryption" in str(excinfo.value)
    
    assert "undecryptable_org" not in manager._engines
    assert "undecryptable_org" not in manager._sessionmakers


@pytest.mark.asyncio
async def test_shutdown_with_no_pools_open(central_db):
    """
    Test calling shutdown_all_pools when no engines/pools are cached.
    Should run cleanly without raising any exceptions.
    """
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    # Cache is empty
    assert len(manager._engines) == 0
    assert len(manager._sessionmakers) == 0
    
    # Calling shutdown should be a no-op and not raise errors
    await manager.shutdown_all_pools()
    
    assert len(manager._engines) == 0
    assert len(manager._sessionmakers) == 0


@pytest.mark.asyncio
async def test_double_shutdown(central_db, monkeypatch):
    """
    Test calling shutdown_all_pools multiple times.
    The first call should dispose the open pools.
    The subsequent calls should not raise any errors.
    """
    passphrase = "test-double-shutdown-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:double_shutdown?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="double_shutdown", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Open the pool
    await manager.get_engine("double_shutdown")
    assert "double_shutdown" in manager._engines
    
    # First shutdown
    await manager.shutdown_all_pools()
    assert "double_shutdown" not in manager._engines
    
    # Second shutdown
    await manager.shutdown_all_pools()
    assert "double_shutdown" not in manager._engines


@pytest.mark.asyncio
async def test_concurrent_different_tenants(central_db, monkeypatch):
    """
    Verify that concurrent engine requests for different tenants run successfully
    without blocking or interfering with each other.
    """
    passphrase = "test-concurrent-diff-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    async with central_db() as session:
        for idx in range(5):
            org_id = f"diff_org_{idx}"
            conn_str = f"sqlite+aiosqlite:///file:{org_id}?mode=memory&cache=shared&uri=true"
            enc_conn = encrypt_helper(conn_str, passphrase)
            session.add(Settings(organization_id=org_id, tenant_connection_string=enc_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Retrieve all 5 engines concurrently
    tasks = [manager.get_engine(f"diff_org_{idx}") for idx in range(5)]
    engines = await asyncio.gather(*tasks)
    
    # All 5 engines should be unique
    assert len(engines) == 5
    assert len(set(engines)) == 5
    
    # Ensure they are cached
    for idx in range(5):
        org_id = f"diff_org_{idx}"
        assert org_id in manager._engines
        
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_missing_encryption_key_env_var(central_db, monkeypatch):
    """
    Verify that if the ENCRYPTION_KEY environment variable is missing,
    get_engine raises a ValueError.
    """
    passphrase = "test-key-2026"
    # Insert entry so query succeeds and goes to decryption
    encrypted_conn = encrypt_helper("sqlite+aiosqlite:///:memory:", passphrase)
    async with central_db() as session:
        session.add(Settings(organization_id="missing_env_org", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    with pytest.raises(ValueError) as excinfo:
        await manager.get_engine("missing_env_org")
    assert "ENCRYPTION_KEY environment variable is not set" in str(excinfo.value)


@pytest.mark.asyncio
async def test_empty_connection_string(central_db, monkeypatch):
    """
    Verify that if the tenant_connection_string is empty in central DB settings,
    get_engine raises a ValueError and does not cache the engine.
    """
    passphrase = "test-empty-conn-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    async with central_db() as session:
        # Empty string as connection string (None is not allowed due to NOT NULL db constraint)
        session.add(Settings(organization_id="empty_org_2", tenant_connection_string=""))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Test empty string
    with pytest.raises(ValueError) as excinfo:
        await manager.get_engine("empty_org_2")
        
    assert "too short" in str(excinfo.value) or "Invalid Base64" in str(excinfo.value) or "Decryption failed" in str(excinfo.value)
    assert "empty_org_2" not in manager._engines


@pytest.mark.asyncio
async def test_dynamic_config_update_lifecycle(central_db, monkeypatch):
    """
    Verify that updating a tenant's connection string in settings table
    does not affect the cached engine until shutdown_all_pools is called.
    """
    passphrase = "test-update-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    conn_str_1 = "sqlite+aiosqlite:///file:update_org1?mode=memory&cache=shared&uri=true"
    conn_str_2 = "sqlite+aiosqlite:///file:update_org2?mode=memory&cache=shared&uri=true"
    
    enc_1 = encrypt_helper(conn_str_1, passphrase)
    enc_2 = encrypt_helper(conn_str_2, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="update_org", tenant_connection_string=enc_1)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Get initial engine and cache it
    engine_initial = await manager.get_engine("update_org")
    
    # Now update the connection string in the settings table
    async with central_db() as session:
        from sqlalchemy import update
        await session.execute(
            update(Settings)
            .where(Settings.organization_id == "update_org")
            .values(tenant_connection_string=enc_2)
        )
        await session.commit()
        
    # Retrieving engine again should return the cached initial engine
    engine_cached = await manager.get_engine("update_org")
    assert engine_cached is engine_initial
    
    # Now shutdown pools to clear the cache
    await manager.shutdown_all_pools()
    
    # Retrieving engine now should fetch the updated connection string and create a new engine
    engine_updated = await manager.get_engine("update_org")
    assert engine_updated is not engine_initial
    
    # Verify we can obtain a session with the new engine
    session = await manager.get_tenant_session("update_org")
    await session.close()
    
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_engine_disposal_and_recreation(central_db, monkeypatch):
    """
    Verify that an engine/session retrieved before shutdown is removed from cache
    after shutdown_all_pools (its pool is disposed), and requesting the engine
    again after shutdown returns a new functional engine object.
    """
    passphrase = "test-disposal-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    conn_str = "sqlite+aiosqlite:///file:disposal_org?mode=memory&cache=shared&uri=true"
    enc_conn = encrypt_helper(conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="disposal_org", tenant_connection_string=enc_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Get session and verify we can execute a query
    from sqlalchemy import text
    session = await manager.get_tenant_session("disposal_org")
    result = await session.execute(text("SELECT 1"))
    assert result.scalar() == 1
    await session.close()
    
    engine = await manager.get_engine("disposal_org")
    
    # Shutdown all pools
    await manager.shutdown_all_pools()
    
    # Verify that the engine and sessionmaker are cleared from the cache
    assert "disposal_org" not in manager._engines
    assert "disposal_org" not in manager._sessionmakers
            
    # Get a new session/engine and verify it works and is a new engine object
    engine_new = await manager.get_engine("disposal_org")
    assert engine_new is not engine
    
    session_new = await manager.get_tenant_session("disposal_org")
    result_new = await session_new.execute(text("SELECT 1"))
    assert result_new.scalar() == 1
    await session_new.close()
    
    await manager.shutdown_all_pools()

