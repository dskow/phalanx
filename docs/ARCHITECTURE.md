# Architecture

Phalanx is a LangGraph state machine where each node is an agent constrained by a typed I/O contract. Side-effectful operations (file read, file write, command execution, network) route through a guardrail layer that is deterministic — i.e., not LLM-driven and not reachable from inside agent context.

## State

The full state object is defined in [`phalanx/state.py`](../phalanx/state.py) as `StudioState`. Conceptually:

| Field | Type | Set by |
|---|---|---|
| `request` | `ModernizationRequest` | input |
| `plan` | `Plan` | planner |
| `diff` | `UnifiedDiff` | implementer |
| `tests` | `TestArtifact` | test_writer |
| `review` | `ReviewVerdict` | reviewer |
| `audit_log` | `list[AuditEvent]` | every node, append-only |

State is a single Pydantic model. LangGraph nodes receive it, return a partial update, and the orchestrator merges. There is no shared mutable state outside the model.

## Agent graph

```
   ┌──────────┐   ┌───────────────┐   ┌────────────┐   ┌──────────┐
   │ planner  │──▶│  implementer  │──▶│ test_writer│──▶│ reviewer │──▶ PR
   └──────────┘   └───────────────┘   └────────────┘   └──────────┘
        │              │  ▲ │              │ ▲              │
        │              ▼  │ ▼              ▼ │              │
        └─────────▶ guardrail layer (deterministic) ◀───────┘
```

Every tool invocation by an agent is intercepted by the guardrail layer. The guardrails are documented in [GUARDRAILS.md](GUARDRAILS.md).

## Contracts

Every agent declares its input and output as a Pydantic model. The orchestrator validates both directions. If validation fails, the run halts with a structured error — there is no retry-on-malformed-output that smuggles bad data into the next stage.

- **Planner output** must include a list of `Change` entries, each with `file_path`, `rationale`, and `acceptance_criterion`.
- **Implementer output** must be a unified diff that applies cleanly to the target tree.
- **Test-writer output** must be a list of new or modified test files plus a passing-run assertion.
- **Reviewer output** is a `ReviewVerdict` of `PASS` or `FAIL` with a per-criterion result and a rationale.

## Audit log

Every state transition appends a structured event:

```json
{
  "ts": "2026-05-19T14:23:01Z",
  "node": "implementer",
  "input_hash": "sha256:...",
  "output_hash": "sha256:...",
  "guardrails_passed": ["input_filter", "tool_gateway"],
  "guardrails_failed": [],
  "duration_ms": 4321,
  "model": "claude-sonnet-4-6",
  "tokens": {"in": 1240, "out": 380}
}
```

The log is the replay primitive. Given the log and a snapshot of the prompts, any run can be reconstructed for incident review.

## Why LangGraph and not a custom loop

LangGraph buys two things this project cares about:

1. **Explicit state transitions.** The graph is a value, not a control flow. The wiring is itself reviewable.
2. **Checkpointing.** Long-running modernization passes can pause for human approval at any node boundary without leaking state into ad-hoc files.

It does not buy us anything we would not have to build ourselves for a custom loop, so we use it.

## Why agents at all, instead of a single prompt

The agents are not in this project because the modernization task is too complex for a single prompt — it usually is not. They are here because the guardrails attach cleanly to *boundaries*, and the agents make the boundaries explicit. A planner/implementer split lets the planner be untrusted-input-heavy (it reads the legacy code through the input filter) while the implementer operates on a Pydantic-validated plan whose scope it cannot exceed.

The implementer does still read the source file to write context-correct unified diffs — a diff that does not match the bytes on disk will not apply — but it does so unfiltered, so injection placeholders in a docstring do not poison the diff context. The defense is layered: the planner already operated on filtered input to decide *what* to change, the implementer's task is bounded by that plan, and the output validator + reviewer catch any attempt to deviate. See the docstring of `phalanx/agents/implementer.py` for the design rationale.
