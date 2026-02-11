# Code Quality Rules

## General
- Don't over-engineer — only make changes directly requested or clearly necessary
- Don't add features, refactor code, or make "improvements" beyond what was asked
- Avoid premature abstractions — three similar lines are better than a premature helper
- Only add error handling at system boundaries (user input, external APIs)

## Security
- Never hardcode secrets, API keys, or credentials in code
- Validate and sanitize user inputs
- Use parameterized queries for database operations
- Be aware of OWASP top 10 vulnerabilities

## Testing
- Write tests that test behavior, not implementation
- Prefer integration tests over unit tests for API endpoints
- Name test files consistently: `*.test.ts`, `*_test.py`, `test_*.py`
