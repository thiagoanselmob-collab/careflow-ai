## 2026-06-30T20:56:21Z
Objective:
Empirically verify the correctness, performance, and boundaries of the load simulation script and the WhatsApp webhook debounce behavior.
1. Run the simulation script with `--help` and verify it exits cleanly and documents flags.
2. Verify that running the test suite `poetry run pytest` passes 100% of the tests.
3. Verify that the simulation script correctly handles API request timeouts, SLA response threshold checking (<500ms), and does not crash when the database connection is checked.
4. Document your empirical findings in a handoff.md in your working directory.
