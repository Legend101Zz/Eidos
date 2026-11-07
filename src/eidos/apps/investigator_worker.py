"""Entry point for the investigator-worker role."""

from eidos.apps.role import main as _main


def main() -> None:
    _main(["investigator-worker"])
