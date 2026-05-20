"""Egress firewall — documents the network posture enforced at the
Docker layer, and provides an in-process audit hook.

The primary enforcement of egress restriction is the Docker network
configuration in docker-compose.yml (the agent container can reach
only the model endpoint). This module is the audit-side companion:
it records every network destination an agent attempts so that
out-of-band attempts are observable even if Docker fails open.
"""

from __future__ import annotations

ALLOWED_HOSTS: frozenset[str] = frozenset({"api.anthropic.com"})


def is_allowed(host: str) -> bool:
    """Return True if the given host is in the egress allowlist."""
    return host in ALLOWED_HOSTS
