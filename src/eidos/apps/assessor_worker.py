"""Entry point for the assessor-worker role."""

from eidos.apps.role import main as _main


def main() -> None:
    _main(["assessor-worker"])
