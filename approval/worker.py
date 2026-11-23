"""Subprocess entry point used by the multi-process proofs.

This is deliberately a thin wrapper: it starts a fresh process (a fresh
``ApprovalService``), performs exactly one operation, and can be told to hold
its write transaction at a gate and/or to terminate immediately after COMMIT.
That is how the tests model "two processes race" and "die after commit, before
the reply".

Examples::

    python -m approval.worker --data-dir data consume ... --crash-after-commit
    python -m approval.worker --data-dir data recover --tenant acme --gate-file go
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Sequence

from .canonical import parse_json_object
from .clock import ManualClock, SystemClock
from .models import Result
from .service import ApprovalService


def _wait_for_file(path: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    target = Path(path)
    while not target.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"gate file never appeared: {path}")
        time.sleep(0.01)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="approval.worker")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--now", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("consume")
    p.add_argument("--tenant", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--request", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--grant", default=None)
    p.add_argument("--ticket", default=None)
    p.add_argument("--expected-version", type=int, default=None)
    p.add_argument("--idem-key", default=None)
    p.add_argument("--ready-file", default=None)
    p.add_argument("--hold-seconds", type=float, default=0.0)
    p.add_argument("--crash-after-commit", action="store_true")
    p.add_argument("--crash-before-commit", action="store_true")
    p.add_argument("--result-file", default=None)

    p = sub.add_parser("rotate")
    p.add_argument("--tenant", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--operator", action="append", required=True)
    p.add_argument("--idem-key", default=None)
    p.add_argument("--ready-file", default=None)
    p.add_argument("--hold-seconds", type=float, default=0.0)
    p.add_argument("--result-file", default=None)

    p = sub.add_parser("recover")
    p.add_argument("--tenant", required=True)
    p.add_argument("--ready-file", default=None)
    p.add_argument("--gate-file", default=None)
    p.add_argument("--gate-timeout", type=float, default=20.0)
    p.add_argument("--result-file", default=None)

    args = parser.parse_args(argv)
    clock = ManualClock(args.now) if args.now else SystemClock()

    def after_commit(operation: str, result: Result) -> None:
        # In crash mode the decision is already durable, but the caller never
        # receives it: we exit before writing the result file or printing.
        if getattr(args, "crash_after_commit", False):
            os._exit(23)
        if args.result_file:
            Path(args.result_file).write_text(result.to_json(), encoding="utf-8")

    def before_commit(operation: str) -> None:
        # Simulate a crash before durability: the transaction rolls back and
        # no audit/idempotency/state row survives.
        if getattr(args, "crash_before_commit", False):
            os._exit(24)

    svc = ApprovalService(
        Path(args.data_dir), clock, fault_hook=after_commit, before_commit=before_commit
    )

    if args.command == "consume":
        def release() -> None:
            if args.ready_file:
                Path(args.ready_file).write_text("ready", encoding="utf-8")
            if args.hold_seconds > 0:
                time.sleep(args.hold_seconds)

        result = svc.consume(
            args.tenant,
            args.service,
            parse_json_object(args.request),
            args.actor,
            grant_id=args.grant,
            ticket_id=args.ticket,
            expected_version=args.expected_version,
            idem_key=args.idem_key,
            release_gate=release if (args.ready_file or args.hold_seconds > 0) else None,
        )
    elif args.command == "rotate":
        def release_rotate() -> None:
            if args.ready_file:
                Path(args.ready_file).write_text("ready", encoding="utf-8")
            if args.hold_seconds > 0:
                time.sleep(args.hold_seconds)

        result = svc.rotate_shift(
            args.tenant,
            args.service,
            args.operator,
            idem_key=args.idem_key,
            release_gate=release_rotate if (args.ready_file or args.hold_seconds > 0) else None,
        )
    else:  # recover
        def gate() -> None:
            if args.ready_file:
                Path(args.ready_file).write_text("ready", encoding="utf-8")
            if args.gate_file:
                _wait_for_file(args.gate_file, args.gate_timeout)

        result = svc.recover(args.tenant, gate=gate if (args.ready_file or args.gate_file) else None)

    if args.result_file and not getattr(args, "crash_after_commit", False):
        Path(args.result_file).write_text(result.to_json(), encoding="utf-8")
    print(result.to_json())
    return 0 if result.ok else 3


if __name__ == "__main__":
    sys.exit(main())
