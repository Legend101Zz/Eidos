"""Hook-phase fixtures for the Composer bridge: every mapped phase is run on a short synthetic job.

Job: 64 samples, batch 16 (4 updates per epoch), 2 epochs, plain SGD, an eval dataloader every epoch,
a checkpoint every epoch, CPU, seed 0. Skipped where Composer (or torch) is not installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace as NS
from typing import Any

import pytest

torch = pytest.importorskip("torch")
composer = pytest.importorskip("composer")

from composer import Trainer  # noqa: E402
from composer.core import Callback, Event  # noqa: E402
from composer.models import ComposerClassifier  # noqa: E402
from torch.utils.data import DataLoader, TensorDataset  # noqa: E402

from eidos_composer import (  # noqa: E402
    PHASE_MAP,
    SUPPORTED_COMPOSER_VERSIONS,
    SUPPORTED_PHASES,
    UNSUPPORTED_PHASES,
    ComposerVersionError,
    EidosPhaseBridge,
    PhaseError,
    PhaseRunner,
    __version__,
    check_composer_version,
)

LR = 0.1
UPDATES_PER_EPOCH = 4
BASELINE_COUNTS = {
    "after-model-construction": 1,
    "after-backward": 8,
    "after-optimizer-step": 8,
    "on-evaluation": 2,
    "on-checkpoint-save": 2,
}
RESUMED_COUNTS = {
    "after-model-construction": 1,
    "on-checkpoint-load": 1,
    "after-backward": 4,
    "after-optimizer-step": 4,
    "on-evaluation": 1,
    "on-checkpoint-save": 1,
}


# ---- the synthetic job ----------------------------------------------------------------------------
def make_model():
    torch.manual_seed(0)
    mlp = torch.nn.Sequential(torch.nn.Linear(8, 16), torch.nn.ReLU(), torch.nn.Linear(16, 2))
    return ComposerClassifier(mlp, num_classes=2)


def make_loader(batch_size: int = 16):
    g = torch.Generator().manual_seed(0)
    x = torch.randn(64, 8, generator=g)
    y = (x[:, 0] > 0).long()
    return DataLoader(TensorDataset(x, y), batch_size=batch_size, shuffle=False)


def make_trainer(bridge, save_dir, *, load_path=None, micro=None, duration="2ep", momentum=0.0):
    model = make_model()
    return Trainer(
        model=model,
        train_dataloader=make_loader(),
        eval_dataloader=make_loader(),
        eval_interval="1ep",
        optimizers=torch.optim.SGD(model.parameters(), lr=LR, momentum=momentum),
        max_duration=duration,
        callbacks=[bridge],
        device="cpu",
        seed=0,
        progress_bar=False,
        log_to_console=False,
        save_folder=str(save_dir),
        save_interval="1ep",
        save_filename="ep{epoch}-ba{batch}-rank{rank}.pt",
        save_latest_filename=None,
        save_overwrite=True,
        load_path=load_path,
        device_train_microbatch_size=micro,
    )


def snap(model) -> list[Any]:
    return [p.detach().clone() for p in model.parameters()]


def flat(model):
    return torch.cat([p.detach().reshape(-1) for p in model.parameters()])


# ---- extensions: a semantics probe and a stateful EMA ---------------------------------------------
class Recorder:
    """Records ``(phase, step)`` for every phase the bridge can fire, in firing order."""

    def __init__(self, runner: PhaseRunner):
        self.events: list[Any] = []
        for phase in SUPPORTED_PHASES:
            runner.register(phase, self._make(phase), name="recorder")

    def _make(self, phase: str):
        def hook(*, step, **_):
            self.events.append((phase, step))

        return hook


class Probe:
    """Checks what a hook sees at after-backward and after-optimizer-step against plain SGD."""

    def __init__(self, runner: PhaseRunner):
        self.last: list[Any] = []
        self.at_backward: dict[str, Any] = {}
        self.updates: list[dict[str, Any]] = []
        runner.register("after-model-construction", self.construct, name="probe")
        runner.register("on-checkpoint-load", self.construct, name="probe")  # weights are restored by then
        runner.register("after-backward", self.backward, name="probe")
        runner.register("after-optimizer-step", self.stepped, name="probe")

    def construct(self, *, step, model, **_):
        self.last = snap(model)

    def backward(self, *, step, model, optimizer=None, **_):
        params = snap(model)
        grads = [None if p.grad is None else p.grad.clone() for p in model.parameters()]
        self.at_backward = {
            "step": step,
            "params": params,
            "grads": grads,
            "optimizer": type(optimizer).__name__,
            "params_equal_pre_update": all(
                torch.equal(a, b) for a, b in zip(params, self.last, strict=False)
            ),
            "has_grads": all(g is not None for g in grads) and any(float(g.abs().sum()) > 0 for g in grads),
        }

    def stepped(self, *, step, model, **_):
        before = self.at_backward
        after = snap(model)
        grads = [p.grad for p in model.parameters()]
        self.updates.append(
            {
                "step": step,
                "backward_step": before["step"],
                "optimizer": before["optimizer"],
                "params_equal_pre_update_at_backward": before["params_equal_pre_update"],
                "has_grads_at_backward": before["has_grads"],
                "params_changed": any(
                    not torch.equal(a, b) for a, b in zip(before["params"], after, strict=False)
                ),
                # plain SGD: new = old - lr * (the whole update's gradient)
                "sgd_relation": all(
                    torch.allclose(n, o - LR * g, atol=1e-6)
                    for n, o, g in zip(after, before["params"], before["grads"], strict=False)
                ),
                "grads_kept": all(
                    g is not None and torch.equal(g, b) for g, b in zip(grads, before["grads"], strict=False)
                ),
            }
        )
        self.last = after


class EmaExtension:
    """A stateful extension: an EMA of the flattened parameters plus an update counter."""

    def __init__(self, runner: PhaseRunner, decay: float = 0.9):
        self.decay = decay
        self.n = 0
        self.ema = None
        self.saved: list[Any] = []
        self.loaded: list[Any] = []
        runner.register("after-optimizer-step", self.update, name="ema-update")
        runner.register("on-checkpoint-save", self.save, name="ema-save")
        runner.register("on-checkpoint-load", self.load, name="ema-load")

    def update(self, *, step, model, **_):
        w = flat(model)
        self.ema = w.clone() if self.ema is None else self.decay * self.ema + (1 - self.decay) * w
        self.n += 1

    def save(self, *, step, state, **_):
        state["components"]["ema"] = {"n": self.n, "ema": self.ema.clone()}
        self.saved.append((step, self.n, self.ema.clone()))

    def load(self, *, step, state, **_):
        payload = state["components"]["ema"]
        self.n = payload["n"]
        self.ema = payload["ema"].clone()
        self.loaded.append((step, payload["n"], payload["ema"].clone()))


@dataclass
class Run:
    bridge: EidosPhaseBridge
    recorder: Recorder
    probe: Probe
    ema: EmaExtension
    trainer: Any
    save_dir: Any
    params: Any = None
    ema_final: Any = None


def run_job(save_dir, *, load_path=None, fit=True) -> Run:
    bridge = EidosPhaseBridge()
    recorder = Recorder(bridge.runner)
    probe = Probe(bridge.runner)
    ema = EmaExtension(bridge.runner)
    trainer = make_trainer(bridge, save_dir, load_path=load_path)
    run = Run(bridge, recorder, probe, ema, trainer, save_dir)
    if fit:
        finish(run)
    return run


def finish(run: Run) -> Run:
    run.trainer.fit()
    run.params = flat(run.trainer.state.model)
    run.ema_final = run.ema.ema.clone()
    return run


@pytest.fixture(scope="module")
def baseline(tmp_path_factory) -> Run:
    return run_job(tmp_path_factory.mktemp("baseline"))


@pytest.fixture(scope="module")
def resumed_construction(baseline, tmp_path_factory):
    """Resume from the epoch-1 checkpoint; stop right after the Trainer is built (the load is done)."""
    run = run_job(
        tmp_path_factory.mktemp("resumed"), load_path=str(baseline.save_dir / "ep1-ba4-rank0.pt"), fit=False
    )
    return run


@pytest.fixture(scope="module")
def resumed(resumed_construction) -> Run:
    return finish(resumed_construction)


# ---- 1. exact phase counts -----------------------------------------------------------------------
def test_phase_counts_on_the_baseline_run(baseline):
    baseline.bridge.runner.expect_counts(BASELINE_COUNTS)
    assert baseline.bridge.snapshot()["phases"] == BASELINE_COUNTS
    # per-hook counts: the recorder is registered on every supported phase
    hooks = baseline.bridge.snapshot()["hooks"]
    assert hooks["after-backward:recorder"] == 8 and hooks["on-checkpoint-save:ema-save"] == 2
    # a fresh run fires BEFORE_LOAD/AFTER_LOAD too (Composer does, even without load_path) but is no resume
    assert baseline.bridge.runner.counts["on-checkpoint-load"] == 0


def test_phase_order_and_steps_on_the_baseline_run(baseline):
    want: list[Any] = [("after-model-construction", 0)]
    for epoch in range(2):
        for i in range(UPDATES_PER_EPOCH):
            step = epoch * UPDATES_PER_EPOCH + i
            want += [("after-backward", step), ("after-optimizer-step", step)]
        end = (epoch + 1) * UPDATES_PER_EPOCH
        want += [("on-evaluation", end), ("on-checkpoint-save", end)]
    assert baseline.recorder.events == want


def test_checkpoint_files_hold_the_extension_state(baseline):
    files = sorted(p.name for p in baseline.save_dir.iterdir())
    assert files == ["ep1-ba4-rank0.pt", "ep2-ba8-rank0.pt"]
    ckpt = torch.load(baseline.save_dir / "ep1-ba4-rank0.pt", weights_only=False)  # our own file
    ours = ckpt["state"]["callbacks"]["EidosPhaseBridge"]
    assert ours["schema"] == "eidos-composer.state/1" and ours["step"] == 4
    assert ours["components"]["ema"]["n"] == 4
    assert torch.equal(ours["components"]["ema"]["ema"], baseline.ema.saved[0][2])


# ---- 2. semantics --------------------------------------------------------------------------------
def test_after_backward_sees_whole_update_gradients_and_pre_update_parameters(baseline):
    ups = baseline.probe.updates
    assert [u["step"] for u in ups] == list(range(8)) == [u["backward_step"] for u in ups]
    assert all(u["optimizer"] == "SGD" for u in ups)
    assert all(u["has_grads_at_backward"] for u in ups)
    assert all(u["params_equal_pre_update_at_backward"] for u in ups)


def test_after_optimizer_step_sees_updated_parameters(baseline):
    ups = baseline.probe.updates
    assert all(u["params_changed"] for u in ups)
    # new parameters are exactly old - lr * (gradient seen at after-backward): that gradient was the update's
    assert all(u["sgd_relation"] for u in ups)
    assert all(u["grads_kept"] for u in ups)  # Composer zeroes gradients at the start of the next batch


# ---- 3. restore ----------------------------------------------------------------------------------
def test_resume_fires_on_checkpoint_load_once_with_the_saved_state(baseline, resumed_construction):
    run = resumed_construction
    # the load has already happened inside Trainer.__init__
    assert run.bridge.runner.counts["on-checkpoint-load"] == 1
    assert run.bridge.runner.counts["after-model-construction"] == 1
    assert int(run.trainer.state.timestamp.batch) == 4
    ((step, n, ema),) = run.ema.loaded
    saved_step, saved_n, saved_ema = baseline.ema.saved[0]
    assert (step, n) == (saved_step, saved_n) == (4, 4)
    assert torch.equal(ema, saved_ema)
    # the extension is restored to exactly what it saved
    assert run.ema.n == 4 and torch.equal(run.ema.ema, saved_ema)
    assert run.bridge.resumed_from_step == 4


def test_resumed_run_reaches_the_uninterrupted_final_state(baseline, resumed):
    resumed.bridge.runner.expect_counts(RESUMED_COUNTS)
    assert [e for e in resumed.recorder.events if e[0] == "after-backward"] == [
        ("after-backward", s) for s in range(4, 8)
    ]
    assert resumed.recorder.events[0] == ("after-model-construction", 0)
    assert resumed.recorder.events[1] == ("on-checkpoint-load", 4)
    assert resumed.ema.n == 8 == baseline.ema.n
    assert torch.allclose(resumed.params, baseline.params, atol=1e-6)
    assert torch.allclose(resumed.ema_final, baseline.ema_final, atol=1e-6)
    assert all(u["params_equal_pre_update_at_backward"] and u["sgd_relation"] for u in resumed.probe.updates)


def test_receipt_records_versions_map_and_counts(baseline, resumed):
    r = baseline.bridge.receipt()
    assert r["composer_version"] == composer.__version__ == "0.32.1"
    assert r["adapter_version"] == __version__ == "0.1.0"
    assert r["phase_map"] == PHASE_MAP and r["unsupported_phases"] == list(UNSUPPORTED_PHASES)
    assert r["phase_counts"]["phases"] == BASELINE_COUNTS and r["resumed_from_step"] is None
    assert resumed.bridge.receipt()["resumed_from_step"] == 4
    json.dumps(r)  # receipts must be JSON


def test_what_is_restored_when_on_checkpoint_load_fires(tmp_path):
    """Composer 0.32.1 loads callbacks in sorted key order, after the model and before the optimizer and
    the timestamp: a load hook sees restored weights but not yet restored optimizer state or step."""
    first = make_trainer(EidosPhaseBridge(), tmp_path / "a", duration="1ep", momentum=0.9)
    first.fit()
    saved_weights = flat(first.state.model)
    assert len(first.state.optimizers[0].state) > 0  # momentum buffers exist
    seen: dict[str, Any] = {}
    bridge = EidosPhaseBridge()

    def on_load(*, step, model, optimizer, state, **_):
        seen["weights_restored"] = torch.equal(flat(model), saved_weights)
        seen["optimizer_state_entries"] = len(optimizer.state)
        seen["timestamp_batch"] = int(bridge._state.timestamp.batch)

    bridge.runner.register("on-checkpoint-load", on_load, name="order")
    trainer = make_trainer(
        bridge,
        tmp_path / "b",
        load_path=str(tmp_path / "a" / "ep1-ba4-rank0.pt"),
        duration="1ep",
        momentum=0.9,
    )
    assert seen == {"weights_restored": True, "optimizer_state_entries": 0, "timestamp_batch": 0}
    assert len(trainer.state.optimizers[0].state) > 0 and int(trainer.state.timestamp.batch) == 4


# ---- 4. refusals ---------------------------------------------------------------------------------
def test_phase_map_and_supported_sets_are_exact():
    assert (
        tuple(PHASE_MAP)
        == SUPPORTED_PHASES
        == (
            "after-model-construction",
            "after-backward",
            "after-optimizer-step",
            "on-checkpoint-save",
            "on-checkpoint-load",
            "on-evaluation",
        )
    )
    assert set(UNSUPPORTED_PHASES) == {
        "before-model-construction",
        "before-optimizer-construction",
        "after-gradient-unscale",
        "before-optimizer-step",
    }
    assert not set(UNSUPPORTED_PHASES) & set(SUPPORTED_PHASES)


@pytest.mark.parametrize("phase", UNSUPPORTED_PHASES)
def test_unsupported_phases_are_refused_at_registration(phase):
    for runner in (PhaseRunner(supported=SUPPORTED_PHASES), EidosPhaseBridge().runner):
        with pytest.raises(PhaseError, match="does not fire"):
            runner.register(phase, lambda **_: None, name="x")


def test_a_broad_runner_with_unsupported_hooks_is_refused():
    broad = PhaseRunner()  # supports every Eidos phase
    broad.register("before-optimizer-step", lambda **_: None, name="x")
    with pytest.raises(PhaseError, match="cannot fire"):
        EidosPhaseBridge(broad)
    late = PhaseRunner()
    bridge = EidosPhaseBridge(late)
    late.register("after-gradient-unscale", lambda **_: None, name="y")  # registered after construction
    with pytest.raises(PhaseError, match="cannot fire"):
        bridge.run_event(Event.FIT_START, NS(timestamp=NS(batch=0)), None)


def test_microbatching_is_refused_at_fit_start(tmp_path):
    bridge = EidosPhaseBridge()
    trainer = make_trainer(bridge, tmp_path, micro=8)
    with pytest.raises(PhaseError, match="device_train_microbatch_size=8"):
        trainer.fit()
    assert bridge.snapshot()["phases"] == {"after-model-construction": 1}  # nothing fired from training


def test_why_microbatching_is_refused(tmp_path):
    """Composer fires AFTER_BACKWARD per microbatch (16 for 4 updates of 2 microbatches) but once per batch
    for AFTER_TRAIN_BATCH; Eidos after-backward is once per update, so the bridge cannot map the former."""

    class Count(Callback):
        def __init__(self):
            self.n: dict[str, int] = {}

        def run_event(self, event, state, logger):
            self.n[event.name] = self.n.get(event.name, 0) + 1

    model = make_model()
    counter = Count()
    Trainer(
        model=model,
        train_dataloader=make_loader(),
        optimizers=torch.optim.SGD(model.parameters(), lr=LR),
        max_duration="1ep",
        callbacks=[counter],
        device="cpu",
        seed=0,
        progress_bar=False,
        log_to_console=False,
        device_train_microbatch_size=8,
    ).fit()
    assert counter.n["BATCH_END"] == 4
    assert counter.n["AFTER_BACKWARD"] == 8 and counter.n["AFTER_TRAIN_BATCH"] == 4


def test_auto_and_repeated_backward_are_refused():
    bridge = EidosPhaseBridge()
    auto = NS(timestamp=NS(batch=0), auto_microbatching=True, device_train_microbatch_size=16)
    with pytest.raises(PhaseError, match="auto"):
        bridge.run_event(Event.FIT_START, auto, None)
    state = NS(
        timestamp=NS(batch=0),
        auto_microbatching=False,
        device_train_microbatch_size=8,
        model=None,
        optimizers=[],
        batch=torch.zeros(16, 2),
    )
    with pytest.raises(PhaseError, match="smaller than the batch size 16"):
        bridge.run_event(
            Event.BATCH_START, state, None
        )  # per-batch check works without a dataloader batch_size
    state.device_train_microbatch_size = 16
    bridge.run_event(Event.BATCH_START, state, None)
    bridge.run_event(Event.AFTER_BACKWARD, state, None)
    with pytest.raises(PhaseError, match="more than once"):
        bridge.run_event(Event.AFTER_BACKWARD, state, None)  # LBFGS closure or microbatches
    assert bridge.runner.counts["after-backward"] == 1


def test_checkpoint_calls_during_a_load_are_not_saves():
    bridge = EidosPhaseBridge()
    state = NS(timestamp=NS(batch=4), model=None, optimizers=[])
    bridge.run_event(Event.BEFORE_LOAD, state, None)
    assert bridge.state_dict()["step"] == 4  # sharded loads ask for a template
    assert bridge.runner.counts["on-checkpoint-save"] == 0
    bridge.run_event(Event.AFTER_LOAD, state, None)
    bridge.state_dict()
    assert bridge.runner.counts["on-checkpoint-save"] == 1


def test_only_a_real_resume_with_matching_step_fires_a_load():
    bridge = EidosPhaseBridge()
    bridge.run_event(Event.INIT, NS(timestamp=NS(batch=0), model=None, optimizers=[]), None)
    bridge.load_state_dict({})  # a checkpoint without our state
    bridge.load_state_dict({"schema": "eidos-composer.state/1", "step": 0, "components": {}})
    assert bridge.runner.counts["on-checkpoint-load"] == 0
    with pytest.raises(PhaseError, match="schema"):
        bridge.load_state_dict({"schema": "other", "step": 3, "components": {}})
    bridge.load_state_dict({"schema": "eidos-composer.state/1", "step": 3, "components": {}})
    assert bridge.runner.counts["on-checkpoint-load"] == 1
    with pytest.raises(PhaseError, match="step 3 but Composer restored step 5"):
        bridge.run_event(Event.AFTER_LOAD, NS(timestamp=NS(batch=5), model=None, optimizers=[]), None)


# ---- 5. version check ----------------------------------------------------------------------------
def test_supported_version_passes():
    assert SUPPORTED_COMPOSER_VERSIONS == ("0.32.1",)
    assert check_composer_version("0.32.1") == "0.32.1"
    assert check_composer_version() == composer.__version__
    EidosPhaseBridge()


@pytest.mark.parametrize("version", ["0.32.0", "0.33.0", "0.31.9", "1.0.0", "0.32.1.dev0"])
def test_unsupported_composer_version_is_refused(monkeypatch, version):
    monkeypatch.setattr(composer, "__version__", version)
    with pytest.raises(ComposerVersionError, match="0.32.1"):
        check_composer_version()
    with pytest.raises(ComposerVersionError):
        EidosPhaseBridge()
