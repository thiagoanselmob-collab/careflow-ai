import base64
import functools
import os
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# Constants matching the Medflow Java encryption/decryption configuration
SALT = b"MedFlowCRM-EncryptionSalt-2024"
ITERATIONS = 600_000
KEY_LENGTH = 32  # 256 bits (32 bytes)
IV_LENGTH = 12   # 12 bytes

@functools.lru_cache(maxsize=32)
def derive_key(passphrase: str) -> bytes:
    """
    Derives a 256-bit AES key from a passphrase using PBKDF2 with HMAC-SHA256.
    Uses UTF-8 encoding for the passphrase, salt 'MedFlowCRM-EncryptionSalt-2024',
    and 600,000 iterations.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LENGTH,
        salt=SALT,
        iterations=ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))

def decrypt_data(encrypted_data_base64: str) -> str:
    """
    Decrypts a Base64-encoded ciphertext.
    Decoded payload structure: IV (12 bytes) + Ciphertext + GCM Authentication Tag (16 bytes).
    Algorithm: AES-256-GCM.
    Passphrase is read from the ENCRYPTION_KEY environment variable.
    
    Raises:
        ValueError: If ENCRYPTION_KEY is unset, the payload format is invalid,
                    or decryption/authentication fails.
    """
    passphrase = os.getenv("ENCRYPTION_KEY")
    if not passphrase:
        raise ValueError("ENCRYPTION_KEY environment variable is not set")

    try:
        # Decode Base64 string to bytes
        raw_data = base64.b64decode(encrypted_data_base64, validate=True)
    except Exception as e:
        raise ValueError(f"Invalid Base64 input: {str(e)}")

    if len(raw_data) < IV_LENGTH:
        raise ValueError("Invalid encrypted payload: too short to contain 12-byte IV")

    # Extract 12-byte IV and the rest (ciphertext + 16-byte GCM tag)
    iv = raw_data[:IV_LENGTH]
    ciphertext_with_tag = raw_data[IV_LENGTH:]

    # Derive AES-256 key from passphrase
    key = derive_key(passphrase)

    # Initialize AES-GCM and decrypt
    aesgcm = AESGCM(key)
    try:
        decrypted_bytes = aesgcm.decrypt(iv, ciphertext_with_tag, None)
    except InvalidTag:
        raise ValueError("Decryption failed: Incorrect passphrase or tampered ciphertext")

    # Decode result to UTF-8 string
    try:
        return decrypted_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Decryption succeeded but content is not valid UTF-8")
