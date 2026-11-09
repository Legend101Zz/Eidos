"""Map MosaicML Composer trainer events onto Eidos training-extension phases.

Eidos phase semantics are defined in ``eidos_pytorch.training``; this module fires the phases whose
meaning holds on Composer 0.32.1 (verified by ``tests/test_bridge.py``) and refuses the rest:

======================  ==============================================================================
Eidos phase             Composer source
======================  ==============================================================================
after-model-construction  ``Event.INIT`` (once per ``Trainer``; also on a resume, before the load)
after-backward          ``Event.AFTER_BACKWARD`` (one microbatch per batch only; refused otherwise)
after-optimizer-step    ``Event.BATCH_END`` (after ``optimizer.step()`` and after the scheduler step)
on-evaluation           ``Event.EVAL_START`` (once per evaluator per evaluation round)
on-checkpoint-save      ``Callback.state_dict()``, which Composer calls once per full checkpoint written
on-checkpoint-load      ``Callback.load_state_dict()``, only when the checkpoint holds a step > 0
======================  ==============================================================================

Events carry ``step`` (updates completed *before* the current one for the update phases, updates
completed so far for evaluation and checkpoint phases, exactly as ``eidos_pytorch.train_loop``),
``model`` (``state.model``) and ``optimizer`` (``state.optimizers[0]``) when it exists.
"""

from __future__ import annotations

from typing import Any

import composer
from composer.core import Callback, Event

from eidos_pytorch.phases import PHASES, PhaseError, PhaseRunner
from eidos_pytorch.training import default_count_examples

__all__ = [
    "PHASE_MAP",
    "SUPPORTED_COMPOSER_VERSIONS",
    "SUPPORTED_PHASES",
    "UNSUPPORTED_PHASES",
    "ComposerVersionError",
    "EidosPhaseBridge",
    "PhaseError",
    "PhaseRunner",
    "check_composer_version",
]

# Composer is archived upstream; 0.32.1 is the final release. Any other version is refused rather
# than guessed at: the event order below was verified on exactly this release.
SUPPORTED_COMPOSER_VERSIONS: tuple[str, ...] = ("0.32.1",)

# Eidos phase -> the Composer mechanism that fires it (strings, so the map can go into a receipt).
PHASE_MAP: dict[str, str] = {
    "after-model-construction": "Event.INIT",
    "after-backward": "Event.AFTER_BACKWARD",
    "after-optimizer-step": "Event.BATCH_END",
    "on-checkpoint-save": "Callback.state_dict",
    "on-checkpoint-load": "Callback.load_state_dict",
    "on-evaluation": "Event.EVAL_START",
}
SUPPORTED_PHASES: tuple[str, ...] = tuple(p for p in PHASES if p in PHASE_MAP)
# Not mapped: before-model-construction (Composer receives an already-built model; nothing runs before
# it), before-optimizer-construction (no event between the model and the optimizer, which is passed in
# or defaulted after INIT), after-gradient-unscale and before-optimizer-step (the only candidate is
# Event.AFTER_TRAIN_BATCH, which is not part of the verified 0.1.0 map).
UNSUPPORTED_PHASES: tuple[str, ...] = tuple(p for p in PHASES if p not in PHASE_MAP)

STATE_SCHEMA = "eidos-composer.state/1"


class ComposerVersionError(RuntimeError):
    """The installed Composer is not a release this adapter was verified against."""


def check_composer_version(version: str | None = None) -> str:
    """Return the Composer version if supported, else raise :class:`ComposerVersionError`."""
    found = str(version if version is not None else getattr(composer, "__version__", "unknown"))
    if found not in SUPPORTED_COMPOSER_VERSIONS:
        raise ComposerVersionError(
            f"eidos-composer supports mosaicml {list(SUPPORTED_COMPOSER_VERSIONS)} only; found {found!r}. "
            "The phase mapping is verified per release (Composer is archived upstream; 0.32.1 is final)."
        )
    return found


def _completed(state: Any) -> int:
    """Optimizer updates (Composer batches) completed so far."""
    return int(state.timestamp.batch)


class EidosPhaseBridge(Callback):
    """Composer callback that fires Eidos phases on a :class:`~eidos_pytorch.phases.PhaseRunner`.

    Pass it to ``Trainer(callbacks=[bridge])``. Extension hooks are registered on ``bridge.runner``
    (by default ``PhaseRunner(supported=SUPPORTED_PHASES)``, which refuses hooks for phases this
    adapter cannot fire) and are called as ``hook(**event)``.

    Extension state in checkpoints: on ``on-checkpoint-save`` the event carries ``state``, a dict with
    a ``components`` mapping; a hook writes its state to ``state["components"][name]`` (picklable
    values; clone tensors). The dict is what Composer stores under ``callbacks["EidosPhaseBridge"]``.
    On a resume the same dict is handed to ``on-checkpoint-load`` and the hook restores itself from
    ``state["components"][name]``.

    Composer 0.32.1 reads checkpoints with ``torch.load(weights_only=False)`` (pickle): only load
    checkpoints this run wrote.
    """

    def __init__(self, runner: PhaseRunner | None = None) -> None:
        check_composer_version()
        super().__init__()
        self.runner = runner if runner is not None else PhaseRunner(SUPPORTED_PHASES)
        self._check_hooks()
        self._state: Any = None
        self._loading = False
        self._backwards_in_batch = 0
        self._loaded_step: int | None = None
        self.resumed_from_step: int | None = None

    # ---- receipt -----------------------------------------------------------------------------
    def snapshot(self) -> dict[str, dict[str, int]]:
        """Phase counts and per-hook invocation counts (``PhaseRunner.snapshot``)."""
        return self.runner.snapshot()

    def receipt(self) -> dict[str, Any]:
        """Realized conditions of this process: versions, the phase map and the counts."""
        from eidos_composer import __version__

        return {
            "adapter": "eidos-composer",
            "adapter_version": __version__,
            "composer_version": check_composer_version(),
            "phase_map": dict(PHASE_MAP),
            "unsupported_phases": list(UNSUPPORTED_PHASES),
            "resumed_from_step": self.resumed_from_step,
            "phase_counts": self.snapshot(),
        }

    # ---- Composer callback surface ----------------------------------------------------------
    def run_event(self, event: Event, state: Any, logger: Any) -> None:
        self._state = state
        if event is Event.INIT:
            self._fire("after-model-construction", state, step=0)
        elif event is Event.BEFORE_LOAD:
            self._loading = True
        elif event is Event.AFTER_LOAD:
            self._loading = False
            self._check_restored_step(state)
        elif event is Event.FIT_START:
            self._check_hooks()
            self._check_unsplit(state)
        elif event is Event.BATCH_START:
            self._backwards_in_batch = 0
            self._check_unsplit(state, batch_size=default_count_examples(state.batch))
        elif event is Event.AFTER_BACKWARD:
            self._backwards_in_batch += 1
            if self._backwards_in_batch > 1:
                raise PhaseError(
                    "Composer fired AFTER_BACKWARD more than once for one batch (gradient accumulation "
                    "over microbatches, or an optimizer that calls its closure repeatedly such as "
                    "LBFGS); Eidos after-backward fires once per update with all gradients accumulated"
                )
            self._fire("after-backward", state, step=_completed(state))
        elif event is Event.BATCH_END:
            # Composer has already advanced the timestamp, so the update just finished is batch - 1.
            self._fire("after-optimizer-step", state, step=max(_completed(state) - 1, 0))
        elif event is Event.EVAL_START:
            self._fire("on-evaluation", state, step=_completed(state))

    def state_dict(self) -> dict[str, Any]:
        """Called by Composer inside ``State.state_dict()`` when a full checkpoint is written."""
        step = _completed(self._state) if self._state is not None else 0
        out: dict[str, Any] = {"schema": STATE_SCHEMA, "step": step, "components": {}}
        # Composer's sharded (FSDP) loader also calls state_dict() to build a load template; that is
        # a load, not a save, so nothing fires between BEFORE_LOAD and AFTER_LOAD.
        if self._state is not None and not self._loading:
            self._fire("on-checkpoint-save", self._state, step=step, state=out)
        return out

    def load_state_dict(self, state: dict[str, Any]) -> None:
        """Called by Composer when it restores a checkpoint that contains this callback's state."""
        if not state:
            return
        if state.get("schema") != STATE_SCHEMA:
            raise PhaseError(
                f"checkpoint extension state has schema {state.get('schema')!r}, want {STATE_SCHEMA!r}"
            )
        step = int(state.get("step", 0))
        if step <= 0:
            return  # not a real resume
        self._loaded_step = step
        self.resumed_from_step = step
        self._fire("on-checkpoint-load", self._state, step=step, state=state)

    # ---- internals --------------------------------------------------------------------------
    def _fire(self, phase: str, trainer_state: Any, /, *, step: int, **extra: Any) -> None:
        """Fire ``phase``; ``extra`` carries the extension ``state`` for the checkpoint phases."""
        event: dict[str, Any] = {"step": step, "model": getattr(trainer_state, "model", None)}
        optimizers = getattr(trainer_state, "optimizers", None)
        if optimizers:
            event["optimizer"] = optimizers[0]
        event.update(extra)
        self.runner.fire(phase, **event)

    def _check_hooks(self) -> None:
        bad = [h for h in self.runner.order() if h.split(":", 1)[0] not in PHASE_MAP]
        if bad:
            raise PhaseError(
                f"hooks registered for phases eidos-composer cannot fire: {bad} (supported: {list(SUPPORTED_PHASES)})"
            )

    def _check_unsplit(self, state: Any, batch_size: int | None = None) -> None:
        """Refuse microbatching: Composer fires AFTER_BACKWARD per microbatch, Eidos once per update."""
        if getattr(state, "auto_microbatching", False):
            raise PhaseError(
                "device_train_microbatch_size='auto' can split batches at run time; eidos-composer needs "
                "one microbatch per batch (after-backward fires once per update)"
            )
        micro = getattr(state, "device_train_microbatch_size", None)
        if batch_size is None:
            batch_size = getattr(getattr(state, "train_dataloader", None), "batch_size", None)
        if micro is not None and batch_size is not None and micro < batch_size:
            raise PhaseError(
                f"device_train_microbatch_size={micro} is smaller than the batch size {batch_size}: Composer "
                "would fire AFTER_BACKWARD per microbatch with partial gradients; eidos-composer needs one "
                "microbatch per batch"
            )

    def _check_restored_step(self, state: Any) -> None:
        if self._loaded_step is not None and _completed(state) != self._loaded_step:
            raise PhaseError(
                f"extension state is from step {self._loaded_step} but Composer restored step {_completed(state)}"
            )
