"""CLI coverage for status variants and recovery."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from procutil import REPO_ROOT


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "approval.cli", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
    )


class CliStatusAndRecover(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.assertEqual(cli("--data-dir", self.root, "seed").returncode, 0)

    def test_status_variants(self):
        summary = cli("--data-dir", self.root, "status", "--tenant", "acme")
        self.assertEqual(summary.returncode, 0, summary.stderr)
        body = json.loads(summary.stdout)
        self.assertTrue(body["exists"])
        self.assertEqual(
            sorted(s["service_id"] for s in body["services"]),
            ["acme-payments", "acme-search"],
        )

        shift = cli("--data-dir", self.root, "status", "--tenant", "acme",
                    "--service", "acme-payments")
        self.assertEqual(shift.returncode, 0)
        self.assertEqual(json.loads(shift.stdout)["shift_version"], 1)

        ticket = cli("--data-dir", self.root, "status", "--tenant", "acme",
                     "--ticket", "ACME-1001")
        self.assertEqual(ticket.returncode, 0)
        self.assertEqual(json.loads(ticket.stdout)["ticket"]["status"], "OPEN")

        missing = cli("--data-dir", self.root, "status", "--tenant", "acme",
                      "--ticket", "NOPE")
        self.assertEqual(missing.returncode, 3)

    def test_recover_cli(self):
        recovered = cli("--data-dir", self.root, "--now", "2026-01-01T00:00:00Z",
                        "recover", "--tenant", "acme", "--json")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        body = json.loads(recovered.stdout)
        self.assertEqual(body["code"], "RECOVERY_COMPLETED")
        self.assertEqual(body["data"]["scanned"], 0)

    def test_tenants_lists_both(self):
        out = cli("--data-dir", self.root, "tenants")
        self.assertEqual(json.loads(out.stdout), ["acme", "globex"])


if __name__ == "__main__":
    unittest.main()
