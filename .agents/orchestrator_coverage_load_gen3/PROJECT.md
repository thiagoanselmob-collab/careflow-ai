# Project: Phase 5.1 (Code Coverage and Load Simulation)

## Architecture
This phase focuses on quality assurance and performance testing of the CareFlow AI backend service.
- **Code Coverage**: Pytest configuration using `pytest-cov` to monitor code coverage of the `app/` directory, ensuring it is >90%.
- **Load Simulation**: A standalone utility script (`scripts/simulate_load.py`) executing concurrent HTTP requests using `asyncio` and `httpx` to simulate multiple WhatsApp clients sending rapid, fragmented messages, validating the 30-second Redis debounce and database state.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | M1: Code Coverage Configuration and Gaps Resolution | Add `pytest-cov`, configure pytest options in `pyproject.toml`, run tests, identify coverage gaps in `app/`, implement unit tests to bridge gaps to >90% coverage. | None | IN_PROGRESS |
| 2 | M2: Load Simulation Script Development | Implement `scripts/simulate_load.py` using `asyncio`/`httpx`, verify debounce/locks logic, ensure webhook response time < 500ms, and report results. | M1 | PLANNED |

## Code Layout
- `app/` - Python source files for CareFlow AI backend.
- `tests/` - pytest unit and integration test suite.
- `scripts/simulate_load.py` - Load simulation script.
- `pyproject.toml` - Project dependency management and configuration.

## Interface Contracts
- `/api/v1/webhook/whatsapp`: POST endpoint accepting payload representing incoming WhatsApp messages.
- Database: Tenant connection config and message buffers.
