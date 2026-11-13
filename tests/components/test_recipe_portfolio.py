"""Run scientific author tests in the numpy/sklearn project interpreter."""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT, SDK_PATH

CASES = (
    "spec_grid", "spec_errors", "registry", "folds", "in_fold", "paired_band",
    "row_pairing", "oof", "timeout", "stable_digest", "heldout", "parity",
    "check_compare", "unpickler", "checkpoint_guard", "rescore", "coverage",
    "operators", "models", "refine", "shift", "toolkit", "seed_zero", "large_plan", "multiclass_checkpoint",
)


@pytest.mark.parametrize("case", CASES)
def test_recipe_portfolio(case, tmp_path):
    python = os.environ.get("EIDOS_NUMPY_PYTHON")
    if not python or not Path(python).is_file():
        pytest.skip("set EIDOS_NUMPY_PYTHON to the numpy/sklearn interpreter")
    result = subprocess.run(
        [python, str(Path(__file__).with_name("portfolio_cases.py")), case, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join((SDK_PATH, str(ROOT))),
             "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
             "VECLIB_MAXIMUM_THREADS": "1"},
        capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
