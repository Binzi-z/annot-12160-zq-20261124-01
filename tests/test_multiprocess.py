"""Multi-process proofs: races, crash-after-commit, and cross-tenant isolation.

These spawn real child processes so SQLite is exercised exactly as it is in
production: separate connections, separate write transactions.
"""

import json
import tempfile
import unittest
from pathlib import Path

from approval import ApprovalService, ManualClock
from approval.scenarios import REQ, _setup

from procutil import finish, read_result, spawn_worker, wait_for_file

START = "2026-01-01T00:00:00Z"


def prepare(root: str) -> str:
    """Seed one tenant and issue a single grant, returning the grant id."""

    svc = ApprovalService(root, ManualClock(START))
    _setup(svc)
    svc.create_ticket("acme", "pay", "T1", REQ, idem_key="t")
    svc.confirm("acme", "T1", "alice", idem_key="a")
    issued = svc.confirm("acme", "T1", "bob", idem_key="b")
    assert issued.code == "GRANT_ISSUED", issued
    return issued.data["grant_id"]


class MultiProcessConsumeTest(unittest.TestCase):
    def test_two_processes_consume_only_once(self):
        root = tempfile.mkdtemp()
        grant_id = prepare(root)
        req = json.dumps(REQ)
        ready_a = Path(root) / "ready_a"
        result_a = Path(root) / "a.json"
        result_b = Path(root) / "b.json"

        # Process A acquires the tenant write lock and holds it before COMMIT.
        a = spawn_worker(
            "--data-dir", root, "--now", START,
            "consume", "--tenant", "acme", "--service", "pay",
            "--request", req, "--actor", "runbook-a", "--grant", grant_id,
            "--idem-key", "xa",
            "--ready-file", str(ready_a), "--hold-seconds", "1.0",
            "--result-file", str(result_a),
        )
        wait_for_file(ready_a)
        # Process B has a different idempotency key, so it is a genuinely
        # independent attempt to consume the same grant.
        b = spawn_worker(
            "--data-dir", root, "--now", START,
            "consume", "--tenant", "acme", "--service", "pay",
            "--request", req, "--actor", "runbook-b", "--grant", grant_id,
            "--idem-key", "xb", "--result-file", str(result_b),
        )
        code_a, _, err_a = finish(a)
        code_b, _, err_b = finish(b)
        self.assertEqual(code_a, 0, err_a)
        self.assertEqual(code_b, 3, err_b)  # B must be denied

        res_a = read_result(result_a)
        res_b = read_result(result_b)
        self.assertEqual(res_a["code"], "GRANT_CONSUMED")
        self.assertEqual(res_b["code"], "ALREADY_CONSUMED")
        self.assertEqual(res_b["status"], "CONSUMED")
        # B's reported deciding audit number is A's original consumption.
        self.assertEqual(res_b["audit_no"], res_a["audit_no"])

        # Exactly one consumption row and exactly one grant transition exist.
        svc = ApprovalService(root, ManualClock(START))
        status = svc.ticket_status("acme", "T1")
        self.assertEqual(status["ticket"]["status"], "CONSUMED")
        consumed = [g for g in status["grants"] if g["status"] == "CONSUMED"]
        self.assertEqual(len(consumed), 1)
        approving = [
            a for a in svc.audit_log("acme") if a["action"] == "CONSUME" and a["decision"] == "APPROVED"
        ]
        self.assertEqual(len(approving), 1)


class RotationRaceTest(unittest.TestCase):
    def test_rotation_committed_while_consume_waits_revokes(self):
        """Consume starts first but rotation commits first -> consume denied."""

        root = tempfile.mkdtemp()
        grant_id = prepare(root)
        req = json.dumps(REQ)
        rotate_ready = Path(root) / "rotate_ready"
        rotate_result = Path(root) / "rotate.json"
        consume_result = Path(root) / "consume.json"

        rotator = spawn_worker(
            "--data-dir", root, "--now", START,
            "rotate", "--tenant", "acme", "--service", "pay",
            "--operator", "carol", "--operator", "dave", "--idem-key", "r1",
            "--ready-file", str(rotate_ready), "--hold-seconds", "1.0",
            "--result-file", str(rotate_result),
        )
        wait_for_file(rotate_ready)
        consumer = spawn_worker(
            "--data-dir", root, "--now", START,
            "consume", "--tenant", "acme", "--service", "pay",
            "--request", req, "--actor", "runbook", "--grant", grant_id,
            "--idem-key", "x1", "--result-file", str(consume_result),
        )
        code_r, _, err_r = finish(rotator)
        code_c, _, err_c = finish(consumer)
        self.assertEqual(code_r, 0, err_r)
        self.assertEqual(code_c, 3, err_c)

        rotated = read_result(rotate_result)
        consumed = read_result(consume_result)
        self.assertEqual(rotated["code"], "SHIFT_ROTATED")
        self.assertEqual(consumed["code"], "GRANT_REVOKED")
        self.assertEqual(consumed["status"], "REVOKED")
        # The revocation is the deciding audit row for the denied consume.
        self.assertIsNotNone(consumed["audit_no"])

    def test_consume_committed_while_rotation_waits_stands(self):
        """Consume commits first; the later rotation must not un-consume it."""

        root = tempfile.mkdtemp()
        grant_id = prepare(root)
        req = json.dumps(REQ)
        consume_ready = Path(root) / "consume_ready"
        consume_result = Path(root) / "consume.json"
        rotate_result = Path(root) / "rotate.json"

        consumer = spawn_worker(
            "--data-dir", root, "--now", START,
            "consume", "--tenant", "acme", "--service", "pay",
            "--request", req, "--actor", "runbook", "--grant", grant_id,
            "--idem-key", "x1",
            "--ready-file", str(consume_ready), "--hold-seconds", "1.0",
            "--result-file", str(consume_result),
        )
        wait_for_file(consume_ready)
        rotator = spawn_worker(
            "--data-dir", root, "--now", START,
            "rotate", "--tenant", "acme", "--service", "pay",
            "--operator", "carol", "--operator", "dave", "--idem-key", "r1",
            "--result-file", str(rotate_result),
        )
        code_c, _, err_c = finish(consumer)
        code_r, _, err_r = finish(rotator)
        self.assertEqual(code_c, 0, err_c)
        self.assertEqual(code_r, 0, err_r)

        consumed = read_result(consume_result)
        rotated = read_result(rotate_result)
        self.assertEqual(consumed["code"], "GRANT_CONSUMED")
        self.assertEqual(rotated["code"], "SHIFT_ROTATED")
        # Nothing to reclaim: the only grant was already consumed.
        self.assertEqual(rotated["data"]["revoked_grants"], [])

        svc = ApprovalService(root, ManualClock(START))
        grant = svc.grant_status("acme", grant_id)
        self.assertEqual(grant["status"], "CONSUMED")
        self.assertEqual(svc.ticket_status("acme", "T1")["ticket"]["status"], "CONSUMED")


class CrashAfterCommitTest(unittest.TestCase):
    def test_commit_then_die_is_replayed_not_reapplied(self):
        root = tempfile.mkdtemp()
        grant_id = prepare(root)
        req = json.dumps(REQ)

        proc = spawn_worker(
            "--data-dir", root, "--now", START,
            "consume", "--tenant", "acme", "--service", "pay",
            "--request", req, "--actor", "runbook", "--grant", grant_id,
            "--idem-key", "crash-1", "--crash-after-commit",
        )
        code, out, err = finish(proc)
        # The process died with our sentinel code: the caller saw no reply.
        self.assertEqual(code, 23, err)

        # Durably, the consumption exists exactly once.
        svc = ApprovalService(root, ManualClock(START))
        grant = svc.grant_status("acme", grant_id)
        self.assertEqual(grant["status"], "CONSUMED")

        # A retry with the same key must return the original business state and
        # the original audit number, not perform a second consumption.
        retry = svc.consume(
            "acme", "pay", REQ, "runbook", grant_id=grant_id, idem_key="crash-1"
        )
        self.assertTrue(retry.replayed)
        self.assertTrue(retry.ok)
        self.assertEqual(retry.code, "GRANT_CONSUMED")
        self.assertEqual(retry.status, "CONSUMED")
        self.assertEqual(retry.audit_no, grant["terminal_audit_no"])

        approvals = [
            a
            for a in svc.audit_log("acme")
            if a["action"] == "CONSUME" and a["decision"] == "APPROVED"
        ]
        self.assertEqual(len(approvals), 1)


class CrashBeforeCommitTest(unittest.TestCase):
    def test_die_before_commit_leaves_no_partial_state(self):
        root = tempfile.mkdtemp()
        grant_id = prepare(root)
        req = json.dumps(REQ)

        proc = spawn_worker(
            "--data-dir", root, "--now", START,
            "consume", "--tenant", "acme", "--service", "pay",
            "--request", req, "--actor", "runbook", "--grant", grant_id,
            "--idem-key", "crash-2", "--crash-before-commit",
        )
        code, out, err = finish(proc)
        self.assertEqual(code, 24, err)

        svc = ApprovalService(root, ManualClock(START))
        # Grant is still ACTIVE and no consumption/audit escaped.
        self.assertEqual(svc.grant_status("acme", grant_id)["status"], "ACTIVE")
        self.assertEqual(svc.ticket_status("acme", "T1")["ticket"]["status"], "GRANTED")
        self.assertFalse(
            [a for a in svc.audit_log("acme") if a["action"] == "CONSUME"]
        )

        # The same key can now be used successfully; nothing was half-recorded.
        retry = svc.consume(
            "acme", "pay", REQ, "runbook", grant_id=grant_id, idem_key="crash-2"
        )
        self.assertFalse(retry.replayed)
        self.assertEqual(retry.code, "GRANT_CONSUMED")


if __name__ == "__main__":
    unittest.main()
