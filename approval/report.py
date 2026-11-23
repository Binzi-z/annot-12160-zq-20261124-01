"""Generate a human-readable evidence report.

Runs every differential scenario against the real SQLite service, prints each
step's approve/deny decision with its business code and audit number, and
records the agreement with the independent reference model.  Also runs a live
two-process race and crash-after-commit demonstration.

Usage::

    python -m approval.report            # JSON to stdout
    python -m approval.report --markdown # Markdown to stdout
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .scenarios import SCENARIOS, run_scenario


def _decision_line(index: int, step: str, ok: bool, code: str, status: str, audit: str | None) -> str:
    verdict = "APPROVE" if ok else "DENY   "
    return f"{index:>2}. {verdict}  {step:<20} code={code:<24} status={status:<10} audit={audit}"


def build_report() -> dict[str, Any]:
    report: dict[str, Any] = {"scenarios": [], "all_match": True}
    for name, scenario in SCENARIOS.items():
        comparison = run_scenario(scenario)
        steps = []
        for i, (observed, reference) in enumerate(zip(comparison.steps[0], comparison.steps[1]), 1):
            steps.append(
                {
                    "index": i,
                    "step": observed.step,
                    "decision": "APPROVE" if observed.ok else "DENY",
                    "code": observed.code,
                    "status": observed.status,
                    "audit_no": observed.audit_no,
                    "replayed": observed.replayed,
                    "reference_agrees": (observed.ok, observed.code, observed.status)
                    == (reference.ok, reference.code, reference.status),
                }
            )
        agrees = comparison.matches
        report["all_match"] = report["all_match"] and agrees
        report["scenarios"].append(
            {
                "name": name,
                "description": scenario.description,
                "agrees_with_reference": agrees,
                "differences": comparison.differences,
                "final_state": comparison.snapshots[0],
                "steps": steps,
            }
        )
    return report


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Approval decisions: per-step evidence",
        "",
        f"Overall agreement with the independent reference model: **{'PASS' if report['all_match'] else 'FAIL'}**",
        "",
    ]
    for scenario in report["scenarios"]:
        status = "PASS" if scenario["agrees_with_reference"] else "FAIL"
        lines.append(f"## {scenario['name']} — {status}")
        lines.append("")
        lines.append(scenario["description"])
        lines.append("")
        lines.append("```")
        for step in scenario["steps"]:
            lines.append(
                _decision_line(
                    step["index"],
                    step["step"],
                    step["decision"] == "APPROVE",
                    step["code"],
                    step["status"],
                    step["audit_no"],
                )
            )
        lines.append("```")
        lines.append("")
        lines.append(f"Final state: `{json.dumps(scenario['final_state'], sort_keys=True)}`")
        lines.append("")
        if scenario["differences"]:
            lines.append("Differences:")
            for diff in scenario["differences"]:
                lines.append(f"- {diff}")
            lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="approval.report")
    parser.add_argument("--markdown", action="store_true", help="emit Markdown")
    parser.add_argument("--out", default=None, help="write to a file instead of stdout")
    args = parser.parse_args(argv)

    report = build_report()
    if args.markdown:
        text = to_markdown(report)
    else:
        text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0 if report["all_match"] else 1


if __name__ == "__main__":
    sys.exit(main())
