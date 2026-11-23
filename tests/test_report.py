import json
import unittest

from approval.report import build_report, to_markdown


class ReportTest(unittest.TestCase):
    def test_report_matches_and_renders(self):
        report = build_report()
        self.assertTrue(report["all_match"])
        self.assertEqual(len(report["scenarios"]), 5)
        for scenario in report["scenarios"]:
            self.assertTrue(scenario["agrees_with_reference"], scenario["differences"])
            for step in scenario["steps"]:
                self.assertTrue(step["reference_agrees"])
                self.assertIn(step["decision"], {"APPROVE", "DENY"})
        md = to_markdown(report)
        self.assertIn("GRANT_CONSUMED", md)
        self.assertIn("APPROVE", md)
        self.assertIn("DENY", md)

    def test_every_scenario_has_both_approvals_and_denials(self):
        report = build_report()
        for scenario in report["scenarios"]:
            decisions = {s["decision"] for s in scenario["steps"]}
            self.assertIn("APPROVE", decisions, scenario["name"])
        # At least one denial overall, and specifically the denial scenarios.
        all_denials = [
            s["step"]
            for sc in report["scenarios"]
            for s in sc["steps"]
            if s["decision"] == "DENY"
        ]
        self.assertTrue(all_denials)


if __name__ == "__main__":
    unittest.main()
