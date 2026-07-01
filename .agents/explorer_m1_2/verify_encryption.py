import os
import base64
import time
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def derive_key(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"MedFlowCRM-EncryptionSalt-2024",
        iterations=600000,
    )
    return kdf.derive(passphrase.encode("utf-8"))

if __name__ == "__main__":
    passphrase = "super-secret-key-123!"
    
    t0 = time.perf_counter()
    key = derive_key(passphrase)
    t1 = time.perf_counter()
    
    print(f"PBKDF2 key derivation took {t1 - t0:.4f} seconds.")
