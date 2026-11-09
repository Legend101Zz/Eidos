"""Symbolic builder used by the tests.

It lives in its own module (importable by name) because ``pg.from_json`` resolves the ``_type`` of
an exported object through the module that defines it.
"""

import pyglove as pg
import torch


@pg.symbolize
def build_mlp(in_dim, hidden, out_dim, act=torch.nn.ReLU):
    return torch.nn.Sequential(torch.nn.Linear(in_dim, hidden), act(), torch.nn.Linear(hidden, out_dim))


def make_space():
    """The 3 x 2 = 6 candidate space: hidden width in {16, 32, 64}, activation in {ReLU, Tanh}."""
    return build_mlp(
        in_dim=8,
        hidden=pg.oneof([16, 32, 64]),
        out_dim=2,
        act=pg.oneof([torch.nn.ReLU, torch.nn.Tanh]),
    )


@pg.patching.patcher([("width", pg.typing.Int())])
def widen(src, width):
    """``"widen?width=128"``: rebind the hidden width."""
    del src
    return {"hidden": width}
