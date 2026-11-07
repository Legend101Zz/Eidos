"""A small generic training loop, determinism helper and the whole-trainer escape hatch.

Update semantics (generic path, ``adapter.loss``): for each update, ``batches_per_update`` batches
are drawn, each loss is divided by that count and back-propagated (gradient accumulation), then one
optimizer step is taken. Phases fire as:

``before-model-construction`` -> ``after-model-construction`` -> ``before-optimizer-construction``
once at start; per update ``after-backward`` (gradients accumulated, still scaled if a GradScaler
is used), ``after-gradient-unscale`` (with a scaler: after ``unscale_``; without one it fires
immediately after ``after-backward`` and sees identical gradients), ``before-optimizer-step``,
``after-optimizer-step``; ``on-checkpoint-save`` / ``on-checkpoint-load`` around checkpoint I/O;
``on-evaluation`` before each ``evaluate`` call. Events always carry ``step`` = number of updates
completed *before* the current one (0-based update index), plus ``model`` (and ``optimizer`` once
it exists). With ``adapter.train_step`` the author owns the update, so only the construction,
checkpoint and evaluation phases fire (``TRAIN_STEP_PHASES``).

Data: ``batch_fn(index) -> batch`` must be a pure function of the global batch index (and the
seed it closes over). The cursor — the next batch index — is checkpointed, which is what makes
resume exact. Loaders that cannot be re-positioned cannot resume exactly.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from eidos_pytorch._util import torch_module
from eidos_pytorch.adapter import AdapterError, validate_adapter
from eidos_pytorch.checkpoint import (
    capture_checkpoint,
    load_checkpoint,
    restore_checkpoint,
    save_checkpoint,
)
from eidos_pytorch.phases import PhaseRunner
from eidos_worker.errors import check_finite

GENERIC_PHASES = (
    "before-model-construction",
    "after-model-construction",
    "before-optimizer-construction",
    "after-backward",
    "after-gradient-unscale",
    "before-optimizer-step",
    "after-optimizer-step",
    "on-checkpoint-save",
    "on-checkpoint-load",
    "on-evaluation",
)
TRAIN_STEP_PHASES = (
    "before-model-construction",
    "after-model-construction",
    "before-optimizer-construction",
    "on-checkpoint-save",
    "on-checkpoint-load",
    "on-evaluation",
)


def seed_everything(seed: int, deterministic: bool = True) -> None:
    """Seed Python, numpy (if importable) and torch; optionally force deterministic algorithms."""
    torch = torch_module()
    random.seed(seed)
    try:
        import numpy

        numpy.random.seed(seed % (2**32))
    except ImportError:
        pass
    torch.manual_seed(seed)
    if deterministic:
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        torch.use_deterministic_algorithms(True)


def default_count_examples(batch: Any) -> int:
    """Best-effort example count: tensor -> dim 0; mapping/sequence -> first element's count; else 1."""
    if hasattr(batch, "shape") and len(batch.shape) > 0:
        return int(batch.shape[0])
    if isinstance(batch, Mapping):
        return default_count_examples(next(iter(batch.values()))) if batch else 0
    if isinstance(batch, (list, tuple)) and batch:
        return default_count_examples(batch[0])
    if hasattr(batch, "__dataclass_fields__"):
        first = next(iter(batch.__dataclass_fields__))
        return default_count_examples(getattr(batch, first))
    return 1


def peak_memory() -> Dict[str, Any]:
    torch = torch_module()
    if torch.cuda.is_available():
        return {
            "peak_memory_bytes": int(torch.cuda.max_memory_allocated()),
            "peak_memory_kind": "cuda_allocated",
        }
    try:
        import resource
        import sys

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "peak_memory_bytes": int(rss if sys.platform == "darwin" else rss * 1024),
            "peak_memory_kind": "process_rss",
        }
    except ImportError:
        return {"peak_memory_bytes": None, "peak_memory_kind": "unavailable"}


@dataclass
class TrainResult:
    model: Any
    optimizer: Any
    scheduler: Any
    step: int
    receipt: Dict[str, Any]
    checkpoints: List[str] = field(default_factory=list)


def train_loop(
    adapter: Any,
    config: Dict[str, Any],
    batch_fn: Callable[[int], Any],
    *,
    steps: int,
    batches_per_update: int = 1,
    checkpoint_every: int = 0,
    checkpoint_dir: Optional[str] = None,
    checkpoint_at_end: bool = False,
    resume_from: Optional[str] = None,
    phases: Optional[PhaseRunner] = None,
    components: Optional[Mapping[str, Any]] = None,
    scaler: Any = None,
    evaluate: Optional[Callable[[Any, int], Any]] = None,
    evaluate_every: int = 0,
    count_examples: Callable[[Any], int] = default_count_examples,
    seed: Optional[int] = None,
    deterministic: bool = False,
    device: Optional[str] = None,
) -> TrainResult:
    """Train until ``steps`` total updates (absolute, so a resumed run continues to the same target).

    Returns a :class:`TrainResult` whose ``receipt`` records realized conditions of *this* process:
    ``start_step``, ``end_step``, ``updates``, ``batches``, ``examples``, ``wall_seconds``,
    ``peak_memory_bytes``/``peak_memory_kind``, ``phase_counts``, ``resumed_from_step``,
    ``final_loss`` (generic path) and ``evaluations``. Raises ``NonFiniteError`` (category
    ``numerical``) if a loss is NaN/inf.
    """
    torch = torch_module()
    caps = validate_adapter(adapter)
    use_step_fn = callable(getattr(adapter, "train_step", None))
    if not use_step_fn and not callable(getattr(adapter, "loss", None)):
        raise AdapterError("generic training needs adapter.loss or adapter.train_step")
    if not callable(getattr(adapter, "make_optimizer", None)):
        raise AdapterError("generic training needs adapter.make_optimizer")
    supported = TRAIN_STEP_PHASES if use_step_fn else GENERIC_PHASES
    if phases is None:
        phases = PhaseRunner(supported)
    else:
        if any(h.split(":")[0] not in supported for h in phases.order()):
            raise AdapterError(f"phases not fired by this training path: {phases.order()}")
    comps = dict(components or {})
    started = time.time()
    if seed is not None:
        seed_everything(seed, deterministic)
    elif deterministic:
        torch.use_deterministic_algorithms(True)
    phases.fire("before-model-construction", step=0, config=config)
    model = adapter.build(config)
    if device is not None:
        model = model.to(device)
    phases.fire("after-model-construction", step=0, model=model)
    phases.fire("before-optimizer-construction", step=0, model=model)
    optimizer = adapter.make_optimizer(model, config)
    scheduler = (
        adapter.make_scheduler(optimizer, config)
        if callable(getattr(adapter, "make_scheduler", None))
        else None
    )
    step, cursor, resumed_from = 0, 0, None
    if resume_from is not None:
        state = load_checkpoint(resume_from)
        phases.fire(
            "on-checkpoint-load", step=state["entries"]["step"], model=model, optimizer=optimizer, state=state
        )
        info = restore_checkpoint(state, model, optimizer, scheduler, scaler, components=comps)
        step, resumed_from = info["step"], info["step"]
        cursor = (info["data_cursor"] or {}).get("next_batch_index", 0)
    start_step = step
    model.train()
    batches = examples = 0
    final_loss: Optional[float] = None
    evaluations: List[Dict[str, Any]] = []
    checkpoints: List[str] = []

    def checkpoint() -> None:
        state = capture_checkpoint(
            model,
            optimizer,
            scheduler,
            scaler,
            step=step,
            data_cursor={"next_batch_index": cursor},
            components=comps,
        )
        phases.fire("on-checkpoint-save", step=step, model=model, optimizer=optimizer, state=state)
        if checkpoint_dir is None:
            raise AdapterError("checkpoint_every/checkpoint_at_end need checkpoint_dir")
        os.makedirs(checkpoint_dir, exist_ok=True)
        checkpoints.append(save_checkpoint(state, os.path.join(checkpoint_dir, f"step-{step:08d}.ckpt")))

    while step < steps:
        group = [batch_fn(cursor + i) for i in range(batches_per_update)]
        cursor += batches_per_update
        batches += len(group)
        examples += sum(count_examples(b) for b in group)
        if use_step_fn:
            out = adapter.train_step(model, group, optimizer, step)
            if isinstance(out, Mapping) and "loss" in out:
                out = out["loss"]
            if isinstance(out, (int, float)) or hasattr(out, "item"):
                final_loss = float(check_finite(out, "loss"))
        else:
            optimizer.zero_grad(set_to_none=True)
            total = 0.0
            for b in group:
                loss = adapter.loss(model, b)
                check_finite(loss, "loss")
                total += float(loss.detach())
                scaled = loss / len(group)
                (scaler.scale(scaled) if scaler is not None else scaled).backward()
            final_loss = total / len(group)
            phases.fire("after-backward", step=step, model=model, optimizer=optimizer)
            if scaler is not None:
                scaler.unscale_(optimizer)
            phases.fire("after-gradient-unscale", step=step, model=model, optimizer=optimizer)
            phases.fire("before-optimizer-step", step=step, model=model, optimizer=optimizer)
            if scaler is not None:
                scaler.step(optimizer)
                scaler.update()
            else:
                optimizer.step()
            phases.fire("after-optimizer-step", step=step, model=model, optimizer=optimizer)
            if scheduler is not None:
                scheduler.step()
        step += 1
        if evaluate is not None and evaluate_every and step % evaluate_every == 0:
            phases.fire("on-evaluation", step=step, model=model)
            evaluations.append({"step": step, "result": evaluate(model, step)})
        if checkpoint_every and step % checkpoint_every == 0:
            checkpoint()
    if checkpoint_at_end and not (checkpoints and checkpoints[-1].endswith(f"step-{step:08d}.ckpt")):
        checkpoint()
    receipt: Dict[str, Any] = {
        "start_step": start_step,
        "end_step": step,
        "resumed_from_step": resumed_from,
        "updates": step - start_step,
        "batches": batches,
        "examples": examples,
        "wall_seconds": round(time.time() - started, 6),
        "phase_counts": phases.snapshot(),
        "final_loss": final_loss,
        "evaluations": evaluations,
        "adapter_capabilities": caps["optional"],
        **peak_memory(),
    }
    return TrainResult(model, optimizer, scheduler, step, receipt, checkpoints)


@dataclass
class TrainContext:
    """Handed to ``adapter.train(plan, context)`` — the whole-trainer escape hatch.

    ``record(key, value)`` stores realized conditions in ``receipt``; ``checkpoint(...)`` captures
    and saves a checkpoint at a boundary the trainer actually supports (and fires
    ``on-checkpoint-save``); ``phases`` is the :class:`PhaseRunner` for any extensions; ``run``
    is the worker ``RunContext`` when running under the SDK runner (else ``None``).
    """

    phases: PhaseRunner
    checkpoint_dir: Optional[str] = None
    run: Any = None
    receipt: Dict[str, Any] = field(default_factory=dict)
    checkpoints: List[str] = field(default_factory=list)

    def record(self, key: str, value: Any) -> None:
        self.receipt[key] = value
        if self.run is not None:
            self.run.record(key, value)

    def checkpoint(
        self, model: Any, optimizer: Any = None, scheduler: Any = None, scaler: Any = None, **kwargs: Any
    ) -> str:
        if self.checkpoint_dir is None:
            raise AdapterError("TrainContext has no checkpoint_dir")
        state = capture_checkpoint(model, optimizer, scheduler, scaler, **kwargs)
        self.phases.fire("on-checkpoint-save", step=state["manifest"]["step"], model=model, state=state)
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        path = save_checkpoint(
            state, os.path.join(self.checkpoint_dir, f"step-{state['manifest']['step']:08d}.ckpt")
        )
        self.checkpoints.append(path)
        return path


def run_training(
    adapter: Any,
    plan: Dict[str, Any],
    context: Optional[TrainContext] = None,
    batch_fn: Optional[Callable[[int], Any]] = None,
) -> Any:
    """Train with ``adapter.train(plan, context)`` if the adapter defines it (the researcher's own
    trainer owns the optimizer, schedule, loop and what a "step" is), else with :func:`train_loop`.

    ``plan`` is a plain dict; for the generic path it carries ``config``, ``steps`` and the other
    ``train_loop`` keyword arguments by name. The escape-hatch return value is passed through; its
    wall time and peak memory are added to ``context.receipt`` if the trainer did not record them.
    """
    context = context or TrainContext(phases=PhaseRunner(getattr(adapter, "phases", None) or None))
    if callable(getattr(adapter, "train", None)):
        started = time.time()
        result = adapter.train(plan, context)
        context.receipt.setdefault("wall_seconds", round(time.time() - started, 6))
        for k, v in peak_memory().items():
            context.receipt.setdefault(k, v)
        context.receipt.setdefault("phase_counts", context.phases.snapshot())
        return result
    if batch_fn is None:
        raise AdapterError("generic training needs batch_fn")
    plan = dict(plan)
    config = plan.pop("config", {})
    result = train_loop(
        adapter, config, batch_fn, phases=context.phases, checkpoint_dir=context.checkpoint_dir, **plan
    )
    context.receipt.update(result.receipt)
    context.checkpoints.extend(result.checkpoints)
    return result
