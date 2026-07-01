import os
import base64
from typing import Optional
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

# Cache for the derived key to avoid repeating the expensive PBKDF2 (600,000 iterations)
# on every decryption request.
_CACHED_DERIVED_KEY: Optional[bytes] = None

# Salt specified for compatibility with Medflow Java equivalent
SALT = b"MedFlowCRM-EncryptionSalt-2024"
ITERATIONS = 600000
KEY_SIZE_BYTES = 32  # 256 bits

def get_derived_key() -> bytes:
    """
    Retrieves or derives the 256-bit AES key using PBKDF2 from the ENCRYPTION_KEY environment variable.
    Caches the derived key to optimize performance.
    """
    global _CACHED_DERIVED_KEY
    if _CACHED_DERIVED_KEY is not None:
        return _CACHED_DERIVED_KEY

    passphrase = os.environ.get("ENCRYPTION_KEY")
    if not passphrase:
        raise ValueError("ENCRYPTION_KEY environment variable is not set or empty")

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE_BYTES,
        salt=SALT,
        iterations=ITERATIONS,
    )
    
    _CACHED_DERIVED_KEY = kdf.derive(passphrase.encode("utf-8"))
    return _CACHED_DERIVED_KEY

def decrypt_data(encrypted_data_base64: str) -> str:
    """
    Decrypts a Base64-encoded string using AES-256-GCM.
    
    Expected format of decoded bytes:
    IV (12 bytes) + Ciphertext (including trailing 16-byte GCM tag)
    
    Args:
        encrypted_data_base64: The Base64 encoded ciphertext string.
        
    Returns:
        The decrypted UTF-8 plaintext string.
        
    Raises:
        ValueError: If decryption fails, the key is invalid, or ciphertext format is incorrect.
    """
    if not encrypted_data_base64:
        raise ValueError("Encrypted data input cannot be empty")

    try:
        raw_bytes = base64.b64decode(encrypted_data_base64)
    except Exception as e:
        raise ValueError(f"Failed to decode Base64 data: {e}")

    # AES-GCM standard nonce is 12 bytes; tag is 16 bytes.
    # Minimum valid length is 12 (IV) + 16 (Tag) = 28 bytes.
    if len(raw_bytes) < 28:
        raise ValueError("Encrypted data is too short to contain valid IV and GCM tag")

    iv = raw_bytes[:12]
    ciphertext_with_tag = raw_bytes[12:]

    try:
        key = get_derived_key()
    except Exception as e:
        raise ValueError(f"Key derivation failed: {e}")

    try:
        aesgcm = AESGCM(key)
        # Decrypt ciphertext and authenticate with GCM tag
        decrypted_bytes = aesgcm.decrypt(iv, ciphertext_with_tag, None)
        return decrypted_bytes.decode("utf-8")
    except InvalidTag:
        raise ValueError("Decryption failed: Invalid tag (data might be tampered or key is incorrect)")
    except Exception as e:
        raise ValueError(f"Decryption failed due to unexpected error: {e}")
