"""Lane B's public dispatcher hooks (no Eidos dependencies in the controller)."""
from pathlib import Path


def run_arm3(run_dir):
    from .common import dispatch
    return dispatch(3, Path(run_dir))


def run_arm4(run_dir):
    from .common import dispatch
    return dispatch(4, Path(run_dir))
