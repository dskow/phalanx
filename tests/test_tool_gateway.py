"""Contract tests for the tool gateway guardrail.

The gateway is the only module in the codebase that may invoke shell
or write to the filesystem on an agent's behalf. These tests pin the
contracts that make that claim defensible:

- Role-based tool denial.
- Pydantic argument validation.
- Path-traversal rejection, with the writable sandbox strictly
  narrower than the readable sandbox.
- Shell metacharacter rejection in argv elements.
- Executable allowlist for ``run_shell``.
- Audit callback fires for every invocation — pass or fail — with a
  stable categorical code on failure.

Subprocess execution is exercised against a mocked ``subprocess.run``
so the tests do not depend on which binaries happen to be in the test
container's PATH. The argv list, ``shell=False`` requirement, and
working directory are asserted on the captured call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from phalanx.guardrails.tool_gateway import (
    EXECUTABLE_ALLOWLIST,
    ROLE_ALLOWLIST,
    Gateway,
    GatewayAudit,
    GatewayConfig,
    GatewayEvent,
    ShellResult,
    ToolGatewayError,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    out.mkdir()
    (target / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (target / "pkg").mkdir()
    (target / "pkg" / "mod.py").write_text("# mod\n", encoding="utf-8")
    return target, out


@pytest.fixture
def recorder() -> tuple[list[GatewayEvent], GatewayAudit]:
    events: list[GatewayEvent] = []

    def record(event: GatewayEvent) -> None:
        events.append(event)

    return events, record


def _gateway(
    sandbox: tuple[Path, Path],
    recorder: tuple[list[GatewayEvent], GatewayAudit] | None = None,
) -> Gateway:
    target, out = sandbox
    audit = recorder[1] if recorder else None
    return Gateway(GatewayConfig(target_root=target, out_root=out), audit=audit)


# --------------------------------------------------------------------------- #
# Architecture-level invariants
# --------------------------------------------------------------------------- #


def test_role_allowlist_matches_architecture_doc() -> None:
    """Roles and per-role tool sets are part of the public contract.

    If any agent's permissions change, this assertion is the place to
    do the bookkeeping — failing this test forces the doc and code to
    move together.
    """
    assert set(ROLE_ALLOWLIST) == {"planner", "implementer", "test_writer", "reviewer"}
    assert ROLE_ALLOWLIST["planner"] == frozenset({"read_file", "list_files"})
    assert "write_file" not in ROLE_ALLOWLIST["planner"]
    assert "run_shell" not in ROLE_ALLOWLIST["planner"]
    assert "write_file" not in ROLE_ALLOWLIST["reviewer"]
    assert "run_shell" not in ROLE_ALLOWLIST["reviewer"]
    assert ROLE_ALLOWLIST["implementer"] >= frozenset(
        {"read_file", "write_file", "run_shell"}
    )


def test_executable_allowlist_is_minimal() -> None:
    assert "git" in EXECUTABLE_ALLOWLIST
    assert "pytest" in EXECUTABLE_ALLOWLIST
    assert "ruff" in EXECUTABLE_ALLOWLIST
    assert "mypy" in EXECUTABLE_ALLOWLIST
    assert "semgrep" in EXECUTABLE_ALLOWLIST
    # Specifically NOT on the allowlist:
    for forbidden in ("sh", "bash", "zsh", "curl", "wget", "nc", "python"):
        assert forbidden not in EXECUTABLE_ALLOWLIST


# --------------------------------------------------------------------------- #
# Role and tool authorization
# --------------------------------------------------------------------------- #


def test_planner_can_read_file(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    content = gw.invoke("planner", "read_file", {"path": "app.py"})
    assert "def f()" in content


def test_planner_cannot_write_file(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("planner", "write_file", {"path": "x.py", "content": "x"})
    assert excinfo.value.code == "role_denied"
    assert excinfo.value.role == "planner"
    assert excinfo.value.tool == "write_file"


def test_planner_cannot_run_shell(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("planner", "run_shell", {"argv": ["git", "status"]})
    assert excinfo.value.code == "role_denied"


def test_reviewer_is_read_only(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    for tool, args in (
        ("write_file", {"path": "x.py", "content": "x"}),
        ("run_shell", {"argv": ["git", "status"]}),
    ):
        with pytest.raises(ToolGatewayError) as excinfo:
            gw.invoke("reviewer", tool, args)
        assert excinfo.value.code == "role_denied"


def test_unknown_role_is_rejected(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("malicious", "read_file", {"path": "app.py"})
    assert excinfo.value.code == "unknown_role"


def test_unknown_tool_is_rejected(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("implementer", "exec_python", {"code": "print(1)"})
    assert excinfo.value.code == "unknown_tool"


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #


def test_read_file_requires_path(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("planner", "read_file", {})
    assert excinfo.value.code == "invalid_args"


def test_write_file_rejects_extra_args(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke(
            "implementer",
            "write_file",
            {"path": "x.py", "content": "x", "mode": "0777"},
        )
    assert excinfo.value.code == "invalid_args"


def test_run_shell_requires_non_empty_argv(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("implementer", "run_shell", {"argv": []})
    assert excinfo.value.code == "invalid_args"


# --------------------------------------------------------------------------- #
# Path sandbox
# --------------------------------------------------------------------------- #


def test_read_file_relative_inside_target(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    content = gw.invoke("planner", "read_file", {"path": "pkg/mod.py"})
    assert "# mod" in content


def test_read_file_path_traversal_is_rejected(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("planner", "read_file", {"path": "../etc/passwd"})
    assert excinfo.value.code == "path_outside_sandbox"


def test_read_file_absolute_path_outside_sandbox_is_rejected(
    sandbox: tuple[Path, Path],
) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("planner", "read_file", {"path": "/etc/passwd"})
    assert excinfo.value.code == "path_outside_sandbox"


def test_read_file_null_byte_in_path_is_rejected(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("planner", "read_file", {"path": "app.py\x00.txt"})
    assert excinfo.value.code == "path_invalid"


def test_write_file_into_out_root_succeeds(sandbox: tuple[Path, Path]) -> None:
    target, out = sandbox
    gw = _gateway(sandbox)
    n = gw.invoke(
        "implementer",
        "write_file",
        {"path": "diff/0001.patch", "content": "hello"},
    )
    written = out / "diff" / "0001.patch"
    assert written.read_text(encoding="utf-8") == "hello"
    assert n == 5


def test_write_file_into_target_root_is_rejected(
    sandbox: tuple[Path, Path],
) -> None:
    """target_root is read-only. The gateway is the *only* enforcement
    of that property — without this test we would have no proof."""
    target, _ = sandbox
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke(
            "implementer",
            "write_file",
            {"path": str(target / "tainted.py"), "content": "owned"},
        )
    assert excinfo.value.code == "path_outside_sandbox"
    assert not (target / "tainted.py").exists()


def test_list_files_returns_relative_paths(sandbox: tuple[Path, Path]) -> None:
    gw = _gateway(sandbox)
    files = gw.invoke("planner", "list_files", {"path": "."})
    assert "app.py" in files
    assert "pkg/mod.py" in files


# --------------------------------------------------------------------------- #
# Shell metacharacter denial
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_element",
    [
        ";",
        "&&",
        "||",
        "|",
        "`whoami`",
        "$(whoami)",
        "${HOME}",
        ">",
        "<",
        ">>",
        "\n",
        "\r",
        "status; rm -rf /",
        "status\nls",
    ],
)
def test_run_shell_rejects_metacharacter_in_argv(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    bad_element: str,
) -> None:
    """The roadmap-named contract test, generalized to every documented
    shell metacharacter family."""
    called: list[Any] = []

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        called.append((args, kwargs))
        raise AssertionError("subprocess.run must not be reached when guardrail fires")

    monkeypatch.setattr(subprocess, "run", fake_run)

    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("implementer", "run_shell", {"argv": ["git", bad_element]})
    assert excinfo.value.code == "shell_metachar"
    assert called == []  # subprocess was never invoked


def test_run_shell_executable_not_on_allowlist_is_rejected(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("subprocess must not run"),
    )
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke("implementer", "run_shell", {"argv": ["sh", "-c", "git status"]})
    assert excinfo.value.code == "executable_denied"


def test_run_shell_cwd_inside_out_root_is_used(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cwd under out_root is honored — verifies the implementer can
    run git apply from a scratch directory."""
    _, out = sandbox
    (out / "scratch").mkdir()
    captured: dict[str, Any] = {}

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv: Any, **kwargs: Any) -> Any:
        captured["cwd"] = kwargs["cwd"]
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    gw = _gateway(sandbox)
    gw.invoke(
        "implementer",
        "run_shell",
        {"argv": ["git", "status"], "cwd": "scratch"},
    )
    assert captured["cwd"] == str((out / "scratch").resolve())


def test_run_shell_cwd_outside_sandbox_is_rejected(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cwd that escapes out_root must be rejected before subprocess
    is reached — same property as path_outside_sandbox for write_file."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("subprocess must not run"),
    )
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke(
            "implementer",
            "run_shell",
            {"argv": ["git", "status"], "cwd": "../escape"},
        )
    assert excinfo.value.code == "path_outside_sandbox"


def test_run_shell_cwd_must_exist(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cwd that resolves inside the sandbox but does not exist as a
    directory is rejected — running a command in a non-existent dir
    would silently misbehave."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("subprocess must not run"),
    )
    gw = _gateway(sandbox)
    with pytest.raises(ToolGatewayError) as excinfo:
        gw.invoke(
            "implementer",
            "run_shell",
            {"argv": ["git", "status"], "cwd": "not_there"},
        )
    assert excinfo.value.code == "cwd_invalid"


def test_run_shell_default_cwd_is_target_root(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backwards-compatibility check: omitting cwd uses target_root."""
    target, _ = sandbox
    captured: dict[str, Any] = {}

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv: Any, **kwargs: Any) -> Any:
        captured["cwd"] = kwargs["cwd"]
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    gw = _gateway(sandbox)
    gw.invoke("implementer", "run_shell", {"argv": ["git", "status"]})
    assert captured["cwd"] == str(target.resolve())


def test_run_shell_invokes_subprocess_without_shell(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway must pass argv as a list, never as a shell string,
    and ``shell=False`` is the load-bearing kwarg. Verified directly
    against the captured subprocess.run call."""
    captured: dict[str, Any] = {}

    class FakeCompleted:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(argv: Any, **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    gw = _gateway(sandbox)
    result = gw.invoke(
        "implementer", "run_shell", {"argv": ["git", "status", "--short"]}
    )
    assert isinstance(result, ShellResult)
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert captured["argv"] == ["git", "status", "--short"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(sandbox[0])


# --------------------------------------------------------------------------- #
# Audit callback
# --------------------------------------------------------------------------- #


def test_audit_callback_records_success(
    sandbox: tuple[Path, Path],
    recorder: tuple[list[GatewayEvent], GatewayAudit],
) -> None:
    gw = _gateway(sandbox, recorder)
    gw.invoke("planner", "read_file", {"path": "app.py"})

    events, _ = recorder
    assert len(events) == 1
    event = events[0]
    assert event.passed is True
    assert event.role == "planner"
    assert event.tool == "read_file"
    assert event.code is None


def test_audit_callback_records_failure_with_code(
    sandbox: tuple[Path, Path],
    recorder: tuple[list[GatewayEvent], GatewayAudit],
) -> None:
    """The roadmap-named contract test: an attempted shell command
    containing ``;`` is rejected with a recorded audit event."""
    gw = _gateway(sandbox, recorder)
    with pytest.raises(ToolGatewayError):
        gw.invoke("implementer", "run_shell", {"argv": ["git", "status;", "ls"]})

    events, _ = recorder
    assert len(events) == 1
    event = events[0]
    assert event.passed is False
    assert event.code == "shell_metachar"
    assert event.role == "implementer"
    assert event.tool == "run_shell"
    assert event.reason is not None
    assert ";" in event.reason or "metachar" in event.reason


def test_audit_callback_is_optional(sandbox: tuple[Path, Path]) -> None:
    """A gateway without an audit hook still works — the hook is for
    audit, not for control flow."""
    gw = _gateway(sandbox)
    content = gw.invoke("planner", "read_file", {"path": "app.py"})
    assert "def f()" in content


# --------------------------------------------------------------------------- #
# Module-level invariant
# --------------------------------------------------------------------------- #


def test_subprocess_is_imported_only_by_the_gateway() -> None:
    """Per docs/GUARDRAILS.md: 'The gateway is the only module in the
    codebase that imports subprocess or os.system.' This test asserts
    that as a static property of the source tree, not as a runtime
    behavior — so a regression that introduces a stray subprocess
    import elsewhere fails immediately rather than at exploit time.

    Tests are explicitly exempt: they may mock subprocess.
    """
    repo_root = Path(__file__).resolve().parent.parent
    phalanx_pkg = repo_root / "phalanx"
    gateway_path = phalanx_pkg / "guardrails" / "tool_gateway.py"

    offenders: list[Path] = []
    for path in phalanx_pkg.rglob("*.py"):
        if path == gateway_path:
            continue
        source = path.read_text(encoding="utf-8")
        if "import subprocess" in source or "from subprocess" in source:
            offenders.append(path)
        if "os.system" in source:
            offenders.append(path)
    assert offenders == [], (
        "subprocess/os.system used outside tool_gateway.py: "
        f"{[str(p) for p in offenders]}"
    )


def test_gateway_module_exposes_documented_surface() -> None:
    """Pin the public symbols so a refactor that renames or removes
    one fails this test before downstream callers break."""
    import phalanx.guardrails.tool_gateway as tg_module

    for name in (
        "Gateway",
        "GatewayConfig",
        "GatewayEvent",
        "ToolGatewayError",
        "ROLE_ALLOWLIST",
        "EXECUTABLE_ALLOWLIST",
        "ShellResult",
    ):
        assert hasattr(tg_module, name), (
            f"tool_gateway missing public symbol: {name}"
        )
