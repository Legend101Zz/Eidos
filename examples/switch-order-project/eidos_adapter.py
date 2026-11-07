"""Thin Eidos adapter over the researcher's own ``switch_order.py`` (which has no Eidos imports).

Register it as ``eidos_adapter:ADAPTER`` with the project root as the environment's project_root.

The adapter gives Eidos components five things the original script already does implicitly:
build (``config["builder"]`` selects SwitchNet / OrderedSwitchNet / any importable "module:Class"),
predict, a deterministic training stream, the training loss (ordinary or group-balanced) and
paired evaluation rows (original and opposite-order twin).
"""

from __future__ import annotations

import importlib

import torch
from torch.nn import functional as F

import switch_order as so

BUILDERS = {"SwitchNet": so.SwitchNet, "OrderedSwitchNet": so.OrderedSwitchNet}
DEFAULT_DATA_SEEDS = {"dev": 8000, "test": 95002, "fixture": 123}
DEFAULT_PAIRS = {"dev": 1000, "test": 4000, "fixture": 16}
PHASES = (
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


def _resolve_builder(name):
    if name in BUILDERS:
        return BUILDERS[name]
    module, sep, attr = str(name).partition(":")
    if not sep or not module or not attr:
        raise ValueError(f"unknown builder {name!r}; use one of {sorted(BUILDERS)} or 'module:Class'")
    obj = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


class _Stream:
    """Batch ``i`` of the original sequential generator, as a pure function of ``i``.

    The original script draws batches one after another from ``Generator().manual_seed(1000 + seed)``.
    Asking for index ``i`` out of order replays the generator, so resume and branch reproduce the
    uninterrupted stream exactly.
    """

    def __init__(self, seed, size):
        self.seed, self.size, self._gen, self._next = seed, size, None, 0

    def __call__(self, index):
        if self._gen is None or index != self._next:
            self._gen = torch.Generator().manual_seed(1000 + self.seed)
            self._next = 0
            while self._next < index:
                so.draw_batch(self.size, self._gen)
                self._next += 1
        batch = so.draw_batch(self.size, self._gen)
        self._next += 1
        return batch


class SwitchAdapter:
    phases = PHASES

    def __init__(self):
        self._balance_groups = False

    # -- model ---------------------------------------------------------------------------------
    def build(self, config):
        self._balance_groups = bool(config.get("balance_groups", False))
        return _resolve_builder(config.get("builder", "SwitchNet"))()

    def builder_ref(self, config):
        name = config.get("builder", "SwitchNet")
        return f"switch_order:{name}" if name in BUILDERS else str(name)

    def predict(self, model, batch):
        return model(batch.events, batch.recorder)

    def examples(self, split_or_config, config=None):
        """``examples(config)`` is the fixed reference batch of the Eidos adapter protocol;
        ``examples(split, config)`` is a deterministic dev/test draw selected by ``data_seed``."""
        if config is None:
            config = split_or_config
            split = config.get("split", "fixture")
        else:
            split = split_or_config
        seed = config.get("data_seed", DEFAULT_DATA_SEEDS.get(split, 0))
        count = config.get("pairs", DEFAULT_PAIRS.get(split, 1000))
        return so.draw_batch(count, torch.Generator().manual_seed(seed))

    # -- training ------------------------------------------------------------------------------
    def train_batches(self, config):
        seed = config.get("stream_seed", config.get("seed", 0))
        return _Stream(seed, config.get("batch_size", so.BATCH_SIZE))

    def loss_terms(self, model, batch):
        """Per-example losses and each example's group (0 = recorder agrees, 1 = exception)."""
        logits = model(batch.events, batch.recorder)
        losses = F.binary_cross_entropy_with_logits(logits, batch.target, reduction="none")
        return losses, (batch.recorder != batch.target).long()

    def loss(self, model, batch):
        losses, groups = self.loss_terms(model, batch)
        if self._balance_groups:
            return (losses[groups == 0].mean() + losses[groups == 1].mean()) / 2
        return losses.mean()

    def make_optimizer(self, model, config):
        return torch.optim.Adam(model.parameters(), lr=config.get("lr", 0.02))

    # -- evaluation ----------------------------------------------------------------------------
    def evaluation_rows(self, model, split, config):
        """Paired rows: each original example with its opposite-order twin, plus a ``cluster``."""
        model.eval()
        batch = self.examples(split, config)
        cluster = str(config.get("cluster", config.get("seed", 0)))
        return [dict(row, cluster=cluster) for row in so.prediction_rows(model, batch)]


ADAPTER = SwitchAdapter()
