"""Entry point for the workflow-worker role."""

from eidos.apps.role import main as _main


def main() -> None:
    _main(["workflow-worker"])
