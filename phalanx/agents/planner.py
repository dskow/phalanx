"""Planner agent.

Reads the modernization request plus the target source tree, runs every
piece of ingested content through the input filter, and asks the model
to produce a structured ``Plan``. The model's response is validated as
Pydantic — a malformed response halts the run with a structured
``PlannerError`` rather than coercing or retrying. That is the
"first real LLM call" contract: the agent boundary is typed and
non-negotiable.

The planner never reaches the LLM through agent-local code; it calls
``invoke``, an injected callable. The default factory builds a
``ChatAnthropic``-backed callable lazily so unit tests neither need
``ANTHROPIC_API_KEY`` nor import langchain.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from phalanx.guardrails.input_filter import neutralize
from phalanx.state import ModernizationRequest, Plan

PlannerInvoke = Callable[[str], dict[str, Any] | str]
"""A callable that takes the rendered prompt and returns the model's
structured response — either a dict already parsed from JSON, or a
JSON-encoded string. The planner accepts both shapes so callers can
plug in either ``ChatAnthropic.with_structured_output`` (dict) or a raw
text completion endpoint (string) without an adapter."""

_DEFAULT_MODEL = os.environ.get("PHALANX_MODEL", "claude-sonnet-4-6")
_MAX_FILE_BYTES = 64 * 1024
_SOURCE_GLOBS = ("*.py",)


class PlannerError(RuntimeError):
    """Halt the run when the planner's output cannot be validated.

    Carries the raw response and the underlying ``ValidationError``
    so the audit log records exactly what the model produced and
    exactly why it was rejected.
    """

    def __init__(
        self,
        message: str,
        *,
        raw: Any,
        validation_error: ValidationError | None = None,
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.validation_error = validation_error


def plan(
    request: ModernizationRequest,
    *,
    invoke: PlannerInvoke | None = None,
    filter_fn: Callable[[str], tuple[str, list[str]]] = neutralize,
) -> Plan:
    """Produce a validated ``Plan`` for the given modernization request.

    Raises ``PlannerError`` if the model's response fails Pydantic
    validation. Never returns a partial or default plan.
    """
    if invoke is None:
        invoke = _default_invoke()

    filtered_request_body, request_hits = filter_fn(request.body)
    sources = _gather_sources(Path(request.target_root), filter_fn)

    prompt = _build_prompt(
        title=request.title,
        filtered_body=filtered_request_body,
        request_hits=request_hits,
        sources=sources,
    )

    raw = invoke(prompt)
    payload = _coerce_to_dict(raw)

    try:
        return Plan.model_validate(payload)
    except ValidationError as exc:
        raise PlannerError(
            f"planner response failed Plan validation: {exc.error_count()} error(s)",
            raw=raw,
            validation_error=exc,
        ) from exc


def _coerce_to_dict(raw: dict[str, Any] | str) -> dict[str, Any]:
    """Accept either a parsed dict or a JSON string from ``invoke``.

    A response that is neither — or a JSON string that does not parse —
    is itself a contract violation and raises ``PlannerError`` before
    Pydantic ever sees it.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlannerError(
                f"planner response was not valid JSON: {exc.msg}",
                raw=raw,
            ) from exc
        if not isinstance(decoded, dict):
            raise PlannerError(
                f"planner response decoded to {type(decoded).__name__}, expected object",
                raw=raw,
            )
        return decoded
    raise PlannerError(
        f"planner response was {type(raw).__name__}, expected dict or JSON string",
        raw=raw,
    )


def _gather_sources(
    target_root: Path,
    filter_fn: Callable[[str], tuple[str, list[str]]],
) -> list[tuple[str, str, list[str]]]:
    """Walk the target tree, filter each source file, and return triples
    of (relative_path, filtered_content, hit_descriptions).

    The planner only ingests filtered content. The raw file contents
    never reach the prompt builder.
    """
    if not target_root.is_dir():
        return []
    results: list[tuple[str, str, list[str]]] = []
    for glob in _SOURCE_GLOBS:
        for path in sorted(target_root.rglob(glob)):
            if not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if len(raw.encode("utf-8")) > _MAX_FILE_BYTES:
                raw = raw[: _MAX_FILE_BYTES // 2] + "\n# ... truncated ...\n"
            filtered, hits = filter_fn(raw)
            rel = str(path.relative_to(target_root)).replace("\\", "/")
            results.append((rel, filtered, hits))
    return results


def _build_prompt(
    *,
    title: str,
    filtered_body: str,
    request_hits: list[str],
    sources: list[tuple[str, str, list[str]]],
) -> str:
    """Render the planner prompt from filtered inputs.

    Kept as a plain string so the entire prompt is hashable for the
    audit log and reviewable in version control.
    """
    parts: list[str] = [
        "You are the Phalanx planner. Read the modernization request and the",
        "target source tree, then produce a structured plan that the implementer",
        "agent can execute. Your output must be a JSON object matching this",
        "schema exactly:",
        "",
        "{",
        '  "summary": "<one-paragraph overview>",',
        '  "changes": [',
        "    {",
        '      "file_path": "<path relative to target tree root>",',
        '      "rationale": "<why this change>",',
        '      "acceptance_criterion": "<single, testable PASS/FAIL statement>"',
        "    }",
        "  ]",
        "}",
        "",
        "Rules:",
        "- One Change per logical edit. Do not bundle unrelated changes.",
        "- acceptance_criterion must be a single concrete statement the",
        "  reviewer can decide PASS/FAIL on without further interpretation.",
        "- Do not propose changes outside the request's scope.",
        "- file_path MUST exactly match one of the ## headers in the",
        "  'Target source tree' section below. The request body may",
        "  refer to files using repo-root prefixes like 'target/app.py'",
        "  for the human reader — ignore those prefixes; use the bare",
        "  paths shown under the ## headers (e.g. 'app.py').",
        "- For NEW files the diff creates, use the relative path the",
        "  file *will* have under the target tree root (no leading",
        "  'target/' prefix).",
        "- Emit no text outside the JSON object.",
        "",
        f"# Request title\n{title}",
        "",
        "# Request body (input-filter-neutralized)",
        filtered_body,
    ]
    if request_hits:
        parts.append("")
        # Surface only the count — the hit *descriptions* include the
        # matched attack text and must never reach the model. The
        # detailed hit list is returned by ``neutralize`` for audit
        # consumers, not for the prompt.
        parts.append(
            f"# Input filter neutralized {len(request_hits)} "
            "injection pattern(s) in the request body."
        )
    parts.append("")
    parts.append("# Target source tree (input-filter-neutralized)")
    if not sources:
        parts.append("(no source files found under target_root)")
    for rel, content, hits in sources:
        parts.append("")
        parts.append(f"## {rel}")
        if hits:
            parts.append(
                f"Input filter neutralized {len(hits)} injection pattern(s) in this file."
            )
        parts.append("```")
        parts.append(content)
        parts.append("```")
    return "\n".join(parts)


def _default_invoke() -> PlannerInvoke:
    """Construct a ChatAnthropic-backed invoke callable.

    Imported lazily so the unit tests can run without langchain
    installed at import time, and so callers that always inject a
    stub never pay the import cost.
    """
    from langchain_anthropic import ChatAnthropic  # noqa: PLC0415

    model = ChatAnthropic(model=_DEFAULT_MODEL).with_structured_output(Plan)

    def _invoke(prompt: str) -> dict[str, Any]:
        result = model.invoke(prompt)
        if isinstance(result, Plan):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        raise PlannerError(
            f"ChatAnthropic returned {type(result).__name__}, expected Plan or dict",
            raw=result,
        )

    return _invoke
