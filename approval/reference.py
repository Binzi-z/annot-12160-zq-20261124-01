"""An independent, in-memory reference model of the approval contract.

This module is intentionally *not* built on the SQLite service.  It re-derives
the decision rules from the specification so that differential tests compare
two implementations rather than one implementation against itself.

It exposes the same external method names and returns the same
:class:`approval.models.Result` shape, which lets a single scenario script
drive both backends and compare business codes and statuses.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import codes
from .models import Result


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _param_hash(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ReferenceApproval:
    """Pure state machine.  One instance models one tenant namespace."""

    def __init__(self, clock_now: datetime | str | None = "2026-01-01T00:00:00Z") -> None:
        self._wall = _utc(clock_now)
        self._effective = self._wall
        self.tenants: set[str] = set()
        self.services: dict[str, dict[str, Any]] = {}
        self.shifts: dict[str, list[dict[str, Any]]] = {}
        self.tickets: dict[str, dict[str, Any]] = {}
        self.confirmations: set[tuple[str, int, int, str]] = set()
        self.confirmation_audit: dict[tuple[str, int, int, str], str] = {}
        self.grants: dict[str, dict[str, Any]] = {}
        self.consumptions: dict[str, str] = {}
        self.idem: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self._tenant_of_grant: dict[str, str] = {}
        self._seq = 0
        self._shift_id = 0

    # ---------------------------------------------------------------- helpers
    def set_now(self, value: datetime | str) -> None:
        self._wall = _utc(value)
        if self._wall > self._effective:
            self._effective = self._wall

    def advance(self, seconds: float) -> None:
        self.set_now(self._effective + timedelta(seconds=seconds))

    def _now(self) -> datetime:
        if self._wall > self._effective:
            self._effective = self._wall
        return self._effective

    def _audit(
        self,
        tenant: str | None,
        action: str,
        decision: str,
        code: str,
        *,
        subject: str = "",
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        self._seq += 1
        audit_no = f"REF-{tenant or 'global'}-{self._seq:08d}"
        self.audit.append(
            {
                "audit_no": audit_no,
                "tenant": tenant,
                "action": action,
                "decision": decision,
                "code": code,
                "subject": subject,
                "actor": actor,
                "detail": detail or {},
                "at": self._now(),
            }
        )
        return audit_no

    def _active_shift(self, service: str) -> dict[str, Any] | None:
        for shift in self.shifts.get(service, []):
            if shift["status"] == "ACTIVE":
                return shift
        return None

    def _replay(
        self, op: str, key: str | None, params: Any, tenant: str
    ) -> Result | None:
        if not key:
            return None
        stored = self.idem.get((op, key))
        if stored is None:
            return None
        if stored["params_hash"] != _param_hash(params):
            audit_no = self._audit(
                tenant, op, codes.DENIED, codes.IDEMPOTENCY_CONFLICT
            )
            return Result(
                False,
                codes.IDEMPOTENCY_CONFLICT,
                codes.CONFLICT,
                audit_no,
                {"operation": op, "idem_key": key},
            )
        return Result.from_payload(stored["result"], replayed=True)

    def _remember(
        self, op: str, key: str | None, params: Any, tenant: str, result: Result
    ) -> Result:
        if key:
            self.idem[(op, key)] = {
                "params_hash": _param_hash(params),
                "result": result.to_payload(),
            }
        return result

    def _fail(
        self,
        tenant: str | None,
        action: str,
        code: str,
        status: str,
        *,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Result:
        audit_no = self._audit(
            tenant, action, codes.DENIED, code, actor=actor, detail=detail
        )
        return Result(False, code, status, audit_no, detail or {})

    # -------------------------------------------------------------- mutations
    def provision_tenant(
        self, tenant_id: str, name: str, *, idem_key: str | None = None
    ) -> Result:
        params = {"tenant_id": tenant_id, "name": name}
        replay = self._replay("PROVISION_TENANT", idem_key, params, tenant_id)
        if replay is not None:
            return replay
        if tenant_id in self.tenants:
            return self._fail(tenant_id, "PROVISION_TENANT", codes.TENANT_EXISTS, codes.READY)
        self.tenants.add(tenant_id)
        audit_no = self._audit(
            tenant_id, "PROVISION_TENANT", codes.APPROVED, codes.TENANT_PROVISIONED
        )
        result = Result(True, codes.TENANT_PROVISIONED, codes.READY, audit_no, {"tenant_id": tenant_id})
        return self._remember("PROVISION_TENANT", idem_key, params, tenant_id, result)

    def add_service(
        self,
        tenant_id: str,
        service_id: str,
        name: str,
        *,
        approval_ttl_seconds: int = 1800,
        idem_key: str | None = None,
    ) -> Result:
        params = {"tenant_id": tenant_id, "service_id": service_id, "name": name, "ttl": approval_ttl_seconds}
        replay = self._replay("ADD_SERVICE", idem_key, params, tenant_id)
        if replay is not None:
            return replay
        if tenant_id not in self.tenants:
            return self._fail(tenant_id, "ADD_SERVICE", codes.TENANT_NOT_FOUND, codes.UNKNOWN)
        if approval_ttl_seconds <= 0:
            return self._fail(tenant_id, "ADD_SERVICE", codes.INVALID_ARGUMENT, codes.UNKNOWN)
        if service_id in self.services:
            return self._fail(tenant_id, "ADD_SERVICE", codes.SERVICE_EXISTS, codes.READY)
        self.services[service_id] = {
            "tenant": tenant_id,
            "name": name,
            "ttl": approval_ttl_seconds,
        }
        self.shifts.setdefault(service_id, [])
        audit_no = self._audit(tenant_id, "ADD_SERVICE", codes.APPROVED, codes.READY)
        result = Result(True, codes.READY, codes.READY, audit_no, {"service_id": service_id})
        return self._remember("ADD_SERVICE", idem_key, params, tenant_id, result)

    def _service_ok(self, tenant: str, service: str) -> bool:
        meta = self.services.get(service)
        return meta is not None and meta["tenant"] == tenant

    def start_shift(
        self,
        tenant_id: str,
        service_id: str,
        operators: list[str],
        *,
        idem_key: str | None = None,
    ) -> Result:
        operators = sorted({o.strip() for o in operators if o and o.strip()})
        params = {"tenant_id": tenant_id, "service_id": service_id, "operators": operators}
        replay = self._replay("START_SHIFT", idem_key, params, tenant_id)
        if replay is not None:
            return replay
        if tenant_id not in self.tenants:
            return self._fail(tenant_id, "START_SHIFT", codes.TENANT_NOT_FOUND, codes.UNKNOWN)
        if not operators:
            return self._fail(tenant_id, "START_SHIFT", codes.INVALID_OPERATORS, codes.UNKNOWN)
        if not self._service_ok(tenant_id, service_id):
            return self._fail(tenant_id, "START_SHIFT", codes.SERVICE_NOT_FOUND, codes.UNKNOWN)
        if self._active_shift(service_id) is not None:
            return self._fail(tenant_id, "START_SHIFT", codes.SHIFT_EXISTS, codes.ACTIVE)
        histories = self.shifts[service_id]
        version = (histories[-1]["version"] + 1) if histories else 1
        self._shift_id += 1
        histories.append(
            {
                "shift_id": self._shift_id,
                "version": version,
                "status": "ACTIVE",
                "operators": operators,
            }
        )
        audit_no = self._audit(
            tenant_id, "START_SHIFT", codes.NEUTRAL, codes.SHIFT_STARTED
        )
        result = Result(
            True,
            codes.SHIFT_STARTED,
            codes.ACTIVE,
            audit_no,
            {"assignment_version": version, "operators": operators},
        )
        return self._remember("START_SHIFT", idem_key, params, tenant_id, result)

    def rotate_shift(
        self,
        tenant_id: str,
        service_id: str,
        operators: list[str],
        *,
        idem_key: str | None = None,
        release_gate: Any = None,
    ) -> Result:
        operators = sorted({o.strip() for o in operators if o and o.strip()})
        params = {"tenant_id": tenant_id, "service_id": service_id, "operators": operators}
        replay = self._replay("ROTATE_SHIFT", idem_key, params, tenant_id)
        if replay is not None:
            return replay
        if tenant_id not in self.tenants:
            return self._fail(tenant_id, "ROTATE_SHIFT", codes.TENANT_NOT_FOUND, codes.UNKNOWN)
        if not operators:
            return self._fail(tenant_id, "ROTATE_SHIFT", codes.INVALID_OPERATORS, codes.UNKNOWN)
        active = self._active_shift(service_id)
        if active is None:
            return self._fail(tenant_id, "ROTATE_SHIFT", codes.NO_ACTIVE_SHIFT, codes.UNKNOWN)
        old_version = active["version"]
        new_version = old_version + 1
        active["status"] = "CLOSED"
        self._shift_id += 1
        self.shifts[service_id].append(
            {
                "shift_id": self._shift_id,
                "version": new_version,
                "status": "ACTIVE",
                "operators": operators,
            }
        )
        revoked = self._reclaim(tenant_id, service_id, new_version, "SHIFT_ROTATED")
        audit_no = self._audit(
            tenant_id, "ROTATE_SHIFT", codes.NEUTRAL, codes.SHIFT_ROTATED
        )
        result = Result(
            True,
            codes.SHIFT_ROTATED,
            codes.ACTIVE,
            audit_no,
            {"old_version": old_version, "new_version": new_version, "revoked_grants": revoked},
        )
        if release_gate is not None:
            release_gate()
        return self._remember("ROTATE_SHIFT", idem_key, params, tenant_id, result)

    def _reclaim(
        self, tenant: str, service: str, stale_before: int, reason: str
    ) -> list[str]:
        revoked: list[str] = []
        for grant_id, grant in self.grants.items():
            if (
                grant["tenant"] == tenant
                and grant["service"] == service
                and grant["status"] == "ACTIVE"
                and grant["version"] < stale_before
            ):
                grant["status"] = codes.REVOKED
                grant["reason"] = reason
                ticket = self.tickets.get(grant["ticket"])
                if ticket is not None and ticket["status"] == codes.GRANTED:
                    ticket["status"] = codes.OPEN
                    ticket["round"] += 1
                revoked.append(grant_id)
        return revoked

    def create_ticket(
        self,
        tenant_id: str,
        service_id: str,
        ticket_id: str,
        request: dict[str, Any],
        *,
        idem_key: str | None = None,
    ) -> Result:
        req_hash = _param_hash(request)
        params = {"tenant_id": tenant_id, "service_id": service_id, "ticket_id": ticket_id, "request": request}
        replay = self._replay("CREATE_TICKET", idem_key, params, tenant_id)
        if replay is not None:
            return replay
        if tenant_id not in self.tenants:
            return self._fail(tenant_id, "CREATE_TICKET", codes.TENANT_NOT_FOUND, codes.UNKNOWN)
        if not self._service_ok(tenant_id, service_id):
            return self._fail(tenant_id, "CREATE_TICKET", codes.SERVICE_NOT_FOUND, codes.UNKNOWN)
        existing = self.tickets.get(ticket_id)
        if existing is not None:
            if (
                existing["tenant"] == tenant_id
                and existing["service"] == service_id
                and existing["request_hash"] == req_hash
            ):
                return self._fail(tenant_id, "CREATE_TICKET", codes.TICKET_EXISTS, existing["status"])
            return self._fail(tenant_id, "CREATE_TICKET", codes.REQUEST_MISMATCH, existing["status"])
        self.tickets[ticket_id] = {
            "tenant": tenant_id,
            "service": service_id,
            "request": request,
            "request_hash": req_hash,
            "status": codes.OPEN,
            "round": 1,
        }
        audit_no = self._audit(tenant_id, "CREATE_TICKET", codes.NEUTRAL, codes.TICKET_CREATED)
        result = Result(True, codes.TICKET_CREATED, codes.OPEN, audit_no, {"ticket_id": ticket_id})
        return self._remember("CREATE_TICKET", idem_key, params, tenant_id, result)

    def _active_grant_for_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        for grant in self.grants.values():
            if grant["ticket"] == ticket_id and grant["status"] == "ACTIVE":
                return grant
        return None

    def confirm(
        self,
        tenant_id: str,
        ticket_id: str,
        operator_id: str,
        *,
        idem_key: str | None = None,
    ) -> Result:
        params = {"tenant_id": tenant_id, "ticket_id": ticket_id, "operator_id": operator_id}
        replay = self._replay("CONFIRM", idem_key, params, tenant_id)
        if replay is not None:
            return replay
        if tenant_id not in self.tenants:
            return self._fail(tenant_id, "CONFIRM", codes.TENANT_NOT_FOUND, codes.UNKNOWN, actor=operator_id)
        ticket = self.tickets.get(ticket_id)
        if ticket is None or ticket["tenant"] != tenant_id:
            return self._fail(tenant_id, "CONFIRM", codes.TICKET_NOT_FOUND, codes.UNKNOWN, actor=operator_id)
        service = ticket["service"]
        if ticket["status"] == codes.CONSUMED:
            return self._fail(tenant_id, "CONFIRM", codes.TICKET_CONSUMED, codes.CONSUMED, actor=operator_id)
        shift = self._active_shift(service)
        if shift is None:
            return self._fail(tenant_id, "CONFIRM", codes.NO_ACTIVE_SHIFT, ticket["status"], actor=operator_id)
        if operator_id not in shift["operators"]:
            return self._fail(tenant_id, "CONFIRM", codes.NOT_ON_SHIFT, ticket["status"], actor=operator_id)
        key = (ticket_id, shift["shift_id"], ticket["round"], operator_id)
        if key in self.confirmations:
            return self._fail(
                tenant_id,
                "CONFIRM",
                codes.DUPLICATE_CONFIRMATION,
                ticket["status"],
                actor=operator_id,
                detail={"confirmation_audit_no": self.confirmation_audit[key]},
            )
        if self._active_grant_for_ticket(ticket_id) is not None:
            return self._fail(tenant_id, "CONFIRM", codes.ALREADY_GRANTED, ticket["status"], actor=operator_id)
        confirmation_audit = self._audit(
            tenant_id, "CONFIRM", codes.NEUTRAL, codes.CONFIRMATION_RECORDED, actor=operator_id
        )
        self.confirmations.add(key)
        self.confirmation_audit[key] = confirmation_audit
        confirmed = sorted(
            op
            for (tid, sid, rnd, op) in self.confirmations
            if tid == ticket_id and sid == shift["shift_id"] and rnd == ticket["round"]
        )
        if len(confirmed) < 2:
            result = Result(
                True,
                codes.CONFIRMATION_RECORDED,
                ticket["status"],
                confirmation_audit,
                {"confirmed_operators": confirmed, "needed": 2},
            )
            return self._remember("CONFIRM", idem_key, params, tenant_id, result)
        grant_id = f"REFG-{ticket_id}-{shift['version']}-{ticket['round']}"
        expires = self._now() + timedelta(seconds=self.services[service]["ttl"])
        audit_no = self._audit(tenant_id, "GRANT_ISSUE", codes.APPROVED, codes.GRANT_ISSUED, actor=operator_id)
        self.grants[grant_id] = {
            "tenant": tenant_id,
            "service": service,
            "ticket": ticket_id,
            "request_hash": ticket["request_hash"],
            "version": shift["version"],
            "round": ticket["round"],
            "status": codes.ACTIVE,
            "issued": self._now(),
            "expires": expires,
            "audit_no": audit_no,
        }
        ticket["status"] = codes.GRANTED
        result = Result(
            True,
            codes.GRANT_ISSUED,
            codes.GRANTED,
            audit_no,
            {
                "grant_id": grant_id,
                "assignment_version": shift["version"],
                "confirmed_operators": confirmed,
                "expires_at": expires.isoformat(),
            },
        )
        return self._remember("CONFIRM", idem_key, params, tenant_id, result)

    def _locate(self, tenant: str, grant_id: str | None, ticket_id: str | None) -> tuple[str, dict[str, Any]] | None:
        if grant_id:
            grant = self.grants.get(grant_id)
            if grant and grant["tenant"] == tenant:
                return grant_id, grant
            return None
        if ticket_id:
            candidates = [
                (gid, g)
                for gid, g in self.grants.items()
                if g["ticket"] == ticket_id and g["tenant"] == tenant
            ]
            if not candidates:
                return None
            active = [c for c in candidates if c[1]["status"] == codes.ACTIVE]
            return (active or candidates)[-1]
        return None

    def consume(
        self,
        tenant_id: str,
        service_id: str,
        request: dict[str, Any],
        actor: str,
        *,
        grant_id: str | None = None,
        ticket_id: str | None = None,
        expected_version: int | None = None,
        idem_key: str | None = None,
        release_gate: Any = None,
    ) -> Result:
        req_hash = _param_hash(request)
        params = {
            "tenant_id": tenant_id,
            "service_id": service_id,
            "grant_id": grant_id,
            "ticket_id": ticket_id,
            "request": request,
            "actor": actor,
            "expected_version": expected_version,
        }
        replay = self._replay("CONSUME", idem_key, params, tenant_id)
        if replay is not None:
            return replay
        if tenant_id not in self.tenants:
            return self._fail(tenant_id, "CONSUME", codes.TENANT_NOT_FOUND, codes.UNKNOWN, actor=actor)
        located = self._locate(tenant_id, grant_id, ticket_id)
        if located is None:
            return self._fail(tenant_id, "CONSUME", codes.GRANT_NOT_FOUND, codes.UNKNOWN, actor=actor)
        gid, grant = located

        def deny(code: str, status: str, detail: dict[str, Any] | None = None) -> Result:
            audit_no = self._audit(tenant_id, "CONSUME", codes.DENIED, code, actor=actor, detail=detail)
            result = Result(False, code, status, audit_no, detail or {})
            return self._remember("CONSUME", idem_key, params, tenant_id, result)

        if grant["service"] != service_id:
            return deny(codes.SERVICE_MISMATCH, grant["status"])
        if grant["request_hash"] != req_hash:
            return deny(codes.REQUEST_MISMATCH, grant["status"])
        if expected_version is not None and expected_version != grant["version"]:
            return deny(codes.STALE_ASSIGNMENT, grant["status"])
        if grant["status"] == codes.CONSUMED:
            return deny(codes.ALREADY_CONSUMED, codes.CONSUMED, {"audit_no": self.consumptions[gid]})
        if grant["status"] == codes.REVOKED:
            return deny(codes.GRANT_REVOKED, codes.REVOKED)
        if grant["status"] == codes.EXPIRED:
            return deny(codes.GRANT_EXPIRED, codes.EXPIRED)
        if grant["status"] != codes.ACTIVE:
            return deny(codes.GRANT_NOT_FOUND, grant["status"])
        if self._now() >= grant["expires"]:
            grant["status"] = codes.EXPIRED
            ticket = self.tickets[grant["ticket"]]
            if ticket["status"] == codes.GRANTED:
                ticket["status"] = codes.OPEN
                ticket["round"] += 1
            self._audit(tenant_id, "EXPIRE_GRANT", codes.NEUTRAL, codes.GRANT_EXPIRED)
            return deny(codes.GRANT_EXPIRED, codes.EXPIRED)
        shift = self._active_shift(grant["service"])
        if shift is None or shift["version"] != grant["version"]:
            grant["status"] = codes.REVOKED
            ticket = self.tickets[grant["ticket"]]
            if ticket["status"] == codes.GRANTED:
                ticket["status"] = codes.OPEN
                ticket["round"] += 1
            self._audit(tenant_id, "REVOKE_GRANT", codes.NEUTRAL, codes.GRANT_REVOKED)
            return deny(codes.STALE_ASSIGNMENT, codes.REVOKED)
        audit_no = self._audit(tenant_id, "CONSUME", codes.APPROVED, codes.GRANT_CONSUMED, actor=actor)
        self.consumptions[gid] = audit_no
        grant["status"] = codes.CONSUMED
        ticket = self.tickets[grant["ticket"]]
        ticket["status"] = codes.CONSUMED
        result = Result(
            True,
            codes.GRANT_CONSUMED,
            codes.CONSUMED,
            audit_no,
            {"grant_id": gid, "assignment_version": grant["version"], "request_hash": req_hash},
        )
        if release_gate is not None:
            release_gate()
        return self._remember("CONSUME", idem_key, params, tenant_id, result)

    def recover(
        self, tenant_id: str, *, hold_seconds: float = 0.0, gate: Any = None
    ) -> Result:
        if tenant_id not in self.tenants:
            return self._fail(tenant_id, "RECOVERY", codes.TENANT_NOT_FOUND, codes.UNKNOWN)
        expired: list[str] = []
        revoked: list[str] = []
        for gid, grant in self.grants.items():
            if grant["tenant"] != tenant_id or grant["status"] != codes.ACTIVE:
                continue
            ticket = self.tickets[grant["ticket"]]
            if self._now() >= grant["expires"]:
                grant["status"] = codes.EXPIRED
                if ticket["status"] == codes.GRANTED:
                    ticket["status"] = codes.OPEN
                    ticket["round"] += 1
                expired.append(gid)
                continue
            shift = self._active_shift(grant["service"])
            if shift is None or shift["version"] != grant["version"]:
                grant["status"] = codes.REVOKED
                if ticket["status"] == codes.GRANTED:
                    ticket["status"] = codes.OPEN
                    ticket["round"] += 1
                revoked.append(gid)
        reopened: list[str] = []
        for ticket_id, ticket in self.tickets.items():
            if ticket["tenant"] != tenant_id or ticket["status"] != codes.GRANTED:
                continue
            if self._active_grant_for_ticket(ticket_id) is None:
                ticket["status"] = codes.OPEN
                ticket["round"] += 1
                reopened.append(ticket_id)
        audit_no = self._audit(tenant_id, "RECOVERY", codes.NEUTRAL, codes.RECOVERY_COMPLETED)
        return Result(
            True,
            codes.RECOVERY_COMPLETED,
            codes.RECOVERED,
            audit_no,
            {"expired": expired, "revoked": revoked, "reopened": reopened},
        )
