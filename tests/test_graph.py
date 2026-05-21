"""Contract tests for the compiled LangGraph state machine.

Verifies the graph topology matches the architecture diagram, that
the planner and implementer nodes are live and the rest are still
pass-through stubs, and that a full run with stub invokes produces
the expected state transitions plus an audit event for every live
node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from phalanx.agents.implementer import ImplementerError
from phalanx.agents.planner import PlannerError
from phalanx.graph import build_graph, describe_graph
from phalanx.guardrails.tool_gateway import Gateway, GatewayConfig
from phalanx.state import ModernizationRequest, StudioState

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _valid_plan_response() -> dict[str, Any]:
    return {
        "summary": "fix subtraction bug",
        "changes": [
            {
                "file_path": "app.py",
                "rationale": "return the sum, not the difference",
                "acceptance_criterion": "add(2,3) == 5",
            }
        ],
    }


def _valid_diff_response() -> dict[str, Any]:
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


@pytest.fixture
def target_and_gateway(tmp_path: Path) -> tuple[Path, Gateway]:
    target = tmp_path / "target"
    out = tmp_path / "out"
    target.mkdir()
    out.mkdir()
    (target / "app.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    gw = Gateway(GatewayConfig(target_root=target, out_root=out))
    return target, gw


def _initial_state(target: Path) -> StudioState:
    return StudioState(
        request=ModernizationRequest(
            title="t", body="b", target_root=str(target)
        )
    )


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #


def test_describe_graph_reads_from_compiled_topology() -> None:
    graph = describe_graph()
    assert graph["nodes"] == ["planner", "implementer", "test_writer", "reviewer"]
    edges = graph["edges"]
    assert isinstance(edges, list)
    assert ["planner", "implementer"] in edges
    assert ["implementer", "test_writer"] in edges
    assert ["test_writer", "reviewer"] in edges


# --------------------------------------------------------------------------- #
# End-to-end with stub invokes
# --------------------------------------------------------------------------- #


def test_graph_runs_through_planner_and_implementer(
    target_and_gateway: tuple[Path, Gateway],
) -> None:
    target, gw = target_and_gateway

    graph = build_graph(
        planner_invoke=lambda _p: _valid_plan_response(),
        implementer_invoke=lambda _p: _valid_diff_response(),
        gateway=gw,
    )
    final = graph.invoke(_initial_state(target))

    plan = final["plan"] if isinstance(final, dict) else final.plan
    diff = final["diff"] if isinstance(final, dict) else final.diff
    assert plan is not None
    assert diff is not None
    assert diff.files_touched == ["app.py"]


def test_implementer_appends_audit_event_with_gateway_marker(
    target_and_gateway: tuple[Path, Gateway],
) -> None:
    target, gw = target_and_gateway
    graph = build_graph(
        planner_invoke=lambda _p: _valid_plan_response(),
        implementer_invoke=lambda _p: _valid_diff_response(),
        gateway=gw,
    )
    final = graph.invoke(_initial_state(target))

    audit_log = (
        final["audit_log"] if isinstance(final, dict) else final.audit_log
    )
    implementer_events = [e for e in audit_log if e.node == "implementer"]
    assert len(implementer_events) == 1
    event = implementer_events[0]
    assert "tool_gateway" in event.guardrails_passed
    assert "input_filter" in event.guardrails_passed
    assert "output_validator" in event.guardrails_passed
    assert event.guardrails_failed == []


def test_implementer_node_halts_when_validation_exhausted(
    target_and_gateway: tuple[Path, Gateway],
) -> None:
    """If the model produces a diff that keeps tripping output
    validation, the retry loop exhausts and the graph halts. We
    simulate this by returning a diff that ruff will flag (unused
    import) and capping iterations at 2."""
    target, gw = target_and_gateway
    lint_failing_diff = {
        "diff_text": (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-def add(a, b):\n"
            "-    return a - b\n"
            "+import os\n"
            "+def add(a, b):\n"
            "+    return a + b\n"
        ),
        "files_touched": ["app.py"],
    }
    graph = build_graph(
        planner_invoke=lambda _p: _valid_plan_response(),
        implementer_invoke=lambda _p: lint_failing_diff,
        gateway=gw,
        max_iterations=2,
    )
    with pytest.raises(ImplementerError) as excinfo:
        graph.invoke(_initial_state(target))
    assert excinfo.value.iterations == 2
    assert excinfo.value.validation_report is not None
    assert "ruff" in excinfo.value.validation_report.failing_tools


def test_implementer_node_halts_without_gateway(
    target_and_gateway: tuple[Path, Gateway],
) -> None:
    """Building the graph without a gateway is allowed (so
    describe_graph can introspect topology) but invoking the
    implementer node without one must halt — never produce a
    diff=None state masquerading as success."""
    target, _ = target_and_gateway
    graph = build_graph(
        planner_invoke=lambda _p: _valid_plan_response(),
        implementer_invoke=lambda _p: _valid_diff_response(),
        gateway=None,
    )
    with pytest.raises(RuntimeError, match="without a gateway"):
        graph.invoke(_initial_state(target))


def test_implementer_failure_propagates(
    target_and_gateway: tuple[Path, Gateway],
) -> None:
    """When the implementer halts, the graph halts. Same contract as
    the planner — no silent recovery, no partial state."""
    target, gw = target_and_gateway
    graph = build_graph(
        planner_invoke=lambda _p: _valid_plan_response(),
        implementer_invoke=lambda _p: {"diff_text": "missing files_touched"},
        gateway=gw,
    )
    with pytest.raises(ImplementerError):
        graph.invoke(_initial_state(target))


def test_planner_failure_propagates(
    target_and_gateway: tuple[Path, Gateway],
) -> None:
    target, gw = target_and_gateway
    graph = build_graph(
        planner_invoke=lambda _p: {"summary": "missing changes"},
        gateway=gw,
    )
    with pytest.raises(PlannerError):
        graph.invoke(_initial_state(target))


def test_stub_nodes_do_not_mutate_state(
    target_and_gateway: tuple[Path, Gateway],
) -> None:
    target, gw = target_and_gateway
    graph = build_graph(
        planner_invoke=lambda _p: _valid_plan_response(),
        implementer_invoke=lambda _p: _valid_diff_response(),
        gateway=gw,
    )
    final = graph.invoke(_initial_state(target))

    if isinstance(final, dict):
        assert final.get("tests") is None
        assert final.get("review") is None
    else:
        assert final.tests is None
        assert final.review is None
