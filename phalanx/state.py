"""Studio state and agent I/O contracts.

These models are the architectural backbone of Phalanx: every agent
declares its input and output by referencing a model here, and the
orchestrator validates both directions at every state transition.
A run that produces output failing validation halts immediately —
there is no silent coercion.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Change(BaseModel):
    """One unit of planned change."""

    file_path: str = Field(description="Path relative to the target tree.")
    rationale: str = Field(description="Why this change is needed.")
    acceptance_criterion: str = Field(
        description="A concrete, testable statement that decides PASS/FAIL."
    )


class ModernizationRequest(BaseModel):
    """A refactor request — the input to a Phalanx run."""

    title: str
    body: str
    target_root: str = Field(description="Path to the target codebase root.")


class Plan(BaseModel):
    """The planner's output: a structured set of changes."""

    summary: str
    changes: list[Change]


class UnifiedDiff(BaseModel):
    """The implementer's output: a unified diff that applies to the target tree."""

    diff_text: str
    files_touched: list[str]


class TestArtifact(BaseModel):
    """The test-writer's output: new or modified tests plus a passing-run assertion."""

    diff_text: str
    files_touched: list[str]
    pytest_exit_code: int


class CriterionResult(BaseModel):
    """One reviewer-verified acceptance criterion."""

    criterion: str
    passed: bool
    notes: str = ""


class ReviewVerdict(BaseModel):
    """The reviewer's output."""

    verdict: Literal["PASS", "FAIL"]
    criteria: list[CriterionResult]
    rationale: str


class AuditEvent(BaseModel):
    """One entry in the append-only audit log.

    The log is the replay primitive: given the log and a snapshot of
    the prompts, any run can be reconstructed for incident review.
    """

    ts: datetime
    node: str
    input_hash: str
    output_hash: str
    guardrails_passed: list[str] = []
    guardrails_failed: list[str] = []
    duration_ms: int
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


class StudioState(BaseModel):
    """The single state object passed between LangGraph nodes."""

    request: ModernizationRequest
    plan: Plan | None = None
    diff: UnifiedDiff | None = None
    tests: TestArtifact | None = None
    review: ReviewVerdict | None = None
    audit_log: list[AuditEvent] = []
