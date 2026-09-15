"""Loop-breaker tests: persistent completion-claim/judge-CONTINUE disagreement parks the goal.

Reproduces the ~30-turn loop of 2026-09-15 (terse "complete — stopping" replies vs judge
ruling CONTINUE on unsatisfiable criterion literals) at unit level: judge stubbed to
CONTINUE, no gates, budget ample. Also covers the disposition-aware judge prompt template.
Run: python3 tests/test_goal_loop_breaker.py -v  (repo root)
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli import goals as G  # noqa: E402

CLAIM = "All criteria met. The goal and all criteria are complete. Complete — stopping."
WORKING = "Ran the sweep; 3 files still failing the gate, fixing next."


def _stub_judge(verdict="continue", reason="criterion 1 lacks evidence"):
    def _judge(goal, last_response, **kwargs):
        return verdict, reason, False, None, False
    return _judge


class _Mgr:
    """GoalManager with storage stubbed out (in-memory state, no DB)."""

    def __init__(self, max_turns=50):
        self._state = G.GoalState(goal="test goal", max_turns=max_turns)
        self._state.status = "active"
        self._save_calls = 0

    def _save(self):
        pass

    def is_waiting(self):
        return False

    def _check_gates(self):
        return None

    def next_continuation_prompt(self):
        return "[Continuing toward your standing goal]"

    def _pause_decision(self, paused_reason, verdict, reason, message):
        return {
            "status": "paused",
            "paused_reason": paused_reason,
            "verdict": verdict,
            "reason": reason,
            "message": message,
            "prompt": None,
        }

    def _waiting_decision(self):
        return None

    def _budget_pause(self, state, verdict, reason):
        raise AssertionError("budget pause should not trigger in these tests")


class LoopBreakerTests(unittest.TestCase):
    def _eval(self, mgr, response):
        return mgr._state and G.GoalManager.evaluate_after_turn(mgr, response)

    def test_three_rejected_claims_pause_the_goal(self):
        mgr = _Mgr()
        with patch.object(G, "judge_goal", _stub_judge()), \
             patch.object(G.GoalManager, "_check_gates", return_value=None), \
             patch.object(G.GoalManager, "_save", lambda self: None):
            d1 = self._eval(mgr, CLAIM)
            d2 = self._eval(mgr, CLAIM)
            self.assertEqual(d1["status"], "active")  # turn 1: still continuing
            self.assertEqual(d2["status"], "active")  # turn 2: still continuing
            d3 = self._eval(mgr, CLAIM)
            self.assertEqual(d3["status"], "paused")  # turn 3: parked for the user
            self.assertIn("completion claims rejected", d3.get("message", ""))

    def test_non_claim_reply_resets_counter(self):
        mgr = _Mgr()
        with patch.object(G, "judge_goal", _stub_judge()), \
             patch.object(G.GoalManager, "_check_gates", return_value=None), \
             patch.object(G.GoalManager, "_save", lambda self: None):
            self._eval(mgr, CLAIM)
            self._eval(mgr, CLAIM)
            self._eval(mgr, WORKING)  # genuine work: reset
            self.assertEqual(mgr._state.completion_claims_rejected, 0)
            d = self._eval(mgr, CLAIM)
            self.assertEqual(d["status"], "active")  # only 1 claim since reset

    def test_judge_done_never_trips_breaker(self):
        mgr = _Mgr()
        with patch.object(G, "judge_goal", _stub_judge(verdict="done", reason="evidenced")), \
             patch.object(G.GoalManager, "_check_gates", return_value=None), \
             patch.object(G.GoalManager, "_save", lambda self: None):
            d = self._eval(mgr, CLAIM)
            self.assertEqual(d["status"], "done")
            self.assertEqual(mgr._state.completion_claims_rejected, 0)

    def test_pause_reason_names_disposition_fix(self):
        mgr = _Mgr()
        with patch.object(G, "judge_goal", _stub_judge()), \
             patch.object(G.GoalManager, "_check_gates", return_value=None), \
             patch.object(G.GoalManager, "_save", lambda self: None):
            d = {}
            for _ in range(3):
                d = self._eval(mgr, CLAIM) or {}
            self.assertIn("disposition_criterion", d.get("message", ""))

    def test_claims_regex_cases(self):
        self.assertTrue(G._claims_completion(CLAIM))
        self.assertFalse(G._claims_completion(WORKING))
        self.assertFalse(G._claims_completion(""))
        self.assertFalse(G._claims_completion("Phase 3 complete; starting Phase 4"))

    def test_claims_regex_matches_qualified_criterion_phrasings(self):
        """Regression: the 2026-09-15 ~150-turn loop.

        The agent's stop line inserted an adjective between the quantifier and the noun
        ("every additional criterion"), which the adjacency-only pattern missed, so the
        loop-breaker counter never incremented and the goal ran to budget.
        """
        for claim in (
            "The goal and every additional criterion are complete. "
            "Stating so explicitly and stopping.",
            "The goal and all 15 criteria are complete.",
            "The goal and every criterion are complete.",
            "The goal and all criteria are met.",
            "The goal and each remaining acceptance criterion is satisfied.",
        ):
            with self.subTest(claim=claim):
                self.assertTrue(G._claims_completion(claim))

    def test_claims_regex_rejects_in_progress_phrasings(self):
        for working in (
            "All 15 criteria carry terminal evidence at HEAD; still verifying gate 4.",
            "Working on criterion 7 now; the goal and remaining criteria are not yet complete.",
            "I will state the criteria explicitly in the report.",
            "Next step: run the gates.",
        ):
            with self.subTest(working=working):
                self.assertFalse(G._claims_completion(working))

    def test_judge_prompt_includes_disposition_semantics(self):
        self.assertIn("DISPOSITIONS:", G.JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE)
        rendered = G.JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE.format(
            goal="g", subgoals_block="- Extra criterion 1: [DISPOSITION: impossible — evidence: raw output]",
            response="r", background_block="", current_time="t",
        )
        self.assertIn("[DISPOSITION:", rendered)


if __name__ == "__main__":
    unittest.main()
