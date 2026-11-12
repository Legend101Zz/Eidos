import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from study import launcher
from study.common import DEVELOPMENT, MODEL, PROMPT_OVERRIDES, read_json, write_json
from study.grade_block import check_bundle_paths, digest, grade_block, grade_run, reproduction_command, success_rule
from study.launcher import plan


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


class BlockModelIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = self.root / "runs"
        self.rows = plan(71, cards=["D01"], runs=1)
        (self.root / "run_arm.py").write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "run_dir = Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
            "record = json.loads((run_dir / 'fixture-record.json').read_bytes())\n"
            "if record is not None:\n"
            "    (run_dir / 'run_record.json').write_text(json.dumps(record))\n"
        )

    def run_block(self, records):
        def prepare(row, target):
            target.mkdir()
            record = records[row["arm"] - 1]
            write_json(target / "fixture-record.json", record)

        env = {name: value for name, value in os.environ.items() if name not in PROMPT_OVERRIDES}
        with patch.dict(os.environ, env, clear=True), patch.object(launcher, "STUDY", self.root), \
                patch.object(launcher, "prepare", side_effect=prepare), \
                patch.object(launcher, "research_python", return_value=sys.executable):
            return launcher.run_block(self.rows, self.runs)

    def run_records(self, block):
        return [read_json(Path(path) / "run_record.json") for path in block["run_dirs"].values()]

    @staticmethod
    def record(status="ok", models=(MODEL,), **fields):
        return dict(status=status, model_ids_seen=list(models), **fields)

    def test_one_missing_identity_ordinary_failure_is_retained_without_reroll(self):
        records = [self.record("crash", ()), *[self.record() for _ in range(3)]]
        block = self.run_block(records)
        self.assertEqual(block["status"], "ok")
        self.assertEqual(self.run_records(block), records)
        with patch.object(launcher.subprocess, "Popen", side_effect=AssertionError("unexpected reroll")):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(launcher.run_block(self.rows, self.runs, replace_incident=True), block)
        self.assertFalse((self.runs / "incidents").exists())

    def test_all_ordinary_failures_can_have_unknown_identity(self):
        records = [self.record("crash", ()), self.record("timeout", ("<synthetic>",)),
                   self.record("token_budget", ()), self.record("invalid_output", ())]
        block = self.run_block(records)
        self.assertEqual(block["status"], "ok")
        self.assertEqual(self.run_records(block), records)

    def test_missing_identity_success_remains_incident(self):
        block = self.run_block([self.record(models=("<synthetic>",)), *[self.record() for _ in range(3)]])
        self.assertEqual(block["status"], "incident")
        self.assertIn("did not record a returned model id", block["incident_reason"])

    def test_unanimous_wrong_identity_remains_incident(self):
        block = self.run_block([self.record(models=("wrong-model",)) for _ in range(4)])
        self.assertEqual(block["status"], "incident")
        self.assertIn("wrong-model", block["incident_reason"])

    def test_mixed_returned_models_on_an_ordinary_failure_remain_incident(self):
        records = [self.record(models=()), self.record("crash", (MODEL, "wrong-model")),
                   self.record(), self.record()]
        block = self.run_block(records)
        self.assertEqual(block["status"], "incident")
        self.assertIn("wrong-model", block["incident_reason"])

    def test_missing_record_remains_infrastructure_incident(self):
        block = self.run_block([None, *[self.record() for _ in range(3)]])
        self.assertEqual(block["status"], "incident")
        self.assertEqual(block["incident_reason"], "arm infrastructure exited without a record")
        self.assertEqual(self.run_records(block)[0]["pre_block_status"], "incident")

    def test_existing_provider_incident_keeps_its_cause_without_returned_id(self):
        records = [self.record("incident", (), incident_reason="provider rate limit"),
                   *[self.record() for _ in range(3)]]
        block = self.run_block(records)
        self.assertEqual(block["status"], "incident")
        self.assertEqual(block["incident_reason"], "provider rate limit")


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

    def test_reproduction_and_confirmation_use_the_declared_checkpoint(self):
        out = self.run / "out"
        write_json(self.run / "arm.json", dict(arm=4, card="D03", run_index=1, block_id="control"))
        checkpoint = "candidate-weights.bin"
        candidate = {
            "code_path": "project.py",
            "model_path": checkpoint,
            "predictions_path": "validation.npz",
            "environment_lock": "environment.txt",
            "reproduce_command": [
                "python", "project.py", "train", "--inputs", "{inputs}", "--out", "{out}", "--seed", "{seed}",
            ],
        }
        for key in ("code_path", "model_path", "predictions_path", "environment_lock"):
            path = out / candidate[key]
            path.write_text(key)
            candidate[key.removesuffix("_path") + "_sha256"] = digest(path)
        write_json(out / "answer.json", {
            "task_id": "D03", "status": "improvement_claimed", "claim_type": "recipe_improvement",
            "evidence": [], "candidate": candidate,
        })

        def dev_command(script, *args):
            if script == "package_task.py":
                args[-1].mkdir()

        def fresh_check(workspace, code, model, validation_inputs, locked_validation, test_inputs):
            self.assertEqual(model, workspace / "candidate" / checkpoint)
            return {"fresh_process_export_equal": True, "prediction_path": str(workspace / "fresh.npz")}

        def train_candidate(workspace, command):
            seed_output = Path(command[command.index("--out") + 1])
            (seed_output / "model.pt").write_text("decoy")
            if seed_output.parent.parent.parent.name == "complete":
                (seed_output / checkpoint).write_text("declared checkpoint")

        def infer(workspace, code, model, test_inputs, prediction):
            self.assertEqual(model.name, checkpoint)
            self.assertEqual(model.read_text(), "declared checkpoint")

        with (
            patch("study.grade_block.dev_command", side_effect=dev_command),
            patch("study.grade_block.research_python", return_value="python"),
            patch("study.grade_block.make_inputs", side_effect=lambda _, path: path.write_text("inputs")),
            patch("study.grade_block.fresh_check", side_effect=fresh_check),
            patch("study.grade_block.candidate_command", side_effect=train_candidate),
            patch("study.grade_block.infer", side_effect=infer),
            patch("study.grade_block.score", return_value={"gain": 0.03}),
            patch("subprocess.run"),
        ):
            complete = grade_run(self.run, self.root / "complete" / "grading")
            self.assertTrue(complete["verified_success"], complete)
            self.assertEqual([item["seed"] for item in complete["reproduction"]], [23, 37])
            missing = grade_run(self.run, self.root / "missing" / "grading")
            self.assertFalse(missing["verified_success"], missing)
            self.assertEqual(missing["status"], "invalid_output")
            self.assertIn(checkpoint, missing["reason"])

    def test_unfinished_block_cannot_be_graded(self):
        path = self.root / "block_record.json"
        write_json(path, {"complete": False})
        with self.assertRaises(ValueError):
            grade_block(path, self.root / "grading", self.root / "results.json")


if __name__ == "__main__":
    unittest.main()
