#!/usr/bin/env python3
"""Authored positive/null/invalid controls, separate from comparative outcomes."""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from study.common import read_json, research_python, write_json
from study.grade_block import digest, grade_run
from study.launcher import prepare
from study.stub_agent import stub


def validation_accuracy(run):
    import numpy as np

    with (
        np.load(run / "task/validation.npz", allow_pickle=False) as data,
        np.load(run / "out/validation_predictions.npz", allow_pickle=False) as predictions,
    ):
        assert np.array_equal(data["ids"], predictions["ids"])
        return float(np.mean(data["y"] == predictions["predictions"])), len(data["y"])


def add_measurement(answer, run, baseline, candidate, n):
    receipt = dict(
        baseline=baseline,
        candidate=candidate,
        n=n,
        split="validation",
        scope="authored grader control; independent test scoring follows",
    )
    write_json(run / "out/control-validation.json", receipt)
    answer["evidence"].append(
        dict(
            path="control-validation.json",
            sha256=digest(run / "out/control-validation.json"),
            role="validation",
            supports="Measured control validation accuracy",
            limitations="Development validation only",
        )
    )
    answer["measurements"] = [
        dict(
            metric="accuracy",
            direction="higher_is_better",
            split="validation",
            denominator=n,
            units="fraction",
            baseline=baseline,
            candidate=candidate,
            uncertainty_method="Authored control; grader independently computes acceptance metrics.",
            evidence_receipt="control-validation.json",
        )
    ]


def qualify(root):
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("qualification requires a fresh output directory")
    root.mkdir(parents=True, exist_ok=True)
    outcomes = {}
    for card in ("D01", "D02", "D12"):
        run = root / card
        prepare(dict(arm=1, task=card, run=1, block_id=f"control-{card}"), run)
        stub(run)
        baseline_accuracy, n = validation_accuracy(run)
        if card == "D02":
            # An authored MLP positive control, never presented as an investigator result.
            code = run / "out/project.py"
            text = code.read_text().replace(
                "self.net = nn.Linear(nin, nout)",
                "self.net = nn.Sequential(nn.Linear(nin, 64), nn.ReLU(), nn.Linear(64, nout))",
            )
            text = text.replace("'digits':8", "'digits':120")
            code.write_text(text)
            subprocess.run(
                [
                    research_python(),
                    str(code),
                    "train",
                    "--inputs",
                    str(run / "task"),
                    "--out",
                    str(run / "out"),
                    "--seed",
                    "11",
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            answer = read_json(run / "out/answer.json")
            answer.update(
                status="improvement_claimed",
                claim_type="recipe_improvement",
                claim="Authored positive control: longer MLP recipe on this development digit fixture.",
            )
            c = answer["candidate"]
            for path_key, sha_key in (
                ("code_path", "code_sha256"),
                ("model_path", "model_sha256"),
                ("predictions_path", "predictions_sha256"),
            ):
                c[sha_key] = digest(run / "out" / c[path_key])
            answer["evidence"][0]["sha256"] = digest(run / "out/receipt.json")
            candidate_accuracy, _ = validation_accuracy(run)
            add_measurement(answer, run, baseline_accuracy, candidate_accuracy, n)
            answer["exposures"] = [
                "Authored grader qualification positive; reference-assisted development evidence"
            ]
            write_json(run / "out/answer.json", answer)
        result = grade_run(run, root / "grading" / card)
        outcomes[card] = result
        assert result["status"] == "valid", result
        if card == "D01":
            assert result["operational_pass"], result
        elif card == "D02":
            assert result["verified_success"] and len(result["reproduction"]) == 2, result
        else:
            assert result["null_correct"] and not result["verified_success"], result
            answer = read_json(run / "out/answer.json")
            answer.update(
                status="improvement_claimed",
                claim_type="checkpoint_improvement",
                claim="Deliberately unsupported positive claim in an authored null control.",
            )
            add_measurement(answer, run, baseline_accuracy, baseline_accuracy, n)
            write_json(run / "out/answer.json", answer)
            false = grade_run(run, root / "grading/null-false-claim")
            assert (
                false["status"] == "valid" and false["false_positive_claim"] and not false["card_success"]
            ), false
            outcomes["null_false_claim"] = false
            (run / "out/answer.json").unlink()
            missing = grade_run(run, root / "grading/missing")
            assert missing["status"] == "invalid_output" and not missing["verified_success"], missing
            outcomes["missing"] = missing
    write_json(root / "qualification.json", outcomes)
    print(
        "QUALIFIED: D01 equality, D02 positive + two fresh seeds, D12 honest null, false claim and missing output"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    qualify(parser.parse_args().out)
