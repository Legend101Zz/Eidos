"""Eidos PyGlove adapter: a pinned transform and search-space adapter for PyGlove symbolic objects.

Worker side only. pyglove is imported lazily after a version check, and torch is never imported or
installed by this package.
"""

from __future__ import annotations

__version__ = "0.1.0"

from eidos_pyglove.adapter import (  # noqa: E402
    SCHEMA,
    SUPPORTED_PYGLOVE,
    Candidate,
    DisallowedTypeError,
    ExportIntegrityError,
    PatchError,
    PyGloveAdapterError,
    SearchSpace,
    UnsupportedPyGloveError,
    builder_for_eidos,
    canonical_json,
    check_pyglove_version,
    export,
    installed_pyglove_version,
    load,
    transform_revision,
    version_supported,
)

__all__ = [
    "SCHEMA",
    "SUPPORTED_PYGLOVE",
    "Candidate",
    "DisallowedTypeError",
    "ExportIntegrityError",
    "PatchError",
    "PyGloveAdapterError",
    "SearchSpace",
    "UnsupportedPyGloveError",
    "__version__",
    "builder_for_eidos",
    "canonical_json",
    "check_pyglove_version",
    "export",
    "installed_pyglove_version",
    "load",
    "transform_revision",
    "version_supported",
]
