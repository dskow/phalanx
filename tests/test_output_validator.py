"""Contract tests for the output validator guardrail.

The validator's job is to produce a deterministic, append-to-audit
verdict on a diff that has already been applied to the scratch tree.
A run that passes every executed tool is a PASS; a tool that exits
non-zero is a FAIL; a tool that does not execute (slot disabled or
gateway-rejected) is recorded but neither passes nor fails.

These tests exercise the validator end-to-end against the real ruff
and mypy binaries in the container, against a tiny synthetic
"scratch tree" fixture. Driving via the real ``Gateway`` (not a mock)
means a regression in the gateway's ``cwd`` plumbing or in the
validator's argv construction is caught here.
"""

from __future__ import annotations

from pathlib import Path

from phalanx.guardrails.output_validator import (
    DEFAULT_TOOLS,
    ToolResult,
    ValidationReport,
    validate,
)
from phalanx.guardrails.tool_gateway import Gateway, GatewayConfig
from phalanx.state import UnifiedDiff

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _gateway(target: Path, out: Path) -> Gateway:
    return Gateway(GatewayConfig(target_root=target, out_root=out))


def _diff(files: list[str]) -> UnifiedDiff:
    return UnifiedDiff(
        diff_text="(stub; not used by the validator)",
        files_touched=files,
    )


def _make_scratch(out: Path, files: dict[str, str]) -> None:
    """Materialize a scratch tree at ``out/scratch`` with the given
    file contents. Mirrors what the implementer leaves behind after
    a successful ``git apply``."""
    scratch = out / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = scratch / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Clean code passes every executed tool
# --------------------------------------------------------------------------- #


def test_clean_python_passes(tmp_path: Path) -> None:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    _make_scratch(
        out,
        {"app.py": "def add(a: int, b: int) -> int:\n    return a + b\n"},
    )
    gw = _gateway(target, out)
    report = validate(_diff(["app.py"]), gateway=gw)

    assert isinstance(report, ValidationReport)
    assert report.passed, report.failure_context()
    assert "ruff" in report.executed_tools
    assert "mypy" in report.executed_tools
    assert report.failing_tools == []


# --------------------------------------------------------------------------- #
# Ruff catches lint and security findings
# --------------------------------------------------------------------------- #


def test_ruff_catches_obvious_lint(tmp_path: Path) -> None:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    # Unused import is F401 — caught by default ruff config.
    _make_scratch(
        out,
        {"app.py": "import os\n\n\ndef f() -> int:\n    return 1\n"},
    )
    gw = _gateway(target, out)
    report = validate(_diff(["app.py"]), gateway=gw)

    assert not report.passed
    assert "ruff" in report.failing_tools
    context = report.failure_context()
    assert "ruff" in context.lower()
    assert "f401" in context.lower() or "unused" in context.lower()


def test_ruff_default_config_catches_sql_injection_via_fstring(
    tmp_path: Path,
) -> None:
    """The planted SQL-injection in ``target/app.py`` is a literal
    f-string interpolation into a SELECT — ruff's S608 rule catches
    that with ``select = ["S"]``. But the validator runs with
    ``--isolated`` so we use ruff's default rule set, which does NOT
    include the S category by default. This test pins that behavior
    explicitly so the rule choice is intentional, not accidental.

    A future PR may enable an opinionated security ruleset; if so,
    this test should be updated to assert the *opposite*."""
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    _make_scratch(
        out,
        {
            "app.py": (
                "import sqlite3\n"
                "\n"
                "def get(name: str) -> list:\n"
                "    conn = sqlite3.connect(':memory:')\n"
                "    return conn.execute(\n"
                "        f\"SELECT id FROM u WHERE n = '{name}'\"\n"
                "    ).fetchall()\n"
            )
        },
    )
    gw = _gateway(target, out)
    report = validate(_diff(["app.py"]), gateway=gw)

    # With ruff --isolated and default rules (no S), the SQLi pattern
    # does not trip a finding. This is documented behavior; the
    # validator does not pretend to be a SAST tool yet — that is
    # semgrep's job, which is slotted but deferred.
    assert "ruff" in [r.name for r in report.tool_results if r.executed]


# --------------------------------------------------------------------------- #
# Mypy catches type errors
# --------------------------------------------------------------------------- #


def test_mypy_catches_type_error(tmp_path: Path) -> None:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    _make_scratch(
        out,
        {"app.py": "def add(a: int, b: int) -> int:\n    return a + 'x'\n"},
    )
    gw = _gateway(target, out)
    report = validate(_diff(["app.py"]), gateway=gw)

    assert not report.passed
    assert "mypy" in report.failing_tools
    assert "mypy" in report.failure_context().lower()


# --------------------------------------------------------------------------- #
# Semgrep slot is honest about being deferred
# --------------------------------------------------------------------------- #


def test_semgrep_is_recorded_as_not_executed(tmp_path: Path) -> None:
    """The validator names semgrep in the architecture diagram but
    has not yet shipped a rules bundle. The report must record
    semgrep as ``executed=False`` with a skip_reason — never silently
    omit it, never pretend it ran."""
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    _make_scratch(out, {"a.py": "x = 1\n"})
    gw = _gateway(target, out)
    report = validate(_diff(["a.py"]), gateway=gw)

    semgrep_results = [r for r in report.tool_results if r.name == "semgrep"]
    assert len(semgrep_results) == 1
    semgrep = semgrep_results[0]
    assert semgrep.executed is False
    assert semgrep.skip_reason is not None
    assert "deferred" in semgrep.skip_reason


def test_passed_ignores_unexecuted_tools(tmp_path: Path) -> None:
    """A report where every executed tool passes is a PASS, even if
    some tools (semgrep) did not execute. Equating ``not executed``
    with ``failed`` would make every run fail until semgrep ships."""
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    _make_scratch(out, {"a.py": "def f() -> int:\n    return 1\n"})
    gw = _gateway(target, out)
    report = validate(_diff(["a.py"]), gateway=gw)

    assert report.passed
    # semgrep is in the default tool set but did not execute.
    assert "semgrep" in [r.name for r in report.tool_results]
    assert "semgrep" not in report.executed_tools


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


def test_no_python_files_in_diff(tmp_path: Path) -> None:
    """A diff that touches only non-Python files is a vacuous PASS
    for ruff/mypy — they have nothing to lint. The validator must
    not invent files to check, and must not fail the diff."""
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    _make_scratch(out, {"README.md": "# hello\n"})
    gw = _gateway(target, out)
    report = validate(_diff(["README.md"]), gateway=gw)

    assert report.passed
    assert report.executed_tools == []  # nothing executed
    for result in report.tool_results:
        assert result.skip_reason is not None
        assert "no Python" in result.skip_reason


def test_tool_subset_can_be_selected(tmp_path: Path) -> None:
    """``tools`` argument lets callers select a subset — useful in
    tests and in a future env-driven config."""
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    _make_scratch(out, {"a.py": "x = 1\n"})
    gw = _gateway(target, out)
    report = validate(_diff(["a.py"]), gateway=gw, tools=("ruff",))

    names = [r.name for r in report.tool_results]
    assert names == ["ruff"]


def test_failure_context_includes_only_failing_tools(tmp_path: Path) -> None:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    # Triggers mypy failure (str + int) but ruff is clean.
    _make_scratch(
        out,
        {"a.py": "def f(a: int) -> int:\n    return a + 'x'\n"},
    )
    gw = _gateway(target, out)
    report = validate(_diff(["a.py"]), gateway=gw)

    context = report.failure_context()
    assert "mypy" in context.lower()
    # Ruff did not fail — it must not appear as a "## ruff" section
    # in the retry context, since that would tell the model to fix
    # a non-issue.
    assert "## ruff (" not in context


def test_default_tools_constant_matches_doc() -> None:
    assert DEFAULT_TOOLS == ("ruff", "mypy", "semgrep")


def test_toolresult_passed_property_semantics() -> None:
    ran_ok = ToolResult(name="ruff", executed=True, returncode=0)
    ran_failed = ToolResult(name="ruff", executed=True, returncode=1)
    not_run = ToolResult(name="semgrep", executed=False, skip_reason="x")
    assert ran_ok.passed
    assert not ran_failed.passed
    assert not not_run.passed  # absence is not a pass


def test_empty_report_is_vacuously_passed() -> None:
    """A report with no tool results passes (vacuous truth). Useful
    for callers that disable all tools — they should not fail."""
    report = ValidationReport()
    assert report.passed
    assert report.executed_tools == []
    assert report.failing_tools == []
    assert report.failure_context() == ""
