"""Implementer agent.

Consumes a validated ``Plan`` and emits a ``UnifiedDiff`` that applies
cleanly to the target tree. Every read, every write, and every shell
invocation routes through the tool gateway — the implementer module
itself never touches ``open()``, ``subprocess``, or the filesystem
directly. The architecture invariant from ``docs/ARCHITECTURE.md``
("the implementer operates on a guardrail-filtered plan and never
re-reads the raw legacy file") is enforced here: source content is
read through the gateway and run through the input filter before
being shown to the model.

The diff is verified by being applied to a scratch tree under
``out_root`` via ``git apply``. The scratch tree is left in place
after a successful apply so downstream agents (test_writer, reviewer,
output_validator) can inspect the post-apply state without
re-deriving it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from phalanx.guardrails.input_filter import neutralize
from phalanx.guardrails.tool_gateway import (
    Gateway,
    ShellResult,
    ToolGatewayError,
)
from phalanx.state import Plan, UnifiedDiff

ImplementerInvoke = Callable[[str], dict[str, Any] | str]

_DEFAULT_MODEL = os.environ.get("PHALANX_MODEL", "claude-sonnet-4-6")
_SCRATCH_SUBDIR = "scratch"
_DIFF_FILE = "change.patch"


class ImplementerError(RuntimeError):
    """Halt the run when the implementer's output cannot be validated
    or does not apply cleanly to the target tree.

    Carries the raw response and, when the failure was a failed
    ``git apply``, the captured stderr — so the audit log records
    exactly what went wrong without re-running the agent.
    """

    def __init__(
        self,
        message: str,
        *,
        raw: Any = None,
        validation_error: ValidationError | None = None,
        apply_stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.validation_error = validation_error
        self.apply_stderr = apply_stderr


def implement(
    plan: Plan,
    *,
    gateway: Gateway,
    invoke: ImplementerInvoke | None = None,
    filter_fn: Callable[[str], tuple[str, list[str]]] = neutralize,
) -> UnifiedDiff:
    """Produce a validated, scratch-tree-applied ``UnifiedDiff``.

    Raises ``ImplementerError`` on schema-validation failure or
    ``git apply`` failure. The function never returns a diff that
    has not been proven to apply.
    """
    if invoke is None:
        invoke = _default_invoke()

    sources = _read_filtered_sources(plan, gateway, filter_fn)
    prompt = _build_prompt(plan, sources)

    raw = invoke(prompt)
    payload = _coerce_to_dict(raw)

    try:
        diff = UnifiedDiff.model_validate(payload)
    except ValidationError as exc:
        raise ImplementerError(
            f"implementer response failed UnifiedDiff validation: "
            f"{exc.error_count()} error(s)",
            raw=raw,
            validation_error=exc,
        ) from exc

    _verify_applies_in_scratch(diff, plan, gateway)
    return diff


# --------------------------------------------------------------------------- #
# Source gathering
# --------------------------------------------------------------------------- #


def _read_filtered_sources(
    plan: Plan,
    gateway: Gateway,
    filter_fn: Callable[[str], tuple[str, list[str]]],
) -> list[tuple[str, str, list[str]]]:
    """Read every plan-referenced file through the gateway and run it
    through the input filter.

    Files the plan references but that do not yet exist (the change
    adds a new file) are returned with empty content — the diff for
    those files is expected to be a pure addition.
    """
    seen: dict[str, tuple[str, list[str]]] = {}
    for change in plan.changes:
        if change.file_path in seen:
            continue
        try:
            raw = gateway.invoke(
                "implementer", "read_file", {"path": change.file_path}
            )
        except ToolGatewayError:
            seen[change.file_path] = ("", [])
            continue
        filtered, hits = filter_fn(raw)
        seen[change.file_path] = (filtered, hits)
    return [(path, content, hits) for path, (content, hits) in seen.items()]


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def _build_prompt(
    plan: Plan, sources: list[tuple[str, str, list[str]]]
) -> str:
    parts: list[str] = [
        "You are the Phalanx implementer. Produce a unified diff that",
        "executes every Change in the plan below. Your output must be a",
        "JSON object matching this schema exactly:",
        "",
        "{",
        '  "diff_text": "<unified diff in standard `--- a/.. / +++ b/..` form>",',
        '  "files_touched": ["<relative path>", ...]',
        "}",
        "",
        "Rules:",
        "- Unified-diff format only. No commentary in diff_text.",
        "- Paths in the diff are relative to the target tree.",
        "- files_touched lists every path mentioned by the diff, no others.",
        "- Make the smallest change that satisfies the plan; do not refactor",
        "  unrelated code. Out-of-scope edits will be rejected by the reviewer.",
        "- Emit no text outside the JSON object.",
        "",
        "# Plan summary",
        plan.summary,
        "",
        "# Planned changes",
    ]
    for i, change in enumerate(plan.changes, start=1):
        parts.append(
            f"{i}. {change.file_path}: {change.rationale} "
            f"(criterion: {change.acceptance_criterion})"
        )
    parts.append("")
    parts.append("# Current source (input-filter-neutralized)")
    if not sources:
        parts.append("(no source files associated with this plan)")
    for path, content, hits in sources:
        parts.append("")
        parts.append(f"## {path}")
        if hits:
            parts.append(
                f"Input filter neutralized {len(hits)} "
                "injection pattern(s) in this file."
            )
        parts.append("```")
        parts.append(content if content else "(file does not yet exist)")
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
            raise ImplementerError(
                f"implementer response was not valid JSON: {exc.msg}",
                raw=raw,
            ) from exc
        if not isinstance(decoded, dict):
            raise ImplementerError(
                f"implementer response decoded to {type(decoded).__name__}, "
                "expected object",
                raw=raw,
            )
        return decoded
    raise ImplementerError(
        f"implementer response was {type(raw).__name__}, "
        "expected dict or JSON string",
        raw=raw,
    )


# --------------------------------------------------------------------------- #
# Scratch-tree verification
# --------------------------------------------------------------------------- #


def _verify_applies_in_scratch(
    diff: UnifiedDiff, plan: Plan, gateway: Gateway
) -> None:
    """Apply ``diff`` to a fresh copy of the plan-referenced files
    under ``out_root/scratch`` via ``git apply``.

    Routes every filesystem and shell operation through the gateway
    so this verification step is itself observable in the audit log.
    Raises ``ImplementerError`` if ``git apply`` reports a non-zero
    exit code or any conflict.
    """
    plan_paths = _ordered_unique_paths(plan)
    # Mirror current source state into the scratch tree. Files that do
    # not exist in the target (the diff creates them) are simply absent
    # from the scratch tree, which is what ``git apply`` expects for a
    # pure-addition hunk.
    for rel in plan_paths:
        try:
            content = gateway.invoke(
                "implementer", "read_file", {"path": rel}
            )
        except ToolGatewayError:
            continue
        gateway.invoke(
            "implementer",
            "write_file",
            {"path": f"{_SCRATCH_SUBDIR}/{rel}", "content": content},
        )

    # Write the diff next to the scratch tree, not inside it.
    gateway.invoke(
        "implementer",
        "write_file",
        {
            "path": f"{_SCRATCH_SUBDIR}/{_DIFF_FILE}",
            "content": diff.diff_text,
        },
    )

    # ``git apply`` is fine outside a git repository — it just needs
    # the source files visible from cwd. The check is non-mutating
    # against target_root and mutating only inside scratch/, which is
    # under out_root and thus inside the writable sandbox.
    result = gateway.invoke(
        "implementer",
        "run_shell",
        {
            "argv": ["git", "apply", "--verbose", _DIFF_FILE],
            "cwd": _SCRATCH_SUBDIR,
        },
    )
    if not isinstance(result, ShellResult):
        raise ImplementerError(
            f"gateway returned {type(result).__name__} from run_shell, "
            "expected ShellResult"
        )
    if result.returncode != 0:
        raise ImplementerError(
            f"git apply failed with exit {result.returncode}",
            apply_stderr=result.stderr,
        )


def _ordered_unique_paths(plan: Plan) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for change in plan.changes:
        if change.file_path not in seen:
            seen.add(change.file_path)
            ordered.append(change.file_path)
    return ordered


# --------------------------------------------------------------------------- #
# Default LLM factory
# --------------------------------------------------------------------------- #


def _default_invoke() -> ImplementerInvoke:
    """Construct a ChatAnthropic-backed invoke callable.

    Imported lazily so the unit tests can run without langchain
    installed at import time.
    """
    from langchain_anthropic import ChatAnthropic  # noqa: PLC0415

    model = ChatAnthropic(model=_DEFAULT_MODEL).with_structured_output(UnifiedDiff)

    def _invoke(prompt: str) -> dict[str, Any]:
        result = model.invoke(prompt)
        if isinstance(result, UnifiedDiff):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        raise ImplementerError(
            f"ChatAnthropic returned {type(result).__name__}, "
            "expected UnifiedDiff or dict",
            raw=result,
        )

    return _invoke
