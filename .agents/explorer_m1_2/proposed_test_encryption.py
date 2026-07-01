import os
import base64
import pytest
import time
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services import encryption

# Test constants
TEST_PASSPHRASE = "test-encryption-key-2026!"
TEST_PLAINTEXT = "postgresql+asyncpg://tenant_user:password@tenant_host:5432/tenant_db"

def encrypt_data_helper(plaintext: str, passphrase: str) -> str:
    """
    Helper function to encrypt data matching the Medflow Java spec.
    Used to generate test vectors for the decryption service tests.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"MedFlowCRM-EncryptionSalt-2024",
        iterations=600000,
    )
    key = kdf.derive(passphrase.encode("utf-8"))
    
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    
    final_bytes = iv + ciphertext_with_tag
    return base64.b64encode(final_bytes).decode("utf-8")


@pytest.fixture(autouse=True)
def setup_encryption_env(monkeypatch):
    """
    Automatically mock/set the environment variable and clear the module-level cached key
    before each test to ensure a clean state.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_PASSPHRASE)
    # Reset cached key to force derivation or use of the mocked environment variable
    encryption._CACHED_DERIVED_KEY = None


def test_decrypt_data_success():
    """
    Test that valid Base64 encrypted ciphertext is correctly decrypted back to the plaintext.
    """
    encrypted_base64 = encrypt_data_helper(TEST_PLAINTEXT, TEST_PASSPHRASE)
    decrypted = encryption.decrypt_data(encrypted_base64)
    assert decrypted == TEST_PLAINTEXT


def test_decrypt_data_invalid_passphrase(monkeypatch):
    """
    Test that decrypting with an incorrect passphrase fails and raises a ValueError.
    """
    encrypted_base64 = encrypt_data_helper(TEST_PLAINTEXT, TEST_PASSPHRASE)
    
    # Change the environment variable passphrase
    monkeypatch.setenv("ENCRYPTION_KEY", "wrong-passphrase")
    encryption._CACHED_DERIVED_KEY = None  # Clear cache to force re-derivation
    
    with pytest.raises(ValueError) as exc_info:
        encryption.decrypt_data(encrypted_base64)
    assert "Decryption failed" in str(exc_info.value)


def test_decrypt_data_missing_env_key(monkeypatch):
    """
    Test that decrypting when ENCRYPTION_KEY env var is missing raises a ValueError.
    """
    encrypted_base64 = encrypt_data_helper(TEST_PLAINTEXT, TEST_PASSPHRASE)
    
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    encryption._CACHED_DERIVED_KEY = None  # Clear cache
    
    with pytest.raises(ValueError) as exc_info:
        encryption.decrypt_data(encrypted_base64)
    assert "ENCRYPTION_KEY environment variable is not set" in str(exc_info.value)


def test_decrypt_data_corrupted_ciphertext():
    """
    Test that tampered ciphertext raises a ValueError (due to GCM tag authentication failure).
    """
    encrypted_base64 = encrypt_data_helper(TEST_PLAINTEXT, TEST_PASSPHRASE)
    decoded_bytes = bytearray(base64.b64decode(encrypted_base64))
    
    # Tamper with a byte in the ciphertext portion
    decoded_bytes[15] ^= 0xFF
    corrupted_base64 = base64.b64encode(decoded_bytes).decode("utf-8")
    
    with pytest.raises(ValueError) as exc_info:
        encryption.decrypt_data(corrupted_base64)
    assert "Decryption failed: Invalid tag" in str(exc_info.value)


def test_decrypt_data_invalid_base64():
    """
    Test that invalid base64 input raises a ValueError.
    """
    with pytest.raises(ValueError) as exc_info:
        encryption.decrypt_data("!!! Not Base64 !!!")
    assert "Failed to decode Base64 data" in str(exc_info.value)


def test_decrypt_data_too_short():
    """
    Test that ciphertext shorter than 28 bytes (12 bytes IV + 16 bytes tag) raises a ValueError.
    """
    too_short_bytes = os.urandom(20)
    too_short_base64 = base64.b64encode(too_short_bytes).decode("utf-8")
    
    with pytest.raises(ValueError) as exc_info:
        encryption.decrypt_data(too_short_base64)
    assert "Encrypted data is too short" in str(exc_info.value)


def test_key_derivation_caching():
    """
    Verify that key derivation is cached, so subsequent decryption calls
    are near-instantaneous (under 1ms) instead of taking ~160ms each.
    """
    encrypted_base64 = encrypt_data_helper(TEST_PLAINTEXT, TEST_PASSPHRASE)
    
    # 1. First run (uncached) - should take some time
    t0 = time.perf_counter()
    encryption.decrypt_data(encrypted_base64)
    t1 = time.perf_counter()
    first_duration = t1 - t0
    
    # 2. Second run (cached) - should be extremely fast
    t2 = time.perf_counter()
    encryption.decrypt_data(encrypted_base64)
    t3 = time.perf_counter()
    second_duration = t3 - t2
    
    # The second run must be significantly faster than the first (e.g., at least 50x faster)
    # and take less than 2 milliseconds.
    assert second_duration < 0.002
    assert second_duration * 50 < first_duration
