"""Eidos adapter for the second researcher's image classifier (``image_reuse.py``).

It produces rows in the same paired-row schema as the switch project (reference/variant logits and
labels, ``stratum``, ``cluster``), so the pair scorer is reused unchanged. Here the "pair" is an
image and the same image under a different exposure gain.
"""

from __future__ import annotations

import torch
from torch.nn import functional as F

import image_reuse as ir

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


class _Stream:
    """Batch ``i`` of one sequential generator as a pure function of ``i`` (replays when needed)."""

    def __init__(self, seed, size):
        self.seed, self.size, self._gen, self._next = seed, size, None, 0

    def __call__(self, index):
        if self._gen is None or index != self._next:
            self._gen = torch.Generator().manual_seed(self.seed)
            self._next = 0
            while self._next < index:
                ir.images(self.size, self._gen)
                self._next += 1
        batch = ir.images(self.size, self._gen)
        self._next += 1
        return batch


class ImageAdapter:
    phases = PHASES

    def build(self, config):
        builder = config.get("builder", "ConvNet")
        if builder != "ConvNet":
            raise ValueError(f"unknown builder {builder!r}; this project has only 'ConvNet'")
        return ir.build_model()

    def builder_ref(self, config):
        return "image_reuse:build_model"

    def predict(self, model, batch):
        patch, _ = batch
        return model(patch).flatten()

    def examples(self, split_or_config, config=None):
        if config is None:
            config, split = split_or_config, split_or_config.get("split", "fixture")
        else:
            split = split_or_config
        seed = config.get("data_seed", 180 if split == "audit" else 7)
        return ir.images(config.get("pairs", 16), torch.Generator().manual_seed(seed))

    def train_batches(self, config):
        # The original script seeds the stream with 10 x the model seed (17 -> 170).
        return _Stream(10 * config.get("stream_seed", config.get("seed", 17)), config.get("batch_size", 64))

    def loss(self, model, batch):
        patch, label = batch
        return F.binary_cross_entropy_with_logits(model(patch).squeeze(1), label.float())

    def make_optimizer(self, model, config):
        return torch.optim.Adam(model.parameters(), lr=config.get("lr", 0.02))

    def evaluation_rows(self, model, split, config):
        model.eval()
        cluster = str(config.get("cluster", config.get("seed", 0)))
        rows = ir.audit_rows(model, config.get("pairs", 400), config.get("data_seed", 180))
        return [dict(row, cluster=cluster) for row in rows]


ADAPTER = ImageAdapter()
