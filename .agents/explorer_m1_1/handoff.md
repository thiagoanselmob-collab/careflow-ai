# Handoff Report: Milestone 1 - R2. Decryption Service Implementation Plan

## 1. Observation
We explored the workspace and observed the following:
* **`pyproject.toml`**: Located at `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/pyproject.toml`. Lines 9–20 show poetry dependencies: FastAPI, SQLAlchemy, asyncpg, etc. but `cryptography` is missing.
* **`PROJECT.md`**: Located at `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/orchestrator_multi_tenant/PROJECT.md`. Line 11 defines the Encryption/Decryption scope: "Python implementation of PBKDF2/AES-256-GCM matching Medflow's Java equivalent. Decrypts `tenant_connection_string`." Line 17 defines Milestone 1 task: "Implement `app/services/encryption.py` and `tests/test_encryption.py`".
* **Java Equivalent Code**: Located at `/Users/thiagoanselmobarbosa/Desktop/medflow full/medflow---crm-da-saúde-(n8n-integrated)-2/medflow-backend/src/main/java/com/medflow/infrastructure/security/EncryptionService.java`. Lines 32–45 verify key cryptographic parameters:
  ```java
  private static final String ALGORITHM = "AES/GCM/NoPadding";
  private static final int GCM_IV_LENGTH = 12;
  private static final int GCM_TAG_LENGTH = 128;
  private static final String KEY_DERIVATION_ALGORITHM = "PBKDF2WithHmacSHA256";
  private static final int PBKDF2_ITERATIONS = 600_000;
  private static final int KEY_LENGTH_BITS = 256;
  private static final byte[] DERIVATION_SALT = "MedFlowCRM-EncryptionSalt-2024".getBytes(StandardCharsets.UTF_8);
  ```
  Lines 83–105 verify how Java handles formatting/decryption:
  ```java
  byte[] combined = Base64.getDecoder().decode(encryptedText);
  byte[] iv = new byte[GCM_IV_LENGTH];
  byte[] cipherText = new byte[combined.length - GCM_IV_LENGTH];
  System.arraycopy(combined, 0, iv, 0, iv.length);
  System.arraycopy(combined, iv.length, cipherText, 0, cipherText.length);
  ```
* **Virtualenv Environment**: Running `poetry env info` verified Python version `3.11.15` and running `poetry run python -c "import cryptography; print(cryptography.__version__)"` verified `cryptography` version `49.0.0` is already in the virtual environment but not declared in `pyproject.toml`.

## 2. Logic Chain
1. **PBKDF2 Derivation Matching**:
   * Java derives the key using `PBKDF2WithHmacSHA256` with `600,000` iterations, 256-bit output size, and salt `"MedFlowCRM-EncryptionSalt-2024"`.
   * This is mathematically equivalent to Python's `PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"MedFlowCRM-EncryptionSalt-2024", iterations=600000)`.
2. **AES-GCM Payload Mapping**:
   * Java's `"AES/GCM/NoPadding"` with a 128-bit authentication tag formats ciphertext as: `IV (12 bytes) + Ciphertext + GCM Tag (16 bytes)`.
   * Python `cryptography.hazmat.primitives.ciphers.aead.AESGCM` takes the key, 12-byte IV (nonce), and data (`ciphertext + tag`).
   * When decoding the Base64 input, the first 12 bytes represent the IV. The remainder is passed directly as the ciphertext/tag payload to `AESGCM.decrypt()`. This yields an identical result.
3. **Performance Optimization (Caching)**:
   * Key derivation via PBKDF2 with 600,000 iterations takes ~200–500ms depending on CPU speed. Running this on every single database connection routing would degrade API performance.
   * To match Java's thread-safe volatile key caching, we must store the derived key in-memory (e.g. using a global cache variable `_derived_key_cache`) after the first derivation.

## 3. Caveats
* **Cache Invalidation**: The derived key cache is initialized once from the `ENCRYPTION_KEY` environment variable. If the environment variable changes dynamically at runtime (which is rare), the application would need a restart to reflect the new key. This matches Java's behavior.
* **Base64 Padding / Encoding**: Standard Base64 encoding is assumed. If base64 strings with url-safe encoding (`urlsafe_b64decode`) are introduced, standard base64 decoding might fail unless specifically handled or kept standard. The Java equivalent uses standard `Base64.getDecoder()`, so standard Python `base64.b64decode` is the correct match.

## 4. Conclusion
We recommend proceeding with implementation. The Python `cryptography` library is fully compatible with Java's crypto implementation using the following layout:

### Step 1: Add Dependency to `pyproject.toml`
Add `cryptography = "^49.0.0"` to the `[tool.poetry.dependencies]` block in `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/pyproject.toml`.

### Step 2: Implement Decryption Service
Create `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/services/encryption.py` with:
```python
import os
import base64
from typing import Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# Fixed Salt used by Medflow Java equivalent
SALT = b"MedFlowCRM-EncryptionSalt-2024"
ITERATIONS = 600_000
KEY_LENGTH = 32  # 32 bytes = 256 bits

# Thread-safe in-memory cache for the derived key
_derived_key_cache: Optional[bytes] = None


def get_encryption_key() -> bytes:
    """
    Derives the AES-256 key from the ENCRYPTION_KEY environment variable.
    Caches the derived key in memory to avoid repeating the computationally
    expensive PBKDF2 operation (600,000 iterations).
    """
    global _derived_key_cache
    if _derived_key_cache is not None:
        return _derived_key_cache

    passphrase = os.getenv("ENCRYPTION_KEY")
    if not passphrase:
        raise ValueError("The ENCRYPTION_KEY environment variable is not set or empty")

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LENGTH,
        salt=SALT,
        iterations=ITERATIONS,
    )
    _derived_key_cache = kdf.derive(passphrase.encode("utf-8"))
    return _derived_key_cache


def decrypt_data(encrypted_data_base64: str) -> str:
    """
    Decrypts a Base64 encoded string using AES-256-GCM with PBKDF2 key derivation.
    Expects the decoded format: IV (12 bytes) + Ciphertext + GCM Authentication Tag (16 bytes).
    
    Args:
        encrypted_data_base64: Base64-encoded ciphertext payload.
        
    Returns:
        The decrypted plaintext as a UTF-8 string.
        
    Raises:
        ValueError: If the input is invalid or decryption fails.
    """
    if not encrypted_data_base64:
        raise ValueError("Encrypted data string cannot be empty")

    try:
        combined = base64.b64decode(encrypted_data_base64)
    except Exception as e:
        raise ValueError(f"Invalid Base64 format: {e}")

    if len(combined) < 28:  # 12 bytes IV + 16 bytes minimum for ciphertext/tag
        raise ValueError("Encrypted data payload is too short")

    iv = combined[:12]
    ciphertext_with_tag = combined[12:]

    key = get_encryption_key()
    aesgcm = AESGCM(key)

    try:
        decrypted_bytes = aesgcm.decrypt(iv, ciphertext_with_tag, None)
        return decrypted_bytes.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}")
```

### Step 3: Implement Unit Tests
Create `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/tests/test_encryption.py` with:
```python
import os
import base64
import pytest
from app.services.encryption import decrypt_data, get_encryption_key, SALT, ITERATIONS, KEY_LENGTH
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

def _encrypt_helper(plain_text: str, passphrase: str, iv: bytes = None) -> str:
    """Helper to encrypt data for tests using same rules."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LENGTH,
        salt=SALT,
        iterations=ITERATIONS,
    )
    key = kdf.derive(passphrase.encode("utf-8"))
    aesgcm = AESGCM(key)
    if iv is None:
        iv = os.urandom(12)
    ciphertext = aesgcm.encrypt(iv, plain_text.encode("utf-8"), None)
    return base64.b64encode(iv + ciphertext).decode("utf-8")


def test_decrypt_static_test_vector(monkeypatch):
    """
    Verify decryption against a pre-calculated test vector.
    """
    passphrase = "MedFlowTestPassphrase2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    # Pre-calculated test vector using 12-byte zero IV
    # Plaintext: "postgresql+asyncpg://tenant_user:secure_pass@localhost:5432/tenant_db"
    test_vector_b64 = "AAAAAAAAAAAAAAAAp+hta4MS9M+Lnj/RYGwaDfToa9l9xvszRzJ3el9yYqCsdcuzQ3z9CdqhiPfY7LCmEakLfONyF7mO1yhc31VGfzDZ71/NVM+Op//KaQu3usSv9ZWsww=="
    
    import app.services.encryption as enc
    monkeypatch.setattr(enc, "_derived_key_cache", None)
    
    decrypted = decrypt_data(test_vector_b64)
    assert decrypted == "postgresql+asyncpg://tenant_user:secure_pass@localhost:5432/tenant_db"


def test_decrypt_roundtrip(monkeypatch):
    """
    Verify that encrypting and then decrypting yields the original value.
    """
    passphrase = "DynamicSecretPassphrase456"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    import app.services.encryption as enc
    monkeypatch.setattr(enc, "_derived_key_cache", None)
    
    original_text = "postgresql+asyncpg://some_user:some_pass@remote_host:5432/tenant_db_1"
    encrypted_b64 = _encrypt_helper(original_text, passphrase)
    
    decrypted = decrypt_data(encrypted_b64)
    assert decrypted == original_text


def test_missing_encryption_key_env(monkeypatch):
    """
    Verify that a ValueError is raised when ENCRYPTION_KEY environment variable is not set.
    """
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    
    import app.services.encryption as enc
    monkeypatch.setattr(enc, "_derived_key_cache", None)
    
    with pytest.raises(ValueError) as exc_info:
        decrypt_data("AAAAAAAAAAAAAAAAp+hta4MS9M+Lnj/RYGwaDfToa9l9xvszRzJ3el9yYqCsdcuzQ3z9CdqhiPfY7LCmEakLfONyF7mO1yhc31VGfzDZ71/NVM+Op//KaQu3usSv9ZWsww==")
    
    assert "ENCRYPTION_KEY environment variable is not set" in str(exc_info.value)


def test_invalid_base64_input(monkeypatch):
    """
    Verify that decoding invalid Base64 raises a ValueError.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", "MedFlowTestPassphrase2026")
    
    import app.services.encryption as enc
    monkeypatch.setattr(enc, "_derived_key_cache", None)
    
    with pytest.raises(ValueError) as exc_info:
        decrypt_data("not-valid-base64-string!!!")
        
    assert "Invalid Base64 format" in str(exc_info.value)


def test_too_short_payload(monkeypatch):
    """
    Verify that payload shorter than 28 bytes raises a ValueError.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", "MedFlowTestPassphrase2026")
    
    import app.services.encryption as enc
    monkeypatch.setattr(enc, "_derived_key_cache", None)
    
    short_payload = base64.b64encode(b"short").decode("utf-8")
    
    with pytest.raises(ValueError) as exc_info:
        decrypt_data(short_payload)
        
    assert "Encrypted data payload is too short" in str(exc_info.value)


def test_corrupted_tag(monkeypatch):
    """
    Verify that altering ciphertext/tag raises decryption failure.
    """
    passphrase = "MedFlowTestPassphrase2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    import app.services.encryption as enc
    monkeypatch.setattr(enc, "_derived_key_cache", None)
    
    original_text = "secret_string"
    encrypted_b64 = _encrypt_helper(original_text, passphrase)
    
    # Corrupt a byte in the encrypted string
    decoded = bytearray(base64.b64decode(encrypted_b64))
    decoded[-1] ^= 0xFF  # Flip bits in the last byte (auth tag)
    corrupted_b64 = base64.b64encode(decoded).decode("utf-8")
    
    with pytest.raises(ValueError) as exc_info:
        decrypt_data(corrupted_b64)
        
    assert "Decryption failed" in str(exc_info.value)
```

## 5. Verification Method
To verify the implementation once coded:
1. Run `poetry install` to ensure any updated dependencies in `pyproject.toml` are correctly installed.
2. Set up a local test run environment and execute `poetry run pytest tests/test_encryption.py`.
3. Check that all 7 test cases pass successfully.
4. Verify the performance latency: running the test suite should execute quickly after the initial PBKDF2 hash derivation due to caching.
