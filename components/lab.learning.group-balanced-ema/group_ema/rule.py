"""Group-balanced EMA update rule (training extension, owned state ``group-ema/1``).

At ``before-optimizer-step`` the combined gradient already exists, but a group-specific gradient does
not. This rule therefore *recomputes* one extra forward/backward per update on the batches the loss was
just computed on (``adapter.loss_terms(model, batch) -> (per_example_losses, group_ids)``), keeps an
exponential moving average of each group's gradient, and replaces ``param.grad`` with the unweighted
mean over groups of those averages. Groups absent from a batch still contribute their remembered
gradient. With ``decay = 0`` there is no memory and, when every group is in every batch, the update
equals the group-balanced loss gradient. The added computation is reported in the receipt.
"""

from __future__ import annotations

from typing import Any

SCHEMA = "group-ema/1"
PHASES = ("before-optimizer-step",)


class GroupBalancedEMA:
    phases = PHASES
    state_schema = SCHEMA

    def __init__(self, config: dict[str, Any], context: Any):
        decay = float(config.get("decay", 0.9))
        if not 0.0 <= decay < 1.0:
            raise ValueError("decay must be in [0, 1)")
        adapter = context.adapter
        if not callable(getattr(adapter, "loss_terms", None)):
            raise ValueError("adapter must define loss_terms(model, batch) -> (losses, group_ids)")
        self.decay, self.adapter, self.context = decay, adapter, context
        self.groups: dict[str, dict[str, Any]] = {}
        self.updates = 0
        self.extra_backward = 0

    # -- training hook -------------------------------------------------------------------------
    def on_phase(self, phase: str, *, model: Any, **_: Any) -> None:
        import torch

        batches = self.context.take_batches()
        if not batches:
            raise RuntimeError(
                "group-ema saw no batches: the trainer did not route loss inputs to extensions"
            )
        params = [(n, p) for n, p in model.named_parameters() if p.requires_grad]
        per_group: dict[str, list[list[Any]]] = {}
        for batch in batches:
            losses, groups = self.adapter.loss_terms(model, batch)
            for group in sorted({int(g) for g in groups.tolist()}):
                loss = losses[groups == group].mean()
                grads = torch.autograd.grad(
                    loss, [p for _, p in params], retain_graph=True, allow_unused=True
                )
                per_group.setdefault(str(group), []).append(
                    [torch.zeros_like(p) if g is None else g.detach() for (_, p), g in zip(params, grads)]  # noqa: B905 (strict= needs Python 3.10; components support 3.9)
                )
                self.extra_backward += 1
        for gid, lists in per_group.items():
            mean = [sum(t) / len(lists) for t in zip(*lists)]  # noqa: B905 (strict= needs Python 3.10; components support 3.9)
            memory = self.groups.get(gid)
            if memory is None:
                self.groups[gid] = {n: g.clone() for (n, _), g in zip(params, mean)}  # noqa: B905 (strict= needs Python 3.10; components support 3.9)
            else:
                for (n, _), g in zip(params, mean):  # noqa: B905 (strict= needs Python 3.10; components support 3.9)
                    memory[n] = self.decay * memory[n] + (1 - self.decay) * g
        for n, p in params:
            p.grad = sum(mem[n] for mem in self.groups.values()) / len(self.groups)
        self.updates += 1

    # -- owned state ---------------------------------------------------------------------------
    def state_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "decay": self.decay,
            "updates": self.updates,
            "extra_backward": self.extra_backward,
            "groups": {gid: {n: t.clone() for n, t in mem.items()} for gid, mem in self.groups.items()},
        }

    def load_state_dict(self, state: dict[str, Any]) -> None:
        if state.get("schema") != SCHEMA:
            raise ValueError(f"unsupported group-ema state schema {state.get('schema')!r}")
        self.updates = int(state["updates"])
        self.extra_backward = int(state["extra_backward"])
        self.groups = {gid: {n: t.clone() for n, t in mem.items()} for gid, mem in state["groups"].items()}

    def receipt(self) -> dict[str, Any]:
        return {
            "state_schema": SCHEMA,
            "decay": self.decay,
            "updates": self.updates,
            "groups_seen": sorted(self.groups),
            "extra_forward_backward_passes": self.extra_backward,
        }


def create(config: dict[str, Any], context: Any) -> GroupBalancedEMA:
    """Factory used as ``{"spec": "group_ema.rule:create", "config": {"decay": 0.9}}``."""
    return GroupBalancedEMA(config, context)
