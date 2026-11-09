"""Entry point for the sealed-runner role."""

from eidos.apps.role import main as _main


def main() -> None:
    _main(["sealed-runner"])
