#!/usr/bin/env python
"""End-to-end acceptance journey for the switch-order study, driven through the real lab.

    EIDOS_HOME=<lab home> lab start
    python tests/journeys/switch_journey.py [--project NAME] [--steps 1,2,...] [--torch-python PY] [--out PATH]

Every action is an authenticated API call (``LabClient``) or a ``lab`` CLI invocation in a fresh
process; the script holds no database, orchestration or artifact-store access. Results go to a JSON
run record (job ids, artifact ids, receipt metrics, timings, per-check outcomes).

Step 7 uses a deterministic *fixture* investigator policy. It exercises the autonomous loop's
mechanics (admission, charging, crash recovery, reconstruction, detach/reattach); it says nothing about
the behaviour of any real model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

from journey_lib import Blocked, Journey, LabError  # noqa: E402
from staging import stage_component  # noqa: E402

ADAPTER = "eidos_adapter:ADAPTER"
TRAIN, EVALUATE, EDIT = "eidos.pytorch.train@1.0.0", "eidos.pytorch.evaluate@1.0.0", "eidos.pytorch.weight-edit@1.0.0"
BEHAVIOR, SCORER = "eidos.pytorch.behavior-check@1.0.0", "lab.pairs.by_group@1.0.0"
ASSESSOR, TEMPLATE = "eidos.assessors.paired-ci@1.0.0", "eidos.templates.improves-under-budget@1.0.0"
RUNNER = "eidos.qualification-runner@1.0.0"
EVAL_CFG = {"adapter": ADAPTER, "evaluation": {"split": "test", "data_seed": 76102, "pairs": 4000}}
EDIT_CFG = {"edits": [{"entry": "readout.weight", "index": [0, -1], "op": "set", "value": 0.0}],
            "reason": "silence the recorder weight"}
LAB_ENV_COMPONENTS = ("eidos.assessors.paired-ci", "eidos.qualification-runner")
TORCH_COMPONENTS = ("eidos.pytorch.train", "eidos.pytorch.evaluate", "eidos.pytorch.weight-edit",
                    "eidos.pytorch.behavior-check")
BM, RA = "paired-report/1:both-correct-rate", "paired-report/1:reference-accuracy"

CHARTER = {
    "study_id": "order-study",
    "question": "Does silencing the recorder weight of OrderedSwitchNet improve paired (both-correct) accuracy "
                "without costing reference accuracy?",
    "population": {"model": "OrderedSwitchNet", "task": "switch order", "evaluation": "test split, data seed 76102"},
    "comparator": {"kind": "unedited parent of the same seed (paired)"},
    "metrics": {
        BM: {"direction": "increase", "denominator": "pairs", "identity": BM},
        RA: {"direction": "increase", "denominator": "pairs", "identity": RA},
    },
    "success_rules": [{
        "id": "early-repair", "metric": BM, "min_effect": 0.05,
        "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95},
        "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
        "guardrails": [{"metric": RA, "max_regression": 0.02}],
    }],
}


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# =================================================================================================
# step 1 - project, environments, components, conflict detection
# =================================================================================================

def step1(j: Journey, a: argparse.Namespace) -> None:
    with j.step("1", "project, environments, built-in components, conflict and absent-phase detection") as st:
        rc, data, err = j.cli("project", "create", j.project, "--budget", "compute_seconds=20000", "--budget", "trials=500")
        j.check("lab project create", rc == 0, rc=rc, err=err[-200:])
        rc, data, err = j.cli("project", "register-env", j.project, "--python", a.torch_python, "--root",
                              str(REPO / "examples/switch-order-project"), "--adapter", ADAPTER)
        j.check("register torch project environment (no package changes)", rc == 0, err=err[-200:])
        stage = Path(a.workdir) / "stage"
        installed, dropped = {}, {}
        for d in sorted((REPO / "components").iterdir()):
            if not (d / "eidos-component.json").exists():
                continue
            src = d
            if d.name in TORCH_COMPONENTS:
                src, dr = stage_component(d, stage)
                dropped[d.name] = dr
            env = "lab" if d.name in LAB_ENV_COMPONENTS else "default"
            t0 = time.time()
            rc, data, err = j.cli("component", "install", j.project, str(src), "--env", env)
            ok = rc == 0 and data and data.get("component_ref")
            j.check(f"install {d.name} ({env})", ok, err=err[-300:] if not ok else "", seconds=round(time.time() - t0, 1))
            if ok:
                installed[d.name] = data["component_ref"]
        j.rec["installed_components"] = installed
        j.rec["fixtures_excluded_from_build_validation"] = dropped
        j.note("binary .ckpt fixtures (git-ignored, generated by components/build_fixtures.py) cannot travel in the CLI's "
               f"text source bundle; these fixtures were excluded from the build job: {dropped}")
        for name, env, label in (("template-qualifier", "lab", "journey template-qualifier runner"),
                                 ("ext-needs-unscale", "default", "journey training extension (after-gradient-unscale)")):
            rc, data, err = j.cli("component", "install", j.project, str(HERE / "support" / name), "--env", env)
            j.check(f"install {label}", rc == 0 and bool(data and data.get("component_ref")), err=err[-300:])
        # ---- dependency conflict through the real validation path -------------------------------
        rc, data, err = j.cli("component", "install", j.project, str(HERE / "support" / "conflict-torch99"))
        j.check("dependency conflict (torch>=99 vs installed torch) refused at validation", rc != 0 and "conflict" in err.lower(),
                rc=rc, err=err[-420:])
        # ---- absent hook phase: the extension needs a phase the adapter's trainer never fires ------
        ext_dir = HERE / "support" / "ext-needs-unscale"
        r = j.run("absent-phase", TRAIN, config={
            "adapter": "limited_adapter:ADAPTER", "builder": "OrderedSwitchNet", "steps": 2, "seed": 1,
            "python_path": [str(ext_dir)],
            "extensions": [{"spec": "unscale_probe:create", "name": "unscale-probe"}]}, expect="failed")
        msg = ((r["job"].get("terminal") or {}).get("error") or {}).get("message", "")
        j.check("absent hook phase rejected (job fails naming the phase)", "after-gradient-unscale" in msg, message=msg[:300])
        # contrast: the real adapter fires every phase, so the same extension runs.
        r = j.run("phase-present", TRAIN, config={
            "adapter": ADAPTER, "builder": "OrderedSwitchNet", "steps": 2, "seed": 1,
            "python_path": [str(ext_dir)], "extensions": [{"spec": "unscale_probe:create", "name": "unscale-probe"}]})
        j.check("same extension runs when the adapter provides the phase",
                (r["receipt"].get("extensions") or {}).get("unscale-probe", {}).get("calls") == 2,
                extensions=r["receipt"].get("extensions"))
        st["numbers"]["components_installed"] = len(installed)


# =================================================================================================
# step 2 - charter before exploration
# =================================================================================================

def step2(j: Journey, a: argparse.Namespace) -> None:
    with j.step("2", "charter before exploration; override rejection; exposure-informed amendment") as st:
        # exploration is refused until a charter is committed
        nc = j.cmd_result("start_investigation", {"question": "exploration without a charter",
                                                   "limits": {"turns": 1, "trials": 2},
                                                   "investigator": {"runtime": "fixture", "max_turns": 1,
                                                                    "settings": {"policy": "conclude-immediately"}}},
                          key=f"inv-nocharter:{j.project}")
        j.expect_error("exploratory run inside an investigation without a charter is refused", "run_component",
                       {"component_ref": "lab.switch.balanced-data@1.0.0", "investigation_id": nc["investigation_id"],
                        "inputs": {}, "config": {"examples": 100, "batch_size": 100, "exception_rate": 0.2, "seed": 1}}, "CHARTER_REQUIRED")
        j.cmd("stop_investigation", {"investigation_id": nc["investigation_id"], "running_job_policy": "drain"})
        j.expect_error("charter without prospective rules and population is invalid", "commit_study_charter",
                       {"charter": {"study_id": "bad", "question": "q", "metrics": {}, "success_rules": []}}, "INVALID")
        c = j.cmd_result("commit_study_charter", {"charter": CHARTER}, key=f"charter:{j.project}")
        j.S["charter"] = c
        j.rec["charter"] = c
        j.check("charter committed at revision 1", c["revision"] == 1, **c)
        # metric ids normalise to the assessor's names
        norm = lambda m: m.rsplit(":", 1)[-1].replace("-", "_")  # noqa: E731 - mirrors paired_ci.assess.normalise
        j.check("charter metric ids normalise to the assessor's metric names",
                {norm(BM), norm(RA)} == {"both_correct_rate", "reference_accuracy"})
        # a template/plan may not carry a threshold; neither may steering
        j.S["probe_claim"] = j.cmd_result("file_claim", {
            "statement": "probe claim for override rejection", "kind": "improves",
            "scope": {"builder": "OrderedSwitchNet"}})["claim_id"]
        draft = {"success_rule_id": "early-repair", "arms": [{"id": "baseline", "role": "baseline"},
                                                            {"id": "candidate", "role": "candidate"}]}
        base_plan = {"claim_id": j.S["probe_claim"], "charter_id": c["charter_id"], "template_ref": TEMPLATE}
        j.expect_error("plan draft that sets min_effect is rejected", "draft_evidence_plan",
                       {**base_plan, "draft": {**draft, "min_effect": 0.0}}, "THRESHOLD_OVERRIDE")
        j.expect_error("plan draft that disables the charter guardrail is rejected", "draft_evidence_plan",
                       {**base_plan, "draft": {**draft, "guardrails": []}}, "THRESHOLD_OVERRIDE")
        j.expect_error("plan draft requesting allow_plan_threshold_override is rejected", "draft_evidence_plan",
                       {**base_plan, "draft": {**draft, "allow_plan_threshold_override": True}}, "THRESHOLD_OVERRIDE")
        inv = j.cmd_result("start_investigation", {"question": "override probe", "charter_id": c["charter_id"],
                                                    "limits": {"turns": 1, "trials": 1}, "investigator": {
                                                        "runtime": "fixture", "settings": {"policy": "conclude-immediately"},
                                                        "max_turns": 1}}, key=f"inv-steer-probe:{j.project}")
        j.expect_error("steering that tries to change a threshold is rejected", "steer_investigation",
                       {"investigation_id": inv["investigation_id"], "instruction": "relax the bar",
                        "structured": {"success_rules": [{"min_effect": 0.0}]}}, "THRESHOLD_OVERRIDE")
        j.cmd("stop_investigation", {"investigation_id": inv["investigation_id"], "running_job_policy": "drain"})
        st["numbers"]["charter_digest"] = c["digest"]


def step2b_amend(j: Journey, a: argparse.Namespace) -> None:
    """Amendment after an exposure (run after step 3 has produced evaluation results that were seen)."""
    with j.step("2b", "exposure-informed charter amendment preserving both rules") as st:
        c = j.S.get("charter") or j.rec.get("charter")
        if not c:
            raise Blocked("no charter")
        ex = j.cmd_result("record_exposure", {"study_id": "order-study", "subject": "evaluation:unedited-step45-both-correct",
                                              "disclosed": {"both_correct_rate": j.S.get("bc45")}})
        amended = json.loads(json.dumps(CHARTER))
        amended["success_rules"].append({
            "id": "early-repair-strict", "metric": BM, "min_effect": 0.25,
            "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95},
            "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
            "guardrails": [{"metric": RA, "max_regression": 0.02}]})
        r = j.cmd_result("amend_study_charter", {
            "charter_id": c["charter_id"], "charter": amended, "exposure_refs": [ex["exposure_id"]],
            "rationale": "Added a stricter rule after seeing the unedited step-45 both-correct rate; the original rule "
                         "stays in force and is not retuned."}, expected_revision=1)
        j.rec["charter_amendment"] = r
        j.check("amendment recorded as exposure-informed", r["exposure_informed"] is True, **r)
        j.check("exposure-informed lineage flag is set", r["exposure_informed_lineage"] is True)
        hist = j.query("record_history", {"kind": "charter", "id": c["charter_id"]})
        revs = {h["revision"]: h for h in hist}
        j.check("revision 1 (original rule) is preserved unchanged", 1 in revs and
                [x["id"] for x in revs[1]["body"]["success_rules"]] == ["early-repair"])
        j.check("revision 2 carries both rules", 2 in revs and
                [x["id"] for x in revs[2]["body"]["success_rules"]] == ["early-repair", "early-repair-strict"])
        j.check("amendment names its exposure and rationale", bool(revs[2]["body"]["amendment"]["exposure_refs"]) and
                "rationale" in revs[2]["body"]["amendment"])
        j.expect_error("amendment without rationale is refused", "amend_study_charter",
                       {"charter_id": c["charter_id"], "charter": amended, "rationale": ""}, "INVALID")


# =================================================================================================
# step 3 - training, evaluation, branching, learning rule, architecture, data generation
# =================================================================================================

def metrics_of(j: Journey, label: str, preds_artifact: str) -> dict:
    r = j.run(f"score:{label}", SCORER, inputs={"predictions": preds_artifact}, config={})
    return {"overall": (r["receipt"].get("metrics") or {}), "report": r["out"].get("report"), "job": r["id"]}


def evaluate(j: Journey, label: str, ckpt: str, **extra) -> dict:
    cfg = {**EVAL_CFG, **extra}
    r = j.run(f"eval:{label}", EVALUATE, inputs={"checkpoint": ckpt}, config=cfg)
    return r


def step3(j: Journey, a: argparse.Namespace) -> None:
    with j.step("3", "train/evaluate/reproduce, branches, edits, learning rule, architecture change, data generation") as st:
        t0 = time.time()
        base_cfg = {"adapter": ADAPTER, "builder": "OrderedSwitchNet", "seed": 41}
        ctrl = j.run("train:control-180", TRAIN, config={**base_cfg, "steps": 180, "checkpoint_every": 45,
                                                         "publish_intermediate_checkpoints": True})
        ck = {s: aid for s, aid in ctrl["out"].items() if s.startswith("checkpoint")}
        ck45, ck180 = ck.get("checkpoint-step-00000045"), ck.get("checkpoint")
        j.check("train published checkpoints at step 45 and 180", bool(ck45 and ck180), slots=sorted(ck))
        if not (ck45 and ck180):
            raise Blocked("no checkpoints")
        j.S.update(ck45=ck45, ck180=ck180, control_fp=ctrl["receipt"].get("state_fingerprint"))
        st["numbers"]["train_control_wall_seconds"] = ctrl["receipt"].get("wall_seconds")
        art45 = j.query("get_artifact", {"artifact_id": ck45})
        digest45_before = art45["digest"]
        # evaluation + reproduction of the design number (unedited, step 45)
        e45 = evaluate(j, "unedited-45", ck45)
        s45 = metrics_of(j, "unedited-45", e45["out"]["predictions"])
        bc = s45["overall"].get("both_correct_rate")
        ra = s45["overall"].get("reference_accuracy")
        j.S["bc45"] = bc
        j.check("reproduce both-correct 0.2625 unedited at step 45", bc is not None and abs(bc - 0.2625) < 1e-9, both_correct=bc)
        j.check("reference accuracy 0.984 at step 45", ra is not None and abs(ra - 0.984) < 1e-9, reference_accuracy=ra)
        st["numbers"].update(both_correct_unedited_45=bc, reference_accuracy_unedited_45=ra)
        e180 = evaluate(j, "unedited-180", ck180)
        s180 = metrics_of(j, "unedited-180", e180["out"]["predictions"])
        st["numbers"].update(both_correct_unedited_180=s180["overall"].get("both_correct_rate"),
                             reference_accuracy_unedited_180=s180["overall"].get("reference_accuracy"))
        j.S.update(pred45=e45["out"]["predictions"], pred180=e180["out"]["predictions"])

        # ---- no-change continuation (restore class exact) vs an uninterrupted control ----------------
        mig = {k: "copy" for k in art45["meta"]["checkpoint"]["state_entries"]}
        rc, data, err = j.cli("checkpoint", "branch", j.project, "--parent", ck45, "--component", TRAIN,
                              "--migration", json.dumps(mig), "--stream-policy", "paired", "--wall-seconds", "600",
                              "--config", json.dumps({**base_cfg, "steps": 180, "verify_no_change": True}))
        j.check("lab checkpoint branch (no-change continuation) planned and launched", rc == 0 and data, err=err[-300:])
        if not data:
            raise Blocked("branch failed")
        bplan, bjob_id = data["result"], data["launch"]["result"]["job_id"]
        j.check("planned restore class is exact", bplan["restore_class"] == "exact", **{k: bplan[k] for k in ("restore_class", "independence_unit")})
        bjob = j.wait_job(bjob_id)
        j.record_job("branch:no-change-45-to-180", bjob)
        brec = (bjob["terminal"] or {}).get("receipt", {})
        j.check("no-change continuation succeeded", bjob["status"] == "succeeded", error=((bjob["terminal"] or {}).get("error")))
        j.check("restore class exact in the receipt", brec.get("restore_class") == "exact", restore_class=brec.get("restore_class"))
        j.check("continuation fingerprint equals the uninterrupted 180-step control",
                brec.get("state_fingerprint") == j.S["control_fp"], branch=brec.get("state_fingerprint"), control=j.S["control_fp"])
        j.check("in-job verify_no_change re-train matches", brec.get("no_change_restore_match") is True)
        st["numbers"]["no_change_fingerprint"] = brec.get("state_fingerprint")
        j.S["branch_a_job"] = bjob_id

        # ---- sibling branch (independent stream) -> shared training history ----------------------
        sib = j.cmd_result("plan_branch", {"parent_checkpoint_id": ck45, "component_ref": TRAIN, "migration": mig,
                                           "stream_policy": "independent", "seed": 7, "intervention": {"kind": "none"},
                                           "config": {**base_cfg, "steps": 90}, "wall_seconds": 600})
        sibl = j.cmd_result("launch_branch", {"trial_id": sib["trial_id"]})
        sjob = j.wait_job(sibl["job_id"])
        j.record_job("branch:sibling-independent-45-to-90", sjob)
        j.check("sibling branch succeeded", sjob["status"] == "succeeded", error=(sjob["terminal"] or {}).get("error"))
        cmp = j.query("compare_trials", {"job_ids": [bjob_id, sibl["job_id"]]})
        j.check("compare_trials marks branch siblings as sharing training history", cmp["shared_training_history"] is True,
                units=[r["independence_unit"] for r in cmp["rows"]])
        j.check("a branch and the uninterrupted run it was forked from share training history",
                j.query("compare_trials", {"job_ids": [bjob_id, ctrl["id"]]})["shared_training_history"] is True)

        # ---- deliberate migration: weight edit at two stages --------------------------------------
        edits = {}
        for stage, ck_id in (("45", ck45), ("180", ck180)):
            ed = j.run(f"edit:{stage}", EDIT, inputs={"checkpoint": ck_id}, config=EDIT_CFG)
            r = ed["receipt"]
            j.check(f"edit@{stage}: parent unchanged and storage independent",
                    r.get("parent_unchanged") is True and r.get("storage_independent") is True,
                    parent_unchanged=r.get("parent_unchanged"), storage_independent=r.get("storage_independent"))
            j.check(f"edit@{stage}: restore class warm-start", r.get("restore_class") == "warm-start", restore_class=r.get("restore_class"))
            edits[stage] = ed["out"]["checkpoint"]
            ee = evaluate(j, f"edited-{stage}", edits[stage])
            se = metrics_of(j, f"edited-{stage}", ee["out"]["predictions"])
            st["numbers"][f"both_correct_edited_{stage}"] = se["overall"].get("both_correct_rate")
            st["numbers"][f"reference_accuracy_edited_{stage}"] = se["overall"].get("reference_accuracy")
            j.S[f"pred_edited_{stage}"] = ee["out"]["predictions"]
            j.S[f"edit{stage}"] = edits[stage]
        j.check("edited step-45 both-correct is 0.844", abs((st["numbers"]["both_correct_edited_45"] or 0) - 0.844) < 1e-9,
                both_correct=st["numbers"]["both_correct_edited_45"])
        # parent immutability: the parent artifact bytes and digest are untouched by the edit
        art45_after = j.query("get_artifact", {"artifact_id": ck45})
        j.check("parent artifact digest unchanged after the edit", art45_after["digest"] == digest45_before)
        import tempfile
        with tempfile.NamedTemporaryFile() as tmp:
            j.client.download(j.project, ck45, tmp.name)
            j.check("downloaded parent bytes still hash to the recorded digest",
                    sha256_file(Path(tmp.name)) == digest45_before.split(":")[-1])
        # a continuation of the edited checkpoint is a branch with its own (warm-start) class
        entries = art45["meta"]["checkpoint"]["state_entries"]
        edited_art = j.query("get_artifact", {"artifact_id": edits["45"]})
        ent_e = (edited_art["meta"].get("checkpoint") or {}).get("state_entries", entries)
        pe = j.cmd_result("plan_branch", {"parent_checkpoint_id": edits["45"], "component_ref": TRAIN,
                                          "migration": {k: "copy" for k in ent_e}, "stream_policy": "paired",
                                          "intervention": {"kind": "change-weights", "note": "continue after the edit"},
                                          "config": {**base_cfg, "steps": 90}, "wall_seconds": 600})
        j.check("branch from an edited checkpoint is planned as warm-start", pe["restore_class"] == "warm-start",
                restore_class=pe["restore_class"])
        pel = j.cmd_result("launch_branch", {"trial_id": pe["trial_id"]})
        pej = j.wait_job(pel["job_id"])
        j.record_job("branch:edited-45-to-90", pej)
        j.check("edited-checkpoint continuation succeeded", pej["status"] == "succeeded", error=(pej["terminal"] or {}).get("error"))

        # ---- stateful learning rule, as a rule from scratch and added at a branch ---------------------
        ext_dir = str(REPO / "components" / "lab.learning.group-balanced-ema")
        ext = {"spec": "group_ema.rule:create", "name": "group-balanced-ema", "config": {"decay": 0.9}}
        lr = j.run("train:group-balanced-ema", TRAIN, config={**base_cfg, "steps": 45, "balance_groups": True,
                                                            "python_path": [ext_dir], "extensions": [ext]})
        exr = (lr["receipt"].get("extensions") or {}).get("group-balanced-ema") or {}
        j.check("stateful learning rule ran (updates counted, state checkpointed)",
                exr.get("updates") == 45 and any("group-balanced-ema" in k for k in
                                                 (j.query("get_artifact", {"artifact_id": lr["out"]["checkpoint"]})["meta"]["checkpoint"]["state_entries"])),
                extension_receipt=exr)
        j.note("extension source is mounted via an absolute python_path to components/lab.learning.group-balanced-ema; "
               "the published release is not what is loaded (train only mounts 'extension.source/1' from a lock bundle)")
        ent = dict(mig)
        ent["component.group-balanced-ema"] = {"action": "init", "mapping": {"fresh": True}}
        pl = j.cmd_result("plan_branch", {"parent_checkpoint_id": ck45, "component_ref": TRAIN, "migration": ent,
                                          "stream_policy": "paired", "intervention": {"kind": "change-learning"},
                                          "config": {**base_cfg, "steps": 90, "balance_groups": True,
                                                     "python_path": [ext_dir], "extensions": [ext]}, "wall_seconds": 600})
        j.check("adding the learning rule at a branch is warm-start with an explicit init for its state",
                pl["restore_class"] == "warm-start", restore_class=pl["restore_class"])
        plj = j.wait_job(j.cmd_result("launch_branch", {"trial_id": pl["trial_id"]})["job_id"])
        j.record_job("branch:learning-rule-45-to-90", plj)
        j.check("learning-rule branch succeeded", plj["status"] == "succeeded", error=(plj["terminal"] or {}).get("error"))

        # ---- architecture change: SwitchNet parent continued as OrderedSwitchNet ----------------------
        sw = j.run("train:switchnet-45", TRAIN, config={**base_cfg, "builder": "SwitchNet", "steps": 45})
        j.check("an unrelated from-scratch run does not share history with the branch",
                j.query("compare_trials", {"job_ids": [bjob_id, sw["id"]]})["shared_training_history"] is False)
        swa = j.query("get_artifact", {"artifact_id": sw["out"]["checkpoint"]})
        am = {k: "copy" for k in swa["meta"]["checkpoint"]["state_entries"]}
        am["model"] = {"action": "transform", "mapping": {"fresh": []}}
        pa = j.cmd_result("plan_branch", {"parent_checkpoint_id": sw["out"]["checkpoint"], "component_ref": TRAIN,
                                          "migration": am, "stream_policy": "paired",
                                          "intervention": {"kind": "change-structure", "from": "SwitchNet", "to": "OrderedSwitchNet"},
                                          "config": {**base_cfg, "builder": "OrderedSwitchNet", "steps": 90}, "wall_seconds": 600})
        j.check("architecture change is planned as warm-start", pa["restore_class"] == "warm-start")
        paj = j.wait_job(j.cmd_result("launch_branch", {"trial_id": pa["trial_id"]})["job_id"])
        j.record_job("branch:architecture-switchnet-to-ordered", paj)
        j.check("SwitchNet -> OrderedSwitchNet branch trained", paj["status"] == "succeeded", error=(paj["terminal"] or {}).get("error"))
        if paj["status"] == "succeeded":
            ach = {o["slot"]: o["artifact_id"] for o in paj["outputs"]}["checkpoint"]
            ae = evaluate(j, "architecture-ordered-90", ach)
            st["numbers"]["both_correct_arch_change_90"] = metrics_of(j, "arch90", ae["out"]["predictions"])["overall"].get("both_correct_rate")

        # ---- synthetic data generation -----------------------------------------------------------------
        dg = j.run("data:balanced", "lab.switch.balanced-data@1.0.0",
                   config={"examples": 400, "batch_size": 100, "exception_rate": 0.25, "mode": "rounded-per-batch", "seed": 3})
        dr = dg["receipt"]
        j.check("synthetic data generation job realised the requested exception rate",
                dr.get("realized_exceptions") == dr.get("requested_exceptions"), receipt={k: dr.get(k) for k in (
                    "requested_exceptions", "realized_exceptions", "realized_exception_rate")})
        st["numbers"]["step3_seconds"] = round(time.time() - t0, 1)
        ra2 = j.query("budgets", {"entries": False})
        st["numbers"]["project_budgets"] = ra2["budgets"]


# =================================================================================================
# step 4 - behaviour check and export
# =================================================================================================

def step4(j: Journey, a: argparse.Namespace) -> None:
    with j.step("4", "fresh-process behaviour check and export; wrong builder refused") as st:
        ed = j.S.get("edit45")
        if not ed:
            raise Blocked("no edited checkpoint from step 3")
        good = j.run("behavior:edited-45-OrderedSwitchNet", BEHAVIOR, inputs={"checkpoint": ed},
                     config={"adapter": ADAPTER, "builder": "OrderedSwitchNet"})
        j.check("behaviour check of the edited checkpoint passes in a fresh process",
                good["receipt"].get("behavior_match") is True and good["receipt"].get("strict_load_ok") is True,
                receipt={k: good["receipt"].get(k) for k in ("behavior_match", "strict_load_ok", "max_abs_diff", "builder")})
        dest = Path(a.workdir) / "export-edited-45"
        rc, data, err = j.cli("export", j.project, "--checkpoint", ed, "--behavior-job", good["id"], "--dest", str(dest))
        j.check("lab export records an export", rc == 0 and data and data["result"].get("export_id"), err=err[-300:])
        if data:
            rec = j.query("get_record", {"kind": "export", "id": data["result"]["export_id"]})
            j.rec["export"] = {"export_id": data["result"]["export_id"], "checkpoint": ed, "behavior_job": good["id"],
                               "dest": str(dest)}
            j.check("export record names the checkpoint digest and the behaviour receipt",
                    rec["body"]["checkpoint"]["artifact_id"] == ed and rec["body"]["behavior_check"]["job_id"] == good["id"])
            j.check("export wrote the checkpoint and its record locally", (dest / "checkpoint.pt").exists() and
                    (dest / "eidos-export.json").exists())
        bad = j.run("behavior:edited-45-wrong-builder-SwitchNet", BEHAVIOR, inputs={"checkpoint": ed},
                    config={"adapter": ADAPTER, "builder": "SwitchNet"})
        br = bad["receipt"]
        j.check("wrong-builder behaviour check fails behaviourally while strict loading passes",
                br.get("behavior_match") is False and br.get("strict_load_ok") is True,
                receipt={k: br.get(k) for k in ("behavior_match", "strict_load_ok", "max_abs_diff", "builder", "reference_builder")})
        rc, data, err = j.cli("export", j.project, "--checkpoint", ed, "--behavior-job", bad["id"])
        j.check("export with the wrong-builder check is refused", rc != 0 and "behavioral check did not match" in err,
                rc=rc, err=err[-300:])
        st["numbers"]["max_abs_diff_wrong_builder"] = br.get("max_abs_diff")


STEPS = [("1", step1), ("2", step2), ("3", step3), ("2b", step2b_amend), ("4", step4)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=f"switch{int(time.time()) % 100000}")
    ap.add_argument("--steps", default="")
    ap.add_argument("--torch-python", default=os.environ.get("EIDOS_TORCH_PYTHON",
                                                             "/workspace/.eidos-dev/h1/envs/py312/bin/python"))
    ap.add_argument("--out", default="/workspace/.eidos-dev/h1/journey-run.json")
    ap.add_argument("--workdir", default="/workspace/.eidos-dev/h1/journey-work")
    ap.add_argument("--resume", action="store_true", help="reuse an existing project and its saved state")
    a = ap.parse_args()
    Path(a.workdir).mkdir(parents=True, exist_ok=True)
    os.chdir(REPO)
    j = Journey(a.project, Path(a.out))
    state_file = Path(a.workdir) / f"{a.project}.state.json"
    if a.resume and state_file.exists():
        j.S = json.loads(state_file.read_text())
    wanted = [s for s in a.steps.split(",") if s] or [k for k, _ in STEPS]
    for key, fn in STEPS:
        if key in wanted:
            fn(j, a)
            state_file.write_text(json.dumps(j.S, default=str))
    j.save()
    bad = [k for k, v in j.rec["steps"].items() if v["status"] != "pass"]
    print("\nSUMMARY", {k: v["status"] for k, v in j.rec["steps"].items()}, "->", a.out)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
