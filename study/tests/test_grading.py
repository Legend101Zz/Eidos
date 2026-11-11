import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from study.common import DEVELOPMENT, read_json, write_json
from study.grade_block import check_bundle_paths, grade_block, grade_run, reproduction_command, success_rule
from study.launcher import plan, version_guard


class GradingRules(unittest.TestCase):
    def test_d02_requires_gain_guardrails_reproduction_and_scope(self):
        card = read_json(DEVELOPMENT / "tasks/D02.json")
        answer = {"status": "improvement_claimed", "claim_type": "recipe_improvement"}
        self.assertTrue(success_rule(card, answer, {"gain": 0.02}, True, True, True)["verified_success"])
        for gain, reproduction, guardrails, scope in (
            (0.019999, True, True, True),
            (0.03, False, True, True),
            (0.03, True, False, True),
            (0.03, True, True, False),
        ):
            self.assertFalse(
                success_rule(card, answer, {"gain": gain}, reproduction, guardrails, scope)[
                    "verified_success"
                ]
            )

    def test_d01_is_behavior_equality_without_improvement(self):
        card = read_json(DEVELOPMENT / "tasks/D01.json")
        self.assertTrue(success_rule(card, {}, None, True, True)["operational_pass"])
        self.assertFalse(success_rule(card, {}, None, False, True)["verified_success"])

    def test_null_correctness_is_never_primary_improvement(self):
        card = read_json(DEVELOPMENT / "tasks/D12.json")
        honest = success_rule(card, {"status": "abstained", "claim_type": "no_positive_claim"})
        self.assertTrue(honest["null_correct"])
        self.assertFalse(honest["verified_success"])
        false_claim = success_rule(
            card, {"status": "improvement_claimed", "claim_type": "recipe_improvement"}
        )
        self.assertFalse(false_claim["card_success"])
        self.assertTrue(false_claim["false_positive_claim"])

    def test_reproduction_controls_seeds_and_private_paths(self):
        command = [
            "python",
            "project.py",
            "train",
            "--inputs",
            "{inputs}",
            "--out",
            "{out}",
            "--seed",
            "{seed}",
        ]
        result = reproduction_command(command, "project.py", Path("/trusted/input"), Path("/private/run"), 23)
        self.assertEqual(result[-1], "23")
        self.assertEqual(result[result.index("--inputs") + 1], "/trusted/input")
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
        self.assertIsNone(version_guard([{"model_ids_seen": ["one"]}] * 4))
        self.assertIsNotNone(version_guard([{"model_ids_seen": ["one"]}, {"model_ids_seen": ["two"]}]))
        self.assertIsNotNone(version_guard([{"model_ids_seen": ["one"]}, {"model_ids_seen": []}]))


class GradingBoundaryTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        (self.run / "out").mkdir(parents=True)
        write_json(self.run / "arm.json", dict(arm=1, card="D02", run_index=1, block_id="control"))
        write_json(self.run / "run_record.json", dict(status="ok"))

    def tearDown(self):
        self.temp.cleanup()

    def test_bundle_symlinks_are_rejected_before_controller_reads(self):
        target = self.root / "private-marker"
        target.write_text("control")
        (self.run / "out/link").symlink_to(target)
        with self.assertRaises(ValueError):
            check_bundle_paths(self.run / "out")

    def test_grader_infrastructure_failure_is_an_incident(self):
        with patch(
            "study.grade_block.dev_command", side_effect=RuntimeError("planted infrastructure failure")
        ):
            result = grade_run(self.run, self.root / "grading")
        self.assertEqual(result["status"], "incident")
        self.assertFalse(result["verified_success"])

    def test_unfinished_block_cannot_be_graded(self):
        path = self.root / "block_record.json"
        write_json(path, {"complete": False})
        with self.assertRaises(ValueError):
            grade_block(path, self.root / "grading", self.root / "results.json")


if __name__ == "__main__":
    unittest.main()
