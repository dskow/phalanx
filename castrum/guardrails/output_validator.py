"""Output validator — runs ruff, mypy, semgrep, and the target test
suite against the implementer's diff. A failure is a hard stop.

Implementation lands in a follow-up PR.
"""

from __future__ import annotations

from castrum.state import UnifiedDiff


def validate(diff: UnifiedDiff) -> tuple[bool, list[str]]:
    """Return (passed, list_of_findings)."""
    raise NotImplementedError("output validator — implemented in a follow-up PR")
