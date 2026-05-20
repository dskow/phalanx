"""LangGraph state machine wiring.

The real graph wires planner -> implementer -> test_writer -> reviewer
with the guardrail layer interposed between every node and any
side-effectful tool. The scaffold version of this module documents
that intent and exposes a ``describe_graph`` helper so the shape of
the system is testable before any agent is implemented.
"""

from __future__ import annotations

NODES: tuple[str, ...] = ("planner", "implementer", "test_writer", "reviewer")

EDGES: tuple[tuple[str, str], ...] = (
    ("planner", "implementer"),
    ("implementer", "test_writer"),
    ("test_writer", "reviewer"),
)


def describe_graph() -> dict[str, object]:
    """Return a JSON-serializable description of the agent graph.

    Used by the smoke test to assert the graph shape is what the
    architecture doc claims. When the real LangGraph build lands,
    this function will reflect the compiled graph rather than the
    static declaration.
    """
    return {"nodes": list(NODES), "edges": [list(e) for e in EDGES]}
