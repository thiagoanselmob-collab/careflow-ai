import base64
import os
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
# The target import path once implemented will be:
from app.services.encryption import decrypt_data, derive_key

# Helper to encrypt data for dynamic test assertions
def encrypt_helper(plaintext: str, passphrase: str) -> str:
    key = derive_key(passphrase)
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    combined = iv + ciphertext_with_tag
    return base64.b64encode(combined).decode("utf-8")

def test_decrypt_data_success(monkeypatch):
    """
    Test successful decryption of dynamically encrypted data.
    """
    passphrase = "my-secure-passphrase-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    original_text = "test_connection_string_value"
    encrypted_b64 = encrypt_helper(original_text, passphrase)
    
    decrypted_text = decrypt_data(encrypted_b64)
    assert decrypted_text == original_text

def test_decrypt_static_vector(monkeypatch):
    """
    Assert that the decryption service correctly decrypts a static ciphertext
    generated with known parameters matching the Java spec.
    """
    passphrase = "test-secret-key"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    # Generated using fixed IV: b"123456789012"
    static_b64 = "MTIzNDU2Nzg5MDEyhuA467Oj7I/aOpkZ8DkCFKks7RKhXmCFleN0nENyR7QnyoVAd86uW5SJhHZmmCdGXTT16cFFFQbQc8Z4vkxfAHmrzpKVFtTW01lPl2ReJA=="
    expected_plaintext = "postgresql+asyncpg://postgres:postgres@localhost:5432/tenant_db"
    
    decrypted = decrypt_data(static_b64)
    assert decrypted == expected_plaintext

def test_decrypt_data_missing_env(monkeypatch):
    """
    Ensure a ValueError is raised if ENCRYPTION_KEY env var is not set.
    """
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    
    with pytest.raises(ValueError) as excinfo:
        decrypt_data("MTIzNDU2Nzg5MDEyhuA4...")
    assert "ENCRYPTION_KEY environment variable is not set" in str(excinfo.value)

def test_decrypt_data_invalid_base64(monkeypatch):
    """
    Ensure ValueError is raised if input is not valid base64.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", "any-key")
    
    with pytest.raises(ValueError) as excinfo:
        decrypt_data("!!!invalid-base64!!!")
    assert "Invalid Base64 input" in str(excinfo.value)

def test_decrypt_data_too_short(monkeypatch):
    """
    Ensure ValueError is raised if decoded payload is less than 12 bytes (IV size).
    """
    monkeypatch.setenv("ENCRYPTION_KEY", "any-key")
    
    # Less than 12 bytes when decoded (e.g. 8 bytes)
    short_payload_b64 = base64.b64encode(b"short").decode("utf-8")
    
    with pytest.raises(ValueError) as excinfo:
        decrypt_data(short_payload_b64)
    assert "too short to contain 12-byte IV" in str(excinfo.value)

def test_decrypt_data_incorrect_key(monkeypatch):
    """
    Ensure decryption fails with a ValueError when the wrong key is provided.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", "correct-key")
    encrypted_b64 = encrypt_helper("my-secret-data", "correct-key")
    
    # Decrypt with wrong key
    monkeypatch.setenv("ENCRYPTION_KEY", "wrong-key")
    with pytest.raises(ValueError) as excinfo:
        decrypt_data(encrypted_b64)
    assert "Decryption failed" in str(excinfo.value)

def test_decrypt_data_tampered_ciphertext(monkeypatch):
    """
    Ensure decryption fails if the ciphertext is tampered with (GCM integrity check fails).
    """
    passphrase = "secure-key"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    encrypted_b64 = encrypt_helper("my-secret-data", passphrase)
    
    raw = bytearray(base64.b64decode(encrypted_b64))
    # Tamper with the ciphertext (flip a bit in the last byte)
    raw[-1] ^= 0x01
    tampered_b64 = base64.b64encode(raw).decode("utf-8")
    
    with pytest.raises(ValueError) as excinfo:
        decrypt_data(tampered_b64)
    assert "Decryption failed" in str(excinfo.value)
