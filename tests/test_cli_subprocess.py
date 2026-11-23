"""End-to-end CLI test through a real subprocess invocation.

This proves the delivered interface works as a shell command, not just as an
imported function.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from procutil import REPO_ROOT


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "approval.cli", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )


class SubprocessCliTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_full_workflow_through_shell(self):
        seeded = cli("--data-dir", self.root, "seed")
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        self.assertTrue(json.loads(seeded.stdout)["all_ok"])

        common = [
            "--data-dir", self.root, "--now", "2026-01-01T00:01:00Z",
        ]
        first = cli(*common, "confirm", "--tenant", "acme", "--ticket", "ACME-1001",
                    "--operator", "alice", "--idem-key", "a", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["code"], "CONFIRMATION_RECORDED")

        second = cli(*common, "confirm", "--tenant", "acme", "--ticket", "ACME-1001",
                     "--operator", "bob", "--idem-key", "b", "--json")
        grant = json.loads(second.stdout)
        self.assertEqual(grant["code"], "GRANT_ISSUED")

        request = json.dumps({"action": "restart", "host": "pay-01", "reason": "memory leak"})
        consumed = cli(*common, "consume", "--tenant", "acme", "--service", "acme-payments",
                       "--request", request, "--actor", "runbook",
                       "--grant", grant["data"]["grant_id"], "--idem-key", "x", "--json")
        self.assertEqual(consumed.returncode, 0, consumed.stderr)
        consumed_body = json.loads(consumed.stdout)
        self.assertEqual(consumed_body["code"], "GRANT_CONSUMED")

        # A duplicate delivery of the same command replays the audit number.
        replay = cli(*common, "consume", "--tenant", "acme", "--service", "acme-payments",
                     "--request", request, "--actor", "runbook",
                     "--grant", grant["data"]["grant_id"], "--idem-key", "x", "--json")
        replay_body = json.loads(replay.stdout)
        self.assertTrue(replay_body["replayed"])
        self.assertEqual(replay_body["audit_no"], consumed_body["audit_no"])

        audit = cli("--data-dir", self.root, "audit", "--tenant", "acme")
        self.assertEqual(audit.returncode, 0)
        rows = json.loads(audit.stdout)
        self.assertTrue(any(r["code"] == "GRANT_CONSUMED" for r in rows))
        self.assertTrue(any(r["decision"] == "DENIED" for r in rows) is False)

        # A denied action exits non-zero so shell scripts can branch on it.
        denied = cli(*common, "consume", "--tenant", "acme", "--service", "acme-payments",
                     "--request", json.dumps({"action": "wrong"}), "--actor", "runbook",
                     "--grant", grant["data"]["grant_id"], "--json")
        self.assertEqual(denied.returncode, 3)
        self.assertFalse(json.loads(denied.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
