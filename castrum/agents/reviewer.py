"""Reviewer agent.

Checks the implementer's diff and the test-writer's tests against
each acceptance criterion the planner defined. Emits PASS or FAIL
with a per-criterion result. The reviewer is the only agent that
can promote a run to a PR-ready state.

Implementation lands in a follow-up PR.
"""

from __future__ import annotations

from castrum.state import Plan, ReviewVerdict, TestArtifact, UnifiedDiff


def review(plan: Plan, diff: UnifiedDiff, tests: TestArtifact) -> ReviewVerdict:
    raise NotImplementedError("reviewer agent — implemented in a follow-up PR")
