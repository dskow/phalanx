"""Reviewer agent.

The final agent in the graph. Reads the plan's acceptance criteria,
the diff that landed, the captured pytest exit code, and the
post-apply scratch tree; asks the model for a structured
``ReviewVerdict`` with a per-criterion result and an overall PASS or
FAIL.

The verdict is *returned*, not raised. A FAIL verdict does not halt
the graph — the run completes and the verdict is stored on
``state.review``. The CLI is the right place to gate downstream
behavior (PR creation) on the verdict; the reviewer's job is to
judge, not to throw.

The only failure mode that halts is a malformed model response that
cannot be validated as ``ReviewVerdict``. That is the same posture
every other agent in this codebase takes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from phalanx.guardrails.input_filter import neutralize
from phalanx.guardrails.tool_gateway import Gateway, ToolGatewayError
from phalanx.state import Plan, ReviewVerdict, TestArtifact, UnifiedDiff

ReviewerInvoke = Callable[[str], dict[str, Any] | str]

_DEFAULT_MODEL = os.environ.get("PHALANX_MODEL", "claude-sonnet-4-6")
_SCRATCH_SUBDIR = "scratch"


class ReviewerError(RuntimeError):
    """Halt the run when the reviewer's output cannot be validated.

    A FAIL ``ReviewVerdict`` is NOT a ReviewerError — that is the
    reviewer doing its job. ReviewerError covers only the case where
    the model's response cannot be parsed as a valid verdict at all.
    """

    def __init__(
        self,
        message: str,
        *,
        raw: Any = None,
        validation_error: ValidationError | None = None,
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.validation_error = validation_error


def review(
    plan: Plan,
    diff: UnifiedDiff,
    tests: TestArtifact,
    *,
    gateway: Gateway,
    invoke: ReviewerInvoke | None = None,
    filter_fn: Callable[[str], tuple[str, list[str]]] = neutralize,
) -> ReviewVerdict:
    """Evaluate the plan's acceptance criteria and emit a verdict.

    Raises ``ReviewerError`` only on schema-validation failure of the
    model's response. PASS and FAIL are both legitimate return
    values; the caller decides what to do with FAIL.
    """
    if invoke is None:
        invoke = _default_invoke()

    sources = _read_post_apply_scratch(diff, gateway, filter_fn)
    prompt = _build_prompt(plan, diff, tests, sources)

    raw = invoke(prompt)
    payload = _coerce_to_dict(raw)

    try:
        verdict = ReviewVerdict.model_validate(payload)
    except ValidationError as exc:
        raise ReviewerError(
            f"reviewer response failed ReviewVerdict validation: "
            f"{exc.error_count()} error(s)",
            raw=raw,
            validation_error=exc,
        ) from exc

    return verdict


# --------------------------------------------------------------------------- #
# Source gathering
# --------------------------------------------------------------------------- #


def _read_post_apply_scratch(
    diff: UnifiedDiff,
    gateway: Gateway,
    filter_fn: Callable[[str], tuple[str, list[str]]],
) -> list[tuple[str, str, list[str]]]:
    """Read every diff-touched file from the scratch tree (post-apply
    state) through the gateway, then run it through the input filter.

    The reviewer reads from scratch, not from target_root — it must
    evaluate the modernized code, not the legacy version.
    """
    sources: list[tuple[str, str, list[str]]] = []
    seen: set[str] = set()
    for rel in diff.files_touched:
        if rel in seen:
            continue
        seen.add(rel)
        scratch_path = str(gateway.out_root / _SCRATCH_SUBDIR / rel)
        try:
            raw = gateway.invoke(
                "reviewer", "read_file", {"path": scratch_path}
            )
        except ToolGatewayError:
            continue
        except (OSError, FileNotFoundError):
            continue
        filtered, hits = filter_fn(raw)
        sources.append((rel, filtered, hits))
    return sources


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def _build_prompt(
    plan: Plan,
    diff: UnifiedDiff,
    tests: TestArtifact,
    sources: list[tuple[str, str, list[str]]],
) -> str:
    parts: list[str] = [
        "You are the Phalanx reviewer — the final judge of this run.",
        "Decide whether each acceptance criterion is met by the diff",
        "that landed and the tests that ran. Your output must be a",
        "JSON object matching this schema exactly:",
        "",
        "{",
        '  "verdict": "PASS" | "FAIL",',
        '  "criteria": [',
        "    {",
        '      "criterion": "<exact criterion text from the plan>",',
        '      "passed": true | false,',
        '      "notes": "<short reasoning>"',
        "    }",
        "  ],",
        '  "rationale": "<short overall rationale>"',
        "}",
        "",
        "Rules:",
        "- Emit one CriterionResult per acceptance criterion. Do not",
        "  drop, merge, or invent criteria.",
        "- A verdict of PASS requires every criterion.passed to be",
        "  true AND tests.pytest_exit_code to be 0. If either fails,",
        "  the verdict is FAIL.",
        "- Cite specific lines from the post-apply source in your",
        "  notes when relevant. Brevity is fine; vague platitudes are",
        "  not.",
        "- Emit no text outside the JSON object.",
        "",
        "# Plan summary",
        plan.summary,
        "",
        "# Acceptance criteria",
    ]
    for i, change in enumerate(plan.changes, start=1):
        parts.append(f"{i}. ({change.file_path}) {change.acceptance_criterion}")
    parts.append("")
    parts.append("# Diff that landed")
    parts.append("```")
    parts.append(diff.diff_text)
    parts.append("```")
    parts.append("")
    parts.append("# Test outcome")
    parts.append(
        f"pytest_exit_code: {tests.pytest_exit_code} "
        f"({'passing' if tests.pytest_exit_code == 0 else 'FAILING'})"
    )
    parts.append(
        f"tests touched: {', '.join(tests.files_touched) if tests.files_touched else '(none)'}"
    )
    parts.append("")
    parts.append("# Post-apply source (input-filter-neutralized)")
    if not sources:
        parts.append("(scratch tree could not be read for these paths)")
    for path, content, hits in sources:
        parts.append("")
        parts.append(f"## {path}")
        if hits:
            parts.append(
                f"Input filter neutralized {len(hits)} "
                "injection pattern(s) in this file."
            )
        parts.append("```")
        parts.append(content)
        parts.append("```")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Response coercion
# --------------------------------------------------------------------------- #


def _coerce_to_dict(raw: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReviewerError(
                f"reviewer response was not valid JSON: {exc.msg}",
                raw=raw,
            ) from exc
        if not isinstance(decoded, dict):
            raise ReviewerError(
                f"reviewer response decoded to {type(decoded).__name__}, "
                "expected object",
                raw=raw,
            )
        return decoded
    raise ReviewerError(
        f"reviewer response was {type(raw).__name__}, "
        "expected dict or JSON string",
        raw=raw,
    )


# --------------------------------------------------------------------------- #
# Default LLM factory
# --------------------------------------------------------------------------- #


def _default_invoke() -> ReviewerInvoke:
    from langchain_anthropic import ChatAnthropic  # noqa: PLC0415

    model = ChatAnthropic(model=_DEFAULT_MODEL).with_structured_output(ReviewVerdict)

    def _invoke(prompt: str) -> dict[str, Any]:
        result = model.invoke(prompt)
        if isinstance(result, ReviewVerdict):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        raise ReviewerError(
            f"ChatAnthropic returned {type(result).__name__}, "
            "expected ReviewVerdict or dict",
            raw=result,
        )

    return _invoke
