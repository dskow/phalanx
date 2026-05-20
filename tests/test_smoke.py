"""Scaffold smoke tests.

These tests verify the project shape and the audit-log writer.
They do not invoke any agent — agent tests land alongside each
agent's implementation in successive PRs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from castrum import __version__
from castrum.audit.logger import AuditLogger
from castrum.cli import main
from castrum.graph import describe_graph
from castrum.guardrails.egress_firewall import ALLOWED_HOSTS, is_allowed
from castrum.state import ModernizationRequest, StudioState


def test_version_is_set() -> None:
    assert __version__ == "0.1.0"


def test_graph_shape_matches_architecture_doc() -> None:
    graph = describe_graph()
    assert graph["nodes"] == ["planner", "implementer", "test_writer", "reviewer"]
    assert ["planner", "implementer"] in graph["edges"]


def test_egress_allowlist_is_minimal() -> None:
    assert ALLOWED_HOSTS == frozenset({"api.anthropic.com"})
    assert is_allowed("api.anthropic.com")
    assert not is_allowed("evil.example")
    assert not is_allowed("github.com")


def test_studio_state_validates_request() -> None:
    state = StudioState(
        request=ModernizationRequest(
            title="t", body="b", target_root="/tmp/x"
        )
    )
    assert state.plan is None
    assert state.audit_log == []


def test_audit_logger_appends_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path)
    logger.record_scaffold_event(node="cli", message="hello", request_title="t")
    logger.record_scaffold_event(node="cli", message="world", request_title="t")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    events = [json.loads(line) for line in lines]
    assert events[0]["message"] == "hello"
    assert events[1]["message"] == "world"
    assert all("ts" in e for e in events)


def test_cli_run_against_scaffold_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    (target / "REQUEST.md").write_text("# Test request\n\nbody", encoding="utf-8")

    rc = main(["run", "--target", str(target), "--out", str(out)])
    assert rc == 0
    assert (out / "audit.jsonl").is_file()

    events = [json.loads(line) for line in (out / "audit.jsonl").read_text().splitlines()]
    assert any(e["request_title"] == "Test request" for e in events)


def test_cli_run_fails_without_request_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()

    with pytest.raises(FileNotFoundError):
        main(["run", "--target", str(target), "--out", str(out)])
