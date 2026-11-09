# Provenance: copied from the Eidos design handoff archive (eidos-claude-handoff), worked example
# "pytorch-lab-worked-example" (image_reuse.py). It is the researcher's own existing code and has no Eidos
# imports; the Eidos adapter lives in eidos_adapter.py.
# Local edit: model construction (build_model) and row production (audit_rows) were factored out of
# make_checkpoint/audit so an adapter can reuse them; the computations are unchanged.
"""Reuse the pair reporter on a frozen image classifier and simulated exposure changes."""
import json

import torch
from torch import nn

from pair_report import summarize


def images(count, rng):
    # Both classes contain a bar. Its direction, independent of location, is the label.
    target = torch.randint(2, (count,), generator=rng)
    patch = 0.1 * torch.rand(count, 1, 12, 12, generator=rng)
    xy = torch.randint(2, 8, (count, 2), generator=rng)
    for i, ((x, y), label) in enumerate(zip(xy.tolist(), target.tolist())):
        if label:
            patch[i, 0, y:y + 4, x] += 0.8
        else:
            patch[i, 0, y, x:x + 4] += 0.8
    return patch, target


def build_model():
    return nn.Sequential(nn.Conv2d(1, 8, 3), nn.ReLU(), nn.AdaptiveMaxPool2d(1),
                         nn.Flatten(), nn.Linear(8, 1))


def make_checkpoint():
    # Stand-in for the second researcher's already-trained network.
    torch.manual_seed(17)
    model = build_model()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.02)
    generator = torch.Generator().manual_seed(170)
    for _ in range(80):
        patch, label = images(64, generator)
        loss = nn.functional.binary_cross_entropy_with_logits(model(patch).squeeze(1), label.float())
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    return model.eval()


@torch.no_grad()
def audit_rows(model, count=400, seed=180):
    patch, label = images(count, torch.Generator().manual_seed(seed))
    gains = torch.tensor([0.7, 0.1]).repeat(count // 2 + 1)[:count]
    reference = model(patch).flatten().tolist()
    variant = model(patch * gains[:, None, None, None]).flatten().tolist()
    return [
        {"pair_id": i, "reference_logit": a, "variant_logit": b, "reference_label": y,
         "variant_label": y, "stratum": f"gain {gain:.1f}"}
        for i, (a, b, y, gain) in enumerate(zip(reference, variant, label.tolist(), gains.tolist()))
    ]


def audit(model):
    return summarize(audit_rows(model))


if __name__ == "__main__":
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    print(json.dumps(audit(make_checkpoint()), indent=2))
