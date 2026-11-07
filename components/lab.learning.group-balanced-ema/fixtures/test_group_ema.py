"""Author test for group-ema/1 (fixture-only: written by the component's author, not independent).

Self-contained plain-function tests; run with ``python fixtures/test_group_ema.py`` in an environment
that has torch (the tests/components suite does this). No Eidos package is needed.
"""

import os
import sys

import torch
from torch import nn
from torch.nn import functional as F

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from group_ema.rule import SCHEMA, create  # noqa: E402


class Adapter:
    def loss_terms(self, model, batch):
        x, y, g = batch
        return F.binary_cross_entropy_with_logits(model(x).squeeze(1), y, reduction="none"), g


class Context:
    def __init__(self):
        self.adapter, self.queue = Adapter(), []

    def take_batches(self):
        out, self.queue = self.queue, []
        return out


def batch(seed, n=20, rare=2):
    gen = torch.Generator().manual_seed(seed)
    x = torch.randn(n, 3, generator=gen)
    y = (x[:, 0] > 0).float()
    g = torch.zeros(n, dtype=torch.long)
    g[:rare] = 1
    return x, y, g


def _grads(model):
    return {n: p.grad.clone() for n, p in model.named_parameters()}


def test_decay_zero_equals_group_balanced_loss():
    torch.manual_seed(0)
    model = nn.Linear(3, 1)
    b = batch(1)
    ctx = Context()
    rule = create({"decay": 0.0}, ctx)
    ctx.queue.append(b)
    losses, groups = Adapter().loss_terms(model, b)
    ((losses[groups == 0].mean() + losses[groups == 1].mean()) / 2).backward()
    expected = _grads(model)
    for p in model.parameters():
        p.grad = None
    losses, _ = Adapter().loss_terms(model, b)
    losses.mean().backward()  # the ordinary combined gradient that exists at the hook
    assert not torch.allclose(model.weight.grad, expected["weight"])
    rule.on_phase("before-optimizer-step", model=model)
    for n, g in _grads(model).items():
        assert torch.allclose(g, expected[n], atol=1e-6), n
    assert rule.receipt()["extra_forward_backward_passes"] == 2


def test_memory_keeps_absent_group():
    torch.manual_seed(0)
    model = nn.Linear(3, 1)
    ctx = Context()
    rule = create({"decay": 0.5}, ctx)
    ctx.queue.append(batch(1))
    rule.on_phase("before-optimizer-step", model=model)
    x, y, g = batch(2)
    ctx.queue.append((x, y, torch.zeros_like(g)))  # group 1 absent this time
    rule.on_phase("before-optimizer-step", model=model)
    assert sorted(rule.state_dict()["groups"]) == ["0", "1"]


def test_state_round_trip_is_exact():
    torch.manual_seed(0)
    model = nn.Linear(3, 1)
    ctx = Context()
    rule = create({"decay": 0.9}, ctx)
    for s in (1, 2, 3):
        ctx.queue.append(batch(s))
        rule.on_phase("before-optimizer-step", model=model)
    state = rule.state_dict()
    assert state["schema"] == SCHEMA
    other = create({"decay": 0.9}, Context())
    other.load_state_dict(state)
    for gid, mem in state["groups"].items():
        for n, t in mem.items():
            assert torch.equal(other.groups[gid][n], t) and other.groups[gid][n] is not t
    ctx2 = Context()
    other.context = ctx2
    ctx.queue.append(batch(4))
    ctx2.queue.append(batch(4))
    m1, m2 = nn.Linear(3, 1), nn.Linear(3, 1)
    m2.load_state_dict(m1.state_dict())
    rule.on_phase("before-optimizer-step", model=m1)
    other.on_phase("before-optimizer-step", model=m2)
    assert torch.equal(m1.weight.grad, m2.weight.grad)


def test_rejects_adapter_without_loss_terms():
    class Bare:
        pass

    class C:
        adapter = Bare()

    try:
        create({}, C())
    except ValueError:
        return
    raise AssertionError("expected ValueError")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
