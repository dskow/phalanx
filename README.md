# Castrum

> *castrum, n. — a fortified Roman military camp built rapidly to a strict, repeatable plan.*

Castrum is a reference implementation of an **autonomous code-modernization studio**: a multi-agent system that takes a refactor request as input and produces a reviewed, tested, security-scanned pull request as output — under a deterministic guardrail layer that keeps autonomous code generation safe enough for production use.

It exists to make three architectural claims visible, runnable, and testable.

## The three claims

### 1. Deterministic execution

Autonomous agents can produce predictable, structurally-sound code if the orchestration layer enforces typed contracts at every agent boundary and the guardrail layer is non-LLM. Castrum demonstrates this with a LangGraph state machine, Pydantic-validated agent I/O, and a replayable structured-event audit log.

### 2. Security at scale

Enterprises will not deploy autonomous coding agents until they can prove the agent cannot exfiltrate data, execute arbitrary code, or be hijacked by a prompt-injected docstring in the codebase it is modernizing. Castrum embeds a runtime guardrail layer — input filter, tool allowlist gateway, egress firewall, SAST output validator — between the agent loop and any side-effectful operation. The guardrails run in the orchestrator, not in agent context, so a prompt cannot disable them.

### 3. Systemic friction removal

The entire flow from a refactor request to a merge-ready PR runs in a single container in minutes, including test generation and security review. The point is not that an LLM writes the code — the point is that the surrounding system is disciplined enough to ship that code without a human gating every step.

## Demo

Everything runs in Docker. The host needs only Docker Desktop.

```bash
cp .env.example .env       # then set ANTHROPIC_API_KEY
docker compose up castrum-run
```

Castrum will:

1. Read [`target/REQUEST.md`](target/REQUEST.md) (a refactor request expressed as a GitHub issue)
2. Plan, implement, test, and review the change against [`target/app.py`](target/app.py)
3. Emit a unified diff, a generated test, a semgrep report, and a structured audit log to `out/`

The bundled target is a small Flask service with three planted issues: a deprecated `@before_first_request` decorator, a SQL-injection vulnerability, and a docstring containing a prompt-injection attempt. The injection is there to demonstrate that the input filter neutralizes it without halting the run.

## Architecture

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — agent state machine and contract schemas
- [`docs/GUARDRAILS.md`](docs/GUARDRAILS.md) — runtime security layer specification

## Status

This is an actively-developed reference implementation. The initial scaffold establishes the project shape, Docker harness, and target codebase. Agents and guardrails land in successive PRs — each one a single reviewable unit with its own contract tests.

See [open PRs](https://github.com/dskow/castrum/pulls) and the [PR-history changelog](https://github.com/dskow/castrum/pulls?q=is%3Apr+is%3Aclosed) for the build trace.

## License

MIT. See [LICENSE](LICENSE).
