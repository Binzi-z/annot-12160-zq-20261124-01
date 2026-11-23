"""Cold-start proofs.

1. Tenant isolation: a deliberately *blocked* recovery for tenant A (its write
   transaction held open behind a gate) must not freeze tenant B.  We prove it
   with a real second process and a hard deadline, not just by timing luck.

2. No resurrection on restart: a grant that expired while the service was down
   is never restored to ACTIVE by recovery.  Time only moves forward per tenant
   (tenant_clock), so a wall-clock rollback cannot extend a grant either.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from approval import ApprovalService, ManualClock
from approval.scenarios import REQ, _setup

from procutil import finish, read_result, spawn_worker, wait_for_file

START = "2026-01-01T00:00:00Z"


class TenantIsolationTest(unittest.TestCase):
    def test_blocked_recovery_of_one_tenant_does_not_freeze_another(self):
        root = tempfile.mkdtemp()
        svc = ApprovalService(root, ManualClock(START))
        # Tenant A (acme) and tenant B (globex) are fully independent databases.
        _setup(svc, tenant="acme", service="pay")
        _setup(svc, tenant="globex", service="db")
        svc.create_ticket("globex", "db", "B1", REQ, idem_key="b-t")
        svc.confirm("globex", "B1", "alice", idem_key="b-a")
        issued = svc.confirm("globex", "B1", "bob", idem_key="b-b")
        self.assertEqual(issued.code, "GRANT_ISSUED", issued)

        ready = Path(root) / "recovery_ready"
        gate = Path(root) / "recovery_go"
        result_a = Path(root) / "recovery_a.json"

        # Start recovery for acme and hold its write transaction open at the gate.
        recoverer = spawn_worker(
            "--data-dir", root, "--now", "2026-01-01T00:05:00Z",
            "recover", "--tenant", "acme",
            "--ready-file", str(ready), "--gate-file", str(gate),
            "--gate-timeout", "30", "--result-file", str(result_a),
        )
        wait_for_file(ready, timeout=20)

        # While acme's recovery is still mid-transaction, globex must work.
        started = time.monotonic()
        consumed = svc.consume(
            "globex", "db", REQ, "runbook", ticket_id="B1", idem_key="b-x"
        )
        elapsed = time.monotonic() - started
        self.assertEqual(consumed.code, "GRANT_CONSUMED", consumed)
        self.assertLess(elapsed, 5.0, f"globex was blocked for {elapsed:.2f}s")

        # globex's decision is durable while acme's recovery is still open.
        fresh = ApprovalService(root, ManualClock(START))
        self.assertEqual(fresh.grant_status("globex", issued.data["grant_id"])["status"], "CONSUMED")

        # Release acme's recovery and let it finish.
        gate.write_text("go", encoding="utf-8")
        code, out, err = finish(recoverer, timeout=60)
        self.assertEqual(code, 0, err)
        rec = read_result(result_a)
        self.assertEqual(rec["code"], "RECOVERY_COMPLETED")

    def test_recovery_only_touches_its_own_tenant(self):
        root = tempfile.mkdtemp()
        svc = ApprovalService(root, ManualClock(START))
        _setup(svc, tenant="acme", service="pay")
        _setup(svc, tenant="globex", service="db")
        for tenant, service, ticket in (("acme", "pay", "A1"), ("globex", "db", "B1")):
            svc.create_ticket(tenant, service, ticket, REQ, idem_key=f"{ticket}-t")
            svc.confirm(tenant, ticket, "alice", idem_key=f"{ticket}-a")
            svc.confirm(tenant, ticket, "bob", idem_key=f"{ticket}-b")

        # Recovery of acme leaves globex's active grant untouched.
        before = svc.grant_status("globex", "G-globex-000001")
        self.assertIsNotNone(before)
        self.assertEqual(before["status"], "ACTIVE")
        svc.recover("acme")
        after = svc.grant_status("globex", "G-globex-000001")
        self.assertEqual(after["status"], "ACTIVE")


class RestartDoesNotResurrectExpiredTest(unittest.TestCase):
    def _issue(self, root: str, ttl: int) -> tuple[str, str]:
        svc = ApprovalService(root, ManualClock(START))
        _setup(svc, ttl=ttl)
        svc.create_ticket("acme", "pay", "T1", REQ, idem_key="t")
        svc.confirm("acme", "T1", "alice", idem_key="a")
        issued = svc.confirm("acme", "T1", "bob", idem_key="b")
        self.assertEqual(issued.code, "GRANT_ISSUED", issued)
        return issued.data["grant_id"], issued.data["expires_at"]

    def test_expired_grant_stays_expired_after_restart(self):
        root = tempfile.mkdtemp()
        grant_id, expires_at = self._issue(root, ttl=600)

        # Service stops.  It restarts later, well past expiry.
        restarted = ApprovalService(root, ManualClock("2026-01-01T01:00:00Z"))
        recovery = restarted.recover("acme")
        self.assertEqual(recovery.code, "RECOVERY_COMPLETED")
        self.assertIn(grant_id, recovery.data["expired"])

        grant = restarted.grant_status("acme", grant_id)
        self.assertEqual(grant["status"], "EXPIRED")

        # It cannot be consumed, and recovery a second time does not revive it.
        denied = restarted.consume("acme", "pay", REQ, "runbook", grant_id=grant_id)
        self.assertEqual(denied.code, "GRANT_EXPIRED")
        restarted.recover("acme")
        self.assertEqual(restarted.grant_status("acme", grant_id)["status"], "EXPIRED")

    def test_wall_clock_rollback_cannot_extend_grant(self):
        root = tempfile.mkdtemp()
        grant_id, _ = self._issue(root, ttl=600)

        # Advance the tenant's effective clock to just before expiry and run a
        # recovery, which records the high-water mark.
        svc = ApprovalService(root, ManualClock("2026-01-01T00:09:00Z"))
        svc.recover("acme")
        self.assertEqual(svc.grant_status("acme", grant_id)["status"], "ACTIVE")

        # Restart with a clock far in the past.  Effective time stays at the
        # high-water mark, so this is not a fresh 10 minutes.
        rolled_back = ApprovalService(root, ManualClock("2025-01-01T00:00:00Z"))
        rolled_back.recover("acme")
        self.assertEqual(rolled_back.grant_status("acme", grant_id)["status"], "ACTIVE")
        summary = rolled_back.tenant_summary("acme")
        self.assertEqual(summary["effective_now"], "2026-01-01T00:09:00.000000Z")

        # Now move past the true expiry; it must expire, not be extended.
        past = ApprovalService(root, ManualClock("2026-01-01T00:10:01Z"))
        past.recover("acme")
        self.assertEqual(past.grant_status("acme", grant_id)["status"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()
