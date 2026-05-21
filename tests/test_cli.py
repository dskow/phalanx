"""Contract tests for the real-run CLI flow.

The CLI ties every agent together: it drives the graph end-to-end,
persists the audit log, and emits the correct artifact for the
final verdict.

Pinned invariants:

- PASS → ``pr_payload.json`` lands in ``out/``, the audit log is
  written, exit code is 0.
- FAIL → ``verdict.json`` lands in ``out/`` (no ``pr_payload.json``),
  exit code is 2. The CLI does not pretend a FAIL run is PR-ready.
- An agent error (schema failure, apply failure, anything raised by
  the agent layer) → ``error.json`` lands in ``out/``, exit code is
  1. The operator can tell the difference between "the reviewer
  judged unfit" and "the agent crashed."
- Missing ``ANTHROPIC_API_KEY`` on a default real run → exit code 3
  with a clear stderr message; no graph invocation. ``--scaffold``
  remains usable without a key.

Tests use ``monkeypatch.setenv`` to control the env-var check and
pass stub invokes through the CLI's keyword-only callable seam
(which exists so this test file can drive an end-to-end CLI flow
without any model traffic).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from phalanx.cli import _cmd_run

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def target_with_request(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    (target / "REQUEST.md").write_text(
        "# fix subtraction bug\n\nMake add return the sum.\n",
        encoding="utf-8",
    )
    (target / "app.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    return target, out


def _plan_response() -> dict[str, Any]:
    return {
        "summary": "fix subtraction bug",
        "changes": [
            {
                "file_path": "app.py",
                "rationale": "return the sum, not the difference",
                "acceptance_criterion": "add(2, 3) == 5",
            }
        ],
    }


def _diff_response() -> dict[str, Any]:
    return {
        "diff_text": (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a - b\n"
            "+    return a + b\n"
        ),
        "files_touched": ["app.py"],
    }


def _test_writer_response() -> dict[str, Any]:
    return {
        "diff_text": (
            "--- /dev/null\n"
            "+++ b/test_app.py\n"
            "@@ -0,0 +1,5 @@\n"
            "+from app import add\n"
            "+\n"
            "+\n"
            "+def test_add() -> None:\n"
            "+    assert add(2, 3) == 5\n"
        ),
        "files_touched": ["test_app.py"],
        "pytest_exit_code": 0,
    }


def _pass_review() -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "criteria": [
            {
                "criterion": "add(2, 3) == 5",
                "passed": True,
                "notes": "implementation returns a + b",
            }
        ],
        "rationale": "criterion met, tests green",
    }


def _fail_review() -> dict[str, Any]:
    return {
        "verdict": "FAIL",
        "criteria": [
            {
                "criterion": "add(2, 3) == 5",
                "passed": False,
                "notes": "could not verify",
            }
        ],
        "rationale": "acceptance criterion not met",
    }


# --------------------------------------------------------------------------- #
# PASS run
# --------------------------------------------------------------------------- #


def test_pass_run_writes_pr_payload(
    target_with_request: tuple[Path, Path],
) -> None:
    target, out = target_with_request
    rc = _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _pass_review(),
    )
    assert rc == 0
    assert (out / "pr_payload.json").is_file()
    assert (out / "audit.jsonl").is_file()
    # FAIL artifact must NOT exist on a PASS run.
    assert not (out / "verdict.json").exists()


def test_pass_payload_schema(
    target_with_request: tuple[Path, Path],
) -> None:
    target, out = target_with_request
    _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _pass_review(),
    )
    payload = json.loads((out / "pr_payload.json").read_text())
    assert payload["title"] == "fix subtraction bug"
    assert "body" in payload
    assert "add" in payload["body"].lower()
    assert payload["diff"]["files_touched"] == ["app.py"]
    assert payload["tests"]["pytest_exit_code"] == 0
    assert payload["review"]["verdict"] == "PASS"


def test_audit_log_contains_one_event_per_node(
    target_with_request: tuple[Path, Path],
) -> None:
    target, out = target_with_request
    _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _pass_review(),
    )
    lines = (out / "audit.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    nodes = [e["node"] for e in events]
    assert nodes == ["planner", "implementer", "test_writer", "reviewer"]


# --------------------------------------------------------------------------- #
# FAIL run
# --------------------------------------------------------------------------- #


def test_fail_run_writes_verdict_not_pr_payload(
    target_with_request: tuple[Path, Path],
) -> None:
    """A FAIL verdict means the work is not PR-ready. The CLI must
    not write a pr_payload.json that would tempt the operator into
    shipping it. Pin both the absence of pr_payload and the exit
    code in one test."""
    target, out = target_with_request
    rc = _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _fail_review(),
    )
    assert rc == 2
    assert (out / "verdict.json").is_file()
    assert not (out / "pr_payload.json").exists()
    # Audit log is still written — failed runs are still auditable.
    assert (out / "audit.jsonl").is_file()


def test_fail_verdict_payload_lists_failing_criteria(
    target_with_request: tuple[Path, Path],
) -> None:
    target, out = target_with_request
    _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _fail_review(),
    )
    verdict = json.loads((out / "verdict.json").read_text())
    assert verdict["verdict"] == "FAIL"
    assert verdict["failing_criteria"], "expected at least one failing criterion"
    assert verdict["failing_criteria"][0]["criterion"] == "add(2, 3) == 5"


# --------------------------------------------------------------------------- #
# Agent error
# --------------------------------------------------------------------------- #


def test_agent_error_writes_error_artifact(
    target_with_request: tuple[Path, Path],
) -> None:
    """A schema-validation failure mid-graph (here: the planner
    returning a malformed plan) is a different category from a FAIL
    verdict. The CLI must distinguish them by writing error.json
    (not verdict.json) and exiting 1 (not 2)."""
    target, out = target_with_request
    rc = _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: {"summary": "missing changes field"},
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _pass_review(),
    )
    assert rc == 1
    assert (out / "error.json").is_file()
    assert not (out / "pr_payload.json").exists()
    assert not (out / "verdict.json").exists()
    err = json.loads((out / "error.json").read_text())
    assert err["error_type"] == "PlannerError"
    # Diagnostic detail must be present — the validation_error
    # message tells the operator *why* the schema check failed.
    assert "validation_error" in err
    assert "changes" in err["validation_error"]


def test_apply_failure_surfaces_git_stderr_in_error_artifact(
    target_with_request: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: a real-model run hit ``git apply failed with exit
    128`` and the operator had no way to see what git actually
    complained about. The CLI must include ``apply_stderr`` in
    error.json AND echo it to the terminal, so debugging does not
    require digging into a JSON file the operator may not even know
    exists.
    """
    target, out = target_with_request
    # A diff that applies cleanly to the source line numbers but
    # references a file content that does not match — git apply
    # rejects this with a clear error on stderr.
    bad_diff = {
        "diff_text": (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-this line is not in the source file\n"
            "+def replacement(): pass\n"
        ),
        "files_touched": ["app.py"],
    }
    rc = _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: bad_diff,
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _pass_review(),
    )
    assert rc == 1
    err = json.loads((out / "error.json").read_text())
    assert err["error_type"] == "ImplementerError"
    # The crucial fields for debugging an apply failure.
    assert "apply_stderr" in err
    assert err["apply_stderr"], "apply_stderr is present but empty"
    assert "attempted_diff" in err
    assert "this line is not in the source file" in err["attempted_diff"]
    # Stderr was also echoed to the terminal for fast triage.
    captured_err = capsys.readouterr().err
    assert "ImplementerError" in captured_err
    assert "apply stderr" in captured_err


# --------------------------------------------------------------------------- #
# Missing API key on real run
# --------------------------------------------------------------------------- #


def test_missing_api_key_halts_with_clear_exit_code(
    target_with_request: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A default real run without ANTHROPIC_API_KEY must halt with
    exit code 3 *before* invoking the graph — the operator gets a
    clear, fast error instead of a deep SDK stack trace."""
    target, out = target_with_request
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No invokes passed → using_defaults is True → the env-var check
    # fires.
    rc = _cmd_run(target, out, max_iterations=2)
    assert rc == 3
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert "--scaffold" in err
    # No graph invocation means no audit log either.
    assert not (out / "audit.jsonl").exists()


def test_missing_api_key_does_not_block_when_invokes_are_injected(
    target_with_request: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all four invokes are injected (as in these tests), the
    env-var check is bypassed — no model traffic actually happens.
    Without this bypass the CLI flow could not be tested at all."""
    target, out = target_with_request
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _pass_review(),
    )
    assert rc == 0


# --------------------------------------------------------------------------- #
# Scratch isolation across runs
# --------------------------------------------------------------------------- #


def test_scratch_is_wiped_at_run_start(
    target_with_request: tuple[Path, Path],
) -> None:
    """Regression: ``./out:/app/out`` host mounts (typical Docker
    usage) used to leak state across runs — a previous run that
    succeeded at the implementer but failed at the test_writer left
    a half-applied tests/test_app.py in scratch, and the next run's
    test_writer read stale content for its prompt. Each run must
    start with a fresh scratch tree.
    """
    target, out = target_with_request
    out.mkdir(parents=True, exist_ok=True)
    # Plant a leftover file from a previous run.
    scratch = out / "scratch"
    scratch.mkdir()
    (scratch / "stale_from_previous_run.py").write_text(
        "this file should not survive into the next run\n",
        encoding="utf-8",
    )
    (scratch / "tests").mkdir()
    (scratch / "tests" / "test_app.py").write_text(
        "# stale test file with content from a previous run's diff\n",
        encoding="utf-8",
    )

    rc = _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _pass_review(),
    )
    assert rc == 0

    # Stale file from the previous run is gone.
    assert not (scratch / "stale_from_previous_run.py").exists()
    # The new run's tests/test_app.py is the model's output, not the stale one.
    new_test_content = (scratch / "test_app.py").read_text(encoding="utf-8")
    assert "stale test file" not in new_test_content


def test_previous_run_deliverables_are_wiped(
    target_with_request: tuple[Path, Path],
) -> None:
    """A previous run's pr_payload.json / verdict.json / error.json /
    audit.jsonl must not leak into a new run. The audit log is
    append-only by default, so without explicit wiping a new run's
    log would be concatenated onto the previous run's content.
    Similarly a stale verdict.json from a previous FAIL would
    persist alongside this run's PASS pr_payload.json, which is
    confusing at best and incorrect at worst.
    """
    target, out = target_with_request
    out.mkdir(parents=True, exist_ok=True)
    # Plant deliverables and scratch state from a previous run.
    (out / "audit.jsonl").write_text("previous run log\n", encoding="utf-8")
    (out / "verdict.json").write_text(
        '{"verdict": "FAIL", "rationale": "previous run"}\n',
        encoding="utf-8",
    )
    (out / "error.json").write_text(
        '{"error_type": "PreviousError"}\n', encoding="utf-8"
    )
    (out / "scratch").mkdir()
    (out / "scratch" / "stale.py").write_text("stale\n", encoding="utf-8")

    rc = _cmd_run(
        target,
        out,
        max_iterations=2,
        planner_invoke=lambda _p: _plan_response(),
        implementer_invoke=lambda _p: _diff_response(),
        test_writer_invoke=lambda _p: _test_writer_response(),
        reviewer_invoke=lambda _p: _pass_review(),
    )
    assert rc == 0

    # Scratch wiped.
    assert not (out / "scratch" / "stale.py").exists()
    # Stale verdict and error from previous FAIL are gone (this
    # run is a PASS — keeping them would contradict the PASS
    # payload that we just wrote).
    assert not (out / "verdict.json").exists()
    assert not (out / "error.json").exists()
    # Audit log was wiped, then rewritten by this run alone.
    log = (out / "audit.jsonl").read_text(encoding="utf-8")
    assert "previous run log" not in log
    assert "planner" in log
