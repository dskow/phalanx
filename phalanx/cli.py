"""Phalanx command-line entry point.

``phalanx run --target <dir> --out <dir>`` drives the full agent
graph end-to-end. The graph reads ``<target>/REQUEST.md``, plans
the modernization, applies a unified diff to a scratch tree under
``<out>``, writes tests, runs them, and asks the reviewer for a
verdict. On PASS the CLI emits ``<out>/pr_payload.json`` — a
JSON-encoded title/body/diff bundle ready to feed to ``gh pr
create``. On FAIL the CLI emits ``<out>/verdict.json`` with the
failing criteria and exits non-zero. Either way the full audit
log lands at ``<out>/audit.jsonl``.

``--scaffold`` runs the CLI in scaffold mode — no agents, no model
calls, just enough wiring to verify the Docker harness end-to-end
without requiring ``ANTHROPIC_API_KEY``. The CI workflow uses this
mode; production runs do not.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from phalanx import __version__
from phalanx.agents.implementer import (
    DEFAULT_MAX_ITERATIONS,
    ImplementerError,
    ImplementerInvoke,
)
from phalanx.agents.planner import PlannerError, PlannerInvoke
from phalanx.agents.reviewer import ReviewerError, ReviewerInvoke
from phalanx.agents.test_writer import TestWriterError, TestWriterInvoke
from phalanx.audit.logger import AuditLogger
from phalanx.graph import build_graph
from phalanx.guardrails.tool_gateway import Gateway, GatewayConfig
from phalanx.state import ModernizationRequest, StudioState

# Exit codes — stable contract for shell consumers.
EXIT_PASS = 0
EXIT_AGENT_ERROR = 1
EXIT_FAIL_VERDICT = 2
EXIT_MISSING_KEY = 3

_PR_PAYLOAD_FILENAME = "pr_payload.json"
_VERDICT_FILENAME = "verdict.json"
_ERROR_FILENAME = "error.json"
_AUDIT_FILENAME = "audit.jsonl"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phalanx",
        description="Autonomous code-modernization studio.",
    )
    parser.add_argument("--version", action="version", version=f"phalanx {__version__}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run the studio against a target codebase.")
    run.add_argument("--target", required=True, type=Path, help="Target codebase root.")
    run.add_argument("--out", required=True, type=Path, help="Output directory.")
    run.add_argument(
        "--scaffold",
        action="store_true",
        help=(
            "Skip agent execution — write only a scaffold audit event "
            "and exit 0. Used by CI to verify the Docker harness "
            "without ANTHROPIC_API_KEY."
        ),
    )
    run.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help="Bound on the implementer's output-validation retry loop.",
    )

    return parser


def _read_request(target: Path) -> ModernizationRequest:
    request_path = target / "REQUEST.md"
    if not request_path.is_file():
        raise FileNotFoundError(f"No REQUEST.md found in target: {request_path}")
    body = request_path.read_text(encoding="utf-8")
    title = body.splitlines()[0].lstrip("# ").strip() if body else "(untitled)"
    return ModernizationRequest(title=title, body=body, target_root=str(target.resolve()))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "run":
        if args.scaffold:
            return _cmd_run_scaffold(args.target, args.out)
        return _cmd_run(args.target, args.out, max_iterations=args.max_iterations)

    return 1


# --------------------------------------------------------------------------- #
# Scaffold mode
# --------------------------------------------------------------------------- #


def _cmd_run_scaffold(target: Path, out: Path) -> int:
    """Scaffold-only run: no agents, no model calls.

    Preserves the demo harness CI uses to prove the Docker plumbing
    works without an API key.
    """
    out.mkdir(parents=True, exist_ok=True)
    request = _read_request(target)

    audit = AuditLogger(out / _AUDIT_FILENAME)
    audit.record_scaffold_event(
        node="cli",
        message=f"scaffold run — agents not invoked (phalanx {__version__})",
        request_title=request.title,
    )

    print(f"phalanx {__version__}: scaffold run complete")
    print(f"  target:  {target}")
    print(f"  out:     {out}")
    print(f"  request: {request.title}")
    print(f"  audit:   {out / _AUDIT_FILENAME}")
    return EXIT_PASS


# --------------------------------------------------------------------------- #
# Real run
# --------------------------------------------------------------------------- #


def _cmd_run(
    target: Path,
    out: Path,
    *,
    max_iterations: int,
    planner_invoke: PlannerInvoke | None = None,
    implementer_invoke: ImplementerInvoke | None = None,
    test_writer_invoke: TestWriterInvoke | None = None,
    reviewer_invoke: ReviewerInvoke | None = None,
) -> int:
    """Drive the full agent graph and emit the run's artifacts.

    The ``*_invoke`` parameters exist so tests can drive an
    end-to-end CLI flow with stub callables — production callers
    pass nothing and the agents' default lazy ChatAnthropic factory
    handles the model wiring.
    """
    out.mkdir(parents=True, exist_ok=True)
    request = _read_request(target)

    # Real runs need the model. Check up front so the user gets a
    # clear, fast failure instead of a deep stack trace from the
    # SDK halfway through the graph. Tests that inject all four
    # invokes bypass this check because they do not call the model.
    using_defaults = (
        planner_invoke is None
        and implementer_invoke is None
        and test_writer_invoke is None
        and reviewer_invoke is None
    )
    if using_defaults and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.stderr.write(
            "phalanx: ANTHROPIC_API_KEY is not set. Either export it "
            "(cp .env.example .env; set the key) or use --scaffold to "
            "verify the harness without making model calls.\n"
        )
        return EXIT_MISSING_KEY

    gateway = Gateway(GatewayConfig(target_root=target, out_root=out))
    graph = build_graph(
        planner_invoke=planner_invoke,
        implementer_invoke=implementer_invoke,
        test_writer_invoke=test_writer_invoke,
        reviewer_invoke=reviewer_invoke,
        gateway=gateway,
        max_iterations=max_iterations,
    )

    initial_state = StudioState(request=request)
    try:
        final = graph.invoke(initial_state)
    except (
        PlannerError,
        ImplementerError,
        TestWriterError,
        ReviewerError,
    ) as exc:
        _emit_error(out, exc, request)
        return EXIT_AGENT_ERROR

    state = _normalize_final_state(final, initial_state)
    _emit_audit_log(out, state)

    if state.review is None:
        # Graph completed without producing a verdict — this should
        # not happen, but if it does it is an agent malfunction, not
        # a FAIL verdict.
        _emit_error(out, RuntimeError("graph completed without a review verdict"), request)
        return EXIT_AGENT_ERROR

    if state.review.verdict == "PASS":
        _emit_pr_payload(out, state, request)
        print(f"phalanx {__version__}: PASS — pr_payload written to {out / _PR_PAYLOAD_FILENAME}")
        return EXIT_PASS

    _emit_verdict(out, state, request)
    print(f"phalanx {__version__}: FAIL — verdict written to {out / _VERDICT_FILENAME}")
    return EXIT_FAIL_VERDICT


# --------------------------------------------------------------------------- #
# Output emission
# --------------------------------------------------------------------------- #


def _normalize_final_state(
    final: Any, initial_state: StudioState
) -> StudioState:
    """LangGraph returns either a dict update or a state model
    depending on the channel configuration. Normalize to a fully
    populated StudioState so emission code does not have to branch."""
    if isinstance(final, StudioState):
        return final
    if isinstance(final, dict):
        merged = initial_state.model_dump()
        merged.update(final)
        return StudioState.model_validate(merged)
    raise TypeError(
        f"graph.invoke returned {type(final).__name__}, expected dict or StudioState"
    )


def _emit_audit_log(out: Path, state: StudioState) -> None:
    audit = AuditLogger(out / _AUDIT_FILENAME)
    for event in state.audit_log:
        audit.append(
            {
                "node": event.node,
                "input_hash": event.input_hash,
                "output_hash": event.output_hash,
                "guardrails_passed": event.guardrails_passed,
                "guardrails_failed": event.guardrails_failed,
                "duration_ms": event.duration_ms,
                "model": event.model,
                "tokens_in": event.tokens_in,
                "tokens_out": event.tokens_out,
            }
        )


def _emit_pr_payload(
    out: Path, state: StudioState, request: ModernizationRequest
) -> None:
    """Write the operator-consumable PR payload on PASS.

    The payload is intentionally JSON — the next mile (``gh pr create``,
    GitHub API, GitLab) is the operator's choice. Phalanx's
    responsibility ends at producing a reviewable bundle.
    """
    assert state.plan is not None  # noqa: S101 — checked by caller's verdict
    assert state.diff is not None  # noqa: S101
    assert state.tests is not None  # noqa: S101
    assert state.review is not None  # noqa: S101

    payload = {
        "title": request.title,
        "body": _compose_pr_body(state, request),
        "diff": {
            "diff_text": state.diff.diff_text,
            "files_touched": state.diff.files_touched,
        },
        "tests": {
            "diff_text": state.tests.diff_text,
            "files_touched": state.tests.files_touched,
            "pytest_exit_code": state.tests.pytest_exit_code,
        },
        "review": state.review.model_dump(),
        "audit_log": str(out / _AUDIT_FILENAME),
    }
    (out / _PR_PAYLOAD_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _emit_verdict(
    out: Path, state: StudioState, request: ModernizationRequest
) -> None:
    """Write the FAIL verdict on a non-PASS run.

    No pr_payload is written — the run is not PR-ready. The verdict
    file is the audit-side record of *why*, with the failing
    criteria spelled out.
    """
    assert state.review is not None  # noqa: S101

    failing = [
        {"criterion": c.criterion, "notes": c.notes}
        for c in state.review.criteria
        if not c.passed
    ]
    payload = {
        "title": request.title,
        "verdict": state.review.verdict,
        "rationale": state.review.rationale,
        "failing_criteria": failing,
        "all_criteria": [c.model_dump() for c in state.review.criteria],
        "pytest_exit_code": (
            state.tests.pytest_exit_code if state.tests else None
        ),
        "audit_log": str(out / _AUDIT_FILENAME),
    }
    (out / _VERDICT_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _emit_error(
    out: Path, exc: Exception, request: ModernizationRequest
) -> None:
    """Write the structured error artifact when an agent halted.

    Distinct from a FAIL verdict: an agent error means the run
    *could not produce* a verdict at all (schema-validation failure,
    git apply failure, unconfigured gateway, etc.). The operator
    needs to know which.
    """
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "title": request.title,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    (out / _ERROR_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sys.stderr.write(f"phalanx: {type(exc).__name__}: {exc}\n")


def _compose_pr_body(
    state: StudioState, request: ModernizationRequest
) -> str:
    """Assemble a human-readable PR body from the run artifacts.

    The body is a string — the operator passes it directly to
    ``gh pr create --body``. Markdown is fine.
    """
    assert state.plan is not None  # noqa: S101
    assert state.diff is not None  # noqa: S101
    assert state.tests is not None  # noqa: S101
    assert state.review is not None  # noqa: S101

    test_status = (
        "passing" if state.tests.pytest_exit_code == 0 else "FAILING"
    )
    lines: list[str] = [
        f"# {request.title}",
        "",
        "Generated by Phalanx — autonomous code-modernization studio.",
        "",
        "## Plan",
        state.plan.summary,
        "",
        "### Acceptance criteria",
    ]
    for change in state.plan.changes:
        lines.append(f"- `{change.file_path}`: {change.acceptance_criterion}")
    lines.extend(
        [
            "",
            "## Implementation",
            f"Files touched: {', '.join(state.diff.files_touched) or '(none)'}",
            "",
            "## Tests",
            f"Status: {test_status} (pytest exit {state.tests.pytest_exit_code})",
            f"Test files: {', '.join(state.tests.files_touched) or '(none)'}",
            "",
            "## Review",
            f"Verdict: **{state.review.verdict}**",
            "",
            state.review.rationale,
            "",
        ]
    )
    if state.review.criteria:
        lines.append("### Criterion results")
        for c in state.review.criteria:
            mark = "[x]" if c.passed else "[ ]"
            line = f"- {mark} {c.criterion}"
            if c.notes:
                line += f" — {c.notes}"
            lines.append(line)
    lines.extend(
        [
            "",
            "---",
            "Every agent decision in this run is logged at "
            f"`{_AUDIT_FILENAME}`. See `docs/GUARDRAILS.md` for the "
            "deterministic boundary layer that governs side effects.",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
