# Monitoring & Tracing Implementation Plan

## Phase 1: Exploration
- **Goal**: Research current code structure, dependencies, LangGraph invocation details, and existing configs.
- **Tasks**:
  - Spawn `teamwork_preview_explorer` to study files and suggest implementation details.
  - Identify specific entry points in `app/main.py`, config setup in `app/core/config.py`, env variables in `.env`, and graph runs in `app/services/agents/graph.py` or webhook endpoints.
- **Verification**: Explorer handoff report with exact target lines and code changes.

## Phase 2: Implementation (R1, R2, R3)
- **Goal**: Apply the code changes.
- **Tasks**:
  - Spawn `teamwork_preview_worker` to:
    - Add `prometheus-fastapi-instrumentator` to `pyproject.toml`.
    - Initialize Prometheus instrumentator in `app/main.py` and expose `/metrics`.
    - Set up python logging configuration to capture LangGraph node traversal order, node execution time, session ID (phone_number), and timestamp.
    - Expose LangSmith tracing configs in `app/core/config.py` and `.env`.
    - Install dependencies via poetry.
- **Verification**: Compilation/build check, and dependency verification.

## Phase 3: Testing & Verification
- **Goal**: Write tests in `tests/test_monitoring.py` and run the entire test suite.
- **Tasks**:
  - Spawn `teamwork_preview_worker` or `teamwork_preview_challenger` to write `tests/test_monitoring.py`.
  - Verify `/metrics` endpoint returns prometheus metrics.
  - Verify LangGraph logs are correctly emitted on stdout in standard python logging format (capturable by pytest's `caplog`).
  - Run `poetry run pytest` and ensure 100% of tests pass.
- **Verification**: Complete test report showing passing tests and coverage reports.

## Phase 4: Review and Auditing
- **Goal**: Peer review the changes and run the forensic auditor to ensure no integrity violations.
- **Tasks**:
  - Spawn `teamwork_preview_reviewer` to review changes.
  - Spawn `teamwork_preview_auditor` to check for integrity.
- **Verification**: Auditor verdict CLEAN, and reviewer approval.
