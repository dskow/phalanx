"""Append-only structured audit log.

The log is the single source of truth for reconstructing a run.
Every state transition, every guardrail decision, and every tool
invocation appends one JSON object to ``audit.jsonl``. The file
format is JSON Lines: one object per line, append-only, no rewrites.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    """Tiny JSONL writer with a stable, append-only contract."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        event = {"ts": _now_iso(), **event}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":"), sort_keys=True))
            f.write("\n")

    def record_scaffold_event(self, *, node: str, message: str, request_title: str) -> None:
        self.append(
            {
                "node": node,
                "phase": "scaffold",
                "message": message,
                "request_title": request_title,
            }
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
