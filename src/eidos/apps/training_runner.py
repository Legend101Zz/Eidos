"""Entry point for the training-runner role."""

from eidos.apps.role import main as _main


def main() -> None:
    _main(["training-runner"])
