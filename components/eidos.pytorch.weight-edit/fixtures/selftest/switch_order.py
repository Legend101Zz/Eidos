# Provenance: copied from the Eidos design handoff archive (eidos-claude-handoff), worked example
# "pytorch-lab-worked-example" (switch_order.py). It is the researcher's own existing code and has no Eidos
# imports; the Eidos adapter lives in eidos_adapter.py.
"""CPU experiment on the order of two switches. Requires Python 3.12 and torch."""
import argparse
import csv
from copy import deepcopy
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

import torch
from torch import nn
from torch.nn import functional as F

from pair_report import summarize


STEPS = 180
SEQUENCE_LENGTH = 12
BATCH_SIZE = 100


@dataclass
class Batch:
    events: torch.Tensor
    recorder: torch.Tensor
    target: torch.Tensor
    pos_a: torch.Tensor
    pos_b: torch.Tensor

    def with_switches_swapped(self):
        events = self.events.clone()
        rows = torch.arange(len(events))
        events[rows, self.pos_a] = 1
        events[rows, self.pos_b] = 0
        return Batch(events, self.recorder, 1 - self.target, self.pos_b, self.pos_a)


def draw_batch(size, rng):
    # 0 and 1 encode switches A and B. Other tokens are background events.
    events = torch.randint(2, 6, (size, SEQUENCE_LENGTH), generator=rng)
    pos_a = torch.randint(SEQUENCE_LENGTH, (size,), generator=rng)
    offset = torch.randint(1, SEQUENCE_LENGTH, (size,), generator=rng)
    pos_b = (pos_a + offset) % SEQUENCE_LENGTH
    rows = torch.arange(size)
    events[rows, pos_a] = 0
    events[rows, pos_b] = 1
    target = (pos_a < pos_b).float()
    recorder = target.clone()
    exceptions = torch.randperm(size, generator=rng)[:round(size * 0.02)]
    recorder[exceptions] = 1 - recorder[exceptions]
    return Batch(events, recorder, target, pos_a, pos_b)


class SwitchNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(6, 12)
        self.readout = nn.Linear(13, 1)

    def pool(self, events):
        return self.embedding(events).mean(dim=1)

    def forward(self, events, recorder):
        pooled = self.pool(events)
        inputs = torch.cat((pooled, recorder[:, None] * 2 - 1), dim=1)
        return self.readout(inputs).squeeze(1)


class OrderedSwitchNet(SwitchNet):
    def pool(self, events):
        position = torch.linspace(0, 2, events.size(1), device=events.device)
        return (self.embedding(events) * position[None, :, None]).mean(dim=1)


def train(seed, model_class=SwitchNet, balance_groups=False):
    torch.manual_seed(seed)
    net = model_class()
    optimizer = torch.optim.Adam(net.parameters(), lr=0.02)
    rng = torch.Generator().manual_seed(1000 + seed)
    for _ in range(STEPS):
        batch = draw_batch(BATCH_SIZE, rng)
        logits = net(batch.events, batch.recorder)
        losses = F.binary_cross_entropy_with_logits(logits, batch.target, reduction="none")
        if balance_groups:
            # Each generated batch contains exactly 98 agreements and 2 exceptions.
            agrees = batch.recorder == batch.target
            loss = (losses[agrees].mean() + losses[~agrees].mean()) / 2
        else:
            loss = losses.mean()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    return net.eval()


@torch.no_grad()
def silence_recorder(trained):
    child = deepcopy(trained)
    child.readout.weight[0, -1].zero_()
    return child


@torch.no_grad()
def diagnose(net, batch):
    swapped = batch.with_switches_swapped()
    scores = net(batch.events, batch.recorder)
    twin_scores = net(swapped.events, swapped.recorder)
    changed_recorder = net(batch.events, 1 - batch.recorder)
    pooled_difference = (net.pool(batch.events) - net.pool(swapped.events)).abs()
    return {
        "original_accuracy": ((scores > 0) == batch.target).float().mean().item(),
        "both_correct": (((scores > 0) == batch.target) & ((twin_scores > 0) == swapped.target)).float().mean().item(),
        "recorder_flip_rate": ((scores > 0) != (changed_recorder > 0)).float().mean().item(),
        "max_pooled_difference": pooled_difference.max().item(),
    }


@torch.no_grad()
def prediction_rows(net, batch):
    swapped = batch.with_switches_swapped()
    original_scores = net(batch.events, batch.recorder).tolist()
    swapped_scores = net(swapped.events, swapped.recorder).tolist()
    gaps = (batch.pos_a - batch.pos_b).abs().tolist()
    rows = []
    for i, gap in enumerate(gaps):
        group = "adjacent" if gap == 1 else ("2-3 apart" if gap < 4 else "4+ apart")
        rows.append({
            "pair_id": i, "reference_logit": original_scores[i], "variant_logit": swapped_scores[i],
            "reference_label": int(batch.target[i]), "variant_label": int(swapped.target[i]),
            "stratum": group,
        })
    return rows


def write_rows(rows, path):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def compare_builders(test):
    seed = 37
    trained = train(seed, OrderedSwitchNet, balance_groups=True)
    weights = trained.state_dict()
    correct = OrderedSwitchNet().eval()
    correct.load_state_dict(weights, strict=True)
    wrong = SwitchNet().eval()
    load_result = wrong.load_state_dict(weights, strict=True)
    with torch.no_grad():
        torch.testing.assert_close(correct(test.events, test.recorder),
                                   trained(test.events, test.recorder), rtol=0, atol=0)
    return {
        "seed": seed, "missing_keys": load_result.missing_keys,
        "unexpected_keys": load_result.unexpected_keys,
        "ordered_builder": summarize(prediction_rows(correct, test))["overall"],
        "mean_builder": summarize(prediction_rows(wrong, test))["overall"],
    }


def run(save_development_pairs=None):
    torch.manual_seed(11)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    development = draw_batch(1000, torch.Generator().manual_seed(8000))
    original = train(11, SwitchNet)
    ordered = train(11, OrderedSwitchNet)
    pilot = {
        "SwitchNet": diagnose(original, development),
        "OrderedSwitchNet": diagnose(ordered, development),
    }
    pilot["recorder_weight_edit"] = diagnose(silence_recorder(ordered), development)
    rows = prediction_rows(ordered, development)
    pilot["ordered_by_gap"] = summarize(rows)
    if save_development_pairs is not None:
        write_rows(rows, save_development_pairs)

    # Anchor cases in each test draw match the recorder 98% of the time.
    # Each opposite-order twin retains its anchor's recorder and background events.
    test = draw_batch(4000, torch.Generator().manual_seed(95002))
    per_condition = {}
    for model in (SwitchNet, OrderedSwitchNet):
        for balanced in (False, True):
            condition = model.__name__ + ("_balanced" if balanced else "_ordinary")
            per_condition[condition] = {}
            for seed in (11, 23, 37):
                net = train(seed, model, balance_groups=balanced)
                per_condition[condition][str(seed)] = summarize(prediction_rows(net, test))
                if model is OrderedSwitchNet and not balanced:
                    edited = silence_recorder(net)
                    edited_seeds = per_condition.setdefault("OrderedSwitchNet_ordinary_edited", {})
                    edited_seeds[str(seed)] = summarize(prediction_rows(edited, test))

    results = {}
    for condition, by_seed in per_condition.items():
        means = {metric: mean(r["overall"][metric] for r in by_seed.values())
                 for metric in ("both_correct_rate", "reference_accuracy")}
        results[condition] = {"mean_across_seeds": means, "by_seed": by_seed}
    return {
        "torch": str(torch.__version__), "device": "cpu", "sequence_length": SEQUENCE_LENGTH,
        "parameters": sum(p.numel() for p in original.parameters()),
        "steps": STEPS, "batch_size": BATCH_SIZE, "development": pilot,
        "evaluation_data_seed": 95002, "test": results, "restore_same_weights": compare_builders(test),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save-development-pairs", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.save_development_pairs), indent=2))
