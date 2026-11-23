"""Edge cases around provisioning and tenant database lifecycle."""

import tempfile
import unittest

from approval import ApprovalService, ManualClock
from approval.store import TenantStore


class ProvisioningEdgeCases(unittest.TestCase):
    def test_initialized_but_unregistered_database_is_treated_as_missing(self):
        # Simulates a provisioning transaction that wrote the schema file but
        # died before COMMIT, leaving a database file with no tenant row.
        root = tempfile.mkdtemp()
        store = TenantStore(root, "ghost")
        store.initialize()
        self.assertTrue(store.exists())

        svc = ApprovalService(root, ManualClock("2026-01-01T00:00:00Z"))
        result = svc.add_service("ghost", "svc", "Svc")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "TENANT_NOT_FOUND")

        # Provisioning then succeeds on the same file and works normally.
        provision = svc.provision_tenant("ghost", "Ghost")
        self.assertTrue(provision.ok)
        self.assertEqual(provision.code, "TENANT_PROVISIONED")

    def test_provision_is_idempotent_with_key(self):
        root = tempfile.mkdtemp()
        svc = ApprovalService(root, ManualClock("2026-01-01T00:00:00Z"))
        first = svc.provision_tenant("acme", "Acme", idem_key="p")
        retry = svc.provision_tenant("acme", "Acme", idem_key="p")
        self.assertTrue(first.ok)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.audit_no, first.audit_no)

    def test_provision_conflicting_name_conflicts(self):
        root = tempfile.mkdtemp()
        svc = ApprovalService(root, ManualClock("2026-01-01T00:00:00Z"))
        svc.provision_tenant("acme", "Acme", idem_key="p")
        conflict = svc.provision_tenant("acme", "Other Name", idem_key="p")
        self.assertEqual(conflict.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(conflict.status, "CONFLICT")


if __name__ == "__main__":
    unittest.main()
