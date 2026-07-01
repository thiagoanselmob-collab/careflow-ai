# Testing Patterns

Loaded from `/Users/thiagoanselmobarbosa/.gemini/config/skills/testing-patterns/SKILL.md`. Refer to original for full rules.
1. Testing Pyramid: E2E (Few) -> Integration (Some) -> Unit (Many).
2. AAA Pattern: Arrange, Act, Assert.
3. Unit Test Principles: Fast (<100ms), Isolated, Repeatable, Self-checking, Timely.
4. Integration Test Principles: Test API endpoints, database queries/transactions, external services. Connect before all, reset before each, clean up after each, disconnect after all.
5. Mocking: Mock external APIs, DB (unit), time/random, network. Don't mock the code under test, simple dependencies, pure functions, in-memory stores.
6. Test Organization: Naming (Should behavior, When condition, Given-when-then), Grouping (describe, it, beforeEach).
7. Test Data: Factories, Fixtures, Builders. Use realistic, minimal, randomized non-essential values.
8. Best Practices: One assert per test, independent tests, fast tests, descriptive names, clean up.
9. Anti-Patterns: Don't test implementation, don't duplicate test code, don't skip cleanup, don't ignore flaky tests.
