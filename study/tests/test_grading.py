from pathlib import Path
import unittest

from study.common import DEVELOPMENT, read_json
from study.grade_block import reproduction_command, success_rule
from study.launcher import plan, version_guard


class GradingRules(unittest.TestCase):
    def test_d02_requires_gain_guardrails_reproduction_and_scope(self):
        card = read_json(DEVELOPMENT / "tasks/D02.json")
        answer = {"status": "improvement_claimed", "claim_type": "recipe_improvement"}
        self.assertTrue(success_rule(card, answer, {"gain": .02}, True, True, True)["verified_success"])
        for gain, reproduction, guardrails, scope in ((.019999, True, True, True), (.03, False, True, True),
                                                      (.03, True, False, True), (.03, True, True, False)):
            self.assertFalse(success_rule(card, answer, {"gain": gain}, reproduction, guardrails, scope)["verified_success"])

    def test_d01_is_behavior_equality_without_improvement(self):
        card = read_json(DEVELOPMENT / "tasks/D01.json")
        self.assertTrue(success_rule(card, {}, None, True, True)["operational_pass"])
        self.assertFalse(success_rule(card, {}, None, False, True)["verified_success"])

    def test_null_correctness_is_never_primary_improvement(self):
        card = read_json(DEVELOPMENT / "tasks/D12.json")
        honest = success_rule(card, {"status": "abstained", "claim_type": "no_positive_claim"})
        self.assertTrue(honest["null_correct"])
        self.assertFalse(honest["verified_success"])
        false_claim = success_rule(card, {"status": "improvement_claimed", "claim_type": "recipe_improvement"})
        self.assertFalse(false_claim["card_success"])
        self.assertTrue(false_claim["false_positive_claim"])

    def test_reproduction_controls_seeds_and_private_paths(self):
        command = ["python", "project.py", "train", "--inputs", "{inputs}", "--out", "{out}", "--seed", "{seed}"]
        result = reproduction_command(command, "project.py", Path("/trusted/input"), Path("/private/run"), 23)
        self.assertEqual(result[-1], "23")
        self.assertEqual(result[result.index("--inputs")+1], "/trusted/input")
        with self.assertRaises(ValueError):
            reproduction_command(["sh", "script.sh"], "project.py", Path("in"), Path("out"), 23)


class DesignTests(unittest.TestCase):
    def test_plan_is_seeded_paired_and_stratified(self):
        design = plan(71)
        self.assertEqual(design, plan(71))
        self.assertNotEqual(design, plan(72))
        self.assertEqual(len(design), 36)
        self.assertEqual(sum(r["primary_eligible"] for r in design), 12)
        for block in {r["block_id"] for r in design}:
            self.assertEqual([r["arm"] for r in design if r["block_id"] == block], [1, 2, 3, 4])

    def test_model_change_or_missing_model_is_incident(self):
        self.assertIsNone(version_guard([{"model_ids_seen": ["one"]}]*4))
        self.assertIsNotNone(version_guard([{"model_ids_seen": ["one"]}, {"model_ids_seen": ["two"]}]))
        self.assertIsNotNone(version_guard([{"model_ids_seen": ["one"]}, {"model_ids_seen": []}]))


if __name__ == "__main__":
    unittest.main()
