"""Approval service: confirmations, grants, rotation, consumption, recovery.

Persistence ordering (see docs/DESIGN.md):

* Every mutating operation is exactly one ``BEGIN IMMEDIATE`` transaction on
  the *tenant's own* SQLite database.
* Shift rotation and grant consumption both write the same tenant database, so
  SQLite serialises them.  Whoever commits first defines the durable order:
    - consume commits first  -> rotation sees no ACTIVE grant to revoke; the
      consumed approval stands and the late rotate leaves it untouched.
    - rotate commits first   -> consumption observes REVOKED and is denied.
* The state change, audit row, and idempotency row commit together.  A process
  that dies after COMMIT but before responding leaves a complete decision that
  a retry replays; a process that dies before COMMIT leaves no trace.
* Recovery only moves ACTIVE -> terminal (EXPIRED/REVOKED).  It never restores
  an ACTIVE grant and never consumes.  Database triggers enforce the same rule
  for any code path that bypasses this module.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from . import codes
from .canonical import canonical_json, params_hash, parse_json_object, request_hash
from .clock import ManualClock, SystemClock, format_timestamp, parse_timestamp
from .models import Result
from .store import TenantStore, list_tenants


class ApprovalService:
    def __init__(
        self,
        root: str | Path,
        clock: ManualClock | SystemClock | None = None,
        fault_hook: Callable[[str, Result], None] | None = None,
        before_commit: Callable[[str], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.clock = clock or SystemClock()
        # Called after a successful COMMIT, before the result is returned.
        # Tests inject a hook that raises to model "commit, then die".
        self.fault_hook = fault_hook
        # Called inside every write transaction, immediately before COMMIT.
        # Raising (or exiting the process) models a crash before the decision
        # becomes durable; SQLite rolls the whole transaction back.
        self.before_commit = before_commit

    @contextmanager
    def _write(self, store: TenantStore, operation: str) -> Iterator[sqlite3.Connection]:
        with store.write() as conn:
            yield conn
            if self.before_commit is not None:
                self.before_commit(operation)

    # ------------------------------------------------------------------ setup
    def _store(self, tenant_id: str) -> TenantStore:
        return TenantStore(self.root, tenant_id)

    def tenant_exists(self, tenant_id: str) -> bool:
        return self._store(tenant_id).exists()

    def list_tenants(self) -> list[str]:
        return list_tenants(self.root)

    def provision_tenant(
        self,
        tenant_id: str,
        name: str,
        *,
        idem_key: str | None = None,
    ) -> Result:
        if not tenant_id or not tenant_id.replace("-", "").replace("_", "").isalnum():
            return Result(False, codes.INVALID_ARGUMENT, codes.UNKNOWN, None)
        store = self._store(tenant_id)
        # initialize() is idempotent (CREATE ... IF NOT EXISTS), so this is safe
        # even if a previous process created the file but died before writing
        # the tenant row.
        store.initialize()
        params = {"tenant_id": tenant_id, "name": name}
        with self._write(store, "PROVISION_TENANT") as conn:
            existing = conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            # ``_effective_now`` writes tenant_clock, which has a FK to tenants,
            # so only use it once the tenant row is known to exist.
            now = (
                self._effective_now(conn, tenant_id)
                if existing
                else format_timestamp(self.clock.now())
            )
            replay = self._replay(conn, "PROVISION_TENANT", idem_key, params, now)
            if replay is not None:
                return replay
            if existing:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=None,
                    now=now,
                    subject_type="tenant",
                    subject_id=tenant_id,
                    action="PROVISION_TENANT",
                    code=codes.TENANT_EXISTS,
                    status=codes.READY,
                    idem_key=idem_key,
                    op="PROVISION_TENANT",
                    params=params,
                )
            conn.execute(
                "INSERT INTO tenants(tenant_id, name) VALUES (?, ?)",
                (tenant_id, name),
            )
            now = self._effective_now(conn, tenant_id)
            audit_no = self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=None,
                subject_type="tenant",
                subject_id=tenant_id,
                action="PROVISION_TENANT",
                decision=codes.APPROVED,
                code=codes.TENANT_PROVISIONED,
                actor=None,
                shift_id=None,
                assignment_version=None,
                detail={"name": name},
                now=now,
            )
            result = Result(
                True,
                codes.TENANT_PROVISIONED,
                codes.READY,
                audit_no,
                {"tenant_id": tenant_id, "name": name},
            )
            self._store_idempotency(
                conn, "PROVISION_TENANT", idem_key, params, tenant_id, result, now
            )
        return result

    def add_service(
        self,
        tenant_id: str,
        service_id: str,
        name: str,
        *,
        approval_ttl_seconds: int = 1800,
        idem_key: str | None = None,
    ) -> Result:
        if approval_ttl_seconds <= 0:
            return Result(False, codes.INVALID_ARGUMENT, codes.UNKNOWN, None)
        store = self._ready_store(tenant_id)
        if store is None:
            return self._tenant_missing(tenant_id)
        params = {
            "tenant_id": tenant_id,
            "service_id": service_id,
            "name": name,
            "ttl": approval_ttl_seconds,
        }
        with self._write(store, "ADD_SERVICE") as conn:
            now = self._effective_now(conn, tenant_id)
            replay = self._replay(conn, "ADD_SERVICE", idem_key, params, now)
            if replay is not None:
                return replay
            existing = conn.execute(
                "SELECT 1 FROM services WHERE service_id = ?", (service_id,)
            ).fetchone()
            if existing:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="service",
                    subject_id=service_id,
                    action="ADD_SERVICE",
                    code=codes.SERVICE_EXISTS,
                    status=codes.READY,
                    detail={"reason": "already exists"},
                    idem_key=idem_key,
                    op="ADD_SERVICE",
                    params=params,
                )
            conn.execute(
                "INSERT INTO services(service_id, tenant_id, name, approval_ttl_seconds)"
                " VALUES (?, ?, ?, ?)",
                (service_id, tenant_id, name, approval_ttl_seconds),
            )
            audit_no = self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=service_id,
                subject_type="service",
                subject_id=service_id,
                action="ADD_SERVICE",
                decision=codes.APPROVED,
                code=codes.READY,
                actor=None,
                shift_id=None,
                assignment_version=None,
                detail=params,
                now=now,
            )
            result = Result(
                True,
                codes.READY,
                codes.READY,
                audit_no,
                {"tenant_id": tenant_id, "service_id": service_id, "ttl": approval_ttl_seconds},
            )
            self._store_idempotency(
                conn, "ADD_SERVICE", idem_key, params, tenant_id, result, now
            )
            return result

    def start_shift(
        self,
        tenant_id: str,
        service_id: str,
        operators: list[str],
        *,
        idem_key: str | None = None,
    ) -> Result:
        store = self._ready_store(tenant_id)
        if store is None:
            return self._tenant_missing(tenant_id)
        operators = sorted({op.strip() for op in operators if op and op.strip()})
        params = {"tenant_id": tenant_id, "service_id": service_id, "operators": operators}
        with self._write(store, "START_SHIFT") as conn:
            now = self._effective_now(conn, tenant_id)
            replay = self._replay(conn, "START_SHIFT", idem_key, params, now)
            if replay is not None:
                return replay
            if not operators:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="shift",
                    subject_id=service_id,
                    action="START_SHIFT",
                    code=codes.INVALID_OPERATORS,
                    status=codes.UNKNOWN,
                    idem_key=idem_key,
                    op="START_SHIFT",
                    params=params,
                )
            if not self._service_exists(conn, tenant_id, service_id):
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="shift",
                    subject_id=service_id,
                    action="START_SHIFT",
                    code=codes.SERVICE_NOT_FOUND,
                    status=codes.UNKNOWN,
                    idem_key=idem_key,
                    op="START_SHIFT",
                    params=params,
                )
            active = self._active_shift(conn, tenant_id, service_id)
            if active is not None:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="shift",
                    subject_id=service_id,
                    action="START_SHIFT",
                    code=codes.SHIFT_EXISTS,
                    status=codes.ACTIVE,
                    detail={"shift_version": active["shift_version"]},
                    idem_key=idem_key,
                    op="START_SHIFT",
                    params=params,
                )
            version = self._next_shift_version(conn, tenant_id, service_id)
            audit_no = self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=service_id,
                subject_type="shift",
                subject_id=f"{tenant_id}/{service_id}/v{version}",
                action="START_SHIFT",
                decision=codes.NEUTRAL,
                code=codes.SHIFT_STARTED,
                actor=None,
                shift_id=None,
                assignment_version=version,
                detail={"operators": operators},
                now=now,
            )
            shift_id = self._insert_shift(
                conn, tenant_id, service_id, version, operators, now
            )
            result = Result(
                True,
                codes.SHIFT_STARTED,
                codes.ACTIVE,
                audit_no,
                {
                    "tenant_id": tenant_id,
                    "service_id": service_id,
                    "shift_id": shift_id,
                    "assignment_version": version,
                    "operators": operators,
                },
            )
            self._store_idempotency(
                conn, "START_SHIFT", idem_key, params, tenant_id, result, now
            )
            return result

    def rotate_shift(
        self,
        tenant_id: str,
        service_id: str,
        operators: list[str],
        *,
        idem_key: str | None = None,
        release_gate: Callable[[], None] | None = None,
    ) -> Result:
        """Close the current assignment version and reclaim unconsumed approvals.

        Registration (closing the old shift, opening the new one) and reclaim
        (revoking ACTIVE grants of older versions) are one transaction.  A
        concurrent consumption is therefore wholly before or wholly after this
        commit, never interleaved (see module docstring).
        """

        store = self._ready_store(tenant_id)
        if store is None:
            return self._tenant_missing(tenant_id)
        operators = sorted({op.strip() for op in operators if op and op.strip()})
        params = {"tenant_id": tenant_id, "service_id": service_id, "operators": operators}
        with self._write(store, "ROTATE_SHIFT") as conn:
            now = self._effective_now(conn, tenant_id)
            replay = self._replay(conn, "ROTATE_SHIFT", idem_key, params, now)
            if replay is not None:
                return replay
            if not operators:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="shift",
                    subject_id=service_id,
                    action="ROTATE_SHIFT",
                    code=codes.INVALID_OPERATORS,
                    status=codes.UNKNOWN,
                    idem_key=idem_key,
                    op="ROTATE_SHIFT",
                    params=params,
                )
            active = self._active_shift(conn, tenant_id, service_id)
            if active is None:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="shift",
                    subject_id=service_id,
                    action="ROTATE_SHIFT",
                    code=codes.NO_ACTIVE_SHIFT,
                    status=codes.UNKNOWN,
                    idem_key=idem_key,
                    op="ROTATE_SHIFT",
                    params=params,
                )
            old_version = active["shift_version"]
            new_version = old_version + 1
            rotate_audit = self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=service_id,
                subject_type="shift",
                subject_id=f"{tenant_id}/{service_id}/v{new_version}",
                action="ROTATE_SHIFT",
                decision=codes.NEUTRAL,
                code=codes.SHIFT_ROTATED,
                actor=None,
                shift_id=active["shift_id"],
                assignment_version=new_version,
                detail={"old_version": old_version, "operators": operators},
                now=now,
            )
            conn.execute(
                "UPDATE shifts SET status = 'CLOSED', ended_at = ? WHERE shift_id = ?",
                (now, active["shift_id"]),
            )
            new_shift_id = self._insert_shift(
                conn, tenant_id, service_id, new_version, operators, now
            )
            revoked = self._reclaim_grants(
                conn,
                tenant_id=tenant_id,
                service_id=service_id,
                stale_before_version=new_version,
                reason="SHIFT_ROTATED",
                now=now,
                actor=None,
            )
            result = Result(
                True,
                codes.SHIFT_ROTATED,
                codes.ACTIVE,
                rotate_audit,
                {
                    "tenant_id": tenant_id,
                    "service_id": service_id,
                    "old_version": old_version,
                    "new_version": new_version,
                    "shift_id": new_shift_id,
                    "operators": operators,
                    "revoked_grants": revoked,
                },
            )
            self._store_idempotency(
                conn, "ROTATE_SHIFT", idem_key, params, tenant_id, result, now
            )
            if release_gate is not None:
                # Runs while the tenant's write lock is still held, i.e. before
                # COMMIT.  Tests use it to force a specific commit order.
                release_gate()
            return result

    def create_ticket(
        self,
        tenant_id: str,
        service_id: str,
        ticket_id: str,
        request: dict[str, Any],
        *,
        idem_key: str | None = None,
    ) -> Result:
        store = self._ready_store(tenant_id)
        if store is None:
            return self._tenant_missing(tenant_id)
        if not isinstance(request, dict):
            return Result(False, codes.INVALID_ARGUMENT, codes.UNKNOWN, None)
        req_json = canonical_json(request)
        req_hash = request_hash(request)
        params = {
            "tenant_id": tenant_id,
            "service_id": service_id,
            "ticket_id": ticket_id,
            "request": req_json,
        }
        with self._write(store, "CREATE_TICKET") as conn:
            now = self._effective_now(conn, tenant_id)
            replay = self._replay(conn, "CREATE_TICKET", idem_key, params, now)
            if replay is not None:
                return replay
            if not self._service_exists(conn, tenant_id, service_id):
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="ticket",
                    subject_id=ticket_id,
                    action="CREATE_TICKET",
                    code=codes.SERVICE_NOT_FOUND,
                    status=codes.UNKNOWN,
                    idem_key=idem_key,
                    op="CREATE_TICKET",
                    params=params,
                )
            existing = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if existing:
                same = (
                    existing["tenant_id"] == tenant_id
                    and existing["service_id"] == service_id
                    and existing["request_hash"] == req_hash
                )
                if same:
                    return self._fail(
                        conn,
                        tenant_id=tenant_id,
                        service_id=service_id,
                        now=now,
                        subject_type="ticket",
                        subject_id=ticket_id,
                        action="CREATE_TICKET",
                        code=codes.TICKET_EXISTS,
                        status=existing["status"],
                        detail={"created_audit_no": existing["created_audit_no"]},
                        idem_key=idem_key,
                        op="CREATE_TICKET",
                        params=params,
                    )
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="ticket",
                    subject_id=ticket_id,
                    action="CREATE_TICKET",
                    code=codes.REQUEST_MISMATCH,
                    status=existing["status"],
                    detail={"existing_request_hash": existing["request_hash"]},
                    idem_key=idem_key,
                    op="CREATE_TICKET",
                    params=params,
                )
            audit_no = self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=service_id,
                subject_type="ticket",
                subject_id=ticket_id,
                action="CREATE_TICKET",
                decision=codes.NEUTRAL,
                code=codes.TICKET_CREATED,
                actor=None,
                shift_id=None,
                assignment_version=None,
                detail={"request_hash": req_hash, "request": request},
                now=now,
            )
            conn.execute(
                "INSERT INTO tickets(ticket_id, tenant_id, service_id, request_json,"
                " request_hash, status, round_no, created_audit_no, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'OPEN', 1, ?, ?, ?)",
                (ticket_id, tenant_id, service_id, req_json, req_hash, audit_no, now, now),
            )
            result = Result(
                True,
                codes.TICKET_CREATED,
                codes.OPEN,
                audit_no,
                {
                    "tenant_id": tenant_id,
                    "service_id": service_id,
                    "ticket_id": ticket_id,
                    "request_hash": req_hash,
                },
            )
            self._store_idempotency(
                conn, "CREATE_TICKET", idem_key, params, tenant_id, result, now
            )
            return result

    # ------------------------------------------------------------- confirmations
    def confirm(
        self,
        tenant_id: str,
        ticket_id: str,
        operator_id: str,
        *,
        idem_key: str | None = None,
    ) -> Result:
        store = self._ready_store(tenant_id)
        if store is None:
            return self._tenant_missing(tenant_id)
        params = {"tenant_id": tenant_id, "ticket_id": ticket_id, "operator_id": operator_id}
        with self._write(store, "CONFIRM") as conn:
            now = self._effective_now(conn, tenant_id)
            replay = self._replay(conn, "CONFIRM", idem_key, params, now)
            if replay is not None:
                return replay

            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ? AND tenant_id = ?",
                (ticket_id, tenant_id),
            ).fetchone()
            if ticket is None:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=None,
                    now=now,
                    subject_type="ticket",
                    subject_id=ticket_id,
                    action="CONFIRM",
                    code=codes.TICKET_NOT_FOUND,
                    status=codes.UNKNOWN,
                    actor=operator_id,
                    idem_key=idem_key,
                    op="CONFIRM",
                    params=params,
                )
            service_id = ticket["service_id"]
            if ticket["status"] == codes.CONSUMED:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="ticket",
                    subject_id=ticket_id,
                    action="CONFIRM",
                    code=codes.TICKET_CONSUMED,
                    status=codes.CONSUMED,
                    actor=operator_id,
                    idem_key=idem_key,
                    op="CONFIRM",
                    params=params,
                )
            shift = self._active_shift(conn, tenant_id, service_id)
            if shift is None:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="ticket",
                    subject_id=ticket_id,
                    action="CONFIRM",
                    code=codes.NO_ACTIVE_SHIFT,
                    status=ticket["status"],
                    actor=operator_id,
                    idem_key=idem_key,
                    op="CONFIRM",
                    params=params,
                )
            operators = self._shift_operators(conn, shift["shift_id"])
            if operator_id not in operators:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="ticket",
                    subject_id=ticket_id,
                    action="CONFIRM",
                    code=codes.NOT_ON_SHIFT,
                    status=ticket["status"],
                    actor=operator_id,
                    shift_id=shift["shift_id"],
                    version=shift["shift_version"],
                    detail={"operators": operators},
                    idem_key=idem_key,
                    op="CONFIRM",
                    params=params,
                )
            round_no = ticket["round_no"]
            duplicate = conn.execute(
                "SELECT * FROM confirmations WHERE ticket_id = ? AND shift_id = ?"
                " AND operator_id = ? AND round_no = ?",
                (ticket_id, shift["shift_id"], operator_id, round_no),
            ).fetchone()
            if duplicate is not None:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="ticket",
                    subject_id=ticket_id,
                    action="CONFIRM",
                    code=codes.DUPLICATE_CONFIRMATION,
                    status=ticket["status"],
                    actor=operator_id,
                    shift_id=shift["shift_id"],
                    version=shift["shift_version"],
                    detail={"confirmation_audit_no": duplicate["audit_no"]},
                    idem_key=idem_key,
                    op="CONFIRM",
                    params=params,
                )
            active_grant = conn.execute(
                "SELECT * FROM grants WHERE ticket_id = ? AND status = 'ACTIVE'",
                (ticket_id,),
            ).fetchone()
            if active_grant is not None:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="ticket",
                    subject_id=ticket_id,
                    action="CONFIRM",
                    code=codes.ALREADY_GRANTED,
                    status=ticket["status"],
                    actor=operator_id,
                    shift_id=shift["shift_id"],
                    version=shift["shift_version"],
                    detail={"grant_id": active_grant["grant_id"]},
                    idem_key=idem_key,
                    op="CONFIRM",
                    params=params,
                )

            confirmation_audit = self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=service_id,
                subject_type="ticket",
                subject_id=ticket_id,
                action="CONFIRM",
                decision=codes.NEUTRAL,
                code=codes.CONFIRMATION_RECORDED,
                actor=operator_id,
                shift_id=shift["shift_id"],
                assignment_version=shift["shift_version"],
                detail={"round_no": round_no},
                now=now,
            )
            conn.execute(
                "INSERT INTO confirmations(ticket_id, shift_id, shift_version, round_no,"
                " operator_id, confirmed_at, audit_no) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    shift["shift_id"],
                    shift["shift_version"],
                    round_no,
                    operator_id,
                    now,
                    confirmation_audit,
                ),
            )
            confirmed = conn.execute(
                "SELECT operator_id FROM confirmations WHERE ticket_id = ? AND shift_id = ?"
                " AND round_no = ? ORDER BY operator_id",
                (ticket_id, shift["shift_id"], round_no),
            ).fetchall()
            confirmed_operators = [row["operator_id"] for row in confirmed]
            if len(confirmed_operators) < 2:
                result = Result(
                    True,
                    codes.CONFIRMATION_RECORDED,
                    ticket["status"],
                    confirmation_audit,
                    {
                        "tenant_id": tenant_id,
                        "service_id": service_id,
                        "ticket_id": ticket_id,
                        "operator_id": operator_id,
                        "assignment_version": shift["shift_version"],
                        "confirmed_operators": confirmed_operators,
                        "needed": 2,
                    },
                )
                self._store_idempotency(
                    conn, "CONFIRM", idem_key, params, tenant_id, result, now
                )
                return result

            # Two distinct on-call operators confirmed the same ticket in the
            # same assignment version: issue the single bound grant.
            grant = self._issue_grant(
                conn,
                ticket=ticket,
                shift=shift,
                confirmed_operators=confirmed_operators,
                now=now,
                actor=operator_id,
            )
            result = Result(
                True,
                codes.GRANT_ISSUED,
                codes.GRANTED,
                grant["audit_no"],
                {
                    "tenant_id": tenant_id,
                    "service_id": service_id,
                    "ticket_id": ticket_id,
                    "grant_id": grant["grant_id"],
                    "assignment_version": shift["shift_version"],
                    "expires_at": grant["expires_at"],
                    "request_hash": ticket["request_hash"],
                    "confirmed_operators": confirmed_operators,
                    "confirmation_audit_no": confirmation_audit,
                },
            )
            self._store_idempotency(
                conn, "CONFIRM", idem_key, params, tenant_id, result, now
            )
            return result

    # -------------------------------------------------------------- consumption
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
        release_gate: Callable[[], None] | None = None,
    ) -> Result:
        store = self._ready_store(tenant_id)
        if store is None:
            return self._tenant_missing(tenant_id)
        if not isinstance(request, dict):
            return Result(False, codes.INVALID_ARGUMENT, codes.UNKNOWN, None)
        req_json = canonical_json(request)
        req_hash = request_hash(request)
        params = {
            "tenant_id": tenant_id,
            "service_id": service_id,
            "grant_id": grant_id,
            "ticket_id": ticket_id,
            "request": req_json,
            "actor": actor,
            "expected_version": expected_version,
        }
        with self._write(store, "CONSUME") as conn:
            now = self._effective_now(conn, tenant_id)
            replay = self._replay(conn, "CONSUME", idem_key, params, now)
            if replay is not None:
                return replay

            grant = self._locate_grant(conn, tenant_id, grant_id, ticket_id)
            if grant is None:
                return self._fail(
                    conn,
                    tenant_id=tenant_id,
                    service_id=service_id,
                    now=now,
                    subject_type="grant",
                    subject_id=grant_id or ticket_id or "?",
                    action="CONSUME",
                    code=codes.GRANT_NOT_FOUND,
                    status=codes.UNKNOWN,
                    actor=actor,
                    idem_key=idem_key,
                    op="CONSUME",
                    params=params,
                )
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (grant["ticket_id"],)
            ).fetchone()

            def deny(
                code: str,
                status: str,
                *,
                detail: dict[str, Any] | None = None,
                determining_audit: str | None = None,
            ) -> Result:
                attempt_audit = self._new_audit(
                    conn,
                    tenant_id=tenant_id,
                    service_id=grant["service_id"],
                    subject_type="grant",
                    subject_id=grant["grant_id"],
                    action="CONSUME",
                    decision=codes.DENIED,
                    code=code,
                    actor=actor,
                    shift_id=grant["shift_id"],
                    assignment_version=grant["assignment_version"],
                    detail={"attempt": True, **(detail or {})},
                    now=now,
                )
                data = {
                    "tenant_id": tenant_id,
                    "service_id": grant["service_id"],
                    "ticket_id": grant["ticket_id"],
                    "grant_id": grant["grant_id"],
                    "assignment_version": grant["assignment_version"],
                    "attempt_audit_no": attempt_audit,
                    **(detail or {}),
                }
                audit_no = determining_audit or attempt_audit
                if determining_audit and determining_audit != attempt_audit:
                    data["decided_audit_no"] = determining_audit
                result = Result(False, code, status, audit_no, data)
                self._store_idempotency(
                    conn, "CONSUME", idem_key, params, tenant_id, result, now
                )
                return result

            if grant["tenant_id"] != tenant_id:
                return deny(codes.REQUEST_MISMATCH, grant["status"], detail={"field": "tenant"})
            if grant["service_id"] != service_id:
                return deny(codes.SERVICE_MISMATCH, grant["status"], detail={"field": "service"})
            if grant["request_hash"] != req_hash:
                return deny(
                    codes.REQUEST_MISMATCH,
                    grant["status"],
                    detail={
                        "field": "request",
                        "expected_hash": grant["request_hash"],
                        "actual_hash": req_hash,
                    },
                )
            if expected_version is not None and expected_version != grant["assignment_version"]:
                return deny(
                    codes.STALE_ASSIGNMENT,
                    grant["status"],
                    detail={
                        "expected_version": expected_version,
                        "actual_version": grant["assignment_version"],
                    },
                )
            if grant["status"] == codes.CONSUMED:
                consuming = conn.execute(
                    "SELECT audit_no FROM consumptions WHERE grant_id = ?",
                    (grant["grant_id"],),
                ).fetchone()
                return deny(
                    codes.ALREADY_CONSUMED,
                    codes.CONSUMED,
                    detail={"actor": actor},
                    determining_audit=consuming["audit_no"] if consuming else None,
                )
            if grant["status"] == codes.REVOKED:
                return deny(
                    codes.GRANT_REVOKED,
                    codes.REVOKED,
                    detail={"terminal_reason": grant["terminal_reason"]},
                    determining_audit=grant["terminal_audit_no"],
                )
            if grant["status"] == codes.EXPIRED:
                return deny(
                    codes.GRANT_EXPIRED,
                    codes.EXPIRED,
                    detail={"expires_at": grant["expires_at"]},
                    determining_audit=grant["terminal_audit_no"],
                )
            if grant["status"] != codes.ACTIVE:
                return deny(codes.GRANT_NOT_FOUND, grant["status"])

            if now >= grant["expires_at"]:
                expiry_audit = self._expire_grant(
                    conn,
                    grant=grant,
                    ticket=ticket,
                    now=now,
                    reason="TTL_ELAPSED",
                    actor=None,
                )
                return deny(
                    codes.GRANT_EXPIRED,
                    codes.EXPIRED,
                    detail={"expires_at": grant["expires_at"]},
                    determining_audit=expiry_audit,
                )

            current_shift = self._active_shift(conn, tenant_id, grant["service_id"])
            if (
                current_shift is None
                or current_shift["shift_version"] != grant["assignment_version"]
            ):
                revoke_audit = self._revoke_grant(
                    conn,
                    grant=grant,
                    ticket=ticket,
                    now=now,
                    reason="STALE_ASSIGNMENT",
                    actor=None,
                )
                return deny(
                    codes.STALE_ASSIGNMENT,
                    codes.REVOKED,
                    detail={"reason": "assignment version is no longer active"},
                    determining_audit=revoke_audit,
                )

            consume_audit = self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=grant["service_id"],
                subject_type="grant",
                subject_id=grant["grant_id"],
                action="CONSUME",
                decision=codes.APPROVED,
                code=codes.GRANT_CONSUMED,
                actor=actor,
                shift_id=grant["shift_id"],
                assignment_version=grant["assignment_version"],
                detail={
                    "ticket_id": grant["ticket_id"],
                    "request_hash": req_hash,
                    "consumed_at": now,
                },
                now=now,
            )
            conn.execute(
                "INSERT INTO consumptions(grant_id, tenant_id, service_id, request_hash,"
                " actor, consumed_at, audit_no) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    grant["grant_id"],
                    tenant_id,
                    service_id,
                    req_hash,
                    actor,
                    now,
                    consume_audit,
                ),
            )
            conn.execute(
                "UPDATE grants SET status = 'CONSUMED', terminal_at = ?,"
                " terminal_reason = 'CONSUMED', terminal_audit_no = ? WHERE grant_id = ?",
                (now, consume_audit, grant["grant_id"]),
            )
            conn.execute(
                "UPDATE tickets SET status = 'CONSUMED', updated_at = ? WHERE ticket_id = ?",
                (now, grant["ticket_id"]),
            )
            result = Result(
                True,
                codes.GRANT_CONSUMED,
                codes.CONSUMED,
                consume_audit,
                {
                    "tenant_id": tenant_id,
                    "service_id": service_id,
                    "ticket_id": grant["ticket_id"],
                    "grant_id": grant["grant_id"],
                    "assignment_version": grant["assignment_version"],
                    "request_hash": req_hash,
                    "consumed_at": now,
                    "actor": actor,
                },
            )
            self._store_idempotency(
                conn, "CONSUME", idem_key, params, tenant_id, result, now
            )
            if release_gate is not None:
                # Runs before COMMIT while the tenant write lock is held.
                release_gate()
        self._after_commit("consume", result)
        return result

    # ---------------------------------------------------------------- recovery
    def recover(
        self,
        tenant_id: str,
        *,
        hold_seconds: float = 0.0,
        gate: Callable[[], None] | None = None,
    ) -> Result:
        """Cold-start reconciliation for exactly one tenant.

        Only ACTIVE -> terminal transitions are allowed.  The recovery runs in
        the tenant's own database, so another tenant is unaffected even while
        this transaction is held open.

        ``hold_seconds``/``gate`` exist so tests can prove cross-tenant
        isolation: they delay *this* tenant's transaction while another
        tenant's operations proceed.
        """

        store = self._ready_store(tenant_id)
        if store is None:
            return self._tenant_missing(tenant_id)
        with self._write(store, "RECOVERY") as conn:
            now = self._effective_now(conn, tenant_id)
            run_started = self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=None,
                subject_type="tenant",
                subject_id=tenant_id,
                action="RECOVERY",
                decision=codes.NEUTRAL,
                code=codes.RECOVERY_COMPLETED,
                actor=None,
                shift_id=None,
                assignment_version=None,
                detail={"phase": "start"},
                now=now,
            )
            cur = conn.execute(
                "INSERT INTO recovery_runs(tenant_id, owner, started_at, status)"
                " VALUES (?, ?, ?, 'RUNNING')",
                (tenant_id, str(uuid.uuid4()), now),
            )
            run_id = cur.lastrowid

            active_shifts = {
                row["service_id"]: row
                for row in conn.execute(
                    "SELECT * FROM shifts WHERE tenant_id = ? AND status = 'ACTIVE'",
                    (tenant_id,),
                ).fetchall()
            }
            scans = conn.execute(
                "SELECT * FROM grants WHERE tenant_id = ? AND status = 'ACTIVE'"
                " ORDER BY grant_id",
                (tenant_id,),
            ).fetchall()

            expired: list[str] = []
            revoked: list[str] = []
            for grant in scans:
                ticket = conn.execute(
                    "SELECT * FROM tickets WHERE ticket_id = ?", (grant["ticket_id"],)
                ).fetchone()
                if ticket is None:
                    continue
                if now >= grant["expires_at"]:
                    self._expire_grant(
                        conn,
                        grant=grant,
                        ticket=ticket,
                        now=now,
                        reason="TTL_ELAPSED_RECOVERY",
                        actor=None,
                    )
                    expired.append(grant["grant_id"])
                    continue
                shift = active_shifts.get(grant["service_id"])
                if shift is None or shift["shift_version"] != grant["assignment_version"]:
                    self._revoke_grant(
                        conn,
                        grant=grant,
                        ticket=ticket,
                        now=now,
                        reason="STALE_ASSIGNMENT_RECOVERY",
                        actor=None,
                    )
                    revoked.append(grant["grant_id"])

            # Defensive: a ticket marked GRANTED with no ACTIVE grant and not
            # CONSUMED is inconsistent; reopen it without touching any grant.
            orphans = conn.execute(
                "SELECT t.* FROM tickets t WHERE t.tenant_id = ? AND t.status = 'GRANTED'"
                " AND NOT EXISTS (SELECT 1 FROM grants g WHERE g.ticket_id = t.ticket_id"
                " AND g.status = 'ACTIVE')",
                (tenant_id,),
            ).fetchall()
            for ticket in orphans:
                conn.execute(
                    "UPDATE tickets SET status = 'OPEN', round_no = round_no + 1,"
                    " updated_at = ? WHERE ticket_id = ?",
                    (now, ticket["ticket_id"]),
                )
                self._new_audit(
                    conn,
                    tenant_id=tenant_id,
                    service_id=ticket["service_id"],
                    subject_type="ticket",
                    subject_id=ticket["ticket_id"],
                    action="RECOVERY",
                    decision=codes.NEUTRAL,
                    code=codes.RECONCILED_TICKET,
                    actor=None,
                    shift_id=None,
                    assignment_version=None,
                    detail={"round_no": ticket["round_no"] + 1},
                    now=now,
                )

            self._new_audit(
                conn,
                tenant_id=tenant_id,
                service_id=None,
                subject_type="tenant",
                subject_id=tenant_id,
                action="RECOVERY",
                decision=codes.NEUTRAL,
                code=codes.RECOVERY_COMPLETED,
                actor=None,
                shift_id=None,
                assignment_version=None,
                detail={
                    "phase": "finish",
                    "scanned": len(scans),
                    "expired": expired,
                    "revoked": revoked,
                    "started_audit_no": run_started,
                },
                now=now,
            )
            conn.execute(
                "UPDATE recovery_runs SET finished_at = ?, scanned = ?, expired = ?,"
                " revoked = ?, status = 'DONE' WHERE run_id = ?",
                (now, len(scans), len(expired), len(revoked), run_id),
            )
            if hold_seconds > 0 or gate is not None:
                # Deliberately keep this tenant's write transaction open.  The
                # next SELECT still sees a consistent snapshot for THIS tenant;
                # other tenant databases are independent files.
                if gate is not None:
                    gate()
                elif hold_seconds > 0:
                    import time

                    time.sleep(hold_seconds)
            result = Result(
                True,
                codes.RECOVERY_COMPLETED,
                codes.RECOVERED,
                run_started,
                {
                    "tenant_id": tenant_id,
                    "scanned": len(scans),
                    "expired": expired,
                    "revoked": revoked,
                    "reopened": [t["ticket_id"] for t in orphans],
                    "run_id": run_id,
                },
            )
        self._after_commit("recover", result)
        return result

    # ----------------------------------------------------------------- queries
    def ticket_status(self, tenant_id: str, ticket_id: str) -> dict[str, Any] | None:
        store = self._store(tenant_id)
        if not store.exists():
            return None
        with store.read() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ? AND tenant_id = ?",
                (ticket_id, tenant_id),
            ).fetchone()
            if row is None:
                return None
            grants = [
                dict(g)
                for g in conn.execute(
                    "SELECT * FROM grants WHERE ticket_id = ? ORDER BY rowid",
                    (ticket_id,),
                ).fetchall()
            ]
            confirmations = [
                dict(c)
                for c in conn.execute(
                    "SELECT * FROM confirmations WHERE ticket_id = ? ORDER BY confirmation_id",
                    (ticket_id,),
                ).fetchall()
            ]
            return {"ticket": dict(row), "grants": grants, "confirmations": confirmations}

    def grant_status(self, tenant_id: str, grant_id: str) -> dict[str, Any] | None:
        store = self._store(tenant_id)
        if not store.exists():
            return None
        with store.read() as conn:
            row = conn.execute(
                "SELECT * FROM grants WHERE grant_id = ? AND tenant_id = ?",
                (grant_id, tenant_id),
            ).fetchone()
            return dict(row) if row else None

    def active_shift(self, tenant_id: str, service_id: str) -> dict[str, Any] | None:
        store = self._store(tenant_id)
        if not store.exists():
            return None
        with store.read() as conn:
            row = self._active_shift(conn, tenant_id, service_id)
            if row is None:
                return None
            result = dict(row)
            result["operators"] = self._shift_operators(conn, row["shift_id"])
            return result

    def audit_log(
        self,
        tenant_id: str,
        *,
        ticket_id: str | None = None,
        decision: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        store = self._store(tenant_id)
        if not store.exists():
            return []
        sql = "SELECT * FROM audit WHERE tenant_id = ?"
        args: list[Any] = [tenant_id]
        if ticket_id is not None:
            sql += " AND (subject_id = ? OR detail_json LIKE ?)"
            args.extend([ticket_id, f'%"{ticket_id}"%'])
        if decision is not None:
            sql += " AND decision = ?"
            args.append(decision)
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with store.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json"))
            except json.JSONDecodeError:
                item["detail"] = {}
            out.append(item)
        return out

    def tenant_summary(self, tenant_id: str) -> dict[str, Any]:
        store = self._store(tenant_id)
        if not store.exists():
            return {"tenant_id": tenant_id, "exists": False}
        with store.read() as conn:
            counts = {}
            for table in ("tickets", "grants", "confirmations", "consumptions", "audit"):
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"]
            grants_by_status = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM grants GROUP BY status"
                ).fetchall()
            }
            tickets_by_status = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
                ).fetchall()
            }
            services = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM services WHERE tenant_id = ? ORDER BY service_id",
                    (tenant_id,),
                ).fetchall()
            ]
            shifts = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM shifts WHERE tenant_id = ? ORDER BY shift_id",
                    (tenant_id,),
                ).fetchall()
            ]
            clock = conn.execute(
                "SELECT last_seen_at FROM tenant_clock WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return {
            "tenant_id": tenant_id,
            "exists": True,
            "counts": counts,
            "grants_by_status": grants_by_status,
            "tickets_by_status": tickets_by_status,
            "services": services,
            "shifts": shifts,
            "effective_now": clock["last_seen_at"] if clock else None,
        }

    # -------------------------------------------------------------- internals
    def _tenant_missing(self, tenant_id: str) -> Result:
        return Result(False, codes.TENANT_NOT_FOUND, codes.UNKNOWN, None, {"tenant_id": tenant_id})

    def _ready_store(self, tenant_id: str) -> TenantStore | None:
        """Return the store only when the database and tenant row both exist.

        A database file without a tenant row can only result from provisioning
        dying before COMMIT; treating it as missing keeps every other operation
        on a clean ``TENANT_NOT_FOUND`` path instead of a foreign-key error.
        """

        store = self._store(tenant_id)
        if not store.exists():
            return None
        with store.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return store if row else None

    def _after_commit(self, operation: str, result: Result) -> None:
        if self.fault_hook is not None:
            self.fault_hook(operation, result)

    def _effective_now(self, conn: sqlite3.Connection, tenant_id: str) -> str:
        wall = format_timestamp(self.clock.now())
        row = conn.execute(
            "SELECT last_seen_at FROM tenant_clock WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        effective = wall
        if row is not None and row["last_seen_at"] > wall:
            effective = row["last_seen_at"]
        # tenant_clock has a foreign key to tenants.  A database file can exist
        # without a tenant row only if provisioning crashed before COMMIT, in
        # which case there is nothing to record; the caller will just use the
        # injected wall time and the operation will be denied as unprovisioned.
        registered = conn.execute(
            "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if registered:
            conn.execute(
                "INSERT INTO tenant_clock(tenant_id, last_seen_at) VALUES (?, ?)"
                " ON CONFLICT(tenant_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (tenant_id, effective),
            )
        return effective

    def _service_exists(
        self, conn: sqlite3.Connection, tenant_id: str, service_id: str
    ) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM services WHERE tenant_id = ? AND service_id = ?",
                (tenant_id, service_id),
            ).fetchone()
            is not None
        )

    def _active_shift(
        self, conn: sqlite3.Connection, tenant_id: str, service_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM shifts WHERE tenant_id = ? AND service_id = ?"
            " AND status = 'ACTIVE'",
            (tenant_id, service_id),
        ).fetchone()

    def _next_shift_version(
        self, conn: sqlite3.Connection, tenant_id: str, service_id: str
    ) -> int:
        row = conn.execute(
            "SELECT MAX(shift_version) AS v FROM shifts WHERE tenant_id = ? AND service_id = ?",
            (tenant_id, service_id),
        ).fetchone()
        return int(row["v"] or 0) + 1

    def _insert_shift(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        service_id: str,
        version: int,
        operators: list[str],
        now: str,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO shifts(tenant_id, service_id, shift_version, status, started_at)"
            " VALUES (?, ?, ?, 'ACTIVE', ?)",
            (tenant_id, service_id, version, now),
        )
        shift_id = int(cur.lastrowid)
        for operator in operators:
            conn.execute(
                "INSERT INTO shift_operators(shift_id, operator_id) VALUES (?, ?)",
                (shift_id, operator),
            )
        return shift_id

    def _shift_operators(self, conn: sqlite3.Connection, shift_id: int) -> list[str]:
        return [
            row["operator_id"]
            for row in conn.execute(
                "SELECT operator_id FROM shift_operators WHERE shift_id = ?"
                " ORDER BY operator_id",
                (shift_id,),
            ).fetchall()
        ]

    def _issue_grant(
        self,
        conn: sqlite3.Connection,
        *,
        ticket: sqlite3.Row,
        shift: sqlite3.Row,
        confirmed_operators: list[str],
        now: str,
        actor: str,
    ) -> dict[str, Any]:
        service = conn.execute(
            "SELECT * FROM services WHERE service_id = ?", (ticket["service_id"],)
        ).fetchone()
        ttl = int(service["approval_ttl_seconds"])
        expires_at = format_timestamp(parse_timestamp(now) + timedelta(seconds=ttl))
        grant_number = self._next_counter(conn, "grant")
        grant_id = f"G-{ticket['tenant_id']}-{grant_number:06d}"
        audit_no = self._new_audit(
            conn,
            tenant_id=ticket["tenant_id"],
            service_id=ticket["service_id"],
            subject_type="grant",
            subject_id=grant_id,
            action="GRANT_ISSUE",
            decision=codes.APPROVED,
            code=codes.GRANT_ISSUED,
            actor=actor,
            shift_id=shift["shift_id"],
            assignment_version=shift["shift_version"],
            detail={
                "ticket_id": ticket["ticket_id"],
                "request_hash": ticket["request_hash"],
                "confirmed_operators": confirmed_operators,
                "expires_at": expires_at,
            },
            now=now,
        )
        conn.execute(
            "INSERT INTO grants(grant_id, tenant_id, service_id, ticket_id, request_hash,"
            " assignment_version, shift_id, round_no, status, issued_at, issued_audit_no,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)",
            (
                grant_id,
                ticket["tenant_id"],
                ticket["service_id"],
                ticket["ticket_id"],
                ticket["request_hash"],
                shift["shift_version"],
                shift["shift_id"],
                ticket["round_no"],
                now,
                audit_no,
                expires_at,
            ),
        )
        conn.execute(
            "UPDATE tickets SET status = 'GRANTED', updated_at = ? WHERE ticket_id = ?",
            (now, ticket["ticket_id"]),
        )
        return {"grant_id": grant_id, "audit_no": audit_no, "expires_at": expires_at}

    def _expire_grant(
        self,
        conn: sqlite3.Connection,
        *,
        grant: sqlite3.Row,
        ticket: sqlite3.Row,
        now: str,
        reason: str,
        actor: str | None,
    ) -> str:
        audit_no = self._new_audit(
            conn,
            tenant_id=grant["tenant_id"],
            service_id=grant["service_id"],
            subject_type="grant",
            subject_id=grant["grant_id"],
            action="EXPIRE_GRANT",
            decision=codes.NEUTRAL,
            code=codes.GRANT_EXPIRED,
            actor=actor,
            shift_id=grant["shift_id"],
            assignment_version=grant["assignment_version"],
            detail={"reason": reason, "expires_at": grant["expires_at"]},
            now=now,
        )
        conn.execute(
            "UPDATE grants SET status = 'EXPIRED', terminal_at = ?, terminal_reason = ?,"
            " terminal_audit_no = ? WHERE grant_id = ?",
            (now, reason, audit_no, grant["grant_id"]),
        )
        if ticket["status"] == codes.GRANTED:
            conn.execute(
                "UPDATE tickets SET status = 'OPEN', round_no = round_no + 1,"
                " updated_at = ? WHERE ticket_id = ?",
                (now, ticket["ticket_id"]),
            )
        return audit_no

    def _revoke_grant(
        self,
        conn: sqlite3.Connection,
        *,
        grant: sqlite3.Row,
        ticket: sqlite3.Row,
        now: str,
        reason: str,
        actor: str | None,
    ) -> str:
        audit_no = self._new_audit(
            conn,
            tenant_id=grant["tenant_id"],
            service_id=grant["service_id"],
            subject_type="grant",
            subject_id=grant["grant_id"],
            action="REVOKE_GRANT",
            decision=codes.NEUTRAL,
            code=codes.GRANT_REVOKED,
            actor=actor,
            shift_id=grant["shift_id"],
            assignment_version=grant["assignment_version"],
            detail={"reason": reason},
            now=now,
        )
        conn.execute(
            "UPDATE grants SET status = 'REVOKED', terminal_at = ?, terminal_reason = ?,"
            " terminal_audit_no = ? WHERE grant_id = ?",
            (now, reason, audit_no, grant["grant_id"]),
        )
        if ticket["status"] == codes.GRANTED:
            conn.execute(
                "UPDATE tickets SET status = 'OPEN', round_no = round_no + 1,"
                " updated_at = ? WHERE ticket_id = ?",
                (now, ticket["ticket_id"]),
            )
        return audit_no

    def _reclaim_grants(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        service_id: str,
        stale_before_version: int,
        reason: str,
        now: str,
        actor: str | None,
    ) -> list[str]:
        stale = conn.execute(
            "SELECT * FROM grants WHERE tenant_id = ? AND service_id = ?"
            " AND status = 'ACTIVE' AND assignment_version < ? ORDER BY grant_id",
            (tenant_id, service_id, stale_before_version),
        ).fetchall()
        revoked: list[str] = []
        for grant in stale:
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (grant["ticket_id"],)
            ).fetchone()
            if ticket is None:
                continue
            self._revoke_grant(
                conn, grant=grant, ticket=ticket, now=now, reason=reason, actor=actor
            )
            revoked.append(grant["grant_id"])
        return revoked

    def _locate_grant(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        grant_id: str | None,
        ticket_id: str | None,
    ) -> sqlite3.Row | None:
        if grant_id:
            row = conn.execute(
                "SELECT * FROM grants WHERE grant_id = ? AND tenant_id = ?",
                (grant_id, tenant_id),
            ).fetchone()
            if row is not None:
                return row
            return None
        if ticket_id:
            row = conn.execute(
                "SELECT * FROM grants WHERE ticket_id = ? AND tenant_id = ?"
                " ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END, rowid DESC LIMIT 1",
                (ticket_id, tenant_id),
            ).fetchone()
            return row
        return None

    def _next_counter(self, conn: sqlite3.Connection, name: str) -> int:
        row = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
        value = int(row["value"]) + 1 if row else 1
        conn.execute(
            "INSERT INTO counters(name, value) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value),
        )
        return value

    def _new_audit(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str | None,
        service_id: str | None,
        subject_type: str,
        subject_id: str,
        action: str,
        decision: str,
        code: str,
        actor: str | None,
        shift_id: int | None,
        assignment_version: int | None,
        detail: dict[str, Any],
        now: str,
    ) -> str:
        number = self._next_counter(conn, "audit")
        prefix = tenant_id or "global"
        audit_no = f"AUD-{prefix}-{number:08d}"
        conn.execute(
            "INSERT INTO audit(audit_no, tenant_id, service_id, subject_type, subject_id,"
            " action, decision, code, actor, shift_id, assignment_version, detail_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_no,
                tenant_id,
                service_id,
                subject_type,
                subject_id,
                action,
                decision,
                code,
                actor,
                shift_id,
                assignment_version,
                canonical_json(detail),
                now,
            ),
        )
        return audit_no

    def _replay(
        self,
        conn: sqlite3.Connection,
        operation: str,
        idem_key: str | None,
        params: dict[str, Any],
        now: str,
    ) -> Result | None:
        if not idem_key:
            return None
        row = conn.execute(
            "SELECT * FROM idempotency WHERE operation = ? AND idem_key = ?",
            (operation, idem_key),
        ).fetchone()
        if row is None:
            return None
        incoming = params_hash(params)
        if row["params_hash"] != incoming:
            # Same key, different parameters: a conflict, not a replay.
            audit_no = self._new_audit(
                conn,
                tenant_id=row["tenant_id"],
                service_id=None,
                subject_type="idempotency",
                subject_id=idem_key,
                action=operation,
                decision=codes.DENIED,
                code=codes.IDEMPOTENCY_CONFLICT,
                actor=None,
                shift_id=None,
                assignment_version=None,
                detail={
                    "stored_params_hash": row["params_hash"],
                    "incoming_params_hash": incoming,
                    "original_audit_no": row["audit_no"],
                },
                now=now,
            )
            return Result(
                False,
                codes.IDEMPOTENCY_CONFLICT,
                codes.CONFLICT,
                audit_no,
                {
                    "operation": operation,
                    "idem_key": idem_key,
                    "original_audit_no": row["audit_no"],
                    "stored_params_hash": row["params_hash"],
                    "incoming_params_hash": incoming,
                },
            )
        conn.execute(
            "UPDATE idempotency SET replay_count = replay_count + 1"
            " WHERE operation = ? AND idem_key = ?",
            (operation, idem_key),
        )
        return Result.from_payload(json.loads(row["result_json"]), replayed=True)

    def _store_idempotency(
        self,
        conn: sqlite3.Connection,
        operation: str,
        idem_key: str | None,
        params: dict[str, Any],
        tenant_id: str,
        result: Result,
        now: str,
    ) -> None:
        if not idem_key:
            return
        conn.execute(
            "INSERT INTO idempotency(operation, idem_key, params_hash, tenant_id,"
            " result_json, audit_no, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                operation,
                idem_key,
                params_hash(params),
                tenant_id,
                result.to_json(),
                result.audit_no,
                now,
            ),
        )

    def _fail(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str | None,
        service_id: str | None,
        now: str,
        subject_type: str,
        subject_id: str,
        action: str,
        code: str,
        status: str,
        actor: str | None = None,
        shift_id: int | None = None,
        version: int | None = None,
        detail: dict[str, Any] | None = None,
        idem_key: str | None = None,
        op: str | None = None,
        params: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Result:
        audit_no = self._new_audit(
            conn,
            tenant_id=tenant_id,
            service_id=service_id,
            subject_type=subject_type,
            subject_id=subject_id,
            action=action,
            decision=codes.DENIED,
            code=code,
            actor=actor,
            shift_id=shift_id,
            assignment_version=version,
            detail={"reason": code, **(detail or {})},
            now=now,
        )
        data = {"attempt_audit_no": audit_no, **(detail or {}), **(extra or {})}
        result = Result(False, code, status, audit_no, data)
        if idem_key and op and params is not None:
            self._store_idempotency(conn, op, idem_key, params, tenant_id or "", result, now)
        return result
