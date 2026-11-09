"""Generic fixture policies for task bundles (deterministic; fixture-only, never real LLM behaviour).

``bundle-baseline-replicate`` reproduces a bundle's own training command over three seeds, scores
each run on the visible validation split with the generic scorer, records the spread and
concludes without a positive claim. It reads only the authorized view (declared inputs + jobs)
and knows nothing about specific tasks, references or answers.
"""

from __future__ import annotations

from typing import Any

from eidos.investigator.runtime import DecisionRequest, fixture_policy

COMMAND_ID = "eidos.project.command"
SCORER_ID = "eidos.task.score-classification"
SEEDS = (11, 23, 37)
TERMINAL = ("succeeded", "failed", "cancelled")


def _latest(view: dict, cid: str) -> str | None:
    """Newest active release of a component id in the turn's catalog snapshot."""
    refs = [c["ref"] for c in view.get("catalog", []) if c["ref"].split("@")[0] == cid and c.get("status", "active") == "active"]
    if not refs:
        return None
    return max(refs, key=lambda r: tuple(int(x) for x in r.split("@")[1].split(".")[:3]))


def _wait(reason: str) -> dict[str, Any]:
    return {"rationale": reason, "calls": [], "control": {"action": "wait_for_jobs"}}


@fixture_policy("bundle-baseline-replicate")
def bundle_baseline_replicate(req: DecisionRequest) -> dict[str, Any]:
    if req.turn_so_far:
        return _wait("calls issued this turn; wait for their jobs")
    view = req.view or {}
    inv = (view.get("investigation") or {}).get("body") or {}
    inputs = inv.get("inputs") or {}
    jobs = view.get("jobs") or []
    if not {"train", "validation"} <= set(inputs):
        return {"rationale": "no declared train/validation inputs", "calls": [],
                "control": {"action": "request_input", "summary": "declare train/validation input artifacts"}}
    command = _latest(view, COMMAND_ID)
    scorer = _latest(view, SCORER_ID)
    if not command or not scorer:
        return {"rationale": "required generic components are not installed", "calls": [],
                "control": {"action": "request_input", "summary": f"install {COMMAND_ID} and {SCORER_ID}"}}
    train = [j for j in jobs if (j["component"] or "").split("@")[0] == COMMAND_ID]
    if not train:
        calls = [{"tool": "run_component", "args": {
            "component_ref": command, "seed": s, "purpose": "reproduction",
            "inputs": {k: inputs[k] for k in ("train", "validation", "task") if k in inputs},
            "config": {"argv": ["python", "project.py", "train", "--inputs", "{work}", "--out", "{out}", "--seed", "{seed}"],
                       "files": {"train": "train.npz", "validation": "validation.npz", "task": "task.json"}, "seed": s},
            "prediction": "baseline reproduces with small seed-to-seed variation",
            "falsifier": "a seed fails or validation accuracy varies widely"}} for s in SEEDS]
        return {"rationale": "reproduce the supplied baseline over three training seeds before any change",
                "purpose": "reproduction", "hypotheses": [{"statement": "the baseline is reproducible",
                "prediction": "all seeds succeed", "falsifier": "any seed fails"}], "calls": calls,
                "control": {"action": "wait_for_jobs"}}
    if any(j["status"] not in TERMINAL for j in train):
        return _wait("baseline training still running")
    scored = {(j.get("inputs") or {}).get("predictions") for j in jobs if (j["component"] or "").split("@")[0] == SCORER_ID}
    pending = []
    for j in train:
        if j["status"] != "succeeded":
            continue
        pred = next((o["artifact_id"] for o in j["outputs"] if o["slot"] == "validation_predictions"), None)
        if pred and pred not in scored:
            pending.append({"tool": "run_component", "args": {"component_ref": scorer, "purpose": "measurement-development",
                            "inputs": {"predictions": pred, "reference": inputs["validation"]},
                            "config": {"split": "validation"}}})
    if pending:
        return {"rationale": "score every baseline run on the visible validation split", "calls": pending,
                "control": {"action": "wait_for_jobs"}}
    scores = [j for j in jobs if (j["component"] or "").split("@")[0] == SCORER_ID]
    if any(j["status"] not in TERMINAL for j in scores):
        return _wait("scoring still running")
    accs = [((j.get("receipt") or {}).get("metrics") or {}).get("accuracy") for j in scores if j["status"] == "succeeded"]
    accs = [a for a in accs if a is not None]
    failed = [j["id"] for j in train + scores if j["status"] != "succeeded"]
    summary = (f"Baseline reproduced over {len(accs)} seed(s); validation accuracy "
               f"{min(accs):.4f}-{max(accs):.4f}" if accs else "No successful baseline runs") + (
               f"; failed jobs: {failed}" if failed else "") + (
               ". No intervention was attempted by this fixture policy, so no improvement is claimed.")
    return {"rationale": "report the reproduced baseline and stop", "purpose": "reproduction",
            "calls": [{"tool": "record_note", "args": {"kind": "observation", "text": summary,
                                                        "evidence_refs": [j["id"] for j in scores]}}],
            "control": {"action": "conclude", "summary": summary, "scope": {"claim": "no_positive_claim"}}}
