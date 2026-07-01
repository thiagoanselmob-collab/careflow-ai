# Change Log - SQLite URI mode fix

## Modified Files

### `app/core/tenant_database.py`
- Refactored `TenantConnectionManager.get_engine()` around line 155 to detect SQLite connection strings.
- Added `connect_args={"uri": True}` specifically when the connection string contains `"sqlite"`, while maintaining the default behavior for other database types (e.g., PostgreSQL).
- This enables proper SQLite URI mode support, resolving the issue where query parameters like `?mode=memory&cache=shared` were ignored and caused SQLite to write physical database files matching `file:*` to the workspace root directory.

## Cleaned Files
- Deleted all physical database files starting with `file:*` in the workspace root directory. These were originally created because SQLite interpreted the URI-like connection strings as relative file paths instead of URIs.

## Verification
- Ran the test suite via `poetry run pytest`.
- Verified that all 93 tests passed (previously, `test_concurrency_debounce_aggregation` failed with an `AssertionError` due to duplicate/persisted data in the physical SQLite file).
