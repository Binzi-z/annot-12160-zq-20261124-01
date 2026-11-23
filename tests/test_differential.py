import unittest

from approval.scenarios import SCENARIOS, run_scenario


class DifferentialTest(unittest.TestCase):
    def test_all_scenarios_match_reference(self):
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                comparison = run_scenario(SCENARIOS[name])
                self.assertTrue(
                    comparison.matches,
                    msg="\n".join(comparison.differences),
                )


if __name__ == "__main__":
    unittest.main()
