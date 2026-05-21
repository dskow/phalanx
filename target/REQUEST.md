# Modernize the users service for Flask 3 and patch SQL injection

**Target file:** `app.py` (path is relative to the target tree root.)

## What is wrong

1. `@app.before_first_request` is **deprecated in Flask 2.3 and removed in Flask 3.x.** The app will not boot on a current Flask. Replace the database initializer with a pattern that works in Flask 3 (factory-style init, or an explicit init call at startup).

2. The `/users` endpoint builds its SQL by f-string interpolation of a query-string parameter. This is a **SQL injection vulnerability** — parameterize the query using sqlite3's placeholder substitution.

3. The `/search` endpoint has **no test coverage at all.** Add at least one test that exercises the endpoint with both an empty and a non-empty `q` parameter.

## Acceptance criteria

- The app imports cleanly under Flask 3 with no `DeprecationWarning` from Flask.
- `/users?name=<value>` runs as a parameterized query — no string interpolation of user input into SQL.
- `tests/test_app.py` contains at least one test for `/search` that covers an empty and a non-empty query.
- Existing tests still pass.

## Out of scope

Adding new endpoints. Logging changes. Restructuring into a Flask blueprint. The point is the smallest viable modernization, not a rewrite.
