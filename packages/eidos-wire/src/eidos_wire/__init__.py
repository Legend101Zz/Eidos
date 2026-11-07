"""Eidos wire contract shared by the lab server and research workers.

Standard library only. Supported interpreter range: Python 3.9+. Any change that removes or
renames a field is a schema-major change (``WIRE_MAJOR``); additive optional fields are minor.
"""

from __future__ import annotations

from eidos_wire.identity import canonical_json, command_key, fingerprint, sha256_bytes, sha256_file
from eidos_wire.manifest import ComponentManifest, ManifestError, load_manifest, parse_manifest
from eidos_wire.messages import (
    WIRE_MAJOR,
    WIRE_MINOR,
    WIRE_SCHEMA,
    InputBinding,
    Invocation,
    OutputEntry,
    OutputManifest,
    WireError,
)

__all__ = [
    "WIRE_MAJOR",
    "WIRE_MINOR",
    "WIRE_SCHEMA",
    "ComponentManifest",
    "InputBinding",
    "Invocation",
    "ManifestError",
    "OutputEntry",
    "OutputManifest",
    "WireError",
    "canonical_json",
    "command_key",
    "fingerprint",
    "load_manifest",
    "parse_manifest",
    "sha256_bytes",
    "sha256_file",
]
