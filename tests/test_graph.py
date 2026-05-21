"""Contract tests for the compiled LangGraph state machine.

These tests verify the graph topology matches the architecture
diagram, that the planner node is live and the rest are still
pass-through stubs, and that a full run with a stub invoke produces
the expected state transitions plus an audit event for the planner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from phalanx.agents.planner import PlannerError
from phalanx.graph import build_graph, describe_graph
from phalanx.state import ModernizationRequest, StudioState


def _valid_plan_response() -> dict[str, Any]:
    return {
        "summary": "stub plan",
        "changes": [
            {
                "file_path": "app.py",
                "rationale": "fix it",
                "acceptance_criterion": "it is fixed",
            }
        ],
    }


def _initial_state(target: Path) -> StudioState:
    return StudioState(
        request=ModernizationRequest(
            title="t", body="b", target_root=str(target)
        )
    )


def test_describe_graph_reads_from_compiled_topology() -> None:
    graph = describe_graph()
    assert graph["nodes"] == ["planner", "implementer", "test_writer", "reviewer"]
    edges = graph["edges"]
    assert isinstance(edges, list)
    assert ["planner", "implementer"] in edges
    assert ["implementer", "test_writer"] in edges
    assert ["test_writer", "reviewer"] in edges


def test_graph_runs_end_to_end_with_stub_invoke(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "x.py").write_text("def f(): return 1\n", encoding="utf-8")

    graph = build_graph(invoke=lambda _prompt: _valid_plan_response())
    final = graph.invoke(_initial_state(target))

    # LangGraph returns either a dict update or a Pydantic model
    # depending on the state-channel configuration. Normalize.
    plan_obj = final["plan"] if isinstance(final, dict) else final.plan
    assert plan_obj is not None
    assert plan_obj.summary == "stub plan"


def test_planner_appends_audit_event_on_success(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    graph = build_graph(invoke=lambda _prompt: _valid_plan_response())
    final = graph.invoke(_initial_state(target))

    audit_log = final["audit_log"] if isinstance(final, dict) else final.audit_log
    planner_events = [e for e in audit_log if e.node == "planner"]
    assert len(planner_events) == 1
    event = planner_events[0]
    assert event.guardrails_passed == ["input_filter"]
    assert event.guardrails_failed == []
    assert event.input_hash.startswith("sha256:")
    assert event.output_hash.startswith("sha256:")
    assert event.duration_ms >= 0


def test_planner_failure_propagates_as_planner_error(tmp_path: Path) -> None:
    """When the planner halts, the graph halts. The failure must not
    be swallowed by LangGraph's error handling and must not produce a
    partial state with ``plan=None`` masquerading as success."""
    target = tmp_path / "target"
    target.mkdir()

    graph = build_graph(invoke=lambda _prompt: {"summary": "missing changes"})

    with pytest.raises(PlannerError):
        graph.invoke(_initial_state(target))


def test_stub_nodes_do_not_mutate_state(tmp_path: Path) -> None:
    """The implementer/test_writer/reviewer nodes are still stubs in
    this PR. The graph must run through them without setting their
    output fields — otherwise we are silently faking progress.
    """
    target = tmp_path / "target"
    target.mkdir()

    graph = build_graph(invoke=lambda _prompt: _valid_plan_response())
    final = graph.invoke(_initial_state(target))

    if isinstance(final, dict):
        assert final.get("diff") is None
        assert final.get("tests") is None
        assert final.get("review") is None
    else:
        assert final.diff is None
        assert final.tests is None
        assert final.review is None
