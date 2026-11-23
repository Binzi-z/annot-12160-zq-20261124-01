"""Regenerate the committed evidence document and assert it is correct.

Running the suite rewrites ``docs/EVIDENCE.md`` from live execution, so the
checked-in evidence cannot silently drift from the code.
"""

import unittest
from pathlib import Path

from approval.report import build_report, to_markdown

DOC = Path(__file__).resolve().parent.parent / "docs" / "EVIDENCE.md"


class EvidenceArtifactTest(unittest.TestCase):
    def test_regenerate_evidence_document(self):
        report = build_report()
        self.assertTrue(report["all_match"])
        DOC.parent.mkdir(parents=True, exist_ok=True)
        DOC.write_text(to_markdown(report), encoding="utf-8")
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("Overall agreement with the independent reference model: **PASS**", text)
        for scenario in report["scenarios"]:
            self.assertIn(f"## {scenario['name']} — PASS", text)


if __name__ == "__main__":
    unittest.main()
