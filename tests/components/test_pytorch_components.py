"""PyTorch components through the real worker protocol in the project interpreter (author tests)."""

import json
import subprocess

import pytest

from conftest import (
    ADAPTER,
    BEHAVIOR,
    EVALUATE,
    EXTENSION_PATH,
    GROUP_EMA,
    TORCH_PY,
    TRAIN,
    WEIGHT_EDIT,
    ckpt_input,
    train_config,
)


def entries_of(res):
    return res.receipt["checkpoints"]["checkpoint"]["state_entries"]


def branch(entries, **over):
    cfg = {
        "migration": {e: "copy" for e in entries},
        "stream_policy": "paired",
        "seed": None,
        "intervention": {"kind": "none"},
        "restore_class": "exact",
    }
    cfg.update(over)
    return cfg


def test_train_receipt_and_checkpoint_contract(torch_run):
    res = torch_run(*TRAIN, config=train_config(steps=6, seed=3, checkpoint_every=3))
    assert res.ok, res.manifest.error
    r = res.receipt
    assert (r["end_step"], r["updates"], r["examples"], r["restore_class"]) == (6, 6, 600, "exact")
    assert r["phase_counts"]["phases"]["before-optimizer-step"] == 6 and r["final_loss"] > 0
    ck = r["checkpoints"]["checkpoint"]
    assert ck["complete"] is True and ck["step"] == 6 and ck["builder"] == "switch_order:OrderedSwitchNet"
    assert {"model", "optimizer", "step", "data_cursor", "component.train-meta"} <= set(ck["state_entries"])
    entry = next(e for e in res.manifest.outputs if e.slot == "checkpoint")
    assert entry.role == "checkpoint" and entry.schema == "eidos-checkpoint/1"
    assert "checkpoint-step-00000003" not in res.slots()  # intermediates are published only on request
    again = torch_run(*TRAIN, config=train_config(steps=6, seed=3))
    assert again.receipt["state_fingerprint"] == r["state_fingerprint"]  # deterministic on CPU


def test_branch_without_change_reproduces_the_uninterrupted_trajectory(torch_run):
    uninterrupted = torch_run(*TRAIN, config=train_config(steps=12, seed=7))
    parent = torch_run(*TRAIN, config=train_config(steps=5, seed=7))
    child = torch_run(
        *TRAIN,
        inputs=ckpt_input(parent, name="parent_checkpoint"),
        config=train_config(steps=12, seed=7, branch=branch(entries_of(parent))),
    )
    assert child.ok, child.manifest.error
    assert child.receipt["resumed_from_step"] == 5 and child.receipt["updates"] == 7
    assert child.receipt["restore_class"] == "exact"
    assert child.receipt["state_fingerprint"] == uninterrupted.receipt["state_fingerprint"]
    assert child.receipt["model_fingerprint"] == uninterrupted.receipt["model_fingerprint"]
    # the branch carries its own seed only when the stream is independent; paired reuses the parent's
    assert child.receipt["branch"]["stream_policy"] == "paired"


def test_independent_stream_diverges_from_the_paired_trajectory(torch_run):
    parent = torch_run(*TRAIN, config=train_config(steps=5, seed=7))
    uninterrupted = torch_run(*TRAIN, config=train_config(steps=9, seed=7))
    child = torch_run(
        *TRAIN,
        inputs=ckpt_input(parent, name="parent_checkpoint"),
        config=train_config(
            steps=9, seed=7, branch=branch(entries_of(parent), stream_policy="independent", seed=99)
        ),
    )
    assert child.ok and child.receipt["stream_seed"] == 99
    assert child.receipt["model_fingerprint"] != uninterrupted.receipt["model_fingerprint"]


def test_branch_must_declare_every_state_entry_and_cannot_overclaim(torch_run):
    parent = torch_run(*TRAIN, config=train_config(steps=4, seed=7))
    entries = entries_of(parent)
    partial = branch(entries)
    del partial["migration"]["optimizer"]
    res = torch_run(
        *TRAIN,
        inputs=ckpt_input(parent, name="parent_checkpoint"),
        config=train_config(steps=6, seed=7, branch=partial),
    )
    assert res.manifest.status == "failed" and "optimizer" in res.manifest.error["message"]
    reset = branch(entries, restore_class="warm-start")
    reset["migration"]["optimizer"] = {"action": "transform", "mapping": {"fresh": True}}
    ok = torch_run(
        *TRAIN,
        inputs=ckpt_input(parent, name="parent_checkpoint"),
        config=train_config(steps=6, seed=7, branch=reset),
    )
    assert ok.ok and ok.receipt["restore_class"] == "warm-start"
    over = torch_run(
        *TRAIN,
        inputs=ckpt_input(parent, name="parent_checkpoint"),
        config=train_config(steps=6, seed=7, branch=dict(reset, restore_class="exact")),
    )
    assert over.manifest.status == "failed" and over.manifest.error["category"] == "contract"


def test_group_ema_extension_state_is_saved_and_resume_is_exact(torch_run):
    def cfg(steps):
        return train_config(
            steps=steps, seed=11, python_path=[EXTENSION_PATH], extensions=[GROUP_EMA], balance_groups=True
        )

    uninterrupted = torch_run(*TRAIN, config=cfg(10))
    parent = torch_run(*TRAIN, config=cfg(4))
    assert "component.group-balanced-ema" in entries_of(parent)
    assert parent.receipt["extensions"]["group-balanced-ema"]["updates"] == 4
    assert parent.receipt["phase_counts"]["hooks"] == {"before-optimizer-step:group-balanced-ema": 4}
    child = torch_run(
        *TRAIN,
        inputs=ckpt_input(parent, name="parent_checkpoint"),
        config=dict(cfg(10), branch=branch(entries_of(parent))),
    )
    assert child.ok, child.manifest.error
    assert child.receipt["state_fingerprint"] == uninterrupted.receipt["state_fingerprint"]
    assert child.receipt["extensions"]["group-balanced-ema"]["updates"] == 10
    assert uninterrupted.receipt["extensions"]["group-balanced-ema"]["extra_forward_backward_passes"] == 20


def test_branch_can_add_a_learning_rule_only_with_an_explicit_init(torch_run):
    parent = torch_run(*TRAIN, config=train_config(steps=4, seed=11))
    cfg = train_config(steps=8, seed=11, python_path=[EXTENSION_PATH], extensions=[GROUP_EMA])
    missing = torch_run(
        *TRAIN,
        inputs=ckpt_input(parent, name="parent_checkpoint"),
        config=dict(cfg, branch=branch(entries_of(parent))),
    )
    assert (
        missing.manifest.status == "failed"
        and "component state mismatch" in missing.manifest.error["message"]
    )
    mig = branch(entries_of(parent), intervention={"kind": "change-learning"}, restore_class="warm-start")
    mig["migration"]["component.group-balanced-ema"] = {"action": "init", "mapping": {"fresh": True}}
    res = torch_run(*TRAIN, inputs=ckpt_input(parent, name="parent_checkpoint"), config=dict(cfg, branch=mig))
    assert res.ok, res.manifest.error
    assert (
        res.receipt["restore_class"] == "warm-start"
        and res.receipt["extensions"]["group-balanced-ema"]["updates"] == 4
    )


def test_group_ema_without_memory_matches_group_balanced_loss(torch_run):
    """decay 0 + every batch containing both groups == the adapter's balance_groups loss."""
    ema = torch_run(
        *TRAIN,
        config=train_config(
            steps=8,
            seed=5,
            python_path=[EXTENSION_PATH],
            extensions=[{**GROUP_EMA, "config": {"decay": 0.0}}],
        ),
    )
    balanced = torch_run(*TRAIN, config=train_config(steps=8, seed=5, balance_groups=True))
    ordinary = torch_run(*TRAIN, config=train_config(steps=8, seed=5))

    def weights(res):
        code = (
            "import sys, json, torch; s = torch.load(sys.argv[1], weights_only=True); "
            "print(json.dumps(s['entries']['model']['readout.weight'].flatten().tolist()))"
        )
        out = subprocess.run(
            [TORCH_PY, "-c", code, str(res.path("checkpoint"))], capture_output=True, text=True, check=True
        ).stdout
        return json.loads(out.strip().splitlines()[-1])

    def gap(a, b):
        return max(abs(x - y) for x, y in zip(weights(a), weights(b)))  # noqa: B905

    assert gap(ema, balanced) < 1e-4
    assert gap(ema, ordinary) > 1e-3


def test_evaluate_stages_paired_rows_with_cluster(torch_run):
    parent = torch_run(*TRAIN, config=train_config(steps=6, seed=5))
    res = torch_run(
        *EVALUATE,
        inputs=ckpt_input(parent),
        config={"adapter": ADAPTER, "evaluation": {"split": "test", "data_seed": 3, "pairs": 40}},
    )
    assert res.ok, res.manifest.error
    lines = res.text("predictions").splitlines()
    assert (
        lines[0] == "pair_id,reference_logit,variant_logit,reference_label,variant_label,stratum,cluster"
        and len(lines) == 41
    )
    assert {row.split(",")[-1] for row in lines[1:]} == {"5"}  # cluster = training seed from the checkpoint
    assert res.receipt["rows"] == 40 and set(res.receipt["metrics"]) == {
        "pairs",
        "both_correct_rate",
        "reference_accuracy",
    }
    assert next(e for e in res.manifest.outputs if e.slot == "predictions").schema == "labeled-binary-pairs/1"


def test_weight_edit_child_is_independent_and_recorded(torch_run):
    import hashlib

    parent = torch_run(*TRAIN, config=train_config(steps=6, seed=5))
    before = hashlib.sha256(parent.path("checkpoint").read_bytes()).hexdigest()
    res = torch_run(
        *WEIGHT_EDIT,
        inputs=ckpt_input(parent),
        config={"edits": [{"entry": "readout.weight", "index": [0, -1], "op": "set", "value": 0.0}]},
    )
    assert res.ok, res.manifest.error
    r = res.receipt
    assert r["parent_unchanged"] and r["storage_independent"] and r["restore_class"] == "warm-start"
    edit = r["edits"][0]
    assert edit["entry"] == "readout.weight" and edit["after"] == 0.0 and edit["before"] != 0.0
    assert r["parent_model_fingerprint"] != r["child_model_fingerprint"]
    assert r["checkpoints"]["checkpoint"]["state_entries"] == entries_of(parent)
    assert (
        r["checkpoints"]["checkpoint"]["step"] == 6
        and r["checkpoints"]["checkpoint"]["builder"] == "switch_order:OrderedSwitchNet"
    )
    assert (
        hashlib.sha256(parent.path("checkpoint").read_bytes()).hexdigest()
        == before
        == r["parent_digest"].split(":")[-1]
    )
    # the edited checkpoint is a valid branch parent: optimizer and component state survive
    cont = torch_run(
        *TRAIN,
        inputs=ckpt_input(res, name="parent_checkpoint"),
        config=train_config(
            steps=8,
            seed=5,
            branch=branch(
                entries_of(parent), restore_class="warm-start", intervention={"kind": "change-weights"}
            ),
        ),
    )
    assert cont.ok and cont.receipt["resumed_from_step"] == 6


def test_weight_edit_rejects_unknown_entries(torch_run):
    parent = torch_run(*TRAIN, config=train_config(steps=2, seed=5))
    res = torch_run(
        *WEIGHT_EDIT, inputs=ckpt_input(parent), config={"edits": [{"entry": "nope", "value": 0}]}
    )
    assert res.manifest.status == "failed" and res.manifest.error["category"] == "contract"


def test_behavior_check_detects_wrong_builder_that_loads_strictly(torch_run):
    parent = torch_run(*TRAIN, config=train_config(steps=6, seed=5))
    wrong = torch_run(
        *BEHAVIOR, inputs=ckpt_input(parent), config={"adapter": ADAPTER, "builder": "SwitchNet"}
    )
    assert wrong.ok, wrong.manifest.error
    assert wrong.receipt["behavior_match"] is False and wrong.receipt["strict_load_ok"] is True
    assert wrong.receipt["max_abs_diff"] > 1e-3 and wrong.receipt["builder"] == "SwitchNet"
    right = torch_run(
        *BEHAVIOR, inputs=ckpt_input(parent), config={"adapter": ADAPTER, "builder": "OrderedSwitchNet"}
    )
    assert right.receipt["behavior_match"] is True and right.receipt["max_abs_diff"] == 0.0


def test_second_model_reuses_scorer_and_reproduces_handoff_audit(torch_run, lab_run):
    from conftest import IMAGE, SCORER

    parent = torch_run(
        *TRAIN, project=IMAGE, config={"adapter": ADAPTER, "builder": "ConvNet", "steps": 80, "seed": 17}
    )
    assert parent.ok, parent.manifest.error
    rows = torch_run(
        *EVALUATE,
        project=IMAGE,
        inputs=ckpt_input(parent),
        config={"adapter": ADAPTER, "evaluation": {"split": "audit", "data_seed": 180, "pairs": 400}},
    )
    assert rows.ok, rows.manifest.error
    report = lab_run(*SCORER, inputs={"predictions": (rows.path("predictions"), "text/csv")}, config={})
    # image_results.json from the handoff: both-correct 0.765 (gain 0.7: 1.0, gain 0.1: 0.53)
    assert report.json("report")["overall"]["both_correct_rate"] == pytest.approx(0.765)
    assert report.json("report")["by_group"]["gain 0.1"]["both_correct_rate"] == pytest.approx(0.53)
    assert report.json("report")["by_group"]["gain 0.7"]["both_correct_rate"] == pytest.approx(1.0)
