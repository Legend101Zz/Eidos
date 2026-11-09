"""Fixture models/adapters for the eidos-pytorch tests (importable from fresh processes)."""

import torch
from torch import nn


class SwitchNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(6, 12)
        self.readout = nn.Linear(13, 1)

    def pool(self, events):
        return self.embedding(events).mean(dim=1)

    def forward(self, events, recorder):
        pooled = self.pool(events)
        return self.readout(torch.cat((pooled, recorder[:, None] * 2 - 1), dim=1)).squeeze(1)


class OrderedSwitchNet(SwitchNet):
    def pool(self, events):
        position = torch.linspace(0, 2, events.size(1), device=events.device)
        return (self.embedding(events) * position[None, :, None]).mean(dim=1)


def build_ordered(config):
    return OrderedSwitchNet()


def build_small(config):
    return nn.Linear(2, 2)


def _switch_batch(size=64, seq=12, seed=0):
    g = torch.Generator().manual_seed(seed)
    events = torch.randint(2, 6, (size, seq), generator=g)
    a = torch.randint(seq, (size,), generator=g)
    b = (a + torch.randint(1, seq, (size,), generator=g)) % seq
    rows = torch.arange(size)
    events[rows, a] = 0
    events[rows, b] = 1
    target = (a < b).float()
    return {"events": events, "recorder": target.clone(), "target": target}


class SwitchAdapter:
    """Non-tensor (dict) batches, non-logit ``predict`` structure: exercises the protocol."""

    phases = ("before-model-construction", "after-backward", "before-optimizer-step", "after-optimizer-step")

    def build(self, config):
        return SwitchNet()

    def predict(self, model, batch):
        return {"logit": model(batch["events"], batch["recorder"]), "n": int(batch["events"].shape[0])}

    def examples(self, config):
        return _switch_batch(16, seed=123)

    def loss(self, model, batch):
        return nn.functional.binary_cross_entropy_with_logits(
            model(batch["events"], batch["recorder"]), batch["target"]
        )

    def make_optimizer(self, model, config):
        return torch.optim.Adam(model.parameters(), lr=config.get("lr", 0.02))

    def make_scheduler(self, optimizer, config):
        return torch.optim.lr_scheduler.StepLR(optimizer, step_size=3, gamma=0.9)


def switch_batch_fn(index):
    return _switch_batch(32, seed=1000 + index)


class DropoutNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.body = nn.Sequential(nn.Linear(8, 16), nn.ReLU(), nn.Dropout(0.3), nn.Linear(16, 1))

    def forward(self, x):
        return self.body(x).squeeze(-1)


class DropoutAdapter:
    def build(self, config):
        return DropoutNet()

    def predict(self, model, batch):
        return model(batch[0])

    def examples(self, config):
        return _dropout_batch(0)

    def loss(self, model, batch):
        x, y = batch
        return nn.functional.mse_loss(model(x), y)

    def make_optimizer(self, model, config):
        return torch.optim.Adam(model.parameters(), lr=0.01)


def _dropout_batch(i):
    g = torch.Generator().manual_seed(77 + i)
    x = torch.randn(8, 8, generator=g)
    return x, x.sum(dim=1) * 0.1


dropout_batch_fn = _dropout_batch


class StepAdapter(DropoutAdapter):
    """Author-owned update with an accumulating (two-batch) step."""

    def train_step(self, model, batches, optimizer, step):
        optimizer.zero_grad()
        total = 0.0
        for b in batches:
            loss = self.loss(model, b)
            loss.backward()
            total += float(loss.detach())
        optimizer.step()
        return {"loss": total / len(batches)}


class WholeTrainer(DropoutAdapter):
    phases = ("on-checkpoint-save",)

    def train(self, plan, context):
        model = self.build(plan["config"])
        opt = self.make_optimizer(model, plan["config"])
        for i in range(plan["steps"]):
            opt.zero_grad()
            self.loss(model, _dropout_batch(i)).backward()
            opt.step()
        context.record("custom_updates", plan["steps"])
        context.checkpoint(model, opt, step=plan["steps"])
        return model


class NanAdapter(DropoutAdapter):
    def loss(self, model, batch):
        return model(batch[0]).sum() * float("nan")
