"""Defense-in-depth: the database itself refuses unsafe transitions.

The service is the intended writer, but these tests bypass it and confirm the
triggers independently prevent resurrection, double consumption, and binding
mismatches.
"""

import sqlite3
import tempfile
import unittest

from approval import ApprovalService, ManualClock
from approval.scenarios import REQ, _setup


class TriggerInvariantTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.svc = ApprovalService(self.root, ManualClock("2026-01-01T00:00:00Z"))
        _setup(self.svc)
        self.svc.create_ticket("acme", "pay", "T1", REQ, idem_key="t")
        self.svc.confirm("acme", "T1", "alice", idem_key="a")
        issued = self.svc.confirm("acme", "T1", "bob", idem_key="b")
        self.grant_id = issued.data["grant_id"]
        self.db = str(self.svc._store("acme").db_path)

    def connect(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def test_terminal_grant_cannot_be_revived(self):
        self.svc.consume("acme", "pay", REQ, "runbook", grant_id=self.grant_id)
        conn = self.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE grants SET status = 'ACTIVE' WHERE grant_id = ?",
                    (self.grant_id,),
                )
        finally:
            conn.close()

    def test_consumption_requires_active_grant(self):
        # Revoke through a rotation, then try to insert a consumption directly.
        self.svc.rotate_shift("acme", "pay", ["carol", "dave"], idem_key="r")
        conn = self.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO consumptions(grant_id, tenant_id, service_id,"
                    " request_hash, actor, consumed_at, audit_no)"
                    " VALUES (?, 'acme', 'pay', 'x', 'evil', '2026-01-01T00:00:00Z', 'A')",
                    (self.grant_id,),
                )
        finally:
            conn.close()

    def test_binding_mismatch_rejected(self):
        conn = self.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO consumptions(grant_id, tenant_id, service_id,"
                    " request_hash, actor, consumed_at, audit_no)"
                    " VALUES (?, 'acme', 'pay', 'wrong-hash', 'evil',"
                    " '2026-01-01T00:00:00Z', 'A')",
                    (self.grant_id,),
                )
        finally:
            conn.close()

    def test_consumed_ticket_is_immutable(self):
        self.svc.consume("acme", "pay", REQ, "runbook", grant_id=self.grant_id)
        conn = self.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE tickets SET status = 'OPEN' WHERE ticket_id = 'T1'")
        finally:
            conn.close()


class IdempotencySemanticsTest(unittest.TestCase):
    def test_conflict_is_distinct_from_request_mismatch(self):
        root = tempfile.mkdtemp()
        svc = ApprovalService(root, ManualClock("2026-01-01T00:00:00Z"))
        _setup(svc)
        svc.create_ticket("acme", "pay", "T1", REQ, idem_key="t")
        svc.confirm("acme", "T1", "alice", idem_key="a")
        issued = svc.confirm("acme", "T1", "bob", idem_key="b")

        ok = svc.consume("acme", "pay", REQ, "runbook", grant_id=issued.data["grant_id"], idem_key="k")
        self.assertEqual(ok.code, "GRANT_CONSUMED")

        # Same key, changed parameters -> conflict.
        conflict = svc.consume(
            "acme", "pay", {"action": "delete"}, "other", grant_id=issued.data["grant_id"], idem_key="k"
        )
        self.assertEqual(conflict.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(conflict.status, "CONFLICT")

        # New key, changed request content -> request mismatch (a business
        # denial, not a key conflict).
        mismatch = svc.consume("acme", "pay", {"action": "delete"}, "runbook", grant_id=issued.data["grant_id"])
        self.assertIn(mismatch.code, {"REQUEST_MISMATCH", "ALREADY_CONSUMED"})

    def test_confirm_retry_replays_recorded_audit(self):
        root = tempfile.mkdtemp()
        svc = ApprovalService(root, ManualClock("2026-01-01T00:00:00Z"))
        _setup(svc)
        svc.create_ticket("acme", "pay", "T1", REQ, idem_key="t")
        first = svc.confirm("acme", "T1", "alice", idem_key="key-a")
        retry = svc.confirm("acme", "T1", "alice", idem_key="key-a")
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.audit_no, first.audit_no)
        self.assertEqual(retry.data, first.data)


if __name__ == "__main__":
    unittest.main()
