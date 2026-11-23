"""Command-line interface for the offline on-call approval tool.

Every command prints one JSON object.  ``--now`` injects a deterministic clock
timestamp, so runs are reproducible without sleeping.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import codes
from .canonical import parse_json_object
from .clock import ManualClock, SystemClock, to_utc
from .fixtures import seed as seed_fixtures
from .models import Result
from .service import ApprovalService


def _service(args: argparse.Namespace) -> ApprovalService:
    clock = ManualClock(args.now) if args.now else SystemClock()
    return ApprovalService(Path(args.data_dir), clock)


def _emit(result: Result, *, as_json: bool) -> int:
    payload = result.to_payload()
    if result.replayed:
        payload["replayed"] = True
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(_human(result))
    return 0 if result.ok else 3


def _human(result: Result) -> str:
    lines = [
        f"{'OK' if result.ok else 'DENIED'}  {result.code}  status={result.status}"
        + ("  (replayed)" if result.replayed else "")
        + (f"  audit={result.audit_no}" if result.audit_no else "")
    ]
    for key, value in result.data.items():
        lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


def _json_arg(text: str) -> dict[str, Any]:
    return parse_json_object(text)


# --------------------------------------------------------------------- handlers
def cmd_seed(args: argparse.Namespace) -> int:
    svc = _service(args)
    existing = svc.list_tenants()
    if existing:
        result = Result(
            False,
            codes.TENANT_EXISTS,
            codes.READY,
            None,
            {"existing_tenants": existing, "hint": "use a fresh --data-dir or run reset"},
        )
        return _emit(result, as_json=args.json)
    results = seed_fixtures(svc, start_time=args.now or "2026-01-01T00:00:00Z")
    summary = {
        "tenants": svc.list_tenants(),
        "seeded_operations": len(results),
        "all_ok": all(r.ok for r in results),
        "results": [r.to_payload() for r in results],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if summary["all_ok"] else 3


def cmd_rotate(args: argparse.Namespace) -> int:
    svc = _service(args)
    result = svc.rotate_shift(
        args.tenant, args.service, args.operator, idem_key=args.idem_key
    )
    return _emit(result, as_json=args.json)


def cmd_confirm(args: argparse.Namespace) -> int:
    svc = _service(args)
    result = svc.confirm(args.tenant, args.ticket, args.operator, idem_key=args.idem_key)
    return _emit(result, as_json=args.json)


def cmd_consume(args: argparse.Namespace) -> int:
    svc = _service(args)
    result = svc.consume(
        args.tenant,
        args.service,
        _json_arg(args.request),
        args.actor,
        grant_id=args.grant,
        ticket_id=args.ticket,
        expected_version=args.expected_version,
        idem_key=args.idem_key,
    )
    return _emit(result, as_json=args.json)


def cmd_recover(args: argparse.Namespace) -> int:
    svc = _service(args)
    result = svc.recover(args.tenant)
    return _emit(result, as_json=args.json)


def cmd_status(args: argparse.Namespace) -> int:
    svc = _service(args)
    if args.ticket:
        value = svc.ticket_status(args.tenant, args.ticket)
    elif args.grant:
        value = svc.grant_status(args.tenant, args.grant)
    elif args.service:
        value = svc.active_shift(args.tenant, args.service)
    else:
        value = svc.tenant_summary(args.tenant)
    if value is None:
        result = Result(False, codes.TICKET_NOT_FOUND, codes.UNKNOWN, None)
        return _emit(result, as_json=args.json)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    svc = _service(args)
    rows = svc.audit_log(
        args.tenant, ticket_id=args.ticket, decision=args.decision, limit=args.limit
    )
    print(json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2, default=str))
    return 0


def cmd_tenants(args: argparse.Namespace) -> int:
    svc = _service(args)
    print(json.dumps(svc.list_tenants(), ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="approval",
        description="Offline on-call privileged approval tool (SQLite).",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="directory holding per-tenant SQLite databases (default: data)",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="inject current time, ISO-8601 (makes runs deterministic)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="emit compact JSON")

    p = sub.add_parser("seed", parents=[common], help="create synthetic local data")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("tenants", parents=[common], help="list known tenants")
    p.set_defaults(func=cmd_tenants)

    p = sub.add_parser("confirm", parents=[common], help="record an on-call confirmation")
    p.add_argument("--tenant", required=True)
    p.add_argument("--ticket", required=True)
    p.add_argument("--operator", required=True)
    p.add_argument("--idem-key", default=None, help="idempotency key for retries")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("rotate", parents=[common], help="register a shift change")
    p.add_argument("--tenant", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--operator", action="append", required=True)
    p.add_argument("--idem-key", default=None)
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser("consume", parents=[common], help="consume a grant exactly once")
    p.add_argument("--tenant", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--request", required=True, help="JSON object bound to the grant")
    p.add_argument("--actor", required=True)
    p.add_argument("--grant", default=None)
    p.add_argument("--ticket", default=None)
    p.add_argument("--expected-version", type=int, default=None)
    p.add_argument("--idem-key", default=None)
    p.set_defaults(func=cmd_consume)

    p = sub.add_parser("recover", parents=[common], help="cold-start recovery for one tenant")
    p.add_argument("--tenant", required=True)
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("status", parents=[common], help="show tenant/ticket/grant state")
    p.add_argument("--tenant", required=True)
    p.add_argument("--ticket", default=None)
    p.add_argument("--grant", default=None)
    p.add_argument("--service", default=None)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("audit", parents=[common], help="print audit log")
    p.add_argument("--tenant", required=True)
    p.add_argument("--ticket", default=None)
    p.add_argument("--decision", choices=["APPROVED", "DENIED", "NEUTRAL"], default=None)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_audit)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.now:
        try:
            args.now = to_utc(args.now).isoformat()
        except ValueError:
            parser.error(f"invalid --now value: {args.now}")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
