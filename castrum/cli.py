"""Castrum command-line entry point.

The scaffold version of this CLI exercises the Docker harness and
the audit-log writer end-to-end without invoking any agent. Running
``castrum run --target <dir> --out <dir>`` reads the modernization
request from ``<target>/REQUEST.md``, records a scaffold event in
the audit log, and exits 0. Real agent execution lands in
subsequent PRs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from castrum import __version__
from castrum.audit.logger import AuditLogger
from castrum.state import ModernizationRequest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="castrum",
        description="Autonomous code-modernization studio.",
    )
    parser.add_argument("--version", action="version", version=f"castrum {__version__}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run the studio against a target codebase.")
    run.add_argument("--target", required=True, type=Path, help="Target codebase root.")
    run.add_argument("--out", required=True, type=Path, help="Output directory.")

    return parser


def _read_request(target: Path) -> ModernizationRequest:
    request_path = target / "REQUEST.md"
    if not request_path.is_file():
        raise FileNotFoundError(f"No REQUEST.md found in target: {request_path}")
    body = request_path.read_text(encoding="utf-8")
    title = body.splitlines()[0].lstrip("# ").strip() if body else "(untitled)"
    return ModernizationRequest(title=title, body=body, target_root=str(target.resolve()))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "run":
        return _cmd_run(args.target, args.out)

    return 1


def _cmd_run(target: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    request = _read_request(target)

    audit = AuditLogger(out / "audit.jsonl")
    audit.record_scaffold_event(
        node="cli",
        message=f"scaffold run — agents not yet implemented (castrum {__version__})",
        request_title=request.title,
    )

    print(f"castrum {__version__}: scaffold run complete")
    print(f"  target:  {target}")
    print(f"  out:     {out}")
    print(f"  request: {request.title}")
    print(f"  audit:   {out / 'audit.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
