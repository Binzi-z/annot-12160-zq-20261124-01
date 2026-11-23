"""Per-decision evidence.

Each scenario is compared step-by-step against an explicit expectation table.
This is stronger than the differential check alone: both implementations could
agree on the wrong answer, so here every approve/deny decision is pinned to the
business code and resulting status the specification requires.
"""

import unittest

from approval.scenarios import SCENARIOS, run_scenario


# (step, ok, code, status)
EXPECTED = {
    "duplicate_confirmation": [
        ("setup", True, "SHIFT_STARTED", "ACTIVE"),
        ("ticket", True, "TICKET_CREATED", "OPEN"),
        ("alice-1", True, "CONFIRMATION_RECORDED", "OPEN"),
        ("alice-2", False, "DUPLICATE_CONFIRMATION", "OPEN"),
        ("bob-1", True, "GRANT_ISSUED", "GRANTED"),
        ("alice-3", False, "DUPLICATE_CONFIRMATION", "GRANTED"),
    ],
    "rotation": [
        ("setup", True, "SHIFT_STARTED", "ACTIVE"),
        ("ticket", True, "TICKET_CREATED", "OPEN"),
        ("alice", True, "CONFIRMATION_RECORDED", "OPEN"),
        ("bob", True, "GRANT_ISSUED", "GRANTED"),
        ("rotate", True, "SHIFT_ROTATED", "ACTIVE"),
        ("old-consume", False, "GRANT_REVOKED", "REVOKED"),
        ("new-alice", True, "CONFIRMATION_RECORDED", "OPEN"),
        ("new-bob", True, "GRANT_ISSUED", "GRANTED"),
        ("new-consume", True, "GRANT_CONSUMED", "CONSUMED"),
    ],
    "expiry": [
        ("setup", True, "SHIFT_STARTED", "ACTIVE"),
        ("ticket", True, "TICKET_CREATED", "OPEN"),
        ("alice", True, "CONFIRMATION_RECORDED", "OPEN"),
        ("bob", True, "GRANT_ISSUED", "GRANTED"),
        ("advance", True, "CLOCK_ADVANCED", "OK"),
        ("late-consume", False, "GRANT_EXPIRED", "EXPIRED"),
        ("reopen-confirm", True, "CONFIRMATION_RECORDED", "OPEN"),
    ],
    "consume_once_and_retry": [
        ("setup", True, "SHIFT_STARTED", "ACTIVE"),
        ("ticket", True, "TICKET_CREATED", "OPEN"),
        ("alice", True, "CONFIRMATION_RECORDED", "OPEN"),
        ("bob", True, "GRANT_ISSUED", "GRANTED"),
        ("consume", True, "GRANT_CONSUMED", "CONSUMED"),
        ("retry-same", True, "GRANT_CONSUMED", "CONSUMED"),
        ("consume-again", False, "ALREADY_CONSUMED", "CONSUMED"),
    ],
    "conflict_and_mismatch": [
        ("setup", True, "SHIFT_STARTED", "ACTIVE"),
        ("ticket", True, "TICKET_CREATED", "OPEN"),
        ("alice", True, "CONFIRMATION_RECORDED", "OPEN"),
        ("bob", True, "GRANT_ISSUED", "GRANTED"),
        ("wrong-request", False, "REQUEST_MISMATCH", "ACTIVE"),
        ("stale-version", False, "STALE_ASSIGNMENT", "ACTIVE"),
        ("consume", True, "GRANT_CONSUMED", "CONSUMED"),
        ("same-key-new-actor", False, "IDEMPOTENCY_CONFLICT", "CONFLICT"),
    ],
}


class EvidenceTest(unittest.TestCase):
    def test_service_matches_expected(self):
        for name, table in EXPECTED.items():
            comparison = run_scenario(SCENARIOS[name])
            service_steps = comparison.steps[0]
            self.assertEqual(len(table), len(service_steps), name)
            for (step, ok, code, status), observed in zip(table, service_steps):
                with self.subTest(scenario=name, step=step):
                    self.assertEqual(observed.step, step)
                    self.assertEqual(
                        (observed.ok, observed.code, observed.status),
                        (ok, code, status),
                    )

    def test_reference_matches_expected(self):
        for name, table in EXPECTED.items():
            comparison = run_scenario(SCENARIOS[name])
            ref_steps = comparison.steps[1]
            for (step, ok, code, status), observed in zip(table, ref_steps):
                with self.subTest(scenario=name, step=step):
                    self.assertEqual(
                        (observed.ok, observed.code, observed.status),
                        (ok, code, status),
                    )

    def test_retry_replays_original_audit_no(self):
        comparison = run_scenario(SCENARIOS["consume_once_and_retry"])
        steps = comparison.steps[0]
        consume = next(s for s in steps if s.step == "consume")
        retry = next(s for s in steps if s.step == "retry-same")
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.audit_no, consume.audit_no)

    def test_already_consumed_points_at_original_decision(self):
        from approval.scenarios import ServiceBackend
        import tempfile

        backend = ServiceBackend(tempfile.mkdtemp())
        from approval.scenarios import _setup, REQ

        _setup(backend)
        backend.create_ticket("acme", "pay", "T1", REQ, idem_key="t")
        backend.confirm("acme", "T1", "alice", idem_key="a")
        backend.confirm("acme", "T1", "bob", idem_key="b")
        first = backend.consume("acme", "pay", REQ, "runbook", ticket_id="T1")
        second = backend.consume("acme", "pay", REQ, "runbook", ticket_id="T1")
        self.assertFalse(second.ok)
        self.assertEqual(second.code, "ALREADY_CONSUMED")
        # The deciding audit number is the original consumption, not the retry.
        self.assertEqual(second.audit_no, first.audit_no)
        self.assertNotEqual(second.data["attempt_audit_no"], first.audit_no)


if __name__ == "__main__":
    unittest.main()
