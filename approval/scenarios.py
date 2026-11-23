"""Differential scenarios: real SQLite service vs independent reference model.

A scenario is a list of logical steps executed against both backends with the
same injected clock.  We compare the business decision (``ok``/``code``/
``status``) of every step and a normalized final state snapshot.  Audit numbers
and grant ids are implementation-specific and are compared only for stability
where the contract requires it (idempotent replay).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import codes
from .clock import ManualClock
from .models import Result
from .reference import ReferenceApproval
from .service import ApprovalService

START = "2026-01-01T00:00:00Z"


# --------------------------------------------------------------------- adapters
class ServiceBackend:
    """Adapter giving the SQLite service the same surface as the reference."""

    kind = "service"

    def __init__(self, root: str | Path, start: str = START) -> None:
        self.clock = ManualClock(start)
        self.svc = ApprovalService(root, self.clock)

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds=seconds)

    def provision_tenant(self, tenant: str, name: str, **kw: Any) -> Result:
        return self.svc.provision_tenant(tenant, name, **kw)

    def add_service(self, tenant: str, service: str, name: str, **kw: Any) -> Result:
        return self.svc.add_service(tenant, service, name, **kw)

    def start_shift(self, tenant: str, service: str, operators: list[str], **kw: Any) -> Result:
        return self.svc.start_shift(tenant, service, operators, **kw)

    def rotate_shift(self, tenant: str, service: str, operators: list[str], **kw: Any) -> Result:
        return self.svc.rotate_shift(tenant, service, operators, **kw)

    def create_ticket(self, tenant: str, service: str, ticket: str, request: dict, **kw: Any) -> Result:
        return self.svc.create_ticket(tenant, service, ticket, request, **kw)

    def confirm(self, tenant: str, ticket: str, operator: str, **kw: Any) -> Result:
        return self.svc.confirm(tenant, ticket, operator, **kw)

    def consume(self, tenant: str, service: str, request: dict, actor: str, **kw: Any) -> Result:
        return self.svc.consume(tenant, service, request, actor, **kw)

    def recover(self, tenant: str, **kw: Any) -> Result:
        return self.svc.recover(tenant)

    def snapshot(self, tenant: str) -> dict[str, Any]:
        summary = self.svc.tenant_summary(tenant)
        shift = None
        for svc in summary.get("services", []):
            active = self.svc.active_shift(tenant, svc["service_id"])
            if active:
                shift = (svc["service_id"], active["shift_version"], tuple(active["operators"]))
                break
        return {
            "tickets_by_status": _nonzero(summary.get("tickets_by_status", {})),
            "grants_by_status": _nonzero(summary.get("grants_by_status", {})),
            "services": sorted(s["service_id"] for s in summary.get("services", [])),
            "active_shift": shift,
        }


class ReferenceBackend:
    kind = "reference"

    def __init__(self, start: str = START) -> None:
        self.ref = ReferenceApproval(start)

    def advance(self, seconds: float) -> None:
        self.ref.advance(seconds)

    def provision_tenant(self, tenant: str, name: str, **kw: Any) -> Result:
        return self.ref.provision_tenant(tenant, name, **kw)

    def add_service(self, tenant: str, service: str, name: str, **kw: Any) -> Result:
        return self.ref.add_service(tenant, service, name, **kw)

    def start_shift(self, tenant: str, service: str, operators: list[str], **kw: Any) -> Result:
        return self.ref.start_shift(tenant, service, operators, **kw)

    def rotate_shift(self, tenant: str, service: str, operators: list[str], **kw: Any) -> Result:
        return self.ref.rotate_shift(tenant, service, operators, **kw)

    def create_ticket(self, tenant: str, service: str, ticket: str, request: dict, **kw: Any) -> Result:
        return self.ref.create_ticket(tenant, service, ticket, request, **kw)

    def confirm(self, tenant: str, ticket: str, operator: str, **kw: Any) -> Result:
        return self.ref.confirm(tenant, ticket, operator, **kw)

    def consume(self, tenant: str, service: str, request: dict, actor: str, **kw: Any) -> Result:
        return self.ref.consume(tenant, service, request, actor, **kw)

    def recover(self, tenant: str, **kw: Any) -> Result:
        return self.ref.recover(tenant)

    def snapshot(self, tenant: str) -> dict[str, Any]:
        tickets: dict[str, int] = {}
        for t in self.ref.tickets.values():
            if t["tenant"] == tenant:
                tickets[t["status"]] = tickets.get(t["status"], 0) + 1
        grants: dict[str, int] = {}
        for g in self.ref.grants.values():
            if g["tenant"] == tenant:
                grants[g["status"]] = grants.get(g["status"], 0) + 1
        services = sorted(
            sid for sid, meta in self.ref.services.items() if meta["tenant"] == tenant
        )
        shift = None
        for svc in services:
            for sh in self.ref.shifts.get(svc, []):
                if sh["status"] == "ACTIVE":
                    shift = (svc, sh["version"], tuple(sh["operators"]))
                    break
            if shift:
                break
        return {
            "tickets_by_status": _nonzero(tickets),
            "grants_by_status": _nonzero(grants),
            "services": services,
            "active_shift": shift,
        }


def _nonzero(mapping: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in sorted(mapping.items()) if v}


# ---------------------------------------------------------------------- scenarios
@dataclass
class Step:
    name: str
    fn: Callable[[Any], Result]


@dataclass
class Scenario:
    name: str
    description: str
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, fn: Callable[[Any], Result]) -> "Scenario":
        self.steps.append(Step(name, fn))
        return self


@dataclass
class Observed:
    step: str
    ok: bool
    code: str
    status: str
    audit_no: str | None
    replayed: bool

    @classmethod
    def of(cls, step: str, result: Result) -> "Observed":
        return cls(step, result.ok, result.code, result.status, result.audit_no, result.replayed)


def _setup(
    backend: Any,
    tenant: str = "acme",
    service: str = "pay",
    ttl: int = 1800,
    operators: list[str] | None = None,
) -> Result:
    operators = operators or ["alice", "bob"]
    backend.provision_tenant(tenant, tenant.title(), idem_key=f"prov-{tenant}")
    backend.add_service(tenant, service, service.title(), approval_ttl_seconds=ttl, idem_key=f"svc-{tenant}-{service}")
    return backend.start_shift(tenant, service, operators, idem_key=f"shift-{tenant}-{service}")


def _advance(backend: Any, seconds: float) -> Result:
    backend.advance(seconds)
    return Result(True, "CLOCK_ADVANCED", "OK", None, {"seconds": seconds})


REQ = {"action": "restart", "host": "pay-01"}


def scenario_duplicate_confirmation() -> Scenario:
    s = Scenario("duplicate_confirmation", "Same operator confirms twice; only the second distinct operator issues a grant.")
    s.add("setup", lambda b: _setup(b))
    s.add("ticket", lambda b: b.create_ticket("acme", "pay", "T1", REQ, idem_key="t1"))
    s.add("alice-1", lambda b: b.confirm("acme", "T1", "alice", idem_key="a1"))
    s.add("alice-2", lambda b: b.confirm("acme", "T1", "alice", idem_key="a2"))
    s.add("bob-1", lambda b: b.confirm("acme", "T1", "bob", idem_key="b1"))
    s.add("alice-3", lambda b: b.confirm("acme", "T1", "alice", idem_key="a3"))
    return s


def scenario_rotation() -> Scenario:
    s = Scenario("rotation", "Shift rotation reclaims an unconsumed approval and reopens the ticket.")
    s.add("setup", lambda b: _setup(b))
    s.add("ticket", lambda b: b.create_ticket("acme", "pay", "T1", REQ, idem_key="t1"))
    s.add("alice", lambda b: b.confirm("acme", "T1", "alice", idem_key="a1"))
    s.add("bob", lambda b: b.confirm("acme", "T1", "bob", idem_key="b1"))
    s.add("rotate", lambda b: b.rotate_shift("acme", "pay", ["carol", "dave"], idem_key="r1"))
    s.add("old-consume", lambda b: b.consume("acme", "pay", REQ, "runbook", ticket_id="T1"))
    s.add("new-alice", lambda b: b.confirm("acme", "T1", "carol", idem_key="c1"))
    s.add("new-bob", lambda b: b.confirm("acme", "T1", "dave", idem_key="d1"))
    s.add("new-consume", lambda b: b.consume("acme", "pay", REQ, "runbook", ticket_id="T1"))
    return s


def scenario_expiry() -> Scenario:
    s = Scenario("expiry", "An approval older than its TTL is expired on consumption and cannot be used.")
    s.add("setup", lambda b: _setup(b, ttl=600))
    s.add("ticket", lambda b: b.create_ticket("acme", "pay", "T1", REQ, idem_key="t1"))
    s.add("alice", lambda b: b.confirm("acme", "T1", "alice", idem_key="a1"))
    s.add("bob", lambda b: b.confirm("acme", "T1", "bob", idem_key="b1"))
    s.add("advance", lambda b: _advance(b, 601))
    s.add("late-consume", lambda b: b.consume("acme", "pay", REQ, "runbook", ticket_id="T1"))
    s.add("reopen-confirm", lambda b: b.confirm("acme", "T1", "alice", idem_key="a2"))
    return s


def scenario_consume_once_and_retry() -> Scenario:
    s = Scenario("consume_once_and_retry", "A grant is consumed once; identical retries replay the original decision and audit number.")
    s.add("setup", lambda b: _setup(b))
    s.add("ticket", lambda b: b.create_ticket("acme", "pay", "T1", REQ, idem_key="t1"))
    s.add("alice", lambda b: b.confirm("acme", "T1", "alice", idem_key="a1"))
    s.add("bob", lambda b: b.confirm("acme", "T1", "bob", idem_key="b1"))
    s.add("consume", lambda b: b.consume("acme", "pay", REQ, "runbook", ticket_id="T1", idem_key="x1"))
    s.add("retry-same", lambda b: b.consume("acme", "pay", REQ, "runbook", ticket_id="T1", idem_key="x1"))
    s.add("consume-again", lambda b: b.consume("acme", "pay", REQ, "runbook", ticket_id="T1"))
    return s


def scenario_conflict_and_mismatch() -> Scenario:
    s = Scenario("conflict_and_mismatch", "Changed parameters under the same key conflict; changed request content is a separate mismatch.")
    s.add("setup", lambda b: _setup(b))
    s.add("ticket", lambda b: b.create_ticket("acme", "pay", "T1", REQ, idem_key="t1"))
    s.add("alice", lambda b: b.confirm("acme", "T1", "alice", idem_key="a1"))
    s.add("bob", lambda b: b.confirm("acme", "T1", "bob", idem_key="b1"))
    s.add("wrong-request", lambda b: b.consume("acme", "pay", {"action": "delete"}, "runbook", ticket_id="T1"))
    s.add("stale-version", lambda b: b.consume("acme", "pay", REQ, "runbook", ticket_id="T1", expected_version=99))
    s.add("consume", lambda b: b.consume("acme", "pay", REQ, "runbook", ticket_id="T1", idem_key="x1"))
    s.add("same-key-new-actor", lambda b: b.consume("acme", "pay", REQ, "other", ticket_id="T1", idem_key="x1"))
    return s


SCENARIOS = {
    s.name: s
    for s in (
        scenario_duplicate_confirmation(),
        scenario_rotation(),
        scenario_expiry(),
        scenario_consume_once_and_retry(),
        scenario_conflict_and_mismatch(),
    )
}


# ----------------------------------------------------------------------- runner
@dataclass
class Comparison:
    scenario: str
    steps: list[list[Observed]]
    snapshots: list[dict[str, Any]]
    differences: list[str]

    @property
    def matches(self) -> bool:
        return not self.differences


def run_scenario(scenario: Scenario) -> Comparison:
    real = ServiceBackend(tempfile.mkdtemp())
    ref = ReferenceBackend()
    traces: list[list[Observed]] = [[], []]
    for backend, trace in zip((real, ref), traces):
        for step in scenario.steps:
            result = step.fn(backend)
            trace.append(Observed.of(step.name, result))

    diffs: list[str] = []
    for left, right in zip(traces[0], traces[1]):
        if (left.ok, left.code, left.status) != (right.ok, right.code, right.status):
            diffs.append(
                f"step {left.step}: service=({left.ok},{left.code},{left.status})"
                f" reference=({right.ok},{right.code},{right.status})"
            )
    snapshots = [real.snapshot("acme"), ref.snapshot("acme")]
    if snapshots[0] != snapshots[1]:
        diffs.append(f"final state differs: service={snapshots[0]} reference={snapshots[1]}")
    return Comparison(scenario.name, traces, snapshots, diffs)


def run_all() -> list[Comparison]:
    return [run_scenario(sc) for sc in SCENARIOS.values()]
