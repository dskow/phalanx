# Roadmap

A PR-by-PR build trace. Each entry corresponds to one open or merged PR. Every PR has a single architectural responsibility and lands with its own contract tests.

## Closed

- [x] **PR #1 — `chore: initial project scaffold`** — Docker harness, Pydantic state contracts, agent/guardrail stubs, target Flask demo app, smoke tests. ([#1](https://github.com/dskow/phalanx/pull/1))
- [x] **`feat(guardrails): input filter neutralizes prompt-injection patterns`** — regex-based pattern library for instruction-override, role-override, exfiltration, and tool-call-smuggling families; planted payload in `target/app.py` is stripped while surrounding source survives; contract tests cover every family.
- [x] **`feat(agents): planner produces Pydantic-validated Plan from REQUEST.md`** — first real LLM call, bound to the input filter (planner never sees raw repository content); malformed model response halts with `PlannerError` rather than coercing; wired into the LangGraph state machine as a live node (implementer/test_writer/reviewer remain pass-through stubs until their own PRs); planner emits an `AuditEvent` with input/output hashes on success.
- [x] **`feat(guardrails): tool gateway with role-based allowlist and path sandbox`** — `Gateway` class with per-role tool allowlist (planner/reviewer read-only; implementer/test_writer can write and run shell), Pydantic-validated tool schemas (`read_file`, `list_files`, `write_file`, `run_shell`), two-zone path sandbox (`target_root` read-only, `out_root` read-write), shell metacharacter denial with executable allowlist (`git`/`pytest`/`ruff`/`mypy`/`semgrep`), audit callback fires on every invocation. "subprocess is imported only by the gateway" is enforced both by ruff per-file ignore and by a static-source test that walks `phalanx/` and fails on any stray import.
- [x] **`feat(agents): implementer produces unified diff via gateway-mediated writes`** — implementer reads plan-referenced files through the gateway, filters via the input filter, asks the model for a `UnifiedDiff`, validates the response (halts with `ImplementerError` on schema failure), and proves the diff applies by running `git apply` in a scratch tree under `out_root` (halts on non-zero exit). Every read, write, and shell call routes through the gateway. Gateway extended with an optional sandbox-validated `cwd` on `run_shell` so the apply step can execute in scratch. Implementer wired into the LangGraph state machine as a live node; emits an `AuditEvent` with both `input_filter` and `tool_gateway` recorded as passed.
- [x] **`feat(guardrails): output validator runs ruff/mypy/semgrep on every diff`** — `validate(diff, gateway)` runs `ruff check --isolated` and `mypy --ignore-missing-imports` against the implementer's scratch tree via the tool gateway (semgrep slot is named but deferred — recorded as `executed=False` with skip reason, not silently omitted). `implement_iteratively` drives the retry loop: on output-validation failure the implementer is re-invoked with the failing-tool output appended as `retry_context`, up to `PHALANX_MAX_ITERATIONS` attempts (default 4). Schema and `git apply` failures still halt one-shot — only output-validation failures are retryable. Implementer node now records `output_validator` in `guardrails_passed`.
- [x] **`feat(agents): test writer adds coverage for the change`** — `write_tests(plan, diff, gateway)` reads the post-apply scratch tree (filtered through input filter), asks the model for tests covering the plan's acceptance criteria, applies the test diff via `git apply`, and runs `pytest` in the scratch tree. The captured pytest exit code is surfaced on `TestArtifact.pytest_exit_code` *as-is* — no interpretation, no retry. The reviewer (next PR) is the right place to decide whether a non-zero exit is a `FAIL` verdict. Schema and `git apply` failures still halt the run with `TestWriterError`. Gateway exposes `target_root`/`out_root` properties so agents can construct absolute paths into the writable sandbox (the test_writer reads post-apply scratch under `out_root`). Test writer wired into the LangGraph state machine as a live node — only the reviewer remains a stub.

## Planned (in dependency order)

Each item below maps to one PR that has not been opened yet. The order matters: every PR depends on the contract the previous one established.

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
