"""Test-writer agent.

Reads the implementer's already-applied scratch tree, asks the model
for new or modified tests that cover the plan's acceptance criteria,
applies the test diff to the scratch tree, runs pytest, and returns
a ``TestArtifact`` carrying the captured exit code.

The exit code is *surfaced*, not interpreted. If pytest exits
non-zero, the test_writer does NOT retry — the failure is left
visible to the reviewer agent, which is the right place to decide
whether a non-passing test run is a legitimate FAIL verdict or a
test that needs to be fixed. Silent retry here would mask the
contract violation.

Failures that DO halt the run:

- Schema-validation failures on the model's response (Pydantic).
- ``git apply`` failures when staging the test diff into scratch.

Both indicate fundamentally broken agent output, not a test-writing
attempt that needed feedback to converge.
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
from phalanx.state import Plan, TestArtifact, UnifiedDiff

TestWriterInvoke = Callable[[str], dict[str, Any] | str]

_DEFAULT_MODEL = os.environ.get("PHALANX_MODEL", "claude-sonnet-4-6")
_SCRATCH_SUBDIR = "scratch"
_TEST_DIFF_FILE = "tests.patch"


class TestWriterError(RuntimeError):
    """Halt the run when the test_writer's output cannot be validated
    or does not apply cleanly to the scratch tree.

    Carries the raw response and (for apply failures) the captured
    stderr — the audit log records exactly what went wrong without
    re-running the agent. A non-zero pytest exit code is NOT a
    TestWriterError; it is recorded in ``TestArtifact.pytest_exit_code``
    and left for the reviewer to interpret.
    """

    # The "Test" prefix matches pytest's collection pattern but this
    # is a project class, not a test case.
    __test__ = False

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


def write_tests(
    plan: Plan,
    diff: UnifiedDiff,
    *,
    gateway: Gateway,
    invoke: TestWriterInvoke | None = None,
    filter_fn: Callable[[str], tuple[str, list[str]]] = neutralize,
) -> TestArtifact:
    """Produce a validated, scratch-applied ``TestArtifact``.

    Raises ``TestWriterError`` on schema-validation failure or
    ``git apply`` failure. Returns a ``TestArtifact`` carrying the
    pytest exit code regardless of whether the tests passed.
    """
    if invoke is None:
        invoke = _default_invoke()

    sources = _read_post_apply_scratch(diff, gateway, filter_fn)
    prompt = _build_prompt(plan, diff, sources)

    raw = invoke(prompt)
    payload = _coerce_to_dict(raw)

    try:
        artifact = TestArtifact.model_validate(payload)
    except ValidationError as exc:
        raise TestWriterError(
            f"test_writer response failed TestArtifact validation: "
            f"{exc.error_count()} error(s)",
            raw=raw,
            validation_error=exc,
        ) from exc

    _apply_test_diff_to_scratch(artifact, gateway)
    artifact = _run_pytest_and_capture_exit(artifact, gateway)
    return artifact


# --------------------------------------------------------------------------- #
# Source gathering
# --------------------------------------------------------------------------- #


def _read_post_apply_scratch(
    diff: UnifiedDiff,
    gateway: Gateway,
    filter_fn: Callable[[str], tuple[str, list[str]]],
) -> list[tuple[str, str, list[str]]]:
    """Read every file the implementer touched, from the scratch tree
    where the diff was just applied. Filter each through the input
    filter before exposing to the model — same posture as the
    implementer, defense in depth.

    The test_writer reads from scratch, not from target_root. Reading
    from target_root would show the pre-diff state and the model
    would write tests against code that has not been modernized.
    """
    sources: list[tuple[str, str, list[str]]] = []
    seen: set[str] = set()
    for rel in diff.files_touched:
        if rel in seen:
            continue
        seen.add(rel)
        # Read paths resolve against target_root by default; the
        # scratch tree lives under out_root, so we hand the gateway
        # an absolute path. The gateway still sandbox-checks it.
        scratch_path = str(gateway.out_root / _SCRATCH_SUBDIR / rel)
        try:
            raw = gateway.invoke(
                "test_writer", "read_file", {"path": scratch_path}
            )
        except ToolGatewayError:
            # File listed in the diff but not on disk in scratch
            # (deletion-only diff, or the implementer left things in
            # a state the gateway cannot read). Skip; the model can
            # still write tests against the diff_text alone.
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
    sources: list[tuple[str, str, list[str]]],
) -> str:
    parts: list[str] = [
        "You are the Phalanx test_writer. The implementer's diff has",
        "just been applied to the scratch tree. Your job is to write",
        "new or modified tests that prove the plan's acceptance",
        "criteria are met by the new code. Your output must be a JSON",
        "object matching this schema exactly:",
        "",
        "{",
        '  "diff_text": "<unified diff that adds/modifies test files>",',
        '  "files_touched": ["<test file path>", ...],',
        '  "pytest_exit_code": 0',
        "}",
        "",
        "Rules:",
        "- Unified-diff format only. diff_text must apply cleanly to",
        "  the scratch tree (the already-modernized code).",
        "- Add tests under a ``tests/`` directory or alongside the",
        "  modified module, whichever matches the project's existing",
        "  convention.",
        "- Each acceptance criterion below should be exercised by at",
        "  least one test.",
        "- pytest_exit_code in your response is a placeholder; the",
        "  test_writer overwrites it with the captured exit after it",
        "  runs pytest. Set it to 0 in your response.",
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
    parts.append("# Diff just applied (the modernization under test)")
    parts.append("```")
    parts.append(diff.diff_text)
    parts.append("```")
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
            raise TestWriterError(
                f"test_writer response was not valid JSON: {exc.msg}",
                raw=raw,
            ) from exc
        if not isinstance(decoded, dict):
            raise TestWriterError(
                f"test_writer response decoded to {type(decoded).__name__}, "
                "expected object",
                raw=raw,
            )
        return decoded
    raise TestWriterError(
        f"test_writer response was {type(raw).__name__}, "
        "expected dict or JSON string",
        raw=raw,
    )


# --------------------------------------------------------------------------- #
# Apply + pytest
# --------------------------------------------------------------------------- #


def _apply_test_diff_to_scratch(
    artifact: TestArtifact, gateway: Gateway
) -> None:
    """Write the test diff into scratch and apply it.

    The implementer's diff is already applied in scratch. The
    test_writer's diff stacks on top, typically as pure additions
    (new test files). A failed apply is a hard halt — the test_writer
    will not silently fall back to running pytest against an
    incomplete tree.
    """
    gateway.invoke(
        "test_writer",
        "write_file",
        {
            "path": f"{_SCRATCH_SUBDIR}/{_TEST_DIFF_FILE}",
            "content": artifact.diff_text,
        },
    )
    result = gateway.invoke(
        "test_writer",
        "run_shell",
        {
            "argv": ["git", "apply", "--verbose", _TEST_DIFF_FILE],
            "cwd": _SCRATCH_SUBDIR,
        },
    )
    if not isinstance(result, ShellResult):
        raise TestWriterError(
            f"gateway returned {type(result).__name__} from run_shell, "
            "expected ShellResult"
        )
    if result.returncode != 0:
        raise TestWriterError(
            f"git apply of test diff failed with exit {result.returncode}",
            apply_stderr=result.stderr,
        )


def _run_pytest_and_capture_exit(
    artifact: TestArtifact, gateway: Gateway
) -> TestArtifact:
    """Run pytest inside the scratch tree and capture the exit code
    onto the artifact. The exit code is surfaced as-is — no
    interpretation, no retry."""
    test_files = [p for p in artifact.files_touched if p.endswith(".py")]
    argv: list[str] = ["pytest", "-q", "--no-header"]
    argv.extend(test_files) if test_files else argv.append(".")
    result = gateway.invoke(
        "test_writer",
        "run_shell",
        {"argv": argv, "cwd": _SCRATCH_SUBDIR},
    )
    if not isinstance(result, ShellResult):
        raise TestWriterError(
            f"gateway returned {type(result).__name__} from run_shell, "
            "expected ShellResult"
        )
    # Preserve the diff/files_touched as the model produced them;
    # only overwrite the exit code, which is the captured runtime
    # value rather than a model claim.
    return TestArtifact(
        diff_text=artifact.diff_text,
        files_touched=artifact.files_touched,
        pytest_exit_code=result.returncode,
    )


# --------------------------------------------------------------------------- #
# Default LLM factory
# --------------------------------------------------------------------------- #


def _default_invoke() -> TestWriterInvoke:
    from langchain_anthropic import ChatAnthropic  # noqa: PLC0415

    model = ChatAnthropic(model=_DEFAULT_MODEL).with_structured_output(TestArtifact)

    def _invoke(prompt: str) -> dict[str, Any]:
        result = model.invoke(prompt)
        if isinstance(result, TestArtifact):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        raise TestWriterError(
            f"ChatAnthropic returned {type(result).__name__}, "
            "expected TestArtifact or dict",
            raw=result,
        )

    return _invoke
