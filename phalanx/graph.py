"""LangGraph state machine wiring.

Builds the agent graph as a real ``langgraph.StateGraph`` over
``StudioState``. Two nodes (planner, implementer) are now live; the
remaining nodes (test_writer, reviewer) are pass-through stubs that
land in their own PRs. Wiring all four from day one keeps the
topology stable across PRs — the only change per follow-up is the
body of one node function.

``describe_graph`` continues to expose the (nodes, edges) shape by
introspecting the compiled graph, so the description and the runtime
topology cannot drift.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from phalanx.agents.implementer import (
    DEFAULT_MAX_ITERATIONS,
    ImplementerInvoke,
    implement_iteratively,
)
from phalanx.agents.planner import PlannerInvoke, plan
from phalanx.agents.test_writer import TestWriterInvoke, write_tests
from phalanx.guardrails.tool_gateway import Gateway
from phalanx.state import AuditEvent, StudioState

_NODE_ORDER: tuple[str, ...] = ("planner", "implementer", "test_writer", "reviewer")


def build_graph(
    *,
    planner_invoke: PlannerInvoke | None = None,
    implementer_invoke: ImplementerInvoke | None = None,
    test_writer_invoke: TestWriterInvoke | None = None,
    gateway: Gateway | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> CompiledStateGraph:
    """Compile the LangGraph for a Phalanx run.

    The ``*_invoke`` callables are the model handles for their
    respective nodes; ``None`` defers to the agent's default
    ChatAnthropic factory. ``gateway`` is the shared tool gateway
    used by every live node that touches the filesystem — ``None``
    means a real run cannot proceed past the planner; the live
    nodes raise if invoked without one. ``max_iterations`` bounds
    the implementer's output-validation retry loop.
    """
    builder: StateGraph = StateGraph(StudioState)
    builder.add_node("planner", _make_planner_node(planner_invoke))
    builder.add_node(
        "implementer",
        _make_implementer_node(implementer_invoke, gateway, max_iterations),
    )
    builder.add_node(
        "test_writer", _make_test_writer_node(test_writer_invoke, gateway)
    )
    builder.add_node("reviewer", _stub_node("reviewer"))

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "implementer")
    builder.add_edge("implementer", "test_writer")
    builder.add_edge("test_writer", "reviewer")
    builder.add_edge("reviewer", END)

    return builder.compile()


def describe_graph() -> dict[str, object]:
    """Return a JSON-serializable description of the agent graph.

    Built by introspecting the compiled graph so the description and
    the runtime topology cannot drift apart. ``START`` and ``END``
    pseudo-nodes are filtered out so consumers see the agent layer.
    """
    graph = build_graph(
        planner_invoke=_describe_only_invoke,
        implementer_invoke=_describe_only_invoke,
        test_writer_invoke=_describe_only_invoke,
    ).get_graph()
    pseudo = {START, END}
    nodes = [n for n in _NODE_ORDER if n in graph.nodes and n not in pseudo]
    edges = [
        [e.source, e.target]
        for e in graph.edges
        if e.source not in pseudo and e.target not in pseudo
    ]
    return {"nodes": nodes, "edges": edges}


def _make_planner_node(
    invoke: PlannerInvoke | None,
) -> Callable[[StudioState], dict[str, Any]]:
    def planner_node(state: StudioState) -> dict[str, Any]:
        start = time.perf_counter()
        result = (
            plan(state.request, invoke=invoke)
            if invoke is not None
            else plan(state.request)
        )
        duration_ms = int((time.perf_counter() - start) * 1000)

        event = AuditEvent(
            ts=datetime.now(UTC),
            node="planner",
            input_hash=_sha256(state.request.body),
            output_hash=_sha256(result.model_dump_json()),
            guardrails_passed=["input_filter"],
            guardrails_failed=[],
            duration_ms=duration_ms,
            model="planner-invoke",
        )
        return {"plan": result, "audit_log": [*state.audit_log, event]}

    return planner_node


def _make_implementer_node(
    invoke: ImplementerInvoke | None,
    gateway: Gateway | None,
    max_iterations: int,
) -> Callable[[StudioState], dict[str, Any]]:
    def implementer_node(state: StudioState) -> dict[str, Any]:
        if state.plan is None:
            # Planner halted upstream; do not silently produce a
            # partial state with diff=None. Halt the graph.
            raise RuntimeError(
                "implementer node reached without a plan — planner must "
                "have halted; that exception should have propagated"
            )
        if gateway is None:
            raise RuntimeError(
                "implementer node invoked without a gateway — call "
                "build_graph(gateway=...) to run past the planner"
            )
        start = time.perf_counter()
        kwargs = {"gateway": gateway, "max_iterations": max_iterations}
        if invoke is not None:
            kwargs["invoke"] = invoke
        diff, _report, _attempts = implement_iteratively(state.plan, **kwargs)
        duration_ms = int((time.perf_counter() - start) * 1000)

        event = AuditEvent(
            ts=datetime.now(UTC),
            node="implementer",
            input_hash=_sha256(state.plan.model_dump_json()),
            output_hash=_sha256(diff.model_dump_json()),
            guardrails_passed=[
                "input_filter",
                "tool_gateway",
                "output_validator",
            ],
            guardrails_failed=[],
            duration_ms=duration_ms,
            model="implementer-invoke",
        )
        return {"diff": diff, "audit_log": [*state.audit_log, event]}

    return implementer_node


def _make_test_writer_node(
    invoke: TestWriterInvoke | None,
    gateway: Gateway | None,
) -> Callable[[StudioState], dict[str, Any]]:
    def test_writer_node(state: StudioState) -> dict[str, Any]:
        if state.plan is None or state.diff is None:
            raise RuntimeError(
                "test_writer node reached without plan+diff — an earlier "
                "node must have halted; that exception should have "
                "propagated"
            )
        if gateway is None:
            raise RuntimeError(
                "test_writer node invoked without a gateway — call "
                "build_graph(gateway=...) to run past the planner"
            )
        start = time.perf_counter()
        artifact = (
            write_tests(state.plan, state.diff, gateway=gateway, invoke=invoke)
            if invoke is not None
            else write_tests(state.plan, state.diff, gateway=gateway)
        )
        duration_ms = int((time.perf_counter() - start) * 1000)

        event = AuditEvent(
            ts=datetime.now(UTC),
            node="test_writer",
            input_hash=_sha256(state.diff.model_dump_json()),
            output_hash=_sha256(artifact.model_dump_json()),
            guardrails_passed=["input_filter", "tool_gateway"],
            guardrails_failed=[],
            duration_ms=duration_ms,
            model="test_writer-invoke",
        )
        return {"tests": artifact, "audit_log": [*state.audit_log, event]}

    return test_writer_node


def _stub_node(name: str) -> Callable[[StudioState], dict[str, Any]]:
    """Placeholder node — no state mutation, no audit event.

    Replaced in a follow-up PR by the live agent for ``name``. Kept
    in the graph from day one so the topology is stable and the
    compiled graph matches the architecture diagram.
    """

    def node(_state: StudioState) -> dict[str, Any]:
        return {}

    node.__name__ = f"{name}_stub"
    return node


def _describe_only_invoke(_prompt: str) -> dict[str, Any]:
    """Sentinel invoke used by ``describe_graph`` — never executed.

    ``describe_graph`` compiles the graph but does not run it; this
    callable exists so ``build_graph`` can be called without
    triggering the lazy ChatAnthropic import in any agent's default
    factory.
    """
    raise RuntimeError("describe_only invoke must not be called")


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
