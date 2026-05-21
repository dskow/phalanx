# Roadmap

A PR-by-PR build trace. Each entry corresponds to one open or merged PR. Every PR has a single architectural responsibility and lands with its own contract tests.

## Closed

- [x] **PR #1 — `chore: initial project scaffold`** — Docker harness, Pydantic state contracts, agent/guardrail stubs, target Flask demo app, smoke tests. ([#1](https://github.com/dskow/phalanx/pull/1))
- [x] **`feat(guardrails): input filter neutralizes prompt-injection patterns`** — regex-based pattern library for instruction-override, role-override, exfiltration, and tool-call-smuggling families; planted payload in `target/app.py` is stripped while surrounding source survives; contract tests cover every family.
- [x] **`feat(agents): planner produces Pydantic-validated Plan from REQUEST.md`** — first real LLM call, bound to the input filter (planner never sees raw repository content); malformed model response halts with `PlannerError` rather than coercing; wired into the LangGraph state machine as a live node (implementer/test_writer/reviewer remain pass-through stubs until their own PRs); planner emits an `AuditEvent` with input/output hashes on success.
- [x] **`feat(guardrails): tool gateway with role-based allowlist and path sandbox`** — `Gateway` class with per-role tool allowlist (planner/reviewer read-only; implementer/test_writer can write and run shell), Pydantic-validated tool schemas (`read_file`, `list_files`, `write_file`, `run_shell`), two-zone path sandbox (`target_root` read-only, `out_root` read-write), shell metacharacter denial with executable allowlist (`git`/`pytest`/`ruff`/`mypy`/`semgrep`), audit callback fires on every invocation. "subprocess is imported only by the gateway" is enforced both by ruff per-file ignore and by a static-source test that walks `phalanx/` and fails on any stray import.

## Planned (in dependency order)

Each item below maps to one PR that has not been opened yet. The order matters: every PR depends on the contract the previous one established.

- [ ] **`feat(agents): implementer produces unified diff via gateway-mediated writes`**
  - Implementer agent uses only the tool gateway for file operations.
  - Output is a `UnifiedDiff` applied to a scratch tree before being inspected.

- [ ] **`feat(guardrails): output validator runs ruff/mypy/semgrep on every diff`**
  - Hard stop on lint/type/SAST failures.
  - Re-invokes the implementer with failure context up to `PHALANX_MAX_ITERATIONS`.

- [ ] **`feat(agents): test writer adds coverage for the change`**
  - Generates test diff, runs pytest in the no-network test container, captures the exit code.

- [ ] **`feat(agents): reviewer evaluates against acceptance criteria, emits verdict`**
  - Final agent. PASS verdict promotes the run to PR-ready state; FAIL halts and the audit log records the failing criterion.

- [ ] **`feat(cli): emit pull-request payload (title, body, diff bundle) on PASS`**
  - Closes the requirement-to-PR loop.
  - The payload is JSON; how it becomes a real PR (gh CLI, GitHub API, GitLab) is left to the operator and intentionally out of scope.

## What "done" looks like

A clean `docker compose up phalanx-run` against `target/` produces:

- A unified diff that fixes the three planted issues (Flask 3 migration, SQL injection, missing test).
- A passing test file for `/search`.
- A `semgrep --config=p/security-audit` report with the original SQL-injection finding cleared.
- An audit log of the full run, including an input-filter hit on the planted prompt injection.
- A PR payload ready to submit via `gh pr create`.

Five minutes, one container, zero host dependencies.

## How to follow along

- Watch the [open PRs](https://github.com/dskow/phalanx/pulls) — each one closes the next item on the list.
- The [closed PRs](https://github.com/dskow/phalanx/pulls?q=is%3Apr+is%3Aclosed) are the changelog. Every closed PR has a Summary and a Test Plan in its body.
- The [CI badge](https://github.com/dskow/phalanx/actions) on the README tracks whether the harness still builds and tests still pass.
