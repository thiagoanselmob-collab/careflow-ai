import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

SALT = b"MedFlowCRM-EncryptionSalt-2024"
ITERATIONS = 600_000
KEY_LENGTH = 32
IV_LENGTH = 12

def derive_key(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LENGTH,
        salt=SALT,
        iterations=ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))

def encrypt_data(plaintext: str, passphrase: str) -> str:
    key = derive_key(passphrase)
    # Use a fixed IV for the static test vector so it's reproducible,
    # or just use a random one and print both the IV and ciphertext.
    # In standard operation IV is random. Let's use a fixed 12-byte IV for a clean test vector.
    iv = b"123456789012"
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    combined = iv + ciphertext_with_tag
    return base64.b64encode(combined).decode("utf-8")

if __name__ == "__main__":
    passphrase = "test-secret-key"
    plaintext = "postgresql+asyncpg://postgres:postgres@localhost:5432/tenant_db"
    encrypted = encrypt_data(plaintext, passphrase)
    print(f"Passphrase: {passphrase}")
    print(f"Plaintext: {plaintext}")
    print(f"Base64 Encrypted: {encrypted}")
