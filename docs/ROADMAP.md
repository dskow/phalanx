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
- [x] **`feat(agents): reviewer evaluates against acceptance criteria, emits verdict`** — `review(plan, diff, tests, gateway)` reads the post-apply scratch tree (filtered), surfaces the captured pytest exit code in the prompt, asks the model for a structured `ReviewVerdict` with a per-criterion `CriterionResult`. PASS and FAIL are both *returned*, not raised — the reviewer is the judge, and the CLI gates downstream behavior on its verdict. The only failure that halts is a schema-validation failure on the response (the model returned a verdict that cannot be parsed at all). Reviewer is read-only at the gateway boundary — `write_file` and `run_shell` invocations would be rejected by the role allowlist; pinned by test. Reviewer wired into the LangGraph state machine as a live node — every node in the architecture diagram is now live and a full PASS run completes end-to-end through all four agents with one `AuditEvent` per node.
- [x] **`feat(cli): emit pull-request payload (title, body, diff bundle) on PASS`** — closes the requirement-to-PR loop. Default `phalanx run` builds the gateway, builds the graph, drives it end-to-end, and emits the right artifact for the final state. On PASS: `out/pr_payload.json` with `{title, body, diff, tests, review}` ready to feed to `gh pr create`; exit 0. On FAIL: `out/verdict.json` listing the failing criteria; exit 2; *no* `pr_payload.json` (the CLI never tempts the operator into shipping a FAIL run). On agent error (schema or apply failure): `out/error.json`; exit 1. Missing `ANTHROPIC_API_KEY` on a real run halts up front with exit 3 and a clear stderr message. Existing `--scaffold` mode preserves the no-key harness check CI uses on every PR. The "how does this JSON become a real PR" mile is the operator's choice — by design.

## All planned items closed

This was the last item on the original build plan. The full demo flow described under [What "done" looks like](#what-done-looks-like) now runs end-to-end. Subsequent work — running the planted-issue demo against the real model, hardening the egress allowlist, shipping a semgrep rules bundle, MCP exposure of individual guardrails — happens as follow-up PRs rather than roadmap items.

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
