# Project: Dynamic Multi-tenant PostgreSQL Connectors

## Socratic Decisions
1. **Passphrase Encoding**: `ENCRYPTION_KEY` is treated as a raw UTF-8 string passphrase.
2. **Settings Table Schema**: Columns are `organization_id` (String/VARCHAR) and `tenant_connection_string` (String/Text).
3. **Tenant Cache Eviction**: Active connection pools are cached indefinitely in-memory until application shutdown.

## Architecture
The application dynamically routes database queries to tenant-specific databases based on the `organization_id` header or context.
1. **Central Database**: Holds a `settings` table storing encrypted `tenant_connection_string` values for each organization.
2. **Encryption/Decryption**: Python implementation of PBKDF2/AES-256-GCM matching Medflow's Java equivalent. Decrypts `tenant_connection_string`.
3. **Tenant Connection Manager**: Automatically spins up, caches, routes to, and cleans up async database engines and connection pools for each tenant using SQLAlchemy + asyncpg.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | R2. Decryption Service | Implement `app/services/encryption.py` and `tests/test_encryption.py` | none | DONE |
| 2 | R1. Medflow Central DB Config | Configure async connection pool in `app/core/database.py` and settings model | none | DONE |
| 3 | R3. Tenant Connection Manager | Implement `app/core/tenant_database.py` and `tests/test_tenant_database.py` | M1, M2 | DONE |
| 4 | E2E & Code Integration | Ensure all tests pass, clean shutdown works, and formatting/lint is clean | M3 | DONE |

## Interface Contracts
### `app/services/encryption.py`
- `def decrypt_data(encrypted_data_base64: str) -> str`: Decrypts a Base64 encoded string using AES-256-GCM with PBKDF2 key derivation.

### `app/core/tenant_database.py`
- `class TenantConnectionManager`:
  - `async def get_tenant_session(organization_id: str) -> AsyncSession`: Returns an isolated session for a tenant database.
  - `async def shutdown_all_pools()`: Closes all cached engines and connection pools cleanly.
