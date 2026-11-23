"""Verify the exact README quick-start sequence produces the documented results."""

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


class ReadmeQuickStartTest(unittest.TestCase):
    def test_quickstart(self):
        root = tempfile.mkdtemp()
        self.assertEqual(cli("--data-dir", root, "seed").returncode, 0)

        first = cli("--data-dir", root, "--now", "2026-01-01T00:01:00Z",
                    "confirm", "--tenant", "acme", "--ticket", "ACME-1001",
                    "--operator", "alice", "--idem-key", "c1")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("CONFIRMATION_RECORDED", first.stdout)

        second = cli("--data-dir", root, "--now", "2026-01-01T00:01:00Z",
                     "confirm", "--tenant", "acme", "--ticket", "ACME-1001",
                     "--operator", "bob", "--idem-key", "c2", "--json")
        self.assertEqual(second.returncode, 0, second.stderr)
        grant = json.loads(second.stdout)
        self.assertEqual(grant["code"], "GRANT_ISSUED")
        grant_id = grant["data"]["grant_id"]
        # The README shows G-acme-000001 for a fresh data dir.
        self.assertEqual(grant_id, "G-acme-000001")

        request = '{"action":"restart","host":"pay-01","reason":"memory leak"}'
        consumed = cli("--data-dir", root, "--now", "2026-01-01T00:02:00Z",
                       "consume", "--tenant", "acme", "--service", "acme-payments",
                       "--request", request, "--actor", "runbook",
                       "--grant", grant_id, "--idem-key", "x1", "--json")
        self.assertEqual(consumed.returncode, 0, consumed.stderr)
        body = json.loads(consumed.stdout)
        self.assertEqual(body["code"], "GRANT_CONSUMED")

        replay = cli("--data-dir", root, "--now", "2026-01-01T00:02:00Z",
                     "consume", "--tenant", "acme", "--service", "acme-payments",
                     "--request", request, "--actor", "runbook",
                     "--grant", grant_id, "--idem-key", "x1", "--json")
        replay_body = json.loads(replay.stdout)
        self.assertTrue(replay_body["replayed"])
        self.assertEqual(replay_body["audit_no"], body["audit_no"])

        status = cli("--data-dir", root, "status", "--tenant", "acme", "--ticket", "ACME-1001")
        self.assertEqual(status.returncode, 0)
        self.assertIn("CONSUMED", status.stdout)

        audit = cli("--data-dir", root, "audit", "--tenant", "acme")
        self.assertEqual(audit.returncode, 0)
        rows = json.loads(audit.stdout)
        self.assertTrue(any(r["code"] == "GRANT_CONSUMED" for r in rows))


if __name__ == "__main__":
    unittest.main()
