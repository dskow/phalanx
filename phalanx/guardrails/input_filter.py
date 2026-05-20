"""Input filter — neutralizes prompt-injection in repository content.

Scans content the agent is about to ingest for known injection
patterns (instruction overrides, role overrides, exfiltration
prompts) and replaces hits with a placeholder. Does not halt the
run; annotates the audit log instead.

Implementation lands in a follow-up PR.
"""

from __future__ import annotations


def neutralize(content: str) -> tuple[str, list[str]]:
    """Return (filtered_content, list_of_hit_descriptions)."""
    raise NotImplementedError("input filter — implemented in a follow-up PR")
