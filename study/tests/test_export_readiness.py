"""Synthetic visible-only examples of what a pre-lock reload can detect."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from study.common import ROOT
from study.fresh_check import equal_predictions

COMPONENT = ROOT / "components/eidos.project.command/project_command.py"

PROJECT = """
import argparse
import json
from pathlib import Path

import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["infer"])
parser.add_argument("--model", type=Path, required=True)
parser.add_argument("--inputs", required=True)
parser.add_argument("--out", required=True)
args = parser.parse_args()
saved = json.loads(args.model.read_text())
with np.load(args.inputs, allow_pickle=False) as data:
    x, ids = data["x"], data["ids"]
if saved["mode"] == "broken_scalar":
    predictions = x + saved["bias"].item()
else:
    # The synthetic train-side prediction used the fitted center in every case.
    predictions = x if saved["mode"] == "omitted_preprocessing" else x - saved["center"]
np.savez(args.out, ids=ids[::-1], predictions=predictions[::-1])
"""


class VisibleExportReadiness(unittest.TestCase):
    def run_fixture(self, mode):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        model = root / "checkpoint.pt"
        inputs = root / "visible_validation.npz"
        training = root / "training_validation_predictions.npz"
        model.write_text(json.dumps({"mode": mode, "bias": 0.5, "center": 3.0}))
        ids, values = np.array([17, 9]), np.array([4.0, 5.0])
        np.savez(inputs, ids=ids, x=values)
        np.savez(training, ids=ids, predictions=values - 3.0)

        spec = importlib.util.spec_from_file_location("study_readiness_project_command", COMPONENT)
        component = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(component)
        bindings = {
            "source": SimpleNamespace(artifact_id="fixture-source"),
            "model": SimpleNamespace(schema="model-file/1"),
            "validation": SimpleNamespace(media_type="application/x-npz"),
        }
        reader = SimpleNamespace(
            json={"source": {"files": {"project.py": PROJECT}}}.__getitem__,
            path={"model": model, "validation": inputs}.__getitem__,
        )
        emitted = {}

        def stage_file(slot, path, **metadata):
            emitted[slot] = (Path(path), metadata)

        writer = SimpleNamespace(stage_file=stage_file, stage_json=lambda *_args, **_kwargs: None)
        result = component.run(
            bindings, {"argv": ["python", "project.py", "infer", "--model", "{work}/model.pt",
                                "--inputs", "{work}/validation.npz", "--out", "{out}/fresh_predictions.npz"],
                       "timeout_seconds": 15},
            reader=reader, writer=writer, context=SimpleNamespace(scratch_dir=root / "worker", seed=1),
        )
        self.assertEqual(result["returncode"], 0)
        output, metadata = emitted["fresh_predictions"]
        self.assertEqual(metadata["schema"], "prediction-array/1")
        return training, output

    def test_training_artifact_can_exist_even_when_checkpoint_reload_raises(self):
        with self.assertRaisesRegex(RuntimeError, "float.*has no attribute 'item'"):
            self.run_fixture("broken_scalar")

    def test_successful_reload_can_still_disagree_on_visible_predictions(self):
        training, output = self.run_fixture("omitted_preprocessing")
        with np.load(training) as locked, np.load(output) as fresh:
            self.assertEqual(set(locked["ids"]), set(fresh["ids"]))
        self.assertFalse(equal_predictions(training, output))

    def test_serialized_preprocessing_round_trip_agrees_after_id_alignment(self):
        training, output = self.run_fixture("round_trip")
        self.assertTrue(equal_predictions(training, output))


    @unittest.skipUnless(sys.platform == "darwin", "requires the study Seatbelt evaluator")
    def test_evaluator_candidate_process_has_all_four_single_thread_caps(self):
        from unittest.mock import patch

        from study.fresh_check import candidate_command
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        workspace = Path(temp.name).resolve()
        (workspace / "candidate").mkdir()
        (workspace / "inputs").mkdir()
        names = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
        code = "import os,json; print(json.dumps({k:os.environ.get(k) for k in " + repr(names) + "}))"
        with patch.dict("os.environ", {"EIDOS_RESEARCH_PYTHON": sys.executable}):
            actual = json.loads(candidate_command(workspace, [sys.executable, "-c", code], timeout=120))
        self.assertEqual(actual, dict.fromkeys(names, "1"))


if __name__ == "__main__":
    unittest.main()
