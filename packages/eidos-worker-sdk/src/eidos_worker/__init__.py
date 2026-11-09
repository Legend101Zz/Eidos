"""Eidos worker SDK: standard library only, Python >= 3.9, never imports the lab server.

The process entry point lives in ``eidos_worker.run`` (not re-exported here so that
``python -m eidos_worker.run`` does not import itself twice)."""

from __future__ import annotations

from eidos_worker.context import ModelClient, RunContext
from eidos_worker.errors import ContractError, NonFiniteError, check_finite, classify
from eidos_worker.llm import LlmClient, LlmRefused, LlmResult
from eidos_worker.reader import ArtifactReader
from eidos_worker.writer import ArtifactWriter

__version__ = "1.0.0"

__all__ = [
    "ArtifactReader",
    "ArtifactWriter",
    "ContractError",
    "LlmClient",
    "LlmRefused",
    "LlmResult",
    "ModelClient",
    "NonFiniteError",
    "RunContext",
    "check_finite",
    "classify",
]
