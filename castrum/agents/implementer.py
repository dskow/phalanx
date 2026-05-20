"""Implementer agent.

Takes the plan and produces a unified diff against the target tree.
Operates on a guardrail-filtered view of the target — never re-reads
raw legacy files that may contain prompt-injection payloads.

Implementation lands in a follow-up PR.
"""

from __future__ import annotations

from castrum.state import Plan, UnifiedDiff


def implement(plan: Plan) -> UnifiedDiff:
    raise NotImplementedError("implementer agent — implemented in a follow-up PR")
