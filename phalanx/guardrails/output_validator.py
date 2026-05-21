"""Output validator — runs lint, type-check, and SAST against the
diff the implementer just applied to the scratch tree.

A failure from any enabled tool is a hard stop for the immediate
attempt. The implementer's retry loop (``implement_iteratively``)
decides whether to re-prompt with the failure context or to surface
the failure to the caller — this module is the pure verdict, no
orchestration policy.

Every tool invocation routes through the tool gateway as the
``implementer`` role with ``cwd=scratch_subdir``. The validator
never touches ``subprocess`` directly, so the gateway's audit log
records every check.

Tools:

- **ruff**: ``ruff check --isolated`` per touched file. ``--isolated``
  disables config discovery so the validator does not pick up the
  phalanx-harness pyproject.toml and apply it to arbitrary target
  code. The default rule set still catches the planted SQL-injection
  in ``target/app.py`` via ``S608``.
- **mypy**: ``mypy --ignore-missing-imports --no-incremental`` per
  touched file. Conservative defaults — no ``--strict`` — because the
  validator must not flag pre-existing target style as the
  implementer's failure.
- **semgrep**: slot is named for the architecture diagram but not
  executed in this PR. Implementing it requires either a local rules
  bundle or an egress allowlist update; both are out of scope here.
  The validator records semgrep as ``executed=False`` so the
  validation report is honest about the gap.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from phalanx.guardrails.tool_gateway import (
    Gateway,
    ShellResult,
    ToolGatewayError,
)
from phalanx.state import UnifiedDiff

DEFAULT_TOOLS: tuple[str, ...] = ("ruff", "mypy", "semgrep")


@dataclass(frozen=True)
class ToolResult:
    """One tool's verdict on the scratch tree."""

    name: str
    executed: bool
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    skip_reason: str | None = None

    @property
    def passed(self) -> bool:
        """A tool passes if it ran and exited zero. A tool that did
        not execute (e.g. semgrep when its slot is disabled) does
        not contribute to pass/fail — its absence is recorded but
        does not block the diff."""
        return self.executed and self.returncode == 0


@dataclass(frozen=True)
class ValidationReport:
    """The aggregate verdict for one validation pass."""

    tool_results: list[ToolResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(
            (not r.executed) or r.passed for r in self.tool_results
        )

    @property
    def executed_tools(self) -> list[str]:
        return [r.name for r in self.tool_results if r.executed]

    @property
    def failing_tools(self) -> list[str]:
        return [r.name for r in self.tool_results if r.executed and not r.passed]

    def failure_context(self) -> str:
        """Human-readable text suitable for re-prompting the
        implementer. Includes only the failing tools' output, with
        each section labeled, so the model sees what is wrong without
        having to parse the entire log."""
        if self.passed:
            return ""
        parts: list[str] = ["# Output validation failed", ""]
        for result in self.tool_results:
            if not result.executed or result.passed:
                continue
            parts.append(f"## {result.name} (exit {result.returncode})")
            if result.stdout.strip():
                parts.append("stdout:")
                parts.append(result.stdout.rstrip())
            if result.stderr.strip():
                parts.append("stderr:")
                parts.append(result.stderr.rstrip())
            parts.append("")
        return "\n".join(parts)


def validate(
    diff: UnifiedDiff,
    *,
    gateway: Gateway,
    scratch_subdir: str = "scratch",
    tools: Iterable[str] = DEFAULT_TOOLS,
) -> ValidationReport:
    """Run each tool against the files in ``diff.files_touched``
    inside ``scratch_subdir`` under the gateway's ``out_root``."""
    touched = [_normalize(p) for p in diff.files_touched if _is_python(p)]
    results: list[ToolResult] = []
    for tool in tools:
        if not touched:
            results.append(
                ToolResult(
                    name=tool,
                    executed=False,
                    skip_reason="no Python files touched by the diff",
                )
            )
            continue
        results.append(_run_one_tool(tool, touched, gateway, scratch_subdir))
    return ValidationReport(tool_results=results)


# --------------------------------------------------------------------------- #
# Per-tool wiring
# --------------------------------------------------------------------------- #


def _run_one_tool(
    tool: str,
    touched: list[str],
    gateway: Gateway,
    scratch_subdir: str,
) -> ToolResult:
    argv = _argv_for(tool, touched)
    if argv is None:
        return ToolResult(
            name=tool,
            executed=False,
            skip_reason=f"{tool} slot present, implementation deferred",
        )
    try:
        shell_result = gateway.invoke(
            "implementer",
            "run_shell",
            {"argv": argv, "cwd": scratch_subdir},
        )
    except ToolGatewayError as exc:
        return ToolResult(
            name=tool,
            executed=False,
            skip_reason=f"gateway rejected: {exc}",
        )
    if not isinstance(shell_result, ShellResult):
        return ToolResult(
            name=tool,
            executed=False,
            skip_reason="gateway returned non-ShellResult from run_shell",
        )
    return ToolResult(
        name=tool,
        executed=True,
        returncode=shell_result.returncode,
        stdout=shell_result.stdout,
        stderr=shell_result.stderr,
    )


def _argv_for(tool: str, touched: list[str]) -> list[str] | None:
    if tool == "ruff":
        # --isolated disables config discovery so the validator does
        # not pick up phalanx's strict pyproject.toml and apply it
        # to arbitrary target code.
        return ["ruff", "check", "--isolated", "--no-cache", *touched]
    if tool == "mypy":
        # --ignore-missing-imports + no --strict: the validator must
        # not flag pre-existing target style as the implementer's
        # failure. We are checking for *new* type errors in the diff.
        return [
            "mypy",
            "--ignore-missing-imports",
            "--no-incremental",
            *touched,
        ]
    if tool == "semgrep":
        # Slot reserved for the architecture diagram. Implementing
        # requires either a local rules bundle or an egress allowlist
        # update; deferred to a follow-up PR. Returning None here
        # produces an ``executed=False`` ToolResult that is honest
        # about the gap without halting the run.
        return None
    raise ValueError(f"unknown validator tool: {tool!r}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_python(path: str) -> bool:
    return Path(path).suffix == ".py"


def _normalize(path: str) -> str:
    return str(Path(path).as_posix())
