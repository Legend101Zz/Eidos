"""Train (or resume / branch) a researcher's model through its Eidos adapter.

See ``method-card.md`` for the config keys. The trainer is generic: nothing here knows what the model
or task is. Everything task-specific comes from the project's adapter (``config["adapter"]``).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from eidos_pytorch import (
    PhaseRunner,
    TrainContext,
    apply_migration,
    load_adapter,
    load_checkpoint,
    resolve_callable,
    run_training,
    save_checkpoint,
    seed_everything,
    state_fingerprint,
)
from eidos_pytorch._util import clone_tree
from eidos_pytorch.training import GENERIC_PHASES, TRAIN_STEP_PHASES
from eidos_worker.errors import ContractError

META = "train-meta"
META_ENTRY = f"component.{META}"
STATEFUL_TARGETS = ("optimizer", "scheduler", "grad_scaler")


class RunMeta:
    """Owned state ``train-meta/1``: what is needed to continue or evaluate this trajectory."""

    SCHEMA = "train-meta/1"

    def __init__(self, **data: Any):
        self.data: dict[str, Any] = {"schema": self.SCHEMA, **data}

    def state_dict(self) -> dict[str, Any]:
        return dict(self.data)

    def load_state_dict(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != self.SCHEMA:
            raise ContractError(f"unsupported train-meta schema {state.get('schema')!r}")
        self.data = dict(state)


class Probe:
    """Lets extensions see the batches the loss was computed on in the current update."""

    def __init__(self, maxlen: int):
        from collections import deque

        self._batches: Any = deque(maxlen=max(1, maxlen))

    def push(self, batch: Any) -> None:
        self._batches.append(batch)

    def take_batches(self) -> list[Any]:
        out = list(self._batches)
        self._batches.clear()
        return out


class ExtensionContext:
    def __init__(self, adapter: Any, probe: Probe, config: dict[str, Any]):
        self.adapter, self.probe, self.run_config = adapter, probe, config

    def take_batches(self) -> list[Any]:
        return self.probe.take_batches()


class RecordingAdapter:
    """Delegates to the adapter, remembering the batches passed to ``loss`` for extensions."""

    def __init__(self, inner: Any, probe: Probe):
        self._inner, self._probe = inner, probe

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name == "loss":

            def loss(model, batch):
                self._probe.push(batch)
                return attr(model, batch)

            return loss
        return attr


def _extend_path(config: Mapping[str, Any], context: Any) -> None:
    base = context.invocation.component.get("source_dir") or "."
    entries = list(config.get("python_path") or [])
    port = context.invocation.ports.get("extension.source/1")
    if isinstance(port, Mapping):
        entries += [port["path"]] if "path" in port else list(port.get("paths", []))
    for entry in entries:
        path = os.path.abspath(entry if os.path.isabs(entry) else os.path.join(base, entry))
        if path not in sys.path:
            sys.path.insert(0, path)


def _normalize_migration(
    parent_entries: Mapping[str, Any], branch: Mapping[str, Any] | None
) -> dict[str, Any]:
    declared = dict((branch or {}).get("migration") or {name: "copy" for name in parent_entries})
    entries: dict[str, Any] = {}
    for name, spec in declared.items():
        spec = {"action": spec} if isinstance(spec, str) else dict(spec)
        item: dict[str, Any] = {"action": spec.get("action"), "entry": name}
        if item["action"] in ("transform", "init"):
            item["transform"] = "mapping"
            item["mapping"] = spec.get("mapping") or {}
        entries[name] = item
    return {"entries": entries, "reason": (branch or {}).get("reason")}


class Session:
    """Everything one training execution needs, built fresh for each execution."""

    def __init__(
        self, adapter: Any, config: Mapping[str, Any], build_cfg: dict[str, Any], batches_per_update: int
    ):
        self.config, self.build_cfg = config, build_cfg
        self.base_adapter = adapter
        extensions = list(config.get("extensions") or [])
        self.probe = Probe(batches_per_update)
        self.adapter = RecordingAdapter(adapter, self.probe) if extensions else adapter
        if callable(getattr(adapter, "train_step", None)):
            supported = TRAIN_STEP_PHASES
        elif callable(getattr(adapter, "train", None)):
            supported = tuple(getattr(adapter, "phases", None) or GENERIC_PHASES)
        else:
            supported = GENERIC_PHASES
        self.phases = PhaseRunner(supported)
        self.meta = RunMeta()
        self.components: dict[str, Any] = {META: self.meta}
        self.extensions: dict[str, Any] = {}
        for ext in extensions:
            name = ext.get("name") or ext["spec"].split(":")[-1]
            if name in self.components or name in self.extensions:
                raise ContractError(f"duplicate extension name {name!r}")
            obj = resolve_callable(ext["spec"])(
                dict(ext.get("config") or {}), ExtensionContext(adapter, self.probe, build_cfg)
            )
            self.extensions[name] = obj
            for phase in ext.get("phases") or list(getattr(obj, "phases", ())):
                self.phases.register(phase, _bind(obj, phase), name=name)
            if callable(getattr(obj, "state_dict", None)):
                self.components[name] = obj

    def fresh_state(self, entry: str, seed: int) -> Any:
        """A fresh value for ``entry`` (used by 'init' and 'transform {fresh}' migrations)."""
        if entry == "step":
            return 0
        if entry == "data_cursor":
            return {"next_batch_index": 0}
        if entry.startswith("component."):
            name = entry.split(".", 1)[1]
            if name == META:
                return self.meta.state_dict()
            if name in self.components:
                return clone_tree(self.components[name].state_dict())
            raise ContractError(f"no configured component named {name!r} to initialise {entry!r}")
        if entry in ("model", "optimizer", "scheduler"):
            seed_everything(seed, deterministic=True)
            model = self.base_adapter.build(self.build_cfg)
            if entry == "model":
                return clone_tree(model.state_dict())
            optimizer = self.base_adapter.make_optimizer(model, self.build_cfg)
            if entry == "optimizer":
                return clone_tree(optimizer.state_dict())
            if not callable(getattr(self.base_adapter, "make_scheduler", None)):
                raise ContractError("adapter has no make_scheduler")
            return clone_tree(self.base_adapter.make_scheduler(optimizer, self.build_cfg).state_dict())
        raise ContractError(f"state entry {entry!r} has no fresh initialisation; drop it instead")


def _bind(obj: Any, phase: str):
    def hook(**event: Any) -> None:
        obj.on_phase(phase, **event)

    return hook


def _mapping_transform(session: Session, seed: int):
    def transform(value: Any, parent_state: Mapping[str, Any], spec: Mapping[str, Any]) -> Any:
        entry, mapping = spec["entry"], spec.get("mapping") or {}
        if "value" in mapping:
            return clone_tree(mapping["value"])
        fresh = mapping.get("fresh")
        if fresh is True or (fresh is None and value is None):
            return session.fresh_state(entry, seed)
        if isinstance(fresh, list) and entry == "model":
            new = session.fresh_state("model", seed)
            out = clone_tree(value)
            for name in fresh:
                if name not in out:
                    raise ContractError(f"model has no entry {name!r} to re-initialise")
                out[name] = new[name]
            return out
        raise ContractError(f"unsupported transform mapping for {entry!r}: {dict(mapping)!r}")

    return transform


def _train_once(
    session: Session,
    steps: int,
    seed: int,
    plan_extra: dict[str, Any],
    ckpt_dir: str,
    run: Any,
    batch_fn: Any,
):
    ctx = TrainContext(phases=session.phases, checkpoint_dir=ckpt_dir, run=run)
    plan = {
        "config": session.build_cfg,
        "steps": steps,
        "batches_per_update": int(session.config.get("batches_per_update", 1)),
        "checkpoint_every": int(session.config.get("checkpoint_every", 0)),
        "checkpoint_at_end": True,
        "components": session.components,
        "seed": seed,
        "deterministic": True,
        **plan_extra,
    }
    run_training(session.adapter, plan, ctx, batch_fn)
    if not ctx.checkpoints:
        raise ContractError("training produced no checkpoint")
    return ctx


def _builder_ref(adapter: Any, build_cfg: dict[str, Any]) -> str | None:
    ref = getattr(adapter, "builder_ref", None)
    return ref(build_cfg) if callable(ref) else build_cfg.get("builder")


def run(inputs, config, *, reader, writer, context):
    import torch

    _extend_path(config, context)
    torch.set_num_threads(int(config.get("torch_threads", 1)))
    for key in ("adapter", "steps"):
        if key not in config:
            raise ContractError(f"config.{key} is required")
    adapter = load_adapter(config["adapter"])
    seed = int(config.get("seed", context.seed if context.seed is not None else 0))
    steps = int(config["steps"])
    build_cfg: dict[str, Any] = dict(config.get("adapter_config") or {})
    if "builder" in config:
        build_cfg["builder"] = config["builder"]
    if "balance_groups" in config:
        build_cfg["balance_groups"] = bool(config["balance_groups"])
    build_cfg["seed"] = seed
    bpu = int(config.get("batches_per_update", 1))
    branch = config.get("branch")
    scratch = str(context.scratch_dir)
    ckpt_dir = os.path.join(scratch, "checkpoints")

    session = Session(adapter, config, build_cfg, bpu)
    session.meta.data.update(
        seed=seed,
        stream_seed=int(config.get("stream_seed", seed)),
        builder=build_cfg.get("builder"),
        builder_ref=_builder_ref(adapter, build_cfg),
        adapter=config["adapter"],
    )

    plan_extra: dict[str, Any] = {}
    parent_info: dict[str, Any] | None = None
    restore_class = "exact"
    no_change = False
    if "parent_checkpoint" in inputs:
        parent_state = load_checkpoint(reader.path("parent_checkpoint"))
        migration = _normalize_migration(parent_state["entries"], branch)
        actions = {n: s["action"] for n, s in migration["entries"].items()}
        no_change = all(a == "copy" for a in actions.values())
        restore_class = "exact" if no_change else "warm-start"
        claimed = (branch or {}).get("restore_class")
        order = ("exact", "numerically-equivalent", "warm-start", "inference-only", "unrestorable")
        if claimed in order and order.index(claimed) < order.index(restore_class):
            raise ContractError(
                f"claimed restore class {claimed!r} is stronger than the migration supports ({restore_class!r})"
            )
        if actions.get("step") == "drop":
            raise ContractError("the step entry cannot be dropped")
        child = apply_migration(parent_state, migration, {"mapping": _mapping_transform(session, seed)})
        policy = (branch or {}).get("stream_policy", "paired")
        entries = child["entries"]
        if META_ENTRY in entries:
            if policy == "independent":
                if (branch or {}).get("seed") is None:
                    raise ContractError("an independent stream needs an explicit branch seed")
                entries[META_ENTRY]["stream_seed"] = int(branch["seed"])
            session.meta.data["stream_seed"] = int(entries[META_ENTRY].get("stream_seed", seed))
        child["manifest"]["state_entries"] = sorted(entries)
        start_path = save_checkpoint(child, os.path.join(scratch, "branch-start.ckpt"))
        plan_extra["resume_from"] = start_path
        parent_info = {
            "step": parent_state["manifest"].get("step"),
            "actions": actions,
            "stream_policy": policy,
            "stream_seed": session.meta.data["stream_seed"],
            "intervention": (branch or {}).get("intervention"),
            "parent_state_fingerprint": state_fingerprint(parent_state["entries"]),
        }
    elif branch:
        raise ContractError("config.branch needs the parent_checkpoint input")

    def batch_fn_for(stream_seed: int):
        if callable(getattr(adapter, "train", None)):
            return None
        if not callable(getattr(adapter, "train_batches", None)):
            raise ContractError("adapter has no train_batches(config) data stream")
        return adapter.train_batches(dict(build_cfg, stream_seed=stream_seed))

    stream_seed = int(session.meta.data["stream_seed"])
    ctx = _train_once(session, steps, seed, plan_extra, ckpt_dir, context, batch_fn_for(stream_seed))
    final_state = load_checkpoint(ctx.checkpoints[-1])
    final_fp = state_fingerprint(final_state["entries"])

    receipt: dict[str, Any] = {
        k: v
        for k, v in ctx.receipt.items()
        if k
        in (
            "start_step",
            "end_step",
            "resumed_from_step",
            "updates",
            "batches",
            "examples",
            "wall_seconds",
            "phase_counts",
            "final_loss",
            "evaluations",
            "adapter_capabilities",
            "peak_memory_bytes",
            "peak_memory_kind",
        )
    }
    receipt.update({k: v for k, v in ctx.receipt.items() if k not in receipt and _plain(v)})
    receipt.update(
        restore_class=restore_class,
        seed=seed,
        stream_seed=stream_seed,
        state_fingerprint=final_fp,
        model_fingerprint=state_fingerprint(final_state["entries"]["model"]),
        extensions={
            name: (obj.receipt() if callable(getattr(obj, "receipt", None)) else {})
            for name, obj in session.extensions.items()
        },
    )
    if parent_info is not None:
        receipt["branch"] = parent_info

    if config.get("verify_no_change"):
        if not no_change:
            raise ContractError("verify_no_change applies only to a branch that copies every state entry")
        reference = Session(adapter, config, build_cfg, bpu)
        reference.meta.data.update(session.meta.data)
        ref_ctx = _train_once(
            reference, steps, seed, {}, os.path.join(scratch, "reference"), None, batch_fn_for(stream_seed)
        )
        ref_fp = state_fingerprint(load_checkpoint(ref_ctx.checkpoints[-1])["entries"])
        receipt["no_change_restore"] = {
            "checked": True,
            "match": ref_fp == final_fp,
            "reference_fingerprint": ref_fp,
            "branch_fingerprint": final_fp,
        }
        receipt["no_change_restore_match"] = ref_fp == final_fp

    builder = _builder_ref(adapter, build_cfg)
    checkpoints: dict[str, Any] = {}
    paths = list(ctx.checkpoints)
    publish_all = bool(config.get("publish_intermediate_checkpoints"))
    for i, path in enumerate(paths):
        last = i == len(paths) - 1
        if not last and not publish_all:
            continue
        state = load_checkpoint(path)
        slot = "checkpoint" if last else f"checkpoint-step-{state['manifest']['step']:08d}"
        writer.stage_file(
            slot, path, media_type="application/octet-stream", schema="eidos-checkpoint/1", role="checkpoint"
        )
        checkpoints[slot] = {
            "step": int(state["manifest"]["step"]),
            "state_entries": list(state["manifest"]["state_entries"]),
            "complete": bool(state["manifest"]["complete"]),
            "builder": builder,
        }
    receipt["checkpoints"] = checkpoints
    receipt["metrics"] = {
        "final_loss": receipt.get("final_loss"),
        "end_step": receipt.get("end_step"),
        "examples": receipt.get("examples"),
    }
    return receipt


def _plain(value: Any) -> bool:
    import json

    try:
        json.dumps(value, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False
