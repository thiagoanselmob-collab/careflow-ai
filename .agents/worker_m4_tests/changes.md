# Changes

## Files Added
- `tests/test_debounce_resetable.py`
  - Created a new unit/integration test file containing `test_debounce_resetable`.
  - Simulates 3 incoming messages for organization `org_debounce` and phone number `+5511999999999` at t=0, t=0.5, and t=1.0.
  - Monkeypatches `settings.debounce_seconds` to `2.0` seconds.
  - Asserts that LangGraph is invoked exactly once.
  - Asserts that the invocation happens approximately at `t=3.0` seconds (2s debounce duration after the last message at `t=1.0`).
  - Asserts that the consolidated text contains all three messages: `"Hello\nAwesome\nWorld"`.

## Verification Status
- Full test suite passed (100 tests passed).
