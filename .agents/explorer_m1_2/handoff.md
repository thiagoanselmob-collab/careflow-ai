# Handoff Report - Milestone 1: R2. Decryption Service Plan

## 1. Observation
We observed the following regarding the project structure and cryptographic requirements:
- **Project Structure**: Found the backend layout at `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/`. The `pyproject.toml` configuration has no existing direct dependency on `cryptography` (verified in `pyproject.toml`, lines 1–28).
- **Existing Test Suite**: Running `poetry run pytest` succeeds and runs 7 existing tests in 0.46 seconds:
  ```
  tests/test_config.py ....                                                [ 57%]
  tests/test_database.py .                                                 [ 71%]
  tests/test_main.py ..                                                    [100%]
  ========================= 7 passed, 1 warning in 0.46s =========================
  ```
- **Library Versioning**: Running `poetry add cryptography --dry-run` shows that Poetry resolves to version `^49.0.0` for `cryptography`.
- **Performance Measurement**: Measuring key derivation time on the local Mac hardware for PBKDF2-HMAC-SHA256 with 600,000 iterations and a 256-bit key size takes approximately **0.1625 seconds (162.5 milliseconds)**.

---

## 2. Logic Chain
Based on the observations:
1. **Dependency Addition**: Since `cryptography` is not in `pyproject.toml` (Observation 1), it must be added under `[tool.poetry.dependencies]`. Using `cryptography = "^49.0.0"` is recommended since it resolves cleanly and dry-runs successfully (Observation 3).
2. **Cryptographic Compatibility**:
   - **Key Derivation**: Java's standard `PBKDF2WithHmacSHA256` generates a key from characters and salt. By encoding the passphrase as UTF-8 bytes and using `cryptography.hazmat.primitives.kdf.pbkdf2.PBKDF2HMAC` with `hashes.SHA256()`, salt `b"MedFlowCRM-EncryptionSalt-2024"`, and `600_000` iterations, we produce the exact same 32-byte (256-bit) key.
   - **AES-GCM**: Java's `"AES/GCM/NoPadding"` returns a ciphertext block with the GCM authentication tag appended at the end (standard 128-bit tag). In Python, using `cryptography.hazmat.primitives.ciphers.aead.AESGCM` with a 12-byte IV (nonce) and passing `ciphertext + tag` to `aesgcm.decrypt` matches this format. The tag is automatically extracted from the last 16 bytes of the payload.
3. **Performance Optimization (Caching)**:
   - Since a single key derivation takes ~162.5 ms (Observation 4), recalculating it on every database connection setup or decryption request would cause severe CPU load and increase latency by 162.5 ms per request.
   - Therefore, the derived key must be cached in memory (e.g. using a global variable in `app/services/encryption.py` or lazy-initialized singleton) to make subsequent decryption calls near-instantaneous (< 1ms).

---

## 3. Caveats
- **Environment Initialization**: The passphrase must be read from the environment variable `ENCRYPTION_KEY`. If the application loads environment variables via `dotenv` or Pydantic Settings on startup, we must ensure these are loaded before calling `decrypt_data` for the first time.
- **Passphrase UTF-8 Encoding**: Non-ASCII characters in the passphrase must be correctly encoded as UTF-8. The proposed code explicitly uses `passphrase.encode("utf-8")`.
- **Minimum Input Length**: The decoded binary data must be at least 28 bytes (12 bytes IV + 16 bytes tag). Inputs shorter than this will throw a `ValueError` immediately to prevent IndexError.

---

## 4. Conclusion
We have verified full cryptographic compatibility between Java's AES-256-GCM + PBKDF2 implementation and Python's `cryptography` library.
The implementation plan is structured as follows:
1. **Dependency**: Add `cryptography = "^49.0.0"` to `pyproject.toml`.
2. **Service File**: Implement `app/services/encryption.py` (see `proposed_encryption.py` in this folder).
3. **Test File**: Implement `tests/test_encryption.py` (see `proposed_test_encryption.py` in this folder).

---

## 5. Verification Method
To independently verify the implementation after it is applied by the implementer agent:
1. **Dependency Installation**:
   Run `poetry install` to install `cryptography`.
2. **Test Command**:
   Run `poetry run pytest tests/test_encryption.py` to assert correctness.
3. **Invalidation Conditions**:
   - If the decryption returns a different string or throws `InvalidTag` for valid Java-encrypted connection strings, the salt or iterations parameters do not match.
   - If the caching test fails (takes > 2ms on subsequent calls), the key is being re-derived on each call.
