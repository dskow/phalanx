"""Test-writer agent.

Given a unified diff, writes tests that cover the change and asserts
those tests pass in a sandbox container. Test runs that fail do not
silently get retried — the failure surfaces to the reviewer.

Implementation lands in a follow-up PR.
"""

from __future__ import annotations

from phalanx.state import TestArtifact, UnifiedDiff


def write_tests(diff: UnifiedDiff) -> TestArtifact:
    raise NotImplementedError("test_writer agent — implemented in a follow-up PR")
