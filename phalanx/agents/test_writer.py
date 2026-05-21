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

from phalanx.agents.implementer import _split_diff_per_file
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
        apply_stdout: str | None = None,
        attempted_diff: str | None = None,
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.validation_error = validation_error
        self.apply_stderr = apply_stderr
        self.apply_stdout = apply_stdout
        self.attempted_diff = attempted_diff


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

    # Mirror pre-existing target files (test fixtures, conftests, the
    # initial test_app.py) into the scratch tree before reading sources
    # or applying the model's diff. Without this, a diff that *modifies*
    # an existing test file (typical: REQUEST.md says "add tests to
    # tests/test_app.py") would fail ``git apply`` with "patch does
    # not apply" because the file the diff references is not on disk.
    _stage_pre_existing_target_files(gateway)

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


def _stage_pre_existing_target_files(gateway: Gateway) -> None:
    """Copy every ``*.py`` file from ``target_root`` into scratch
    unless it is already there (because the implementer staged a
    modified version).

    Lets the model write a diff that *modifies* a pre-existing file
    (e.g. add new test cases to ``tests/test_app.py``) without
    ``git apply`` failing on a missing source. Preserves the
    implementer's modifications by skipping any file that already
    exists in scratch — that file was deliberately placed there
    with the modernized contents.
    """
    target_root = gateway.target_root
    scratch_root = gateway.out_root / _SCRATCH_SUBDIR
    if not target_root.is_dir():
        return
    for path in sorted(target_root.rglob("*.py")):
        if not path.is_file():
            continue
        try:
            rel = str(path.relative_to(target_root)).replace("\\", "/")
        except ValueError:
            continue
        scratch_abs = scratch_root / rel
        if scratch_abs.exists():
            continue
        try:
            content = gateway.invoke(
                "test_writer", "read_file", {"path": rel}
            )
        except (ToolGatewayError, OSError, FileNotFoundError):
            continue
        try:
            gateway.invoke(
                "test_writer",
                "write_file",
                {"path": f"{_SCRATCH_SUBDIR}/{rel}", "content": content},
            )
        except (ToolGatewayError, OSError):
            continue


def _read_post_apply_scratch(
    diff: UnifiedDiff,
    gateway: Gateway,
    filter_fn: Callable[[str], tuple[str, list[str]]],
) -> list[tuple[str, str, list[str]]]:
    """Read source content the test_writer needs to see.

    Two layers:

    1. Files the implementer touched, read from the post-apply
       scratch tree (the modernized version).
    2. Any other ``*.py`` files now in scratch — typically the
       pre-existing test files staged by
       ``_stage_pre_existing_target_files``. The model needs to
       know these exist so it writes diffs against the right paths
       (modify existing tests vs. create new files).

    Every file is filtered through ``filter_fn`` before being shown
    to the model — defense in depth.
    """
    sources: list[tuple[str, str, list[str]]] = []
    seen: set[str] = set()
    scratch_root = gateway.out_root / _SCRATCH_SUBDIR

    # Layer 1: implementer-touched files first, so they get the
    # prominent position in the prompt.
    for rel in diff.files_touched:
        if rel in seen:
            continue
        seen.add(rel)
        scratch_path = str(scratch_root / rel)
        try:
            raw = gateway.invoke(
                "test_writer", "read_file", {"path": scratch_path}
            )
        except ToolGatewayError:
            continue
        except (OSError, FileNotFoundError):
            continue
        filtered, hits = filter_fn(raw)
        sources.append((rel, filtered, hits))

    # Layer 2: any other Python files in scratch (existing tests,
    # conftests, fixtures). Discovery via rglob is direct filesystem
    # access on the writable sandbox — the content read still routes
    # through the gateway for the audit trail.
    if scratch_root.is_dir():
        for path in sorted(scratch_root.rglob("*.py")):
            if not path.is_file():
                continue
            try:
                rel = str(path.relative_to(scratch_root)).replace("\\", "/")
            except ValueError:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            try:
                raw = gateway.invoke(
                    "test_writer", "read_file", {"path": str(path)}
                )
            except (ToolGatewayError, OSError, FileNotFoundError):
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
        "Import paths: pytest runs with the scratch tree root on",
        "sys.path. Use imports that match the ## headers below — a",
        "file at ``## app.py`` is imported as ``from app import ...``,",
        "a file at ``## pkg/mod.py`` is imported as ``from pkg.mod",
        "import ...``. Do NOT prepend repo-root path components like",
        "``target/`` (the request may mention paths that way for the",
        "human reader; that is not how the scratch tree is laid out).",
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
    # Write the full diff for the audit trail.
    gateway.invoke(
        "test_writer",
        "write_file",
        {
            "path": f"{_SCRATCH_SUBDIR}/{_TEST_DIFF_FILE}",
            "content": artifact.diff_text,
        },
    )
    # Split per file so a bad hunk count in one file cannot bleed
    # into the next file's header — same posture as the implementer.
    chunks = _split_diff_per_file(artifact.diff_text)
    if not chunks:
        raise TestWriterError(
            "diff_text contains no recognizable file sections",
            attempted_diff=artifact.diff_text,
        )
    for index, chunk in enumerate(chunks):
        chunk_filename = f"tests_chunk_{index:03d}.patch"
        gateway.invoke(
            "test_writer",
            "write_file",
            {
                "path": f"{_SCRATCH_SUBDIR}/{chunk_filename}",
                "content": chunk,
            },
        )
        # Same forgiving flags as the implementer — model-generated
        # diffs often have off-by-one hunk counts that git's strict
        # mode rejects but ``--recount`` repairs.
        result = gateway.invoke(
            "test_writer",
            "run_shell",
            {
                "argv": [
                    "git",
                    "apply",
                    "--verbose",
                    "--recount",
                    "--whitespace=fix",
                    chunk_filename,
                ],
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
                f"git apply of test diff failed on chunk "
                f"{index + 1}/{len(chunks)} with exit {result.returncode}",
                apply_stderr=result.stderr,
                apply_stdout=result.stdout,
                attempted_diff=chunk,
            )


_CONFTEST_CONTENT = (
    "# Auto-generated by Phalanx test_writer.\n"
    "#\n"
    "# (1) Puts the scratch tree root on sys.path so tests can\n"
    "# import the modernized modules by their tree-root-relative\n"
    "# paths (``from app import app``). Without this, pytest's\n"
    "# default rootdir detection adds the test file's directory to\n"
    "# sys.path instead, and collection fails with ImportError.\n"
    "#\n"
    "# (2) Aliases ``target`` to the scratch tree so pre-existing\n"
    "# tests written for the repo-rooted layout\n"
    "# (``from target.app import app``) still resolve. The scratch\n"
    "# tree is laid out as the target subtree without the repo's\n"
    "# leading ``target/`` prefix, so this alias is what keeps the\n"
    "# legacy import shape working.\n"
    "import sys\n"
    "import types\n"
    "from pathlib import Path\n"
    "\n"
    "_scratch_root = Path(__file__).resolve().parent\n"
    "sys.path.insert(0, str(_scratch_root))\n"
    "\n"
    "_target_pkg = types.ModuleType('target')\n"
    "_target_pkg.__path__ = [str(_scratch_root)]  # namespace-package shape\n"
    "sys.modules.setdefault('target', _target_pkg)\n"
)


def _write_pytest_conftest(gateway: Gateway) -> None:
    """Drop a conftest.py at the scratch root that puts the scratch
    tree on sys.path.

    Written every time, after the test diff has been applied — so
    the model's diff has first say on what conftest.py looks like
    if it cared to write one. The overwrite-after-apply order means
    Phalanx's conftest wins, which is what we want: import-path
    setup is a runtime concern, not part of the test artifact the
    operator sees in the PR payload.
    """
    gateway.invoke(
        "test_writer",
        "write_file",
        {
            "path": f"{_SCRATCH_SUBDIR}/conftest.py",
            "content": _CONFTEST_CONTENT,
        },
    )


def _run_pytest_and_capture_exit(
    artifact: TestArtifact, gateway: Gateway
) -> TestArtifact:
    """Run pytest inside the scratch tree and capture the exit code
    onto the artifact. The exit code is surfaced as-is — no
    interpretation, no retry."""
    _write_pytest_conftest(gateway)
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
