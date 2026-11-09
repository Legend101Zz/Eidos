import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
import lab_models  # noqa: E402
from torch import nn  # noqa: E402

from eidos_pytorch import (  # noqa: E402
    AdapterError,
    CheckpointError,
    MigrationError,
    PhaseError,
    PhaseRunner,
    StorageSharedError,
    apply_migration,
    behavioral_restore_check,
    capture_checkpoint,
    compare_outputs,
    devices,
    find_shared_storage,
    load_adapter,
    load_checkpoint,
    record_behavior,
    restore_checkpoint,
    run_training,
    save_checkpoint,
    state_fingerprint,
    train_loop,
    verify_storage_independence,
)
from eidos_pytorch.training import TrainContext  # noqa: E402
from eidos_worker import classify  # noqa: E402

HERE = str(Path(__file__).parent)


def _trained_pair(steps=3):
    model = nn.Linear(4, 3)
    opt = torch.optim.Adam(model.parameters(), lr=0.1)
    for _ in range(steps):
        opt.zero_grad()
        model(torch.randn(5, 4)).pow(2).sum().backward()
        opt.step()
    return model, opt


def test_adapter_loading_and_validation():
    ad = load_adapter("lab_models:SwitchAdapter")
    assert hasattr(ad, "predict")
    with pytest.raises(AdapterError):
        load_adapter("lab_models:SwitchNet")  # a module class without build/predict
    with pytest.raises(AdapterError):
        load_adapter("nocolon")

    class Bad:
        build = staticmethod(lambda c: None)
        predict = staticmethod(lambda m, b: None)
        phases = ("nope",)

    from eidos_pytorch import validate_adapter

    with pytest.raises(AdapterError):
        validate_adapter(Bad())


def test_optimizer_alias_fixture_naive_shares_eidos_does_not():
    parent_model, parent_opt = _trained_pair()
    # Naive in-memory "branch": the design's observed hazard.
    naive_model = copy.deepcopy(parent_model)
    naive_opt = torch.optim.Adam(naive_model.parameters(), lr=0.1)
    naive_opt.load_state_dict(parent_opt.state_dict())
    shared = find_shared_storage(parent_opt, naive_opt)
    assert shared, "expected naive load_state_dict to alias Adam moments (design fixture)"
    with pytest.raises(StorageSharedError):
        verify_storage_independence(parent_opt, naive_opt)
    # Eidos path.
    state = capture_checkpoint(parent_model, parent_opt, step=3)
    child_model = nn.Linear(4, 3)
    child_opt = torch.optim.Adam(child_model.parameters(), lr=0.1)
    restore_checkpoint(state, child_model, child_opt)
    assert (
        verify_storage_independence({"m": parent_model, "o": parent_opt}, {"m": child_model, "o": child_opt})
        > 0
    )
    assert state_fingerprint(child_model) == state_fingerprint(parent_model)
    before = state_fingerprint(parent_opt.state_dict())
    for _ in range(3):
        child_opt.zero_grad()
        child_model(torch.randn(5, 4)).pow(2).sum().backward()
        child_opt.step()
    assert state_fingerprint(parent_opt.state_dict()) == before
    assert state_fingerprint(parent_model) != state_fingerprint(child_model)
    # the captured snapshot is itself independent of the live parent
    assert verify_storage_independence(parent_opt, state["entries"]["optimizer"]) > 0


def test_checkpoint_roundtrip_manifest_and_errors(tmp_path):
    model, opt = _trained_pair()
    sched = torch.optim.lr_scheduler.StepLR(opt, 2)

    class Comp:
        def __init__(self):
            self.v = {"mem": torch.ones(2)}

        def state_dict(self):
            return self.v

        def load_state_dict(self, s):
            self.v = s

    comp = Comp()
    state = capture_checkpoint(
        model, opt, sched, step=7, data_cursor={"next_batch_index": 9}, components={"rare": comp}
    )
    man = state["manifest"]
    assert man["schema"] == "eidos-checkpoint/1" and man["complete"] is True
    assert {
        "model",
        "optimizer",
        "scheduler",
        "rng.python",
        "rng.torch",
        "data_cursor",
        "step",
        "component.rare",
    } <= set(man["state_entries"])
    assert man["parameters"]["weight"] == {"shape": [3, 4], "dtype": "float32"}
    assert man["optimizer_param_names"] == [["weight", "bias"]]
    path = save_checkpoint(state, tmp_path / "a.ckpt")
    loaded = load_checkpoint(path)
    m2 = nn.Linear(4, 3)
    o2 = torch.optim.Adam(m2.parameters())
    s2 = torch.optim.lr_scheduler.StepLR(o2, 2)
    c2 = Comp()
    c2.v = {}
    info = restore_checkpoint(loaded, m2, o2, s2, components={"rare": c2})
    assert info["step"] == 7 and info["data_cursor"] == {"next_batch_index": 9}
    assert (
        torch.equal(c2.v["mem"], comp.v["mem"])
        and c2.v["mem"] is not loaded["entries"]["component.rare"]["mem"]
    )
    assert state_fingerprint(m2) == state_fingerprint(model)
    with pytest.raises(CheckpointError, match="no target"):
        restore_checkpoint(loaded, nn.Linear(4, 3))
    with pytest.raises(CheckpointError, match="component"):
        restore_checkpoint(loaded, m2, o2, s2)
    with pytest.raises(CheckpointError, match="parameter"):
        restore_checkpoint(capture_checkpoint(model), nn.Linear(4, 2))
    inc = capture_checkpoint(model, accumulation_position=1)
    assert inc["manifest"]["complete"] is False
    with pytest.raises(CheckpointError, match="incomplete"):
        restore_checkpoint(inc, nn.Linear(4, 3))
    broken = {"manifest": dict(man, state_entries=["model"]), "entries": state["entries"]}
    with pytest.raises(CheckpointError):
        restore_checkpoint(broken, m2, o2, s2)


def test_rng_restored():
    model = nn.Linear(2, 2)
    import random

    state = capture_checkpoint(model)
    a = (random.random(), torch.rand(1).item())
    restore_checkpoint(state, model)
    assert (random.random(), torch.rand(1).item()) == a


def test_migration_actions_and_rejections():
    model, opt = _trained_pair()
    sched = torch.optim.lr_scheduler.StepLR(opt, 2)
    parent = capture_checkpoint(model, opt, sched, step=5, rng=False)
    parent_fp = state_fingerprint(parent["entries"])

    def reset_head(value, parent_state, spec):
        value["bias"].zero_()
        return value

    def fresh_state(value, parent_state, spec):
        return {"buf": torch.zeros(2)}

    migration = {
        "entries": {
            "model": {"action": "transform", "transform": "reset_head"},
            "optimizer": {"action": "copy"},
            "scheduler": {"action": "drop"},
            "step": {"action": "transform", "transform": "zero_step"},
            "component.new": {"action": "init", "transform": "fresh"},
        },
        "reason": "head reset",
    }
    child = apply_migration(
        parent, migration, {"reset_head": reset_head, "fresh": fresh_state, "zero_step": lambda v, p, s: 0}
    )
    assert state_fingerprint(parent["entries"]) == parent_fp  # parent untouched
    assert set(child["entries"]) == {"model", "optimizer", "step", "component.new"}
    assert torch.all(child["entries"]["model"]["bias"] == 0) and not torch.all(model.bias == 0)
    assert child["manifest"]["migration"]["actions"]["scheduler"] == "drop"
    assert child["manifest"]["migration"]["parent"]["state_fingerprint"] == parent_fp
    assert verify_storage_independence(parent["entries"], child["entries"]) > 0
    m2 = nn.Linear(4, 3)
    restore_checkpoint(
        child,
        m2,
        torch.optim.Adam(m2.parameters()),
        components={"new": type("C", (), {"load_state_dict": lambda s, v: None})()},
        restore_rng_state=False,
    )
    # undeclared parent entry rejected
    bad = {"entries": {k: v for k, v in migration["entries"].items() if k != "scheduler"}}
    with pytest.raises(MigrationError, match="not declared"):
        apply_migration(
            parent, bad, {"reset_head": reset_head, "fresh": fresh_state, "zero_step": lambda v, p, s: 0}
        )
    with pytest.raises(MigrationError, match="transform"):
        apply_migration(parent, migration, {})
    with pytest.raises(MigrationError, match="overwrite"):
        apply_migration(
            parent,
            {"entries": dict(migration["entries"], optimizer={"action": "init", "transform": "fresh"})},
            {"fresh": fresh_state, "reset_head": reset_head, "zero_step": lambda v, p, s: 0},
        )
    with pytest.raises(MigrationError, match="action"):
        apply_migration(
            parent,
            {"entries": dict(migration["entries"], optimizer={"action": "keep"})},
            {"reset_head": reset_head, "fresh": fresh_state, "zero_step": lambda v, p, s: 0},
        )


def test_fingerprints_and_compare():
    m = nn.Linear(2, 2)
    f1 = state_fingerprint(m)
    assert f1 == state_fingerprint(m.state_dict())
    with torch.no_grad():
        m.weight[0, 0] += 1e-7
    assert state_fingerprint(m) != f1
    a = {"__tensor__": "float32", "shape": [2], "data": [1.0, 2.0]}
    b = {"__tensor__": "float32", "shape": [2], "data": [1.0, 2.0 + 1e-7]}
    assert compare_outputs(a, b)["ok"] and not compare_outputs(a, b, atol=0, rtol=0)["ok"]
    assert not compare_outputs(a, {"__tensor__": "float64", "shape": [2], "data": [1.0, 2.0]})["ok"]


def test_fresh_process_behavior_and_wrong_builder(tmp_path):
    ad = load_adapter("lab_models:SwitchAdapter")
    torch.manual_seed(5)
    model = lab_models.SwitchNet()
    recorded = record_behavior(ad, model, {})
    ckpt = save_checkpoint(capture_checkpoint(model, step=0), tmp_path / "w.ckpt")
    ok = behavioral_restore_check(ckpt, "lab_models:SwitchAdapter", {}, recorded, extra_paths=[HERE])
    assert ok.ok and ok.strict_load_ok and ok.max_abs_diff == 0.0
    wrong = behavioral_restore_check(
        ckpt,
        "lab_models:SwitchAdapter",
        {},
        recorded,
        builder_spec="lab_models:build_ordered",
        extra_paths=[HERE],
    )
    assert wrong.strict_load_ok, "strict load must accept the wrong builder (that is the hazard)"
    assert not wrong.ok and wrong.stage == "compare" and wrong.max_abs_diff > 1e-3
    # a builder with different shapes fails at load
    shape = behavioral_restore_check(
        ckpt,
        "lab_models:SwitchAdapter",
        {},
        recorded,
        builder_spec="lab_models:build_small",
        extra_paths=[HERE],
    )
    assert not shape.ok


def test_phase_runner():
    r = PhaseRunner(("after-backward", "before-optimizer-step"))
    calls = []
    r.register("after-backward", lambda **kw: calls.append(("a", kw["step"])), "a")
    r.register("after-backward", lambda **kw: calls.append(("b", kw["step"])), "b")
    with pytest.raises(PhaseError):
        r.register("on-evaluation", lambda **kw: None)
    with pytest.raises(PhaseError):
        r.register("bogus", lambda **kw: None)
    with pytest.raises(PhaseError):
        r.register("after-backward", lambda **kw: None, "a")
    r.fire("after-backward", step=0)
    r.fire("after-backward", step=1)
    assert calls == [("a", 0), ("b", 0), ("a", 1), ("b", 1)]
    assert r.order() == ["after-backward:a", "after-backward:b"]
    r.expect_counts({"after-backward": 2})
    with pytest.raises(PhaseError):
        r.expect_counts({"after-backward": 3})
    assert r.snapshot()["hooks"]["after-backward:a"] == 2


def test_train_loop_receipt_phases_accumulation():
    ad = lab_models.DropoutAdapter()
    seen = []
    runner = PhaseRunner()
    runner.register("before-optimizer-step", lambda step, **kw: seen.append(step), "probe")
    res = train_loop(
        ad, {}, lab_models.dropout_batch_fn, steps=4, batches_per_update=2, phases=runner, seed=1
    )
    r = res.receipt
    assert (r["updates"], r["batches"], r["examples"]) == (4, 8, 64)
    assert r["phase_counts"]["phases"]["after-optimizer-step"] == 4 and seen == [0, 1, 2, 3]
    assert r["phase_counts"]["phases"]["before-model-construction"] == 1
    assert r["peak_memory_bytes"] and r["wall_seconds"] >= 0 and r["final_loss"] is not None


def test_train_step_adapter_and_phase_restriction():
    runner = PhaseRunner()
    runner.register("after-backward", lambda **kw: None, "x")
    with pytest.raises(AdapterError):
        train_loop(lab_models.StepAdapter(), {}, lab_models.dropout_batch_fn, steps=1, phases=runner)
    res = train_loop(
        lab_models.StepAdapter(), {}, lab_models.dropout_batch_fn, steps=3, batches_per_update=2, seed=1
    )
    assert res.receipt["updates"] == 3 and res.receipt["batches"] == 6


def test_non_finite_loss_is_numerical():
    with pytest.raises(Exception) as e:
        train_loop(lab_models.NanAdapter(), {}, lab_models.dropout_batch_fn, steps=1, seed=0)
    assert classify(e.value) == "numerical"


def test_whole_trainer_escape_hatch(tmp_path):
    ctx = TrainContext(phases=PhaseRunner(("on-checkpoint-save",)), checkpoint_dir=str(tmp_path))
    model = run_training(lab_models.WholeTrainer(), {"config": {}, "steps": 3}, ctx)
    assert ctx.receipt["custom_updates"] == 3 and ctx.receipt["phase_counts"]["phases"] == {
        "on-checkpoint-save": 1
    }
    assert len(ctx.checkpoints) == 1 and load_checkpoint(ctx.checkpoints[0])["entries"]["step"] == 3
    assert state_fingerprint(model) == state_fingerprint(restore_into_fresh(ctx.checkpoints[0]))


def restore_into_fresh(path):
    m = lab_models.DropoutNet()
    opt = torch.optim.Adam(m.parameters())
    restore_checkpoint(load_checkpoint(path), m, opt)
    return m


def test_exact_resume_in_fresh_process(tmp_path):
    n, k = 12, 5
    ad = lab_models.DropoutAdapter()
    full = train_loop(ad, {}, lab_models.dropout_batch_fn, steps=n, seed=999, deterministic=True)
    part = train_loop(
        ad,
        {},
        lab_models.dropout_batch_fn,
        steps=k,
        seed=999,
        deterministic=True,
        checkpoint_dir=str(tmp_path),
        checkpoint_every=k,
    )
    assert len(part.checkpoints) == 1
    out = tmp_path / "child.json"
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join([HERE] + [p for p in [os.environ.get("PYTHONPATH")] if p])
    )
    proc = subprocess.run(
        [sys.executable, str(Path(HERE) / "resume_child.py"), part.checkpoints[0], str(n), str(out)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    child = json.loads(out.read_text())
    assert child["receipt"]["resumed_from_step"] == k and child["receipt"]["updates"] == n - k
    assert child["params"] == state_fingerprint(full.model), "resumed params must be bit-identical"
    assert child["opt"] == state_fingerprint(full.optimizer.state_dict())


def test_devices_shape():
    d = devices()
    assert d["cpu"]["available"] and d["exact_restore_across_devices"] is False
    assert {"cuda", "mps", "torch"} <= set(d)


# ---- determinism scope and devices (found on an Apple-Silicon Mac, 2026-11-08) ----------------------
_mps = pytest.mark.skipif(not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()),
                          reason="needs an MPS device")


def _det_state():
    return torch.are_deterministic_algorithms_enabled(), torch.is_deterministic_algorithms_warn_only_enabled()


def test_train_loop_scopes_determinism_and_records_it():
    before = _det_state()
    try:
        torch.use_deterministic_algorithms(False)
        res = train_loop(lab_models.SwitchAdapter(), {}, lab_models.switch_batch_fn, steps=2, seed=1, deterministic=True)
        # a deterministic call must not leave the process deterministic (a later call on a device without
        # deterministic kernels would then fail although it asked for deterministic=False)
        assert _det_state() == (False, False)
        assert res.receipt["determinism"] == {"requested": True, "mode": "strict", "nondeterministic_ops": []}
        assert res.receipt["device"] == "cpu"
        torch.use_deterministic_algorithms(True)
        res = train_loop(lab_models.SwitchAdapter(), {}, lab_models.switch_batch_fn, steps=1, seed=1)
        assert _det_state() == (True, False) and res.receipt["determinism"]["mode"] == "unchanged"
    finally:
        torch.use_deterministic_algorithms(before[0], warn_only=before[1])


def test_restore_rng_notes_mps_state_without_mps(monkeypatch):
    import eidos_pytorch.checkpoint as C
    monkeypatch.setattr(C, "_mps_available", lambda torch: False)
    notes = C.restore_rng({"rng.mps": torch.zeros(4, dtype=torch.uint8)})
    assert notes == ["MPS RNG state present but MPS is unavailable; skipped"]


def _to(device):
    return lambda i: {k: v.to(device) for k, v in lab_models.switch_batch_fn(i).items()}


@_mps
def test_deterministic_training_on_mps_is_warn_only_and_recorded():
    before = _det_state()
    # nn.Embedding backward has no deterministic MPS kernel: strict mode raised RuntimeError here
    res = train_loop(lab_models.SwitchAdapter(), {}, _to("mps"), steps=2, seed=1, deterministic=True, device="mps")
    d = res.receipt["determinism"]
    assert res.receipt["device"] == "mps" and d["requested"] and d["mode"] == "warn-only"
    assert any("embedding" in m for m in d["nondeterministic_ops"]), d
    assert _det_state() == before
    assert next(res.model.parameters()).device.type == "mps"


@_mps
def test_checkpoint_captures_and_restores_the_mps_rng(tmp_path):
    from eidos_pytorch.checkpoint import capture_rng, restore_rng
    torch.mps.manual_seed(7)
    state = capture_rng()
    assert "rng.mps" in state
    a = torch.rand(5, device="mps").cpu()
    assert restore_rng(state) == []
    assert torch.equal(torch.rand(5, device="mps").cpu(), a)


@_mps
def test_checkpoint_saved_on_mps_resumes_on_cpu(tmp_path):
    res = train_loop(lab_models.SwitchAdapter(), {}, _to("mps"), steps=3, seed=1, device="mps",
                     checkpoint_dir=str(tmp_path), checkpoint_at_end=True)
    state = load_checkpoint(res.checkpoints[-1])
    assert all(t.device.type == "cpu" for t in state["entries"]["model"].values())
    cont = train_loop(lab_models.SwitchAdapter(), {}, _to("cpu"), steps=5, seed=1, device="cpu",
                      resume_from=res.checkpoints[-1])
    assert cont.receipt["resumed_from_step"] == 3 and cont.receipt["end_step"] == 5
    assert next(cont.model.parameters()).device.type == "cpu"
