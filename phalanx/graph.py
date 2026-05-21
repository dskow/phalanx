"""LangGraph state machine wiring.

Builds the agent graph as a real ``langgraph.StateGraph`` over
``StudioState``. The planner is a live node that invokes the planner
agent; the remaining nodes (implementer, test_writer, reviewer) are
pass-through stubs that land in their own PRs. Wiring them now keeps
the graph topology stable across PRs — the only change per follow-up
is the body of one node function.

``describe_graph`` continues to expose the (nodes, edges) shape used
by the smoke test, but now reads it from the compiled graph rather
than from a static declaration.
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

from phalanx.agents.planner import PlannerInvoke, plan
from phalanx.state import AuditEvent, StudioState

_NODE_ORDER: tuple[str, ...] = ("planner", "implementer", "test_writer", "reviewer")
_PLANNER_MODEL_LABEL = "planner-invoke"


def build_graph(invoke: PlannerInvoke | None = None) -> CompiledStateGraph:
    """Compile the LangGraph for a Phalanx run.

    ``invoke`` is the model callable passed through to the planner.
    When ``None``, the planner constructs its default ChatAnthropic
    client on first use — fine for production, not what tests want.
    """
    builder: StateGraph = StateGraph(StudioState)
    builder.add_node("planner", _make_planner_node(invoke))
    builder.add_node("implementer", _stub_node("implementer"))
    builder.add_node("test_writer", _stub_node("test_writer"))
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
    graph = build_graph(invoke=_describe_only_invoke).get_graph()
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
        result = plan(state.request, invoke=invoke) if invoke is not None else plan(state.request)
        duration_ms = int((time.perf_counter() - start) * 1000)

        event = AuditEvent(
            ts=datetime.now(UTC),
            node="planner",
            input_hash=_sha256(state.request.body),
            output_hash=_sha256(result.model_dump_json()),
            guardrails_passed=["input_filter"],
            guardrails_failed=[],
            duration_ms=duration_ms,
            model=_PLANNER_MODEL_LABEL,
        )
        return {"plan": result, "audit_log": [*state.audit_log, event]}

    return planner_node


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
    triggering the lazy ChatAnthropic import in the planner's default
    factory.
    """
    raise RuntimeError("describe_only invoke must not be called")


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
