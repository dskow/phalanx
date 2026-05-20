"""Tool gateway — the only module in Phalanx that may invoke shell
or filesystem-writing operations.

Every tool call from an agent passes through ``invoke``. The gateway
confirms the tool is on the allowlist for the current role, validates
arguments against the tool's schema, confines filesystem access to
the sandbox, and rejects shell metacharacters.

Implementation lands in a follow-up PR.
"""

from __future__ import annotations

from typing import Any


def invoke(role: str, tool_name: str, args: dict[str, Any]) -> Any:
    raise NotImplementedError("tool gateway — implemented in a follow-up PR")
