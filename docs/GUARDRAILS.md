# Guardrails

The guardrail layer is the boundary between LLM-generated intent and side-effectful execution. Every guardrail is:

- **Deterministic** — no LLM in the decision path; a guardrail's verdict is a function of its input alone.
- **Non-bypassable from a prompt** — guardrails run in the orchestrator process, not in agent context. A prompt-injected instruction in a docstring cannot disable them.
- **Auditable** — every guardrail decision is appended to the audit log with input hash and verdict.

## The four guardrails

### 1. Input filter — `castrum/guardrails/input_filter.py`

Scans every piece of repository content the agent is about to ingest for known prompt-injection patterns:

- Instruction overrides: `ignore (previous|prior|all) instructions`
- Role overrides: `you are now`, `act as`, `from now on you will`
- Exfiltration prompts: `print your environment`, `reveal your system prompt`
- Tool-call smuggling: bare `<tool_use>` or function-call JSON in user-supplied content

Hits do not halt the run. The filter strips the offending span, replaces it with a placeholder token, and annotates the audit log. This is the correct behavior because the agent may legitimately need to read a docstring that mentions these phrases — the goal is to neutralize injected instructions, not refuse to operate on suspicious code.

The bundled target codebase contains a planted injection in `target/app.py`'s `/users` docstring specifically to exercise this guardrail.

### 2. Tool gateway — `castrum/guardrails/tool_gateway.py`

Agents do not call shell commands or filesystem APIs directly. Every tool invocation is intercepted by the gateway, which:

- Confirms the tool name is on the allowlist for the current agent role. (Example: the planner may `read_file` but not `write_file`.)
- Validates arguments against the tool's Pydantic schema.
- Confines filesystem access to paths under the target tree and the output directory. Path traversal (`../`) is normalized and rejected if it escapes the sandbox.
- Rejects shell-style metacharacters in command arguments: `;`, `&&`, `||`, `|`, backticks, `$(...)`, redirects.

The gateway is the only module in the codebase that imports `subprocess` or `os.system`. This is enforced by a `ruff` rule, not by convention.

### 3. Egress firewall — Docker network policy

The agent container has no network egress except to the configured model endpoint (`api.anthropic.com` by default). This is enforced at the Docker network level via an explicit destination allowlist, not in Python — so a prompt that talks the agent into running `curl` cannot escape the network boundary even if it somehow bypasses the tool gateway.

The egress allowlist is declared in `docker-compose.yml` and is the smallest set of hosts the agent provably needs. Adding a host requires a code change, not a runtime config.

### 4. Output validator — `castrum/guardrails/output_validator.py`

Every diff the implementer produces is applied to a scratch copy of the target and run through:

- `ruff check` — lint and basic correctness
- `mypy --strict` on the changed files
- `semgrep --config=p/security-audit` on the result
- The target's own test suite, in a separate container with no network

A failure at any of these is a hard stop. The implementer is re-invoked with the failing output as context, up to a fixed retry limit. If the limit is exhausted, the run terminates with a `FAIL` verdict and no PR is opened.

## What guardrails do not do

- They do not judge whether a refactor is *good*. That is the reviewer agent's job.
- They do not constrain *which* code patterns the implementer can write — that is also the reviewer's job.
- They do not replace human code review for production deployment. They make it safer to *trust* the agent's output enough to invite human review at all.

## Threat model

Castrum's guardrails are designed against three threats, ranked by likelihood:

1. **Prompt injection from the codebase under modernization.** The most common attack surface — a docstring, README, or comment in the target tree that tries to redirect the agent. Mitigated by the input filter.
2. **Tool-use exfiltration.** An agent that has been redirected attempts to read secrets or call external APIs. Mitigated by the tool gateway (filesystem confinement) and the egress firewall (network confinement).
3. **Hallucinated-but-plausible vulnerable code.** An agent confidently produces code that introduces a vulnerability the prompt did not ask for. Mitigated by the output validator's SAST pass.

What is explicitly out of scope: a malicious operator who controls the orchestrator process itself. If you do not trust the host running Castrum, no in-process guardrail will save you.
