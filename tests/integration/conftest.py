"""Integration fixtures for the source controller and installed lab boundary."""
from importlib import import_module
from pathlib import Path

import pytest


@pytest.fixture
def study_arm4(monkeypatch):
    # Console pytest need not put the checkout on sys.path. The study controller
    # lives in the checkout while Eidos imports continue to come from the test build.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    return import_module("study.eidos_arms.arm4")
