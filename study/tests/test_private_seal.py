"""Fabricated seal and candidate controls. No private study assets are read."""

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from study.common import DEVELOPMENT, ENVELOPE, read_json, render_prompt
from study.grade_block import grade_block
from study.eidos_arms.charters import charter
from study.fresh_check import candidate_command, fresh_check, infer
from study.launcher import plan, run_block
from study.private_card_registry import PRIVATE_CARD_BARS
from study.private_seal import MODEL_FILES, PrivateSeal


def digest(data):
    return hashlib.sha256(data).hexdigest()


BASELINE_PROJECT = """
import argparse
from pathlib import Path
import numpy as np
parser = argparse.ArgumentParser()
parser.add_argument('action')
parser.add_argument('--inputs', type=Path)
parser.add_argument('--out', type=Path)
parser.add_argument('--seed')
parser.add_argument('--model', type=Path)
args = parser.parse_args()
if args.action == 'train':
    assert not (Path(__file__).parent / 'model.pt').exists() or args.seed == '11'
    (args.out / 'model.pt').write_text('fabricated model')
    (args.out / 'receipt.json').write_text('{}')
    ids = np.load(args.inputs / 'validation.npz')['ids']
    np.savez(args.out / 'validation_predictions.npz', ids=ids, predictions=np.zeros(len(ids), dtype=int))
else:
    assert args.model.read_text() == 'fabricated model'
    ids = np.load(args.inputs)['ids']
    np.savez(args.out, ids=ids, predictions=np.zeros(len(ids), dtype=int))
"""


def make_seal(root):
    def write(path, value):
        path.write_text(json.dumps(value))

    for card, (metric, bar) in PRIVATE_CARD_BARS.items():
        directory = root / card
        bundle = directory / "bundle"
        bundle.mkdir(parents=True)
        for name in MODEL_FILES - {"task.json"}:
            (bundle / name).write_text(f"synthetic {name}")
        (bundle / "project.py").write_text(BASELINE_PROJECT)
        (bundle / "answer_schema.py").write_bytes((DEVELOPMENT / "validate_answer.py").read_bytes())
        for split in ("train", "validation"):
            np.savez(bundle / f"{split}.npz", x=[[1], [2]], y=[0, 1], ids=[10, 20])
        write(bundle / "task.json", {
            "task_id": card,
            "project": card,
            "hashes": {path.name: digest(path.read_bytes()) for path in bundle.iterdir()},
        })
        np.savez(directory / "test_inputs.npz", x=[[1], [2]], ids=[10, 20])
        np.savez(directory / "test_labels.npz", y=[0, 1], ids=[10, 20])
        np.savez(directory / "baseline_test.npz", ids=[20, 10], predictions=[0, 0])
        write(directory / "manifest.json", {
            "id": card, "task_type": "classification" if metric == "accuracy" else "regression",
            "metric": metric, "min_reference_gain": bar, "qualified": True, "seeds": [11, 23, 37],
            "files": {path.relative_to(directory).as_posix(): digest(path.read_bytes())
                      for path in directory.rglob("*") if path.is_file()},
        })
    files = {p.relative_to(root).as_posix(): digest(p.read_bytes())
             for p in root.rglob("*") if p.is_file()}
    salt = bytes(range(32))
    commitment = digest(salt + json.dumps(files, sort_keys=True, separators=(",", ":")).encode())
    write(root / "commitment-opening.json", {"files": files, "salt": salt.hex(), "sha256": commitment})
    return commitment


class SealTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.private = self.root / "seal"
        self.commitment = make_seal(self.private)

    def test_staged_allowlist_and_independent_score(self):
        seal = PrivateSeal(self.private, self.commitment)
        staged = self.root / "model-facing"
        (self.private / "N05/bundle/unlisted-secret").write_text("excluded")
        seal.stage_bundle("N05", staged)
        self.assertEqual({p.name for p in staged.iterdir()}, MODEL_FILES)
        np.savez(self.root / "prediction.npz", ids=[20, 10], predictions=[1, 0])
        result = seal.score("N05", self.root / "prediction.npz")
        self.assertEqual((result["baseline"], result["candidate"], result["gain"]), (0.5, 1.0, 0.5))

    def test_wrong_commitment_file_change_and_symlink_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "commitment"):
            PrivateSeal(self.private, "0" * 64)
        seal = PrivateSeal(self.private, self.commitment)
        target = self.private / "N05/bundle/brief.md"
        original = target.read_bytes()
        target.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "digest changed"):
            seal.stage_bundle("N05", self.root / "bundle")
        target.unlink()
        outside = self.root / "outside"
        outside.write_bytes(original)
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            PrivateSeal(self.private, self.commitment)

    def test_roster_prompt_charter_and_budget_parity(self):
        rows = plan(42, PRIVATE_CARD_BARS)
        self.assertEqual(len(rows), 48)
        self.assertTrue(all(r["family"] == r["task"] and r["primary_eligible"] for r in rows))
        for card, (metric, bar) in PRIVATE_CARD_BARS.items():
            for arm in (1, 2, 3, 4):
                self.assertIn(f"{bar:.0%}" if metric == "rmse" else str(bar), render_prompt(arm, card))
            policy = charter(card)
            self.assertEqual(policy["exploration_only"], metric == "rmse")
            self.assertEqual(policy["resource_limits"], {
                "wall_seconds": ENVELOPE["wall_s"], "model_tokens": ENVELOPE["tokens"],
                "threads": ENVELOPE["threads"],
            })

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Seatbelt")
    def test_all_four_stub_arms_stage_and_grade_the_same_sealed_card(self):
        seal = PrivateSeal(self.private, self.commitment)
        lab = self.root / "stub-lab"
        lab_python = lab / "bin/python"
        lab_python.parent.mkdir(parents=True)
        lab_python.symlink_to(sys.executable)
        (lab / "pilot-build.json").write_text(json.dumps({
            "eidos_commit_sha": "synthetic", "python": str(lab_python),
        }))
        env = {
            "EIDOS_STUDY_PYTHON": str(lab_python),
            "EIDOS_RESEARCH_PYTHON": sys.executable,
            "EIDOS_PILOT_CHARTER_COMMIT": "synthetic",
            "EIDOS_PILOT_CHARTER_PATH": "docs/experiments/pilot-4/CHARTER.md",
        }
        runs = self.root / "runs"
        runs.mkdir()
        rows = plan(42, ["N05"], runs=1)
        with (
            patch.dict(os.environ, env),
            patch("study.private_seal.configured_seal", return_value=seal),
            patch("study.grade_block.configured_seal", return_value=seal),
        ):
            block = run_block(rows, runs, stub=True)
            block_dir = runs / block["block_id"]
            if block["status"] != "ok":
                logs = [(path.parent.name, path.read_text()) for path in sorted(block_dir.glob("arm*/launcher.log"))]
                self.fail(f"{block['incident_reason']}; stub logs: {logs}")
            for arm in (1, 2, 3, 4):
                arm_dir = block_dir / f"arm{arm}"
                self.assertEqual({p.name for p in (arm_dir / "task").iterdir()}, MODEL_FILES)
                self.assertEqual(read_json(arm_dir / "arm.json")["envelope"], ENVELOPE)
            grades = grade_block(block_dir / "block_record.json", self.root / "grades", self.root / "results.json")
        self.assertEqual(len(grades), 4)
        for grade in grades:
            self.assertEqual(grade["status"], "valid", grade.get("reason"))
            self.assertEqual(grade["numeric"]["gain"], 0.0)
            self.assertTrue(grade["fresh_check"]["fresh_process_export_equal"])
            self.assertEqual(len(grade["reproduction"]), 2)
            self.assertFalse(grade["verified_success"])

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Seatbelt")
    def test_isolated_inference_may_write_sidecars_beside_its_output(self):
        workspace = self.root / "workspace"
        (workspace / "candidate").mkdir(parents=True)
        inputs = workspace / "test_inputs.npz"
        inputs.write_bytes(b"fabricated predictions")
        code = workspace / "candidate/project.py"
        code.write_text("""
import argparse
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('action')
p.add_argument('--model')
p.add_argument('--inputs', type=Path)
p.add_argument('--out', type=Path)
a = p.parse_args()
(a.out.parent / 'receipt.json').write_text('{}')
a.out.with_suffix('.receipt.json').write_text('{}')
a.out.write_bytes(a.inputs.read_bytes())
""")
        output = workspace / "validation-inference/predictions.npz"
        with patch.dict(os.environ, {"EIDOS_RESEARCH_PYTHON": sys.executable}):
            infer(workspace, code, workspace / "candidate/model.pt", inputs, output, isolated=True)
        self.assertEqual(output.read_bytes(), inputs.read_bytes())
        self.assertEqual((output.parent / "receipt.json").read_text(), "{}")
        self.assertEqual(output.with_suffix(".receipt.json").read_text(), "{}")

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Seatbelt")
    def test_isolated_sidecars_do_not_open_channels_between_inferences(self):
        workspace = self.root / "workspace"
        (workspace / "candidate").mkdir(parents=True)
        (workspace / "inputs").mkdir()
        validation = workspace / "inputs/validation.npz"
        validation.write_text("validation")
        test_input = workspace / "test_inputs.npz"
        test_input.write_text("test")
        confirmation = workspace / "confirmation-inference-11"
        confirmation.mkdir()
        (confirmation / "receipt.json").write_text("earlier confirmation")
        code = workspace / "candidate/project.py"
        code.write_text("""
import argparse
import os
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('action')
p.add_argument('--model')
p.add_argument('--inputs', type=Path)
p.add_argument('--out', type=Path)
a = p.parse_args()
root = Path.cwd().parent
scratch = a.out.parent / 'scratch'
for name in ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
    assert Path(os.environ[name]) == scratch
(scratch / 'temporary').write_text('scratch')
(a.out.parent / 'nested').mkdir()
(a.out.parent / 'nested/receipt.json').write_text('{}')
hidden = [root / 'confirmation-inference-11/receipt.json']
if a.out.parent.name == 'test-inference':
    hidden.append(root / 'validation-inference/receipt.json')
for path in hidden:
    try:
        path.read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError(f'read another inference: {path}')
for path in [root / 'side-channel', Path.cwd() / 'side-channel',
             root / 'inputs/validation.npz', root / 'test_inputs.npz', *hidden]:
    try:
        path.write_text('leak')
    except PermissionError:
        pass
    else:
        raise AssertionError(f'unexpected writable destination: {path}')
(a.out.parent / 'receipt.json').write_text('{}')
a.out.write_bytes(a.inputs.read_bytes())
""")
        with patch.dict(os.environ, {"EIDOS_RESEARCH_PYTHON": sys.executable}):
            for name, inputs in (("validation", validation), ("test", test_input)):
                output = workspace / f"{name}-inference/predictions.npz"
                infer(workspace, code, workspace / "candidate/model.pt", inputs, output, isolated=True)
                self.assertEqual(output.read_text(), name)
                self.assertEqual((output.parent / "receipt.json").read_text(), "{}")
                self.assertEqual((output.parent / "nested/receipt.json").read_text(), "{}")
        self.assertEqual(validation.read_text(), "validation")
        self.assertEqual(test_input.read_text(), "test")
        self.assertEqual((confirmation / "receipt.json").read_text(), "earlier confirmation")
        self.assertFalse((workspace / "side-channel").exists())
        self.assertFalse((workspace / "candidate/side-channel").exists())

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Seatbelt")
    def test_fresh_check_isolated_accepts_sidecar_writing_candidate(self):
        workspace = self.root / "workspace"
        (workspace / "candidate").mkdir(parents=True)
        (workspace / "inputs").mkdir()
        code = workspace / "candidate/project.py"
        code.write_text(BASELINE_PROJECT.replace(
            "    ids = np.load(args.inputs)['ids']",
            "    (args.out.parent / 'receipt.json').write_text('{}')\n"
            "    args.out.with_suffix('.receipt.json').write_text('{}')\n"
            "    ids = np.load(args.inputs)['ids']",
        ))
        model = workspace / "candidate/model.pt"
        model.write_text("fabricated model")
        validation = workspace / "inputs/validation.npz"
        test_input = workspace / "test_inputs.npz"
        locked_validation = workspace / "locked_validation.npz"
        np.savez(validation, x=[[1], [2]], ids=[10, 20])
        np.savez(test_input, x=[[3], [4]], ids=[30, 40])
        np.savez(locked_validation, ids=[20, 10], predictions=[0, 0])
        with patch.dict(os.environ, {"EIDOS_RESEARCH_PYTHON": sys.executable}):
            result = fresh_check(
                workspace, code, model, validation, locked_validation, test_input, isolated=True,
            )
        self.assertTrue(result["fresh_process_export_equal"])
        self.assertTrue(result["locked_validation_equal"])
        self.assertTrue(result["test_reload_equal"])
        self.assertEqual(result["prediction_path"], str(workspace / "test-inference/predictions.npz"))
        for name in ("validation-inference", "test-inference", "fresh-test-inference"):
            output = workspace / name / "predictions.npz"
            self.assertEqual((output.parent / "receipt.json").read_text(), "{}")
            self.assertEqual(output.with_suffix(".receipt.json").read_text(), "{}")

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Seatbelt")
    def test_candidate_inference_can_read_current_input_and_cannot_retain_or_reuse_it(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "inputs").mkdir()
        (workspace / "candidate").mkdir()
        test_input = workspace / "test_inputs.npz"
        test_input.write_text("fabricated")
        previous = workspace / "test-inference"
        previous.mkdir()
        (previous / "predictions.npz").write_text("previous")
        code = workspace / "candidate/project.py"
        code.write_text("""
import argparse
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('action')
p.add_argument('--model')
p.add_argument('--inputs', type=Path)
p.add_argument('--out', type=Path)
a = p.parse_args()
with open('/dev/null', 'r+b') as device:
    device.write(b'probe')
root = Path.cwd().parent
previous = root / 'test-inference/predictions.npz'
for path in (previous, root / 'test_inputs.npz'):
    if path == a.inputs:
        continue
    try:
        path.read_bytes()
    except PermissionError:
        continue
    raise AssertionError('read hidden input')
try:
    (root / 'side-channel').write_text('leak')
except PermissionError:
    pass
else:
    raise AssertionError('unexpected writable destination')
a.out.write_bytes(a.inputs.read_bytes())
""")
        output = workspace / "fresh-test-inference/predictions.npz"
        with patch.dict(os.environ, {"EIDOS_RESEARCH_PYTHON": sys.executable}):
            infer(workspace, code, workspace / "candidate/model.pt", test_input, output, isolated=True)
            self.assertEqual(output.read_text(), "fabricated")
            validation = workspace / "inputs/validation.npz"
            validation.write_text("validation")
            validation_output = workspace / "validation-inference/predictions.npz"
            infer(workspace, code, workspace / "candidate/model.pt", validation, validation_output, isolated=True)
            self.assertEqual(validation_output.read_text(), "validation")
            training = self.root / "training"
            training.mkdir()
            (training / "candidate").mkdir()
            candidate_command(training, [sys.executable, "-c",
                "from pathlib import Path; "
                f"p=Path({str(test_input)!r}); "
                "assert not p.is_symlink(); "
                "\ntry: p.read_bytes()\nexcept PermissionError: pass\nelse: raise AssertionError('read test input')"])


if __name__ == "__main__":
    unittest.main()
