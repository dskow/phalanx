"""Tool gateway — the only module in Phalanx that may invoke shell
or write to the filesystem.

Every tool call from an agent passes through ``Gateway.invoke``. The
gateway:

- Confirms the requested tool is on the allowlist for the calling role.
- Validates arguments against a Pydantic schema for that tool.
- Confines filesystem access: ``target_root`` is read-only, ``out_root``
  is read-write, and every other path is rejected — path traversal
  (``..``) is normalized and refused if it escapes the sandbox.
- Rejects shell metacharacters in ``run_shell`` arguments: ``;``,
  ``&&``, ``||``, ``|``, backticks, ``$(...)``, ``${...}``, redirects,
  newlines.
- Restricts ``run_shell`` argv[0] to a small executable allowlist.

The gateway never uses ``shell=True``. Argv is passed as a list, so a
shell metacharacter inside an arg never reaches a shell parser even if
the gateway's regex check ever missed one — defense in depth.

Per-role tool allowlists encode "least privilege" per the architecture
doc: the planner can read but not write; only the implementer and
test_writer may invoke shell commands; the reviewer is read-only.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

# --------------------------------------------------------------------------- #
# Public configuration
# --------------------------------------------------------------------------- #

ROLE_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "planner": frozenset({"read_file", "list_files"}),
    "implementer": frozenset(
        {"read_file", "list_files", "write_file", "run_shell"}
    ),
    "test_writer": frozenset(
        {"read_file", "list_files", "write_file", "run_shell"}
    ),
    "reviewer": frozenset({"read_file", "list_files"}),
}

EXECUTABLE_ALLOWLIST: frozenset[str] = frozenset(
    {"git", "pytest", "ruff", "mypy", "semgrep"}
)

# Any of these characters anywhere in a run_shell argv element causes a
# hard reject. The argv-list invocation model already prevents shell
# interpretation, but rejecting the characters at the gateway means an
# attempted injection is observable in the audit log instead of being
# silently treated as a literal arg.
_SHELL_METACHAR_RE = re.compile(r"[;&|`$<>\n\r\x00]|\$\(|\$\{|\|\||&&")

_RUN_SHELL_TIMEOUT_SECONDS = 60


# --------------------------------------------------------------------------- #
# Errors and audit shape
# --------------------------------------------------------------------------- #


class ToolGatewayError(RuntimeError):
    """Raised when the gateway refuses a call.

    Carries a stable ``code`` so consumers can branch on the failure
    mode (and so the audit log records a categorical reason, not just
    a free-text string).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        role: str,
        tool: str,
        args: Mapping[str, Any],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.role = role
        self.tool = tool
        # Named ``tool_args`` rather than ``args`` because
        # ``BaseException.args`` is a load-bearing attribute used by
        # ``str(exc)`` — overwriting it with a dict would silently
        # break exception formatting.
        self.tool_args = dict(args)


@dataclass(frozen=True)
class GatewayConfig:
    """Filesystem sandbox boundaries for a Gateway."""

    target_root: Path
    out_root: Path


@dataclass(frozen=True)
class GatewayEvent:
    """One audit-able invocation of the gateway."""

    role: str
    tool: str
    args: Mapping[str, Any]
    passed: bool
    code: str | None = None
    reason: str | None = None


GatewayAudit = Callable[[GatewayEvent], None]


# --------------------------------------------------------------------------- #
# Tool schemas
# --------------------------------------------------------------------------- #


class ReadFileArgs(BaseModel):
    model_config = {"extra": "forbid"}
    path: str = Field(description="Path relative to target_root or absolute inside it.")


class ListFilesArgs(BaseModel):
    model_config = {"extra": "forbid"}
    path: str = Field(default=".", description="Directory under target_root.")
    pattern: str = Field(default="*", description="Glob pattern.")


class WriteFileArgs(BaseModel):
    model_config = {"extra": "forbid"}
    path: str = Field(description="Path under out_root.")
    content: str


class RunShellArgs(BaseModel):
    model_config = {"extra": "forbid"}
    argv: list[str] = Field(min_length=1)
    cwd: str | None = Field(
        default=None,
        description=(
            "Optional working directory for the command. If unset, the "
            "gateway uses target_root. If set, the gateway resolves it "
            "against out_root and rejects any path outside the sandbox."
        ),
    )


_TOOL_SCHEMAS: Mapping[str, type[BaseModel]] = {
    "read_file": ReadFileArgs,
    "list_files": ListFilesArgs,
    "write_file": WriteFileArgs,
    "run_shell": RunShellArgs,
}


# --------------------------------------------------------------------------- #
# Shell result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShellResult:
    """The captured outcome of a ``run_shell`` invocation.

    The gateway never raises on a non-zero exit code — the caller is
    the right place to decide what to do with a failing tool run
    (e.g. the output validator re-invokes the implementer with the
    failure context). The gateway only halts when a guardrail check
    rejects the call.
    """

    argv: list[str] = field(default_factory=list)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #


class Gateway:
    """Single entry point for every tool the agent layer invokes."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        audit: GatewayAudit | None = None,
    ) -> None:
        # Resolve sandbox roots once at construction; later checks
        # compare against these resolved values so a symlink the
        # operator created cannot smuggle the sandbox boundary out
        # from under us.
        self._target = config.target_root.resolve()
        self._out = config.out_root.resolve()
        self._audit = audit

    @property
    def target_root(self) -> Path:
        """Resolved absolute path of the read-only zone.

        Agents that need to build an absolute path into the writable
        zone (e.g. the test_writer reading the post-apply scratch
        tree under ``out_root``) call ``gateway.out_root`` and join
        from there. Exposing the roots is safe because every path
        the agent then passes back is still sandbox-checked on the
        way in.
        """
        return self._target

    @property
    def out_root(self) -> Path:
        """Resolved absolute path of the read-write zone."""
        return self._out

    # ----- public surface ---------------------------------------------------

    def invoke(self, role: str, tool: str, args: Mapping[str, Any]) -> Any:
        """Execute ``tool`` on behalf of ``role`` if permitted."""
        try:
            validated_args = self._authorize_and_validate(role, tool, args)
            result = self._dispatch(tool, validated_args)
        except ToolGatewayError as exc:
            # Helpers raise with ``role="?"``/``tool="?"`` placeholders
            # because they do not know the calling context. Backfill
            # here so the exception object matches the audit event.
            exc.role = role
            exc.tool = tool
            self._emit(
                GatewayEvent(
                    role=role,
                    tool=tool,
                    args=args,
                    passed=False,
                    code=exc.code,
                    reason=str(exc),
                )
            )
            raise
        self._emit(
            GatewayEvent(role=role, tool=tool, args=args, passed=True)
        )
        return result

    # ----- authorization + schema ------------------------------------------

    def _authorize_and_validate(
        self, role: str, tool: str, args: Mapping[str, Any]
    ) -> BaseModel:
        if role not in ROLE_ALLOWLIST:
            raise ToolGatewayError(
                f"unknown role: {role!r}",
                code="unknown_role",
                role=role,
                tool=tool,
                args=args,
            )
        if tool not in _TOOL_SCHEMAS:
            raise ToolGatewayError(
                f"unknown tool: {tool!r}",
                code="unknown_tool",
                role=role,
                tool=tool,
                args=args,
            )
        if tool not in ROLE_ALLOWLIST[role]:
            raise ToolGatewayError(
                f"role {role!r} is not permitted to invoke {tool!r}",
                code="role_denied",
                role=role,
                tool=tool,
                args=args,
            )
        try:
            return _TOOL_SCHEMAS[tool].model_validate(args)
        except ValidationError as exc:
            raise ToolGatewayError(
                f"invalid arguments for {tool!r}: {exc.error_count()} error(s)",
                code="invalid_args",
                role=role,
                tool=tool,
                args=args,
            ) from exc

    def _dispatch(self, tool: str, args: BaseModel) -> Any:
        # Dispatch by the validated model's actual type. The ``tool``
        # name is already authorization-checked; here it is only used
        # for the unreachable-branch error message.
        if isinstance(args, ReadFileArgs):
            return self._read_file(args)
        if isinstance(args, ListFilesArgs):
            return self._list_files(args)
        if isinstance(args, WriteFileArgs):
            return self._write_file(args)
        if isinstance(args, RunShellArgs):
            return self._run_shell(args)
        # Defensive: _authorize_and_validate should already reject this.
        raise ToolGatewayError(
            f"no dispatcher for tool: {tool!r}",
            code="unknown_tool",
            role="?",
            tool=tool,
            args={},
        )

    # ----- tool implementations --------------------------------------------

    def _read_file(self, args: ReadFileArgs) -> str:
        path = self._resolve_in_sandbox(args.path, base=self._target, writable=False)
        return path.read_text(encoding="utf-8")

    def _list_files(self, args: ListFilesArgs) -> list[str]:
        base = self._resolve_in_sandbox(args.path, base=self._target, writable=False)
        if not base.is_dir():
            return []
        return sorted(
            str(p.relative_to(self._target)).replace("\\", "/")
            for p in base.rglob(args.pattern)
            if p.is_file()
        )

    def _write_file(self, args: WriteFileArgs) -> int:
        path = self._resolve_in_sandbox(args.path, base=self._out, writable=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.write_text(args.content, encoding="utf-8")

    def _run_shell(self, args: RunShellArgs) -> ShellResult:
        argv = list(args.argv)
        executable = argv[0]
        if executable not in EXECUTABLE_ALLOWLIST:
            raise ToolGatewayError(
                f"executable not on allowlist: {executable!r}",
                code="executable_denied",
                role="?",
                tool="run_shell",
                args={"argv": argv},
            )
        for element in argv:
            if _SHELL_METACHAR_RE.search(element):
                raise ToolGatewayError(
                    f"shell metacharacter in argv element: {element!r}",
                    code="shell_metachar",
                    role="?",
                    tool="run_shell",
                    args={"argv": argv},
                )
        if args.cwd is None:
            cwd = self._target
        else:
            cwd = self._resolve_in_sandbox(args.cwd, base=self._out, writable=True)
            if not cwd.is_dir():
                raise ToolGatewayError(
                    f"cwd does not exist or is not a directory: {args.cwd!r}",
                    code="cwd_invalid",
                    role="?",
                    tool="run_shell",
                    args={"argv": argv, "cwd": args.cwd},
                )
        # shell=False is the load-bearing line. Argv is passed directly
        # to exec(), so there is no shell parser to interpret a stray
        # metachar even if the regex above ever has a gap.
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_RUN_SHELL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        return ShellResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    # ----- sandbox enforcement ---------------------------------------------

    def _resolve_in_sandbox(
        self, raw: str, *, base: Path, writable: bool
    ) -> Path:
        """Resolve ``raw`` against ``base`` and confirm it stays inside
        the appropriate sandbox.

        ``writable`` distinguishes the two zones: a read path may be
        either inside target_root *or* out_root; a write path must be
        inside out_root specifically. This is what makes target_root
        a read-only zone — there is no code path that produces a
        writable Path inside it.
        """
        if "\x00" in raw:
            raise ToolGatewayError(
                "null byte in path",
                code="path_invalid",
                role="?",
                tool="?",
                args={"path": raw},
            )
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolGatewayError(
                f"path could not be resolved: {raw!r} ({exc})",
                code="path_invalid",
                role="?",
                tool="?",
                args={"path": raw},
            ) from exc

        in_target = _is_within(resolved, self._target)
        in_out = _is_within(resolved, self._out)
        if writable:
            if not in_out:
                raise ToolGatewayError(
                    f"write outside out_root: {raw!r} -> {resolved}",
                    code="path_outside_sandbox",
                    role="?",
                    tool="?",
                    args={"path": raw},
                )
        elif not (in_target or in_out):
            raise ToolGatewayError(
                f"read outside sandbox: {raw!r} -> {resolved}",
                code="path_outside_sandbox",
                role="?",
                tool="?",
                args={"path": raw},
            )
        return resolved

    # ----- audit ------------------------------------------------------------

    def _emit(self, event: GatewayEvent) -> None:
        if self._audit is None:
            return
        self._audit(event)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _is_within(path: Path, root: Path) -> bool:
    """True if ``path`` (already resolved) is ``root`` or below it.

    ``Path.is_relative_to`` exists on 3.9+, but using it bare swallows
    the resolve step on either side. The caller resolves explicitly
    so this function only does the containment check.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
