"""Planner agent.

Reads the modernization request and the target codebase, produces a
structured ``Plan`` with per-change acceptance criteria. Acceptance
criteria are the contract the reviewer agent later uses to decide
PASS/FAIL — the planner is the only agent that gets to define them.

Implementation lands in a follow-up PR. The signature is stable.
"""

from __future__ import annotations

from phalanx.state import ModernizationRequest, Plan


def plan(request: ModernizationRequest) -> Plan:
    raise NotImplementedError("planner agent — implemented in a follow-up PR")
