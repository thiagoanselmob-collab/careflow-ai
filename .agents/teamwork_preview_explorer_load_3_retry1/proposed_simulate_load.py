# Webhook Load Simulation Analysis & Design Report

## Executive Summary
This report analyzes the WhatsApp webhook endpoint (`POST /api/v1/webhook/whatsapp`), its database buffering, Redis-backed debounce mechanisms, and concurrency control. It identifies crucial verification gaps in the existing `scripts/simulate_load.py` implementation—specifically, the lack of intermediate database buffering verification during the debounce window and missing failure assertions (exit codes, percentiles)—and proposes a robust, enhanced design with a complete proposed script implementation.

---

## 1. Webhook, Debounce, and Database Architecture Review

The system implements a decoupled, event-driven pattern to process high-throughput WhatsApp messages under 500ms while maintaining message order and preventing agent execution overload.

### A. Webhook Endpoint (`POST /api/v1/webhook/whatsapp`)
- **Routing**: Accepts request parameter `organization_id` (representing the tenant).
- **Latency Control**: Immediately responds with `{"status": "queued"}` (HTTP 200) without waiting for processing, ensuring response times remain well below the 500ms SLA.
- **Workflow**:
  1. Inspects payload to ignore status updates (nested or flat `"statuses"`).
  2. Detects human takeover or outgoing messages if `"fromMe"` is True, updating the client status to `ATENDIMENTO_HUMANO` and turning off the AI bot.
  3. Inserts the message raw content into the tenant's `message_buffer` table.
  4. Stores the timestamp of the message in Redis under `last_msg_time:{organization_id}:{phone_number}`.
  5. Offloads message processing asynchronously to FastAPI `BackgroundTasks` via `process_message_debounce`.

### B. Redis-Backed Resettable Debounce & Concurrency Locks
- **Timer Reset**: The background task `process_message_debounce` sleeps for `settings.debounce_seconds` (default: 30.0s). Upon waking, it compares `current_time` against the Redis `last_msg_time` key. If another message arrived during the sleep, the elapsed time is less than `debounce_seconds`, and the task exits silently. This effectively pushes the debounce window forward.
- **Concurrency Mutex**: To prevent multiple threads/tasks from processing the same phone number concurrently, the system tries to acquire a Redis lock `lock_key = f"{organization_id}:{phone_number}:lock"`. It retries 5 times (0.1s apart) to handle transient lock-release windows.
- **Orphan Prevention Loop**: Once the lock is acquired, the task runs in a `while True` loop to fetch and delete buffered messages in a single database transaction. This ensures that any messages arriving *during* the execution of the LangGraph agent are caught and processed in subsequent loops before the lock is released.

### C. Database Structure & Tenant Isolation
- **Tenant Connection Cache**: `TenantConnectionManager` queries the central `settings` database, retrieves and decrypts the encrypted tenant connection string (`tenant_connection_string`), and caches the dynamic engine/sessionmaker.
- **Tenant Schemas**:
  - `message_buffer`:
    - `id`: Autoincrement primary key.
    - `phone_number`: String (50) representing sender.
    - `content`: Text content of the message.
    - `created_at`: Time of receipt.
  - `dados_cliente`:
    - `phone_number`: Primary key.
    - `status`: String, defaults to `EM_CONTATO`.
    - `created_at`: Registration timestamp.

---

## 2. Gaps in the Existing `scripts/simulate_load.py`

While a basic script exists, it suffers from several limitations that reduce its effectiveness for load simulation and verification:

1. **No Intermediate Buffering Verification**:
   - The existing script only verifies the database *after* waiting for the debounce duration (T = 32s).
   - If the debounce mechanism were broken (e.g. processing messages immediately), the final database check would still show `buffer_count = 0` and the test would mistakenly pass.
   - **Solution**: The script must query the database *during* the debounce window (e.g., at T = 10s) to verify that all rapid/fragmented messages are accumulated in `message_buffer` and have *not* been deleted or processed yet.

2. **No Strict Response Time Assertions**:
   - The script prints the average latency but does not assert that *every* request was processed in `< 500ms`.
   - Under concurrency, average latency can hide slow requests (long-tail latency).
   - **Solution**: Compute and display percentiles (P50, P95, P99) and explicitly check that individual latencies do not exceed 500ms.

3. **No Exit Code Integration**:
   - The script outputs visual console lines (`✅`, `❌`) but always exits with `0`, making it useless for CI/CD pipeline automation.
   - **Solution**: Track failures and exit with `sys.exit(1)` if response times exceed 500ms, HTTP responses fail, or database verification fails.

---

## 3. Proposed Script Design

The proposed script structure implements a **double-verification state flow**:

```
[Start Test]
     │
     ├── Step 1: Fire concurrent bursts (10 numbers, 3 msgs each, 0.5s interval)
     │           Measure latencies using time.perf_counter()
     │
     ├── Step 2: Validate API Response Times
     │           Ensure 100% of calls are HTTP 200 and latency < 500ms
     │
     ├── Step 3: Mid-Debounce Database Verification (Wait 5s after burst)
     │           Query message_buffer: Ensure exactly 30 messages exist
     │           (Verifies that messages are buffering, not processed immediately)
     │
     ├── Step 4: Wait for Debounce Expiry (Wait remaining ~30s)
     │
     └── Step 5: Post-Debounce Database Verification
                 Query message_buffer: Ensure it is empty (0 messages)
                 Query dados_cliente: Ensure all 10 phone numbers are registered
```

### Proposed `scripts/simulate_load.py` Implementation

Below is the proposed design code. It includes proper decryption support, dynamic tenant engine routing, telemetry percentiles, intermediate database state verification, and process exit codes.

```python
#!/usr/bin/env python3
"""
scripts/simulate_load.py

Simulates concurrent WhatsApp webhook load, measures response times, 
and verifies database buffering and debounce aggregation.
"""

import asyncio
import time
import sys
import os
import argparse
import numpy as np
from typing import List, Dict, Any
import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Ensure the root directory is in the path to allow imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings
from app.services.encryption import decrypt_data
from app.models.settings import Settings


async def send_webhook(
    client: httpx.AsyncClient,
    base_url: str,
    tenant_id: str,
    phone: str,
    content: str
) -> Dict[str, Any]:
    """
    Sends a single webhook request and returns details (status, latency).
    """
    url = f"{base_url.rstrip('/')}/api/v1/webhook/whatsapp"
    params = {"organization_id": tenant_id}
    payload = {
        "phone_number": phone,
        "content": content
    }
    
    start_time = time.perf_counter()
    try:
        response = await client.post(url, params=params, json=payload, timeout=10.0)
        latency = time.perf_counter() - start_time
        return {
            "success": response.status_code == 200,
            "status_code": response.status_code,
            "latency": latency,
            "error": None if response.status_code == 200 else response.text
        }
    except Exception as e:
        latency = time.perf_counter() - start_time
        return {
            "success": False,
            "status_code": 0,
            "latency": latency,
            "error": str(e)
        }


async def simulate_phone_load(
    client: httpx.AsyncClient,
    base_url: str,
    tenant_id: str,
    phone: str,
    num_messages: int
) -> List[Dict[str, Any]]:
    """
    Simulates rapid messages sent by a single phone number with an interval of 0.5s.
    """
    results = []
    for i in range(1, num_messages + 1):
        content = f"Fragmented message {i} from {phone}"
        res = await send_webhook(client, base_url, tenant_id, phone, content)
        results.append(res)
        if i < num_messages:
            await asyncio.sleep(0.5)
    return results


async def run_load(
    base_url: str,
    tenant_id: str,
    num_phones: int,
    num_messages: int
) -> List[Dict[str, Any]]:
    """
    Simulates load across multiple phone numbers concurrently.
    """
    async with httpx.AsyncClient() as client:
        tasks = []
        for i in range(1, num_phones + 1):
            phone = f"+55119000000{i:02d}"
            tasks.append(simulate_phone_load(client, base_url, tenant_id, phone, num_messages))
        
        flat_results = await asyncio.gather(*tasks)
        # Flatten results
        return [res for phone_res in flat_results for res in phone_res]


async def get_tenant_engine(tenant_id: str):
    """
    Decrypts the tenant connection string from the central settings and returns the engine.
    """
    engine = create_async_engine(settings.database_url, echo=False, future=True)
    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    
    try:
        async with session_maker() as session:
            stmt = select(Settings).where(Settings.organization_id == tenant_id)
            res = await session.execute(stmt)
            setting = res.scalar_one_or_none()
            if not setting:
                raise ValueError(f"Tenant configuration not found for organization: {tenant_id}")
            encrypted_conn_str = setting.tenant_connection_string
            
        decrypted_conn_str = decrypt_data(encrypted_conn_str)
        if decrypted_conn_str.startswith("postgresql://"):
            decrypted_conn_str = decrypted_conn_str.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif decrypted_conn_str.startswith("postgres://"):
            decrypted_conn_str = decrypted_conn_str.replace("postgres://", "postgresql+asyncpg://", 1)
        elif decrypted_conn_str.startswith("sqlite://"):
            decrypted_conn_str = decrypted_conn_str.replace("sqlite://", "sqlite+aiosqlite://", 1)
            
        return create_async_engine(decrypted_conn_str, echo=False, future=True)
    finally:
        await engine.dispose()


async def check_database_state(tenant_engine) -> Dict[str, Any]:
    """
    Queries the database directly to inspect the count of buffer rows and clients.
    """
    async with tenant_engine.connect() as conn:
        buf_res = await conn.execute(text("SELECT COUNT(*) FROM message_buffer"))
        buffer_count = buf_res.scalar() or 0
        
        clients_res = await conn.execute(text("SELECT phone_number FROM dados_cliente"))
        clients = [row[0] for row in clients_res.fetchall()]
        
    return {
        "buffer_count": buffer_count,
        "clients": clients
    }


async def main():
    parser = argparse.ArgumentParser(description="Simulate WhatsApp Webhook Concurrency and verify Debounce consolidation")
    parser.add_argument("--url", default="http://localhost:8000", help="FastAPI Server Base URL")
    parser.add_argument("--tenant", default="org_debug", help="Tenant ID to target")
    parser.add_argument("--phones", type=int, default=10, help="Number of concurrent phone senders")
    parser.add_argument("--messages", type=int, default=3, help="Number of messages per phone sender")
    args = parser.parse_args()

    # Validate that encryption key is set
    if not os.getenv("ENCRYPTION_KEY"):
        print("[CRITICAL] ENCRYPTION_KEY environment variable is not set. Database verification will fail.")
        sys.exit(1)

    print(f"=== Starting WhatsApp Webhook Load Simulation ===")
    print(f"Server Target   : {args.url}")
    print(f"Tenant Target   : {args.tenant}")
    print(f"Concurrency     : {args.phones} phone numbers")
    print(f"Rate Limit      : {args.messages} messages/phone (0.5s interval)")
    print(f"Total Webhooks  : {args.phones * args.messages}")
    print(f"SLA SLA Limit   : < 500ms per request")
    
    # 1. Execute Webhook load
    print("\nSending concurrent webhook bursts...")
    start_time = time.perf_counter()
    results = await run_load(args.url, args.tenant, args.phones, args.messages)
    end_time = time.perf_counter()
    
    total_duration = end_time - start_time
    total_requests = len(results)
    
    # Parse latencies and status codes
    latencies_ms = [r["latency"] * 1000 for r in results]
    failures = [r for r in results if not r["success"]]
    slow_requests = [l for l in latencies_ms if l > 500]
    
    print("\n=== Webhook Telemetry Report ===")
    print(f"Total Requests  : {total_requests}")
    print(f"Success Count   : {total_requests - len(failures)}")
    print(f"Failure Count   : {len(failures)}")
    print(f"Total Duration  : {total_duration:.2f}s")
    
    if latencies_ms:
        print(f"Min Latency     : {min(latencies_ms):.2f}ms")
        print(f"Mean Latency    : {np.mean(latencies_ms):.2f}ms")
        print(f"Median Latency  : {np.median(latencies_ms):.2f}ms")
        print(f"P95 Latency     : {np.percentile(latencies_ms, 95):.2f}ms")
        print(f"P99 Latency     : {np.percentile(latencies_ms, 99):.2f}ms")
        print(f"Max Latency     : {max(latencies_ms):.2f}ms")
    
    # Validate API assertions
    has_api_failures = False
    if failures:
        print("\n❌ Webhook requests failed:")
        for idx, f in enumerate(failures[:5]):
            print(f"   Error {idx+1}: {f['error']} (Status Code: {f['status_code']})")
        has_api_failures = True
        
    if slow_requests:
        print(f"\n❌ SLA Violation: {len(slow_requests)} requests exceeded the 500ms latency budget.")
        has_api_failures = True
    else:
        print("\n✅ SLA Confirmed: 100% of webhook requests returned in < 500ms.")

    # 2. Database Verification
    print("\nInitializing Tenant Database connection pool...")
    try:
        tenant_engine = await get_tenant_engine(args.tenant)
    except Exception as e:
        print(f"[CRITICAL] Failed to connect or decrypt tenant database: {e}")
        sys.exit(1)
        
    db_failed = False
    try:
        # Phase 1: Verify intermediate buffering state (during 30-second debounce window)
        print("\n=== Phase 1: Mid-Debounce Verification ===")
        print("Sleeping 5 seconds to allow requests to settle, but checking before 30s expiry...")
        await asyncio.sleep(5.0)
        
        state = await check_database_state(tenant_engine)
        expected_buffered = args.phones * args.messages
        print(f"Buffered messages in database : {state['buffer_count']} (Expected: {expected_buffered})")
        
        if state['buffer_count'] == expected_buffered:
            print("✅ Webhook correctly buffered all messages in database during the debounce window.")
        else:
            print("❌ Failure: Messages were not fully buffered or were consumed prematurely.")
            db_failed = True
            
        # Phase 2: Verify post-debounce state (after 30s)
        # We need to wait for the last message's debounce timer (30s) to run.
        # Total time since last message was sent (t=1s if 3 messages) is about 6s + 26s sleep.
        remaining_wait = 32.0 - 5.0
        print(f"\n=== Phase 2: Post-Debounce Verification ===")
        print(f"Sleeping {remaining_wait}s for debounce window to expire and process...")
        await asyncio.sleep(remaining_wait)
        
        state = await check_database_state(tenant_engine)
        print(f"Buffered messages in database : {state['buffer_count']} (Expected: 0)")
        
        expected_phones = {f"+5511900000{i:02d}" for i in range(1, args.phones + 1)}
        registered_phones = set(state['clients'])
        missing_phones = expected_phones - registered_phones
        
        print(f"Clients registered in database: {len(registered_phones)} (Expected: >= {args.phones})")
        
        if state['buffer_count'] == 0:
            print("✅ Message buffer successfully cleared after debounce execution.")
        else:
            print(f"❌ Failure: {state['buffer_count']} messages remain stuck in the database buffer.")
            db_failed = True
            
        if not missing_phones:
            print("✅ All simulated phone numbers were successfully registered in client database.")
        else:
            print(f"❌ Failure: Simulated phone numbers {missing_phones} were not registered in database.")
            db_failed = True
            
    finally:
        await tenant_engine.dispose()

    # Exit code processing
    if has_api_failures or db_failed:
        print("\n❌ LOAD TEST FAILED. Check telemetry logs above.")
        sys.exit(1)
    else:
        print("\n✅ LOAD TEST COMPLETED SUCCESSFULLY.")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
```
