import os
import base64
import time
import asyncio
import pytest
from app.services.encryption import decrypt_data, derive_key
from tests.test_encryption import encrypt_helper

def test_kdf_performance(monkeypatch):
    """
    Measure performance of KDF derivation and decryption.
    """
    passphrase = "stress-test-passphrase-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    plaintext = "Hello, world! This is a test of the encryption performance."
    encrypted_b64 = encrypt_helper(plaintext, passphrase)
    
    # Clear the cache to ensure clean benchmark
    derive_key.cache_clear()
    
    # Measure time for a single decryption (uncached)
    start_time = time.perf_counter()
    decrypted = decrypt_data(encrypted_b64)
    end_time = time.perf_counter()
    
    elapsed = end_time - start_time
    print(f"\nSingle decryption took: {elapsed:.4f} seconds")
    assert decrypted == plaintext
    
    # Measure time for 10 decryptions (cached)
    start_time_10 = time.perf_counter()
    for _ in range(10):
        decrypt_data(encrypted_b64)
    end_time_10 = time.perf_counter()
    elapsed_10 = end_time_10 - start_time_10
    print(f"10 decryptions took: {elapsed_10:.4f} seconds")
    
    # With cache, 10 cached decryptions should be extremely fast (usually < 5ms)
    assert elapsed_10 < 0.05

@pytest.mark.asyncio
async def test_async_event_loop_blocking(monkeypatch):
    """
    Verify if decrypt_data blocks the event loop when run concurrently with other tasks.
    """
    passphrase = "stress-test-passphrase-2026-blocking"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    encrypted_b64 = encrypt_helper("some-secret-data", passphrase)
    
    # Clear cache to force key derivation and therefore event loop blocking
    derive_key.cache_clear()
    
    # We will run a background async task that sleeps for 10ms repeatedly.
    # If the event loop is blocked, this task will experience significant latency.
    latencies = []
    stop_event = asyncio.Event()
    
    async def heartbeat():
        while not stop_event.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            t1 = time.perf_counter()
            latencies.append(t1 - t0 - 0.01)
            
    heartbeat_task = asyncio.create_task(heartbeat())
    
    # Wait for heartbeat to start
    await asyncio.sleep(0.05)
    
    # Run decryption (uncached, should block)
    t_start = time.perf_counter()
    decrypt_data(encrypted_b64)
    t_end = time.perf_counter()
    
    stop_event.set()
    await heartbeat_task
    
    max_latency = max(latencies) if latencies else 0
    print(f"\nDecryption time: {t_end - t_start:.4f} seconds")
    print(f"Max heartbeat latency (event loop block): {max_latency:.4f} seconds")
    
    # If max_latency is close to the decryption time, it proves the event loop was blocked.
    assert max_latency > 0.03

def test_extreme_passphrases(monkeypatch):
    """
    Test edge cases with unusual passphrases (e.g. very long, empty, non-ASCII/Unicode).
    """
    # 1. Unicode/Emoji passphrase
    passphrase_emoji = "🔑MedFlow-🔐-🌟-2026-Café"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase_emoji)
    plaintext = "Sensitive medical record data"
    
    enc_emoji = encrypt_helper(plaintext, passphrase_emoji)
    assert decrypt_data(enc_emoji) == plaintext
    
    # 2. Extremely long passphrase (e.g. 10 KB)
    passphrase_long = "a" * 10000
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase_long)
    enc_long = encrypt_helper(plaintext, passphrase_long)
    assert decrypt_data(enc_long) == plaintext

def test_decrypt_payload_intermediate_length(monkeypatch):
    """
    Ensure behavior when payload length is between IV length (12) and minimum valid GCM payload (28).
    """
    monkeypatch.setenv("ENCRYPTION_KEY", "any-key")
    
    # 20 bytes payload (Base64 encoded)
    # 20 bytes is > 12 (IV length) but < 28 (IV + Tag)
    payload_20_bytes = base64.b64encode(b"A" * 20).decode("utf-8")
    
    # Let's see if this raises a ValueError (InvalidTag or other)
    with pytest.raises(ValueError) as excinfo:
        decrypt_data(payload_20_bytes)
    print(f"\nIntermediate length error: {excinfo.value}")
    # The current implementation raises: "Decryption failed: Incorrect passphrase or tampered ciphertext"
    # or does it raise another error? Let's check.
