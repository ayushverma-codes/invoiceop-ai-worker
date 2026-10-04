"""The eval harness must compute metrics from results, and its scenario table must stay consistent."""
import unittest

from agent.state import STATUSES
from evals.run_eval import SCENARIOS, summarize


def _row(**kw):
    base = {"ok": True, "tags": [], "recovered": False, "verification_ran": True, "verification_passed": True,
            "actual": "completed", "human_escalations": 0, "retry_count": 0, "browser_actions": 10, "llm_calls": 5}
    return {**base, **kw}


class EvalHarnessTests(unittest.TestCase):
    def test_scenarios_use_valid_statuses_and_unique_names(self):
        names = [s[0] for s in SCENARIOS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(s[4] in STATUSES for s in SCENARIOS))

    def test_summary_flags_false_completion(self):
        s = summarize([_row(), _row(verification_passed=False)])
        self.assertEqual(s["false_completions (completed but DB wrong, must be 0)"], 1)

    def test_summary_rates(self):
        s = summarize([_row(tags=["recovery"], recovered=True), _row(ok=False, actual="failed", verification_ran=False)])
        self.assertEqual(s["task_success_rate (actual status == expected status)"], "1/2")
        self.assertEqual(s["recovery_success_rate (injected AP error -> completed)"], "1/1")


if __name__ == "__main__":
    unittest.main()
