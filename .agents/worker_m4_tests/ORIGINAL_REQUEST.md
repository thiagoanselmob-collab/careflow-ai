## 2026-06-30T12:02:43Z
You are teamwork_preview_worker.
Your working directory is `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/worker_m4_tests/`.
Your mission is to resolve the victory audit rejection by implementing the required resetable debounce timing test.

Please apply the following changes:

### 1. Create the Test File `tests/test_debounce_resetable.py`
Create the file `tests/test_debounce_resetable.py`. The test file must:
- Use `pytest` and `pytest_asyncio`.
- Use a `FakeRedis` client for Redis mocking, and mock the tenant database connections/sessions, the WhatsApp client, the Medflow client, and the LangGraph graph invoke.
- Monkeypatch/override the settings to set `settings.debounce_seconds = 2.0` (i.e. `DEBOUNCE_SECONDS = 2`).
- Simulate 3 incoming messages for organization `org_debounce` and phone number `+5511999999999` at specific timing intervals:
  - At `t=0`: Insert Message 1 ("Hello") into the database buffer, set the `last_msg_time` key in Redis, and start the background task: `task1 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))`.
  - Sleep for `0.5` seconds.
  - At `t=0.5s`: Insert Message 2 ("Awesome") into the database buffer, set the `last_msg_time` key in Redis, and start the background task: `task2 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))`.
  - Sleep for `0.5` seconds.
  - At `t=1.0s`: Insert Message 3 ("World") into the database buffer, set the `last_msg_time` key in Redis, and start the background task: `task3 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))`.
  - Gather all three tasks using `await asyncio.gather(task1, task2, task3)`.
- Assert the following timing and consolidation conditions:
  1. LangGraph is invoked **exactly once** (meaning `mock_graph_invoke.call_count == 1`).
  2. The invocation happens approximately at `t=3.0$ seconds (which is `DEBOUNCE_SECONDS` (2s) after the last message at `t=1.0s`). Use a reasonable timing margin, for example: `assert 2.8 <= elapsed_time <= 3.8`.
  3. The input content passed to the supervisor/graph contains all 3 messages consolidated using newlines: `"Hello\nAwesome\nWorld"`.

### 2. Verify all tests pass
Run `poetry run pytest tests/test_debounce_resetable.py` to ensure it passes. Then run `poetry run pytest` to ensure all tests (totaling at least 100 tests) pass.

MANDATORY INTEGRITY WARNING — include this verbatim in the Worker's dispatch prompt:
> DO NOT CHEAT. All implementations must be genuine. DO NOT
> hardcode test results, create dummy/facade implementations, or
> circumvent the intended task. A Forensic Auditor will independently
> verify your work. Integrity violations WILL be detected and your
> work WILL be rejected.

Please document the new test file in `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/worker_m4_tests/changes.md` and deliver your handoff report in `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/worker_m4_tests/handoff.md`. Communicate back when complete.
