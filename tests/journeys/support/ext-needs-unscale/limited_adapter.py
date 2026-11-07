"""Journey fixture adapter: the switch adapter, but its (delegated) training loop fires no
``after-gradient-unscale`` phase. Defining ``train`` makes the trainer honour ``phases``; it is never
called because registering the extension fails first."""

from eidos_adapter import ADAPTER as _BASE


class _Limited:
    phases = tuple(p for p in _BASE.phases if p != "after-gradient-unscale")

    def __getattr__(self, name):
        return getattr(_BASE, name)

    def train(self, plan, context):  # pragma: no cover - never reached
        raise RuntimeError("fixture adapter does not train")


ADAPTER = _Limited()
