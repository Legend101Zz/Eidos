"""Eidos composer: MosaicML Composer trainer events as Eidos training-extension phases.

Optional worker-side adapter. Composer is imported when this package is imported; the lab server and
``eidos-worker-sdk`` never import it and never depend on it.
"""

from __future__ import annotations

__version__ = "0.1.0"

from eidos_composer.bridge import (  # noqa: E402
    PHASE_MAP,
    SUPPORTED_COMPOSER_VERSIONS,
    SUPPORTED_PHASES,
    UNSUPPORTED_PHASES,
    ComposerVersionError,
    EidosPhaseBridge,
    PhaseError,
    PhaseRunner,
    check_composer_version,
)

__all__ = [
    "PHASE_MAP",
    "SUPPORTED_COMPOSER_VERSIONS",
    "SUPPORTED_PHASES",
    "UNSUPPORTED_PHASES",
    "ComposerVersionError",
    "EidosPhaseBridge",
    "PhaseError",
    "PhaseRunner",
    "__version__",
    "check_composer_version",
]
