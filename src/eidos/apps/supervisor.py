"""Entry point for the supervisor role."""

from eidos.apps.role import main as _main


def main() -> None:
    _main(["supervisor"])
