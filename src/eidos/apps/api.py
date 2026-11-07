"""Entry point for the api role."""

from eidos.apps.role import main as _main


def main() -> None:
    _main(["api"])
