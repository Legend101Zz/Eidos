"""Fresh-process check for the ordinary investigator project interface.

The public development fresh_check.py hard-codes reference ProjectModel/config
and cannot load the different agent_project.Model format. This adapter checks
arbitrary submitted project.py via its supplied infer CLI, inside no-network
Seatbelt. It never imports candidate source into the controller or scorer.
"""

import subprocess
from pathlib import Path

from study.common import agent_env, research_python, sandbox_command
from study.run_arm import stop_process_tree


def candidate_command(workspace, command, timeout=120):
    workspace = Path(workspace).resolve()
    env = agent_env(workspace)
    # No API key is added to candidate execution; network is denied as well.
    process = subprocess.Popen(
        sandbox_command(
            workspace,
            command,
            network=False,
            readonly=[workspace / "candidate", workspace / "inputs", workspace / "test_inputs.npz"],
        ),
        cwd=workspace / "candidate",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process_tree(process)
        raise ValueError("candidate execution exceeded confirmation timeout") from None
    stop_process_tree(process)
    if process.returncode:
        raise ValueError(f"candidate exited {process.returncode}: " + stderr.decode(errors="replace")[-3000:])
    return stdout.decode(errors="replace")


def infer(workspace, code, model, inputs, output):
    candidate_command(
        workspace,
        [
            research_python(),
            str(code),
            "infer",
            "--model",
            str(model),
            "--inputs",
            str(inputs),
            "--out",
            str(output),
        ],
    )


def equal_predictions(left, right):
    import numpy as np

    with np.load(left, allow_pickle=False) as a, np.load(right, allow_pickle=False) as b:
        for value in (a, b):
            if set(value.files) != {"ids", "predictions"}:
                return False
            ids, pred = value["ids"], value["predictions"]
            if ids.ndim != 1 or pred.shape != ids.shape or len(np.unique(ids)) != len(ids):
                return False
            if not np.isfinite(pred).all():
                return False
        ai, bi = np.argsort(a["ids"]), np.argsort(b["ids"])
        return bool(
            np.array_equal(a["ids"][ai], b["ids"][bi])
            and np.array_equal(a["predictions"][ai], b["predictions"][bi])
        )


def fresh_check(workspace, code, model, validation_inputs, locked_validation, test_inputs):
    workspace = Path(workspace)
    validation = workspace / "fresh_validation.npz"
    infer(workspace, code, model, validation_inputs, validation)
    validation_equal = equal_predictions(locked_validation, validation)
    first, second = workspace / "test_predictions.npz", workspace / "fresh_test_predictions.npz"
    infer(workspace, code, model, test_inputs, first)
    infer(workspace, code, model, test_inputs, second)
    test_equal = equal_predictions(first, second)
    return {
        "fresh_process_export_equal": validation_equal and test_equal,
        "locked_validation_equal": validation_equal,
        "test_reload_equal": test_equal,
        "prediction_path": str(first),
        "implementation": "study/fresh_check.py ordinary-project adapter",
    }
