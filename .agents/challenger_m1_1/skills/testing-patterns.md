# Testing Patterns

> Principles for reliable test suites.

## AAA Pattern

| Step | Purpose |
|------|---------|
| **Arrange** | Set up test data |
| **Act** | Execute code under test |
| **Assert** | Verify outcome |

## Good Unit Tests

| Principle | Meaning |
|-----------|---------|
| Fast | < 100ms each |
| Isolated | No external deps |
| Repeatable | Same result always |
| Self-checking | No manual verification |
| Timely | Written with code |

## What to Unit Test

- Business logic
- Edge cases
- Error handling
