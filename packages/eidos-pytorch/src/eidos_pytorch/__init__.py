"""Eidos PyTorch support: adapters, isolated checkpoints, migrations, fingerprints, phases, training.

torch is imported lazily inside functions and is never installed by this package.
"""

from __future__ import annotations

from eidos_pytorch.adapter import (
    OPTIONAL_METHODS,
    AdapterError,
    ModelAdapter,
    load_adapter,
    resolve_callable,
    validate_adapter,
)
from eidos_pytorch.checkpoint import (
    SCHEMA,
    CheckpointError,
    StorageSharedError,
    capture_checkpoint,
    find_shared_storage,
    load_checkpoint,
    restore_checkpoint,
    save_checkpoint,
    verify_storage_independence,
)
from eidos_pytorch.devices import devices
from eidos_pytorch.fingerprint import (
    BehaviorReport,
    behavior_fingerprint,
    behavioral_restore_check,
    compare_outputs,
    record_behavior,
    reference_outputs,
    state_fingerprint,
)
from eidos_pytorch.migration import MigrationError, apply_migration
from eidos_pytorch.phases import PHASES, PhaseError, PhaseRunner
from eidos_pytorch.training import (
    TrainContext,
    TrainResult,
    run_training,
    seed_everything,
    train_loop,
)

__version__ = "1.0.0"

__all__ = [
    "OPTIONAL_METHODS",
    "PHASES",
    "SCHEMA",
    "AdapterError",
    "BehaviorReport",
    "CheckpointError",
    "MigrationError",
    "ModelAdapter",
    "PhaseError",
    "PhaseRunner",
    "StorageSharedError",
    "TrainContext",
    "TrainResult",
    "apply_migration",
    "behavior_fingerprint",
    "behavioral_restore_check",
    "capture_checkpoint",
    "compare_outputs",
    "devices",
    "find_shared_storage",
    "load_adapter",
    "load_checkpoint",
    "record_behavior",
    "reference_outputs",
    "resolve_callable",
    "restore_checkpoint",
    "run_training",
    "save_checkpoint",
    "seed_everything",
    "state_fingerprint",
    "train_loop",
    "validate_adapter",
    "verify_storage_independence",
]
